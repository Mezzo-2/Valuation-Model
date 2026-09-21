"""拆分取数：年报父口径 + 向量检索，供收入拆分 agent 使用。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from valuation.segment_split.split_agent import SplitAgentDeps, run_revenue_split
from valuation.segment_split.split_plan import RESIDUAL_NAMES, require_split_plan
from valuation.shared.io import OUTPUT_DIR, dump_json, load_json, logs_dir, spec_dir
from valuation.comein.comein import ComeinClient
from valuation.shared.cli import run_dir_arg
from valuation.historical_financials.fetch import parse_amount

MAX_VECTOR = 14
VECTOR_SNIPPET = 16000
TOTAL_NAMES = frozenset({"合计", "总计", "主营业务合计", "分部合计"})
REPORT_TYPES = frozenset({"REPORT", "FOREIGN_REPORT"})
IMAGE_TYPES = frozenset({"IMAGE"})
_G_OK = frozenset({40.0, 50.0, 100.0, 200.0, 400.0, 800.0})
_SEGMENT = ("分业务", "分产品", "分部", "业务线", "分项收入", "营收拆分", "收入拆分", "按传输速率")
_PNL = ("销售费用", "管理费用", "研发费用", "税金及附加")
_FORECAST_PAGE = (
    "盈利预测",
    "利润表",
    "损益表",
    "预测指标",
    "主要财务比率",
    "Income statement",
    "Income Statement",
)
_SPEC = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?(?:G|T))(?![A-Za-z])", re.I)
_SPEC_DENY = frozenset({"2G", "3G", "4G", "5G", "6G"})


def run_live_split(run_dir: Path) -> dict[str, Any]:
    """拉年报父口径，调用一次收入拆分 agent，写出 split_plan.json。"""
    facts = load_json(spec_dir(run_dir) / "facts.json")
    company = str(facts.get("company") or "")
    ticker = str(facts.get("ticker") or "")
    full_code = str(facts.get("full_code") or "")
    hist = list(facts["hist_periods"])
    fcst = list(facts["forecast_periods"])
    revenue = list(facts["income"]["营业收入"])
    market, code = _market_and_code(full_code, ticker)
    years = [period[:4] for period in hist]
    log_root = logs_dir(run_dir) / "mcp"
    calls: list[dict[str, Any]] = []
    as_of = datetime.now().isoformat(timespec="seconds")

    with ComeinClient() as mcp:
        official, official_names = _fetch_official(
            mcp, calls, log_root, full_code or f"{market}{code}", years
        )
        deps = SplitAgentDeps(
            company=company,
            ticker=ticker,
            forecast_years=[period[:4] for period in fcst],
            official_names=official_names,
            official_rows=list(official.get("rows") or []),
            search=_mcp_search(mcp, calls, log_root),
        )
        plan = run_revenue_split(deps)
        dump_json(log_root / "split_plan_raw.json", plan.model_dump())
        if deps.draft:
            dump_json(log_root / "split_caliber_raw.json", deps.draft.model_dump())
        all_hits = list(deps.hits)
        vector_hits = _dedupe_vector(all_hits)[:MAX_VECTOR]
        queries = deps.queries
        draft_dump = deps.draft.model_dump() if deps.draft else {}

    payload = {
        "company": company,
        "ticker": ticker,
        "ts": as_of,
        **plan.model_dump(),
    }
    canonical = require_split_plan(payload, hits=all_hits)
    dump_json(spec_dir(run_dir) / "split_plan.json", canonical)
    if draft_dump:
        dump_json(spec_dir(run_dir) / "split_caliber.json", {"company": company, "ticker": ticker, "ts": as_of, **draft_dump})
    pack = {
        "company": company,
        "ticker": ticker,
        "full_code": full_code or f"{market}{code}",
        "as_of": as_of,
        "facts_slice": {
            "hist_periods": hist,
            "forecast_periods": fcst,
            "unit": facts.get("unit") or "亿元",
            "营业收入": revenue,
        },
        "official_segments": official,
        "split_caliber": draft_dump,
        "split_plan": canonical,
        "vector_queries": queries,
        "vector_hits": vector_hits,
        "constraints": {
            "goal": "先确认券商口径，再按该口径查数、合并过细行，不选方法",
            "numbers_out_of_scope": "不填历史金额、不填预测数字",
        },
    }
    dump_json(logs_dir(run_dir) / "evidence" / "split_pack.json", pack)
    dump_json(logs_dir(run_dir) / "search_log.json", calls)
    dump_json(log_root / "split_calls.json", calls)
    return pack


def fetch_split_evidence(run_dir: Path) -> dict[str, Any]:
    return run_live_split(run_dir)


def run_split_smoke(
    company: str,
    ticker: str,
    *,
    out_dir: Path | None = None,
    full_code: str = "",
) -> dict[str, Any]:
    """只跑拆分判断：拉年报父口径 + 检索，不依赖 historical_financials。"""
    from valuation.shared.env import load_env

    load_env()
    now = datetime.now()
    last = now.year - 1
    years = [str(last - 2), str(last - 1), str(last)]
    forecast_years = [str(last + 1), str(last + 2), str(last + 3)]
    market, code = _market_and_code(full_code, ticker)
    resolved = full_code or f"{market}{code}"
    root = out_dir or (OUTPUT_DIR / "smoke" / ticker)
    root.mkdir(parents=True, exist_ok=True)
    log_root = root / "logs" / "mcp"
    log_root.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []
    as_of = now.isoformat(timespec="seconds")

    print(f"[official] 调 get_main_business_segments  fullCode={resolved}  years={years}", flush=True)
    with ComeinClient() as mcp:
        official, official_names = _fetch_official(mcp, calls, log_root, resolved, years)
        print(f"[official] 最新年报父口径：{official_names or '（空）'}", flush=True)
        for row in official.get("rows") or []:
            if row.get("is_total"):
                continue
            print(f"  {row.get('period')}  {row.get('name')}  {row.get('revenue_yi')}", flush=True)
        deps = SplitAgentDeps(
            company=company,
            ticker=ticker,
            forecast_years=forecast_years,
            official_names=official_names,
            official_rows=list(official.get("rows") or []),
            search=_mcp_search(mcp, calls, log_root),
        )
        plan = run_revenue_split(deps)
        all_hits = list(deps.hits)
        vector_hits = _dedupe_vector(all_hits)[:MAX_VECTOR]
        queries = deps.queries
        draft_dump = deps.draft.model_dump() if deps.draft else {}

    payload = {"company": company, "ticker": ticker, "ts": as_of, **plan.model_dump()}
    canonical = require_split_plan(payload, hits=all_hits)
    plan_path = root / "split_plan.json"
    caliber_path = root / "split_caliber.json"
    dump_json(plan_path, canonical)
    if draft_dump:
        dump_json(caliber_path, {"company": company, "ticker": ticker, "ts": as_of, **draft_dump})
    pack = {
        "company": company,
        "ticker": ticker,
        "full_code": resolved,
        "as_of": as_of,
        "official_segments": official,
        "split_caliber": draft_dump,
        "split_plan": canonical,
        "vector_queries": queries,
        "vector_hits": vector_hits,
        "plan_path": str(plan_path),
        "caliber_path": str(caliber_path) if draft_dump else "",
    }
    dump_json(root / "logs" / "evidence" / "split_pack.json", pack)
    dump_json(root / "logs" / "search_log.json", calls)
    return pack


def _fetch_official(mcp, calls, log_root: Path, full_code: str, years: list[str]) -> tuple[dict[str, Any], list[str]]:
    official_raw = _logged(
        calls,
        "get_main_business_segments",
        {
            "companyInfos": [{"fullCode": full_code}],
            "itemClassify": ["按产品"],
            "reportDates": [{"fiscalYear": year} for year in years],
            "searchData": ["营业收入"],
        },
        lambda: mcp.main_business_segments(
            [{"fullCode": full_code}],
            [{"fiscalYear": year} for year in years],
            ["营业收入"],
            ["按产品"],
        ),
    )
    dump_json(log_root / "get_main_business_segments.json", official_raw)
    official = _parse_official(official_raw, "按产品")
    if not official["rows"]:
        industry_raw = _logged(
            calls,
            "get_main_business_segments",
            {
                "companyInfos": [{"fullCode": full_code}],
                "itemClassify": ["按行业"],
                "reportDates": [{"fiscalYear": year} for year in years],
                "searchData": ["营业收入"],
            },
            lambda: mcp.main_business_segments(
                [{"fullCode": full_code}],
                [{"fiscalYear": year} for year in years],
                ["营业收入"],
                ["按行业"],
            ),
        )
        dump_json(log_root / "get_main_business_segments_industry.json", industry_raw)
        official = _parse_official(industry_raw, "按行业")
    return official, _product_names(official, latest_only=True)


def main() -> int:
    run_dir, _ = run_dir_arg()
    pack = run_live_split(run_dir)
    plan = pack.get("split_plan") or {}
    print(
        f"split: caliber={plan.get('caliber')} "
        f"segments={len(plan.get('segments') or [])} "
        f"queries={len(pack.get('vector_queries') or [])}"
    )
    print(f"plan: {spec_dir(run_dir) / 'split_plan.json'}")
    return 0


def _mcp_typed_search(
    mcp: ComeinClient,
    calls: list[dict[str, Any]],
    log_root: Path,
    *,
    start: str | None = None,
):
    """单类 contentTypes 检索。brief_agent 主控四轮召回。"""
    start = start or (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")

    def search(
        query: str,
        allow_image: bool,
        query_id: str,
        content_type: str,
    ) -> list[dict[str, Any]]:
        arguments = {
            "query": query,
            "filterImage": not allow_image,
            "start_time": start,
            "topK": 15,
            "contentTypes": [content_type],
        }
        print(
            f"[mcp] searchComeinResource\n"
            f"      query={query}\n"
            f"      type={content_type} filterImage={arguments['filterImage']} "
            f"start={start} topK=15",
            flush=True,
        )
        raw = _logged(
            calls,
            "searchComeinResource",
            arguments,
            lambda: mcp.search_comein_resource(**arguments),
            allow_fail=True,
        )
        dump_json(log_root / f"searchComeinResource_{query_id}.json", raw)
        if isinstance(raw, dict) and raw.get("error"):
            print(f"[mcp] 失败：{raw.get('error')}", flush=True)
        parsed = _parse_vector(
            raw, query_id, allow_image=allow_image, content_type=content_type
        )
        print(f"[mcp] 解析后 {len(parsed)} 条进 agent", flush=True)
        return parsed

    return search


def _mcp_search(mcp: ComeinClient, calls: list[dict[str, Any]], log_root: Path):
    start = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    types = ["domestic_report", "foreign_report"]

    def search(query: str, allow_image: bool, query_id: str) -> list[dict[str, Any]]:
        arguments = {
            "query": query,
            "filterImage": not allow_image,
            "start_time": start,
            "topK": 15,
            "contentTypes": types,
        }
        print(
            f"[mcp] searchComeinResource\n"
            f"      query={query}\n"
            f"      filterImage={arguments['filterImage']} start={start} topK=15 types={types}",
            flush=True,
        )
        raw = _logged(
            calls,
            "searchComeinResource",
            arguments,
            lambda: mcp.search_comein_resource(**arguments),
            allow_fail=True,
        )
        dump_json(log_root / f"searchComeinResource_{query_id}.json", raw)
        if isinstance(raw, dict) and raw.get("error"):
            print(f"[mcp] 失败：{raw.get('error')}", flush=True)
        parsed = _parse_vector(raw, query_id, allow_image=allow_image)
        print(f"[mcp] 解析后 {len(parsed)} 条进 agent", flush=True)
        return parsed

    return search


def _harvest_specs(*texts: str) -> list[str]:
    counts: dict[str, int] = {}
    for text in texts:
        for match in _SPEC.finditer(str(text or "")):
            token = match.group(1)
            unit = token[-1].upper()
            token = token[:-1] + unit
            if token in _SPEC_DENY:
                continue
            number = _spec_number(token)
            if token.endswith("T") and not 0 < number <= 8:
                continue
            if token.endswith("G") and number not in _G_OK:
                continue
            counts[token] = counts.get(token, 0) + 1
    ranked = sorted(
        counts.items(),
        key=lambda item: (item[1], 1 if item[0].endswith("T") else 0, _spec_number(item[0])),
        reverse=True,
    )
    return [token for token, _ in ranked[:5]]


def _spec_number(token: str) -> float:
    try:
        return float(token[:-1])
    except ValueError:
        return 0.0


def _latest_year(official: dict[str, Any]) -> str:
    years: list[str] = []
    for row in official.get("rows") or []:
        period = str(row.get("period") or "")
        year = period[:4]
        if year.isdigit():
            years.append(year)
    return max(years) if years else ""


def _product_names(official: dict[str, Any], *, latest_only: bool = True) -> list[str]:
    latest = _latest_year(official) if latest_only else ""
    names: list[str] = []
    seen: set[str] = set()
    for row in official.get("rows") or []:
        period = str(row.get("period") or "")
        if latest and period[:4] != latest:
            continue
        name = str(row.get("name") or "").strip()
        if not name or name in TOTAL_NAMES or name in RESIDUAL_NAMES or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _parse_official(payload: Any, classify: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for rec in _records(payload):
        year = str(rec.get("fiscalYear") or rec.get("year") or "")
        period = f"{year}A" if year.isdigit() else year
        indicators = rec.get("indicators") if isinstance(rec.get("indicators"), list) else [rec]
        for item in indicators:
            if not isinstance(item, dict):
                continue
            name = str(item.get("segmentName") or item.get("itemName") or item.get("name") or "").strip()
            indicator = str(item.get("indicatorName") or item.get("name") or "")
            if indicator and indicator != "营业收入" and "segmentName" not in item:
                continue
            raw = item.get("value") or item.get("营业收入") or item.get("revenue")
            amount = parse_amount(raw)
            if not name or amount is None:
                continue
            rows.append(
                {
                    "period": period,
                    "name": name,
                    "classify": classify,
                    "revenue_yi": amount,
                    "revenue_raw": raw,
                    "is_total": name in TOTAL_NAMES,
                    "source_tool": "get_main_business_segments",
                }
            )
    return {"item_classify": classify, "rows": rows}


def _parse_vector(
    payload: Any,
    query_id: str,
    *,
    allow_image: bool = False,
    content_type: str = "",
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    allowed = REPORT_TYPES | {""}
    if allow_image:
        allowed |= IMAGE_TYPES
    loose = content_type in {"minutes", "comment", "article"}
    for rec in _items(payload):
        if not isinstance(rec, dict):
            continue
        doc = str(rec.get("docType") or "").upper()
        if not loose and doc not in allowed:
            continue
        if loose and not allow_image and doc == "IMAGE":
            continue
        text = str(rec.get("chunkText") or rec.get("viewpoint") or rec.get("mainText") or "")
        if not text.strip():
            continue
        if not loose and _is_forecast_sheet(text):
            continue
        hits.append(
            {
                "query_id": query_id,
                "title": str(rec.get("title") or "").strip(),
                "institution": str(rec.get("institutionName") or rec.get("author") or "").strip(),
                "date": _biz_date(rec.get("businessTime")),
                "score": rec.get("score"),
                "doc_type": rec.get("docType"),
                "source": rec.get("source"),
                "doc_id": rec.get("docId") or rec.get("businessId"),
                "chunk_id": rec.get("chunkId"),
                "pointer": str(rec.get("url") or rec.get("displayUrl") or ""),
                "has_table": "<table" in text.lower(),
                "specs": _harvest_specs(text),
                "snippet": _keep_chunk(text),
                "content_type": content_type,
            }
        )
    return hits


def _dedupe_vector(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for hit in hits:
        key = str(hit.get("chunk_id") or hit.get("pointer") or hit.get("title") or "")
        if not key:
            key = f"{hit.get('title')}|{hit.get('date')}|{len(hit.get('snippet') or '')}"
        prev = best.get(key)
        if prev is None or _vector_rank(hit) > _vector_rank(prev):
            merged = dict(hit)
            if prev and prev.get("query_id") != hit.get("query_id"):
                merged["query_id"] = "+".join(
                    sorted({str(prev.get("query_id") or ""), str(hit.get("query_id") or "")})
                )
            best[key] = merged
    return sorted(best.values(), key=_vector_rank, reverse=True)


_LINE_INCOME = re.compile(r"(?:业务|设备|产品|器件|板块).{0,12}(?:营业收入|营收|收入)")


def _is_forecast_sheet(text: str) -> bool:
    """公司合计利润表丢掉。正文里已有分业务/分项收入的切片留下。"""
    if any(token in text for token in _SEGMENT) or _LINE_INCOME.search(text):
        return False
    if any(token in text for token in _FORECAST_PAGE):
        return True
    if sum(token in text for token in _PNL) >= 2 and "营业收入" in text:
        return True
    return False


def _vector_rank(hit: dict[str, Any]) -> tuple[int, int, int, float]:
    text = str(hit.get("snippet") or "")
    income = 1 if _LINE_INCOME.search(text) else 0
    segment = 1 if any(token in text for token in _SEGMENT) else 0
    table = 1 if hit.get("has_table") or "<table" in text.lower() else 0
    return (income, segment, table, float(hit.get("score") or 0))


def _keep_chunk(text: str) -> str:
    text = str(text or "").strip()
    if len(text) <= VECTOR_SNIPPET:
        return text
    return text[: VECTOR_SNIPPET - 1] + "…"


def _biz_date(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return ""
    return str(value or "")[:19]


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("result", "data", "list", "records", "items", "content"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return _records(payload)


def _records(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if node and all(isinstance(item, dict) for item in node):
                found.extend(item for item in node if isinstance(item, dict))
                return
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            for key in ("data", "list", "records", "items", "content"):
                if key in node:
                    walk(node[key])
                    if found:
                        return

    walk(payload)
    return found


def _market_and_code(full_code: str, ticker: str) -> tuple[str, str]:
    code = (full_code or ticker).strip()
    lowered = code.lower()
    for prefix in ("sz", "sh", "bj", "hk", "us"):
        if lowered.startswith(prefix):
            return prefix, code[len(prefix) :]
    if ticker.startswith("6"):
        return "sh", ticker
    if ticker.startswith(("4", "8", "9")):
        return "bj", ticker
    return "sz", ticker


def _logged(
    calls: list[dict[str, Any]],
    tool: str,
    arguments: dict[str, Any],
    fn,
    allow_fail: bool = False,
):
    try:
        result = fn()
    except Exception as exc:
        calls.append({"tool": tool, "arguments": arguments, "ok": False, "hits": 0, "error": str(exc)})
        if allow_fail:
            return {"error": str(exc)}
        raise
    hits = _hit_count(result)
    calls.append({"tool": tool, "arguments": arguments, "ok": True, "hits": hits})
    return result


def _hit_count(payload: Any) -> int:
    return len(_items(payload)) if not isinstance(payload, dict) or "error" not in payload else 0


if __name__ == "__main__":
    raise SystemExit(main())
