"""确认 split_plan 后，由模型补搜并拍出历史分部收入。代码只校验，不代填。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from valuation.comein.comein import ComeinClient
from valuation.segment_split.split_plan import (
    has_company_residual,
    is_residual_name,
    normalize_caliber,
    normalize_split_plan,
)
from valuation.segment_split.split_agent import SearchTurn, _compact_hits, _log, _log_messages
from valuation.segment_split.schema import (
    EXPLAIN_KEYS,
    FILL_METHODS,
    QUALITY,
    REVENUE_SCOPE,
    SchemaError,
    require_split,
    t_recon,
)
from valuation.shared.env import build_chat_model
from valuation.shared.io import dump_json, load_json, logs_dir, spec_dir

SearchFn = Callable[[str, bool, str], list[dict[str, Any]]]

MAX_HIST_SEARCHES = 4
_FORECAST_HINT = re.compile(r"展望|盈利预测|利润表|损益表|我们预计")


class HistCell(BaseModel):
    year: str = Field(description="历史年，须与 facts 的 2023A 这种键一致")
    amount: float = Field(description="该年该分部营业收入，亿元")
    quality: Literal["直接披露", "有据可查", "推算", "倒推", "兜底"] = Field(
        description="格子质量。年报或券商表抄到的用直接披露或有据可查"
    )
    why: str = Field(description="这个数从哪来，或为何推算/倒推")


class HistSeg(BaseModel):
    name: str = Field(description="必须是 split_plan 里已有的分部名，不许改名")
    years: list[HistCell] = Field(description="每个历史年一格，不许缺年")


class HistFill(BaseModel):
    """历史拆分回填。名单已锁定，这里只拍金额。"""

    fill_method: Literal["官方抄录", "卖方抄录", "残差倒推", "结构推算"]
    segments: list[HistSeg]
    split_logic: str
    hist_data_note: str
    other_note: str
    sources_text: str
    final_rationale: str


@dataclass
class HistFillDeps:
    company: str
    ticker: str
    hist_periods: list[str]
    forecast_years: list[str]
    revenue: list[float]
    plan_names: list[str]
    plan_segments: list[dict[str, Any]]
    official_names: list[str]
    official_rows: list[dict[str, Any]]
    search: SearchFn
    max_searches: int = MAX_HIST_SEARCHES
    verbose: bool = True
    stage: str = "hist"
    hits: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    search_seq: int = 0

    @property
    def searches_left(self) -> int:
        return max(0, self.max_searches - len(self.queries))

    @property
    def stage_queries(self) -> list[dict[str, Any]]:
        return self.queries

    @property
    def label(self) -> str:
        if self.company and self.ticker:
            return f"{self.company}（{self.ticker}）"
        return self.company or self.ticker


def _search_instructions(ctx: RunContext[HistFillDeps]) -> str:
    deps = ctx.deps
    names = "、".join(deps.plan_names)
    official = _official_text(deps)
    example = f"{deps.company} {deps.plan_names[0] if deps.plan_names else ''} 近三年分业务营业收入或占比"
    return (
        f"你为{deps.label}补搜历史分部收入，用来填已确认的拆分名单。\n"
        f"已锁定分部：{names}。不许问名单外的新产品，不许问预测年。\n"
        f"年报主营构成已有：{official}\n"
        f"还可检索 {deps.searches_left} 次。问句须含公司名或代码，并点一个已锁定分部或写分产品/分业务。\n"
        f"年报对得上的行不用再问。缺哪一年哪一行的历史收入或占比，就搜那一行。\n"
        f"例如：{example}。够了就把 stop 设为 true。"
    )


def _fill_instructions(ctx: RunContext[HistFillDeps]) -> str:
    deps = ctx.deps
    names = "、".join(deps.plan_names)
    years = "、".join(deps.hist_periods)
    totals = "；".join(
        f"{year} {REVENUE_SCOPE}={amount}" for year, amount in zip(deps.hist_periods, deps.revenue)
    )
    residual = (
        "、".join(
            name
            for name in deps.plan_names
            if is_residual_name(name, official_parents=deps.official_names)
        )
        or "无"
    )
    company_res = next(
        (
            name
            for name in deps.plan_names
            if is_residual_name(name, parent="", official_parents=deps.official_names)
            and not str(next((item.get("parent") for item in deps.plan_segments if item.get("name") == name), "") or "")
        ),
        "",
    )
    company_slot = company_res or "公司级残差"
    return (
        f"你是{deps.label}的历史拆分回填。名单已确认，只拍每个历史年的分部营业收入（亿元）。\n"
        f"分部必须恰好是：{names}。历史年必须恰好是：{years}。\n"
        f"对账锚：{totals}。每年分部加总必须对上该年{REVENUE_SCOPE}。"
        f"该年年报有父项时，该父口径下各子项加该父口径残差必须对上该父项。\n"
        f"年报行名会改，要判断是同一条、父口径合计，还是新行；不要把父口径总数填进子项。"
        f"年报不再单列的叶子可以写 0，未拆开的金额进该父口径残差。\n"
        f"年报父项有披露数就原样抄，禁止为了对上{REVENUE_SCOPE}去改年报父项。"
        f"公司层差额（{REVENUE_SCOPE}减去各年报父项合计）只能写入「{company_slot}」。\n"
        f"年报抄得到就抄；券商表能对上已锁定子项也可以用。缺的年可以按结构推算或把残差进「{residual}」。\n"
        f"质量用：直接披露、有据可查、推算、倒推、兜底。\n"
        f"不许增删分部，不写一致预期。"
    )


search_agent = Agent(
    output_type=SearchTurn,
    deps_type=HistFillDeps,
    retries=0,
    instructions=_search_instructions,
)

fill_agent = Agent(
    output_type=HistFill,
    deps_type=HistFillDeps,
    retries=0,
    instructions=_fill_instructions,
)


def run_live_hist_fill(run_dir: Path) -> dict:
    from valuation.segment_split.collect import _mcp_search

    root = spec_dir(run_dir)
    facts = load_json(root / "facts.json")
    plan_path = root / "split_plan.json"
    if not plan_path.is_file():
        raise SystemExit("缺少 split_plan.json。先跑 segment_split 并确认口径。")
    raw_plan = load_json(plan_path)
    plan = normalize_split_plan(raw_plan)
    segs = [item for item in (plan.get("segments") or []) if isinstance(item, dict) and item.get("name")]
    if [item.get("name") for item in segs] != [
        item.get("name") for item in (raw_plan.get("segments") or []) if isinstance(item, dict)
    ]:
        dump_json(plan_path, plan)
        caliber_path = root / "split_caliber.json"
        if caliber_path.is_file():
            dump_json(caliber_path, normalize_caliber(load_json(caliber_path)))
    if len(segs) < 2:
        raise SystemExit("split_plan 分部不足两行")
    official = _load_official(run_dir, facts)
    hist = list(facts["hist_periods"])
    revenue = [float(item) for item in facts["income"][REVENUE_SCOPE]]
    fcst = [str(item)[:4] for item in (facts.get("forecast_periods") or [])]
    log_root = logs_dir(run_dir) / "mcp"
    log_root.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []

    with ComeinClient() as mcp:
        deps = HistFillDeps(
            company=str(facts.get("company") or plan.get("company") or ""),
            ticker=str(facts.get("ticker") or plan.get("ticker") or ""),
            hist_periods=hist,
            forecast_years=fcst,
            revenue=revenue,
            plan_names=[str(item["name"]).strip() for item in segs],
            plan_segments=segs,
            official_names=[str(name).strip() for name in (plan.get("official_parents") or []) if str(name).strip()],
            official_rows=list(official.get("rows") or []),
            search=_mcp_search(mcp, calls, log_root),
        )
        draft = run_hist_fill(deps)
        dump_json(log_root / "hist_fill_raw.json", draft.model_dump())

    dump_json(logs_dir(run_dir) / "search_log_hist.json", calls)
    dump_json(
        logs_dir(run_dir) / "evidence" / "hist_fill_pack.json",
        {
            "company": deps.company,
            "ticker": deps.ticker,
            "as_of": datetime.now().isoformat(timespec="seconds"),
            "official_segments": official,
            "vector_queries": deps.queries,
            "hist_fill": draft.model_dump(),
        },
    )
    try:
        return notes_from_draft(facts, plan, official, draft)
    except SchemaError as exc:
        raise SystemExit("历史拆分回填未通过规范表:\n  " + "\n  ".join(exc.errors)) from exc


def run_hist_fill(deps: HistFillDeps, *, model=None) -> HistFill:
    chat = model or build_chat_model()
    _log(deps, "[hist] 按拆分计划补搜历史分部收入")
    while deps.searches_left > 0:
        turn = _run_search_turn(deps, chat)
        if turn.stop:
            _log(deps, f"[hist] 停搜：{turn.why or '已够'}")
            break
        _execute_search(deps, turn.query, turn.allow_image)
    return compile_hist_fill(deps, model=chat)


def compile_hist_fill(deps: HistFillDeps, *, model=None) -> HistFill:
    slot_errors = plan_slot_errors(deps)
    if slot_errors:
        raise SchemaError(slot_errors)
    chat = model or build_chat_model()
    hint = ""
    last_errors: list[str] = []
    for attempt in range(1, 4):
        _log(deps, f"[hist] 写回填 第{attempt}次")
        result = fill_agent.run_sync(_fill_user(deps, hint), deps=deps, model=chat)
        _log_messages(deps, result.all_messages())
        draft = result.output
        last_errors = hist_errors(deps, draft)
        if not last_errors:
            return draft
        _log(deps, "[hist] 回填不合法：" + "；".join(last_errors))
        hint = "回填不合法：" + "；".join(last_errors)
    raise SchemaError(last_errors or ["写历史回填失败"])


def notes_from_draft(facts: dict, plan: dict, official: dict, draft: HistFill) -> dict:
    hist = list(facts["hist_periods"])
    by_name = {str(item.name).strip(): item for item in draft.segments}
    last = hist[-1] if hist else ""
    last_total = float(facts["income"][REVENUE_SCOPE][-1]) if facts["income"][REVENUE_SCOPE] else 0.0
    sources = [
        {
            "source_id": "S1",
            "source_tool": "get_main_business_segments",
            "source_title": "年报主营构成（按产品/营业收入）",
            "summary": official.get("item_classify") or "按产品",
            "segments_identified": [str(item.get("name") or "") for item in plan.get("segments") or []],
        }
    ]
    sources.append(
        {
            "source_id": "S2",
            "source_tool": "searchComeinResource",
            "source_title": "拆分计划确认后的历史检索",
            "summary": draft.sources_text or "模型按已锁定分部补搜历史收入",
            "segments_identified": [str(item.name) for item in draft.segments],
        }
    )
    final_segments = []
    for item in plan.get("segments") or []:
        name = str(item.get("name") or "").strip()
        row = by_name[name]
        revenue = {cell.year: float(cell.amount) for cell in row.years}
        quality = {cell.year: cell.quality for cell in row.years}
        why = "；".join(cell.why for cell in row.years if cell.why)
        amount = revenue.get(last)
        share = float(amount) / last_total if last_total and amount is not None else None
        tag = _note_tag(quality.get(last) or "推算")
        final_segments.append(
            {
                "name": name,
                "parent": str(item.get("parent") or "").strip(),
                "预测方法": "",
                "historical_revenue": revenue,
                "data_quality": quality,
                "source_refs": ["S1", "S2"],
                "revenue_share_latest": share,
                "note": f"{tag} {why or '按已确认名单回填历史收入。'}",
            }
        )
    fill_method = draft.fill_method if draft.fill_method in FILL_METHODS else "残差倒推"
    if plan.get("caliber") == "official" and fill_method == "官方抄录":
        caliber = "official"
    elif fill_method == "卖方抄录":
        caliber = "sellside"
    else:
        caliber = "spec_mix"
    notes = {
        "company": plan.get("company") or facts.get("company"),
        "ticker": plan.get("ticker") or facts.get("ticker"),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "historical_periods": hist,
        "revenue_unit": facts.get("unit") or "亿元",
        "revenue_scope": REVENUE_SCOPE,
        "split_caliber": caliber,
        "fill_method": fill_method,
        "official_parents": [str(name).strip() for name in (plan.get("official_parents") or []) if str(name).strip()],
        "final_segments": final_segments,
        "split_explanation": {
            "拆分逻辑": draft.split_logic,
            "历史数据说明": draft.hist_data_note,
            "其他业务说明": draft.other_note,
            "主要来源": draft.sources_text,
        },
        "final_rationale": draft.final_rationale,
        "sources": sources,
    }
    for key in EXPLAIN_KEYS:
        if not str(notes["split_explanation"].get(key) or "").strip():
            notes["split_explanation"][key] = "见回填说明。"
    if not str(notes["final_rationale"] or "").strip():
        notes["final_rationale"] = notes["split_explanation"]["拆分逻辑"]
    return require_split(notes, facts, require_method=False)


def plan_slot_errors(deps: HistFillDeps) -> list[str]:
    """名单是否具备同时对上营业收入和年报父项的格子。缺格子时不要让模型空转。"""
    official = list(deps.official_names)
    if not official:
        return []
    if has_company_residual(deps.plan_segments, official):
        return []
    errors: list[str] = []
    for i, year in enumerate(deps.hist_periods):
        reported = float(deps.revenue[i])
        official_sum = 0.0
        missing = False
        for parent in official:
            amount = _official_amount(deps, year, parent)
            if amount is None:
                missing = True
                break
            official_sum += amount
        if missing:
            continue
        if abs(official_sum - reported) > t_recon(reported):
            errors.append(
                f"{year} 年报父项合计 {official_sum} 对不上 {REVENUE_SCOPE} {reported}，名单缺公司级残差"
            )
    return errors


def hist_errors(deps: HistFillDeps, draft: HistFill) -> list[str]:
    errors: list[str] = []
    names = [str(item.name).strip() for item in draft.segments]
    if set(names) != set(deps.plan_names):
        errors.append(f"分部必须恰好是 {'、'.join(deps.plan_names)}，实际 {'、'.join(names)}")
    if names and len(names) != len(set(names)):
        errors.append("分部名称重复")
    by_name = {str(item.name).strip(): item for item in draft.segments}
    parent_of = {
        str(item.get("name") or "").strip(): str(item.get("parent") or "").strip()
        for item in deps.plan_segments
        if str(item.get("name") or "").strip()
    }
    official = list(deps.official_names)
    for i, year in enumerate(deps.hist_periods):
        total = 0.0
        reported = float(deps.revenue[i])
        amounts: dict[str, float] = {}
        qualities: dict[str, str] = {}
        for name in deps.plan_names:
            row = by_name.get(name)
            if not row:
                continue
            cell = next((item for item in row.years if item.year == year), None)
            if cell is None:
                errors.append(f"{name} 缺少 {year}")
                continue
            if cell.quality not in QUALITY:
                errors.append(f"{name} {year} quality 非法: {cell.quality}")
            amount = float(cell.amount)
            amounts[name] = amount
            qualities[name] = cell.quality
            total += amount
        if abs(total - reported) > t_recon(reported):
            errors.append(
                f"{year} 分部加总 {total} 对不上 {REVENUE_SCOPE} {reported}（容差 {t_recon(reported)}）"
            )
        errors.extend(_parent_year_errors(deps, year, amounts, qualities, parent_of, official))
    if draft.fill_method not in FILL_METHODS:
        errors.append(f"fill_method 非法: {draft.fill_method}")
    return errors


def _official_amount(deps: HistFillDeps, year: str, name: str) -> float | None:
    year_key = str(year)[:4]
    for row in deps.official_rows:
        if row.get("is_total"):
            continue
        period = str(row.get("period") or "")
        if period != year and period[:4] != year_key:
            continue
        if str(row.get("name") or "").strip() != name:
            continue
        raw = row.get("revenue_yi")
        if raw is None:
            continue
        return float(raw)
    return None


def _official_names_in_year(deps: HistFillDeps, year: str) -> set[str]:
    year_key = str(year)[:4]
    names: set[str] = set()
    for row in deps.official_rows:
        if row.get("is_total"):
            continue
        period = str(row.get("period") or "")
        if period != year and period[:4] != year_key:
            continue
        name = str(row.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _parent_year_errors(
    deps: HistFillDeps,
    year: str,
    amounts: dict[str, float],
    qualities: dict[str, str],
    parent_of: dict[str, str],
    official: list[str],
) -> list[str]:
    errors: list[str] = []
    disclosed = _official_names_in_year(deps, year)
    for parent in official:
        members = [name for name in deps.plan_names if parent_of.get(name) == parent]
        if not members:
            continue
        official_amt = _official_amount(deps, year, parent)
        if official_amt is None:
            continue
        child_sum = sum(amounts.get(name, 0.0) for name in members)
        if abs(child_sum - official_amt) > t_recon(official_amt):
            errors.append(
                f"{year} {parent} 子项加总 {child_sum} 对不上年报父项 {official_amt}"
                f"（容差 {t_recon(official_amt)}）"
            )
        for name in members:
            if name == parent:
                continue
            if is_residual_name(name, parent=parent, official_parents=official):
                continue
            amount = amounts.get(name)
            if amount is None:
                continue
            if abs(amount - official_amt) <= t_recon(official_amt):
                errors.append(f"{year} 不要把父口径 {parent} 总数填进子项 {name}")
            if abs(amount) <= 1e-12 and name not in disclosed and qualities.get(name) == "直接披露":
                errors.append(f"{year} {name} 年报未单列，0 不能标直接披露")
    return errors


def _load_official(run_dir: Path, facts: dict) -> dict[str, Any]:
    from valuation.segment_split.collect import _fetch_official, _market_and_code

    pack_path = logs_dir(run_dir) / "evidence" / "split_pack.json"
    if pack_path.is_file():
        pack = load_json(pack_path)
        official = pack.get("official_segments") or {}
        if official.get("rows"):
            return official
    ticker = str(facts.get("ticker") or "")
    full_code = str(facts.get("full_code") or "")
    years = [str(period)[:4] for period in facts.get("hist_periods") or []]
    market, code = _market_and_code(full_code, ticker)
    resolved = full_code or f"{market}{code}"
    log_root = logs_dir(run_dir) / "mcp"
    log_root.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []
    with ComeinClient() as mcp:
        official, _ = _fetch_official(mcp, calls, log_root, resolved, years)
    if not official.get("rows"):
        raise SystemExit("年报主营构成是空的，无法回填历史拆分。")
    return official


def _official_text(deps: HistFillDeps) -> str:
    rows = "；".join(
        f"{row.get('period')} {row.get('name')} {row.get('revenue_yi')}"
        for row in deps.official_rows
        if not row.get("is_total")
    )
    return rows or "（空）"


def _run_search_turn(deps: HistFillDeps, model) -> SearchTurn:
    hint = ""
    last_errors: list[str] = []
    for attempt in range(1, 4):
        _log(deps, f"[hist] 写问句  第{len(deps.queries) + 1}拍 尝试{attempt}")
        result = search_agent.run_sync(_search_user(deps, hint), deps=deps, model=model)
        _log_messages(deps, result.all_messages())
        turn = result.output
        if turn.stop:
            return turn
        last_errors = _query_errors(deps, turn.query)
        if last_errors:
            hint = "问句不合规：" + "；".join(last_errors)
            _log(deps, "[hist] " + hint)
            continue
        return turn
    raise RuntimeError("写历史检索问句失败：" + "；".join(last_errors))


def _execute_search(deps: HistFillDeps, query: str, allow_image: bool = False) -> None:
    _log(deps, f"[hist] 问句：{query}")
    if deps.searches_left <= 0:
        raise RuntimeError("检索次数已用完")
    deps.search_seq += 1
    query_id = f"hist_search_{deps.search_seq}"
    hits = deps.search(query, allow_image, query_id)
    _log(deps, f"[hist] 回来 {len(hits)} 条")
    for i, hit in enumerate(_compact_hits(hits, limit=6, names=deps.plan_names), 1):
        _log(
            deps,
            f"  hit[{i}] {hit.get('date')} {hit.get('institution')} {hit.get('title')}\n"
            f"         {hit.get('peek')}",
        )
    deps.queries.append(
        {
            "id": query_id,
            "intent": "hist",
            "query": query,
            "allow_image": allow_image,
            "anchor_segments": list(deps.plan_names),
        }
    )
    deps.hits.extend(hits)


def _search_user(deps: HistFillDeps, hint: str) -> str:
    asked = "；".join(item.get("query") or "" for item in deps.queries) or "还没有检索"
    hits = json.dumps(_compact_hits(deps.hits, limit=10, names=deps.plan_names), ensure_ascii=False)
    text = (
        f"已锁定分部：{'、'.join(deps.plan_names)}\n"
        f"历史年：{'、'.join(deps.hist_periods)}\n"
        f"年报：{_official_text(deps)}\n"
        f"已问过：{asked}\n"
        f"还可检索 {deps.searches_left} 次。\n"
        f"hits={hits}\n"
        "缺历史收入就写下一句；年报和切片已够就把 stop 设为 true。"
    )
    if hint:
        text += "\n" + hint
    return text


def _fill_user(deps: HistFillDeps, hint: str) -> str:
    hits = json.dumps(_compact_hits(deps.hits, limit=18, names=deps.plan_names), ensure_ascii=False)
    totals = "；".join(
        f"{year}={amount}" for year, amount in zip(deps.hist_periods, deps.revenue)
    )
    text = (
        "检索结束，不要再检索。按已锁定名单拍出每个历史年的分部收入（亿元）。\n"
        f"分部：{'、'.join(deps.plan_names)}\n"
        f"历史年：{'、'.join(deps.hist_periods)}\n"
        f"{REVENUE_SCOPE}对账：{totals}\n"
        f"年报：{_official_text(deps)}\n"
        f"hits={hits}\n"
    )
    if hint:
        text += hint
    return text


def _query_errors(deps: HistFillDeps, query: str) -> list[str]:
    text = str(query or "").strip()
    errors: list[str] = []
    if deps.company not in text and deps.ticker not in text:
        errors.append(f"问句须包含公司名或代码（{deps.label}）")
    if _FORECAST_HINT.search(text):
        errors.append("问句不要写盈利预测或利润表")
    for year in deps.forecast_years:
        if year and year in text:
            errors.append(f"问句不要写入预测年 {year}")
    named = any(name and name in text for name in deps.plan_names)
    topical = any(token in text for token in ("分业务", "分产品", "分部", "营收", "收入"))
    if not named and not topical:
        errors.append("问句须点已锁定分部，或写分产品/分业务收入")
    return errors


def _note_tag(quality: str) -> str:
    if quality == "兜底":
        return "[兜底]"
    if quality in QUALITY:
        return f"[{quality}]"
    return "[推算]"
