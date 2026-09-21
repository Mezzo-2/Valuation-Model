"""本公司一致预期 + 候选 Forward PE。同业 PE 直接取 pricePerformance.peForward。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from valuation.comein.comein import ComeinClient
from valuation.segment_split.schema import require_consensus
from valuation.shared.houses import short_house_name
from valuation.shared.io import dump_json

YUAN_PER_YI = 100_000_000.0


def fetch_subject_consensus(
    mcp: ComeinClient,
    facts: dict,
    log_root: Path,
    calls: list[dict[str, Any]],
) -> dict:
    query = _subject_query(facts)
    years = _forecast_years(facts)
    raw = _call(
        calls,
        "getStockProfitForecast",
        {"queries": [query], "forecastYears": years, "size": 20},
        lambda: mcp.profit_forecast([query], forecastYears=years, size=20),
        log_root / "profit_forecast_subject.json",
    )
    houses, gaps = parse_institution_estimates(raw, facts, shares=_subject_shares(facts))
    payload = {
        "company": facts.get("company"),
        "ticker": facts.get("ticker"),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": datetime.now().strftime("%Y-%m-%d"),
        "forecast_periods": list(facts.get("forecast_periods") or []),
        "revenue_unit": "亿元",
        "revenue_scope": "营业收入",
        "eps_unit": "元/股",
        "eps_scope": "EPS",
        "institution_estimates": houses,
        "provider_aggregates": [],
        "open_gaps": gaps,
    }
    if not houses and not gaps:
        payload["open_gaps"] = ["getStockProfitForecast 未返回可识别机构"]
        payload["coverage_status"] = "unavailable"
    return require_consensus(payload, facts)


def fill_candidates(
    mcp: ComeinClient,
    candidates: list[dict[str, Any]],
    facts: dict,
    log_root: Path,
    calls: list[dict[str, Any]],
    log_prefix: str = "peers",
) -> list[dict[str, Any]]:
    subject = _norm_ticker(facts.get("ticker") or "")
    rows = []
    seen: set[str] = set()
    for item in candidates:
        raw_ticker = str(item.get("ticker") or item.get("code") or "").strip()
        ticker = _norm_ticker(raw_ticker)
        if not ticker or ticker == subject or ticker in seen:
            continue
        seen.add(ticker)
        rows.append(
            {
                "name": str(item.get("name") or "").strip(),
                "ticker": ticker,
                "query": raw_ticker or ticker,
                "why": str(item.get("why") or "").strip(),
            }
        )
    if not rows:
        return []
    queries = [row["query"] for row in rows]
    snap_map: dict[str, dict[str, Any]] = {}
    price_map: dict[str, dict[str, Any]] = {}
    for i, chunk in enumerate(_chunks(queries, 10)):
        suffix = "" if i == 0 else f"_{i + 1}"
        snap_map.update(
            _by_ticker(
                _call(
                    calls,
                    "get_financial_snapshot",
                    {"queries": chunk},
                    lambda chunk=chunk: mcp.financial_snapshot(chunk),
                    log_root / f"financial_snapshot_{log_prefix}{suffix}.json",
                )
            )
        )
        price_map.update(
            _price_by_ticker(
                _call(
                    calls,
                    "pricePerformance",
                    {
                        "queries": chunk,
                        "include": [
                            "regular_market",
                            "market_value",
                            "valuation",
                            "standardized_info",
                        ],
                    },
                    lambda chunk=chunk: mcp.price_performance(
                        chunk,
                        include=[
                            "regular_market",
                            "market_value",
                            "valuation",
                            "standardized_info",
                        ],
                    ),
                    log_root / f"price_performance_{log_prefix}{suffix}.json",
                )
            )
        )
    filled = []
    for row in rows:
        ticker = row["ticker"]
        filled.append(
            _fill_one(
                row,
                snap_map.get(ticker) or {},
                price_map.get(ticker) or {},
            )
        )
    return filled


def parse_institution_estimates(
    raw: Any,
    facts: dict,
    *,
    shares: float | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    gaps: list[str] = []
    block = _first_block(raw)
    if not block:
        return [], ["getStockProfitForecast 无本公司数据"]
    fcst = [str(year) for year in (facts.get("forecast_periods") or [])]
    want = {int(year[:4]) for year in fcst if str(year)[:4].isdigit()}
    company = str(facts.get("company") or "")
    houses: list[dict[str, Any]] = []
    for i, item in enumerate(block.get("forecastList") or [], 1):
        if not isinstance(item, dict):
            continue
        inst = short_house_name(item.get("orgNameAbbr") or item.get("institution") or "")
        date = _date(item.get("publishDate") or item.get("report_date"))
        if not inst or not date:
            continue
        estimates: dict[str, dict[str, float | None]] = {year: {"revenue": None, "eps": None} for year in fcst}
        for line in item.get("forecastItems") or []:
            if not isinstance(line, dict):
                continue
            year_n = _as_int(line.get("year"))
            if year_n is None or year_n not in want:
                continue
            label = f"{year_n}E"
            if label not in estimates:
                continue
            rev = _to_yi(line.get("revenue"), line.get("unit"))
            profit = _to_yuan(line.get("parentNetProfit"), line.get("unit"))
            eps = (profit / shares) if profit is not None and shares else None
            estimates[label] = {"revenue": rev, "eps": eps}
        houses.append(
            {
                "institution": inst,
                "report_date": date,
                "source_id": f"C{i}",
                "source_tool": "getStockProfitForecast",
                "source_title": f"{inst}-{company}-{date}",
                "estimates": estimates,
            }
        )
    houses = _dedup_houses(houses)
    if shares is None:
        gaps.append("缺股本，机构 EPS 未回推")
    if not houses:
        gaps.append("盈利预测没有可点名机构")
    return houses, gaps


def _fill_one(
    row: dict[str, Any],
    snapshot: dict,
    price: dict,
) -> dict[str, Any]:
    ticker = row["ticker"]
    snapshot = _aligned_block(snapshot, ticker, row.get("query"))
    price = _aligned_block(price, ticker, row.get("query"))
    info = snapshot.get("standardizedInfo") or {}
    price_info = price.get("standardized_info") or {}
    name = (
        row.get("name")
        or info.get("assetName")
        or price_info.get("stockShortName")
        or row["ticker"]
    )
    currency = str(price_info.get("currency") or "CNY").upper()
    _shares, last = _shares_and_last(snapshot, price)
    mcap = _to_yi((price.get("market_value") or {}).get("totalMarketValue"), currency)
    if mcap is None:
        mcap = _snapshot_number(snapshot, "总市值")
        if mcap is not None and mcap > 1000:
            mcap = mcap / YUAN_PER_YI
    valuation = price.get("valuation") or {}
    pe_y1 = _as_float(valuation.get("peForward"))
    if pe_y1 is not None and pe_y1 <= 0:
        pe_y1 = None
    pe_ttm = _as_float(valuation.get("peTtm"))
    if pe_ttm is None:
        pe_ttm = _snapshot_number(snapshot, "市盈率TTM")
    return {
        "name": _display_name(str(name), ticker),
        "ticker": ticker,
        "why": row.get("why") or "",
        "pe_y1": pe_y1,
        "pe_ttm": pe_ttm,
        "mcap": mcap,
        "thin": pe_y1 is None,
        "currency": currency,
        "price": last,
    }


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _subject_query(facts: dict) -> str:
    return str(facts.get("ticker") or facts.get("company") or "")


def _subject_shares(facts: dict) -> float | None:
    million = _as_float((facts.get("market") or {}).get("当前总股本_百万股"))
    if million:
        return million * 1_000_000.0
    return None


def _forecast_years(facts: dict) -> list[int]:
    years = []
    for period in facts.get("forecast_periods") or []:
        year = str(period)[:4]
        if year.isdigit():
            years.append(int(year))
    return years


def _call(calls: list[dict[str, Any]], tool: str, arguments: dict[str, Any], fn, path: Path) -> Any:
    try:
        result = fn()
    except Exception as exc:
        calls.append({"tool": tool, "arguments": arguments, "ok": False, "error": str(exc)})
        dump_json(path, {"error": str(exc)})
        return {"error": str(exc)}
    calls.append({"tool": tool, "arguments": arguments, "ok": True})
    dump_json(path, result)
    return result


def _first_block(raw: Any) -> dict[str, Any]:
    data = _payload_data(raw)
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, dict) and (value.get("forecastList") or value.get("standardizedInfo")):
                return value
        if data.get("forecastList"):
            return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def _by_ticker(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    data = _payload_data(raw)
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                ticker = _norm_ticker(
                    key
                    or (value.get("standardizedInfo") or {}).get("ticker")
                    or (value.get("standardizedInfo") or {}).get("fullCode")
                )
                if ticker:
                    out[ticker] = value
    elif isinstance(data, list):
        for value in data:
            if not isinstance(value, dict):
                continue
            ticker = _norm_ticker(
                value.get("query")
                or (value.get("standardizedInfo") or {}).get("ticker")
            )
            if ticker:
                out[ticker] = value
    return out


def _price_by_ticker(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    data = _payload_data(raw)
    rows = data if isinstance(data, list) else []
    if isinstance(data, dict):
        rows = list(data.values()) if not data.get("query") else [data]
    for value in rows:
        if not isinstance(value, dict):
            continue
        ticker = _norm_ticker(
            value.get("query")
            or (value.get("standardized_info") or {}).get("stockCode")
        )
        if ticker:
            out[ticker] = value
    return out


def _payload_data(raw: Any) -> Any:
    if not isinstance(raw, dict) or raw.get("error"):
        return {}
    code = raw.get("code")
    if code not in (None, "", 0, "0", 200, "200"):
        return {}
    return raw.get("data")


def _aligned_block(block: dict, ticker: str, query: Any) -> dict:
    if not block:
        return {}
    std = block.get("standardizedInfo") or block.get("standardized_info") or {}
    std_t = _norm_ticker(std.get("ticker") or std.get("stockCode") or std.get("fullCode") or "")
    aliases = {_norm_ticker(ticker), _norm_ticker(query)}
    aliases.discard("")
    if std_t and aliases and std_t not in aliases:
        return {}
    return block


def _shares_and_last(snapshot: dict, price: dict) -> tuple[float | None, float | None]:
    shares = _as_float((price.get("market_value") or {}).get("totalShare"))
    last = _as_float((price.get("regular_market") or {}).get("latestPrice"))
    if shares is None:
        revenue = _snapshot_number(snapshot, "营业收入")
        per_share = _snapshot_number(snapshot, "每股营业收入")
        if revenue and per_share and per_share > 0:
            shares = revenue / per_share
    if last is None and shares and shares > 0:
        mcap_yuan = _snapshot_number(snapshot, "总市值")
        if mcap_yuan and mcap_yuan > 0:
            last = mcap_yuan / shares
    return shares, last


def _snapshot_number(snapshot: dict, label: str) -> float | None:
    for item in snapshot.get("snapshotItems") or []:
        if isinstance(item, dict) and str(item.get("name_cn") or "") == label:
            return _as_float(item.get("value"))
    return None


def _dedup_houses(houses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for item in houses:
        key = str(item.get("institution") or "")
        old = best.get(key)
        if old is None or str(item.get("report_date") or "") > str(old.get("report_date") or ""):
            best[key] = item
    out = list(best.values())
    for i, item in enumerate(out, 1):
        item["source_id"] = f"C{i}"
    return out


def _display_name(name: str, ticker: str) -> str:
    name = name.replace("（", "(").replace("）", ")")
    if f"({ticker})" in name:
        return name.replace("(", "（").replace(")", "）")
    return f"{name}（{ticker}）"


def _norm_ticker(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace(".XSHG", "").replace(".XSHE", "").replace(".SS", "").replace(".SZ", "")
    if text.lower().startswith(("sh", "sz", "bj")) and text[2:].isdigit():
        return text[2:]
    if "." in text:
        text = text.split(".")[0]
    return text


def _to_yi(value: Any, unit: Any) -> float | None:
    number = _to_yuan(value, unit)
    if number is None:
        return None
    return number / YUAN_PER_YI


def _to_yuan(value: Any, unit: Any) -> float | None:
    number = _as_float(value)
    if number is None:
        return None
    text = str(unit or "").upper()
    if "亿" in str(unit or ""):
        return number * YUAN_PER_YI
    if "万" in str(unit or ""):
        return number * 10_000.0
    if text in {"", "CNY", "RMB", "元", "YUAN"}:
        return number
    return number


def _date(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("%", "")
        if not text or text in {"无数据", "盈利为负"}:
            return None
        value = text
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
