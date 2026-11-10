"""确认 split_plan 后补搜历史收入；无依据的兜底零子项在预测前退回年报父项。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from valuation.comein.comein import ComeinClient
from valuation.segment_split.split_plan import (
    has_company_residual,
    is_residual_name,
    money,
    money_sum,
    normalize_split_plan,
    plug_parent_residuals,
    resolve_unsupported_children,
    same_money,
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
    amount: float = Field(description="该年该分部营业收入，采用事实文件的币种和金额单位")
    quality: Literal["直接披露", "有据可查", "推算", "倒推", "兜底"] = Field(
        description="格子质量。年报原行用直接披露；券商原表有对应数字用有据可查"
    )
    why: str = Field(description="这个数从哪来，或为何推算/倒推")


class HistSeg(BaseModel):
    name: str = Field(description="split_plan 里已有的分部名")
    years: list[HistCell] = Field(description="每个历史年一格")


class SourceDocument(BaseModel):
    title: str = Field(description="检索结果中的完整文件标题")
    date: str = Field(description="检索结果中的发布日期，YYYY-MM-DD")
    institution: str = Field(default="", description="券商或发布机构")


class HistFill(BaseModel):
    """历史拆分回填。名单已锁定，这里只拍金额。"""

    fill_method: Literal["官方抄录", "卖方抄录", "残差倒推", "结构推算"] = Field(
        description="历史填数方法，必填，只能是这四者之一：官方抄录、卖方抄录、残差倒推、结构推算"
    )
    segments: list[HistSeg]
    split_logic: str
    other_note: str
    source_documents: list[SourceDocument] = Field(default_factory=list)
    final_rationale: str = ""


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
    plan_caliber: str = "official"
    max_searches: int = MAX_HIST_SEARCHES
    verbose: bool = True
    stage: str = "hist"
    amount_unit: str = "亿元"
    hits: list[dict[str, Any]] = field(default_factory=list)
    prior_hits: list[dict[str, Any]] = field(default_factory=list)
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
        f"已锁定分部：{names}。问句点已锁定分部，不带预测年。\n"
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
    parent_plugs = [
        name
        for name in deps.plan_names
        if any(
            str(item.get("name") or "").strip() == name
            and str(item.get("parent") or "").strip()
            and is_residual_name(
                name,
                parent=str(item.get("parent") or ""),
                official_parents=deps.official_names,
            )
            for item in deps.plan_segments
        )
    ]
    if parent_plugs:
        recon = (
            f"每年分部加总必须对上该年{REVENUE_SCOPE}。"
            f"该年年报有父项时，该父口径下各子项加已锁定的父口径其他必须对上该父项。"
            f"未拆开的金额进{'、'.join(parent_plugs)}。\n"
        )
    else:
        recon = (
            "名单里未预留父口径其他。若各子项在业务构成上已完整覆盖该年报父项，"
            "券商因四舍五入或占比推算产生的微小尾差须由你在子项中直接消化轧平，使子项加总严格等于年报父项，且子项加总不得超过年报父项。"
            "仅当年报或研报只披露了父项下的部分子业务（确有未单列的其余业务），或子项均为年报直接披露且加总小于父项时，"
            f"子项才按各自金额填写并留出正向差额，回填后由程序自动补上父口径其他（此时已锁定分部加总可小于该年{REVENUE_SCOPE}，差额等于这些父项未覆盖部分）。\n"
        )
    return (
        f"你是{deps.label}的历史拆分回填。名单已确认，只拍每个历史年的分部营业收入（{deps.amount_unit}）。\n"
        "来源币种不同时须依据明确汇率转换，不能直接混用金额；无法核实转换时写明缺口。\n"
        f"分部必须恰好是：{names}。历史年必须恰好是：{years}。\n"
        f"对账锚：{totals}。{recon}"
        f"年报行名会改，要判断是同一条、父口径合计，还是新行。"
        f"子项只填该叶子自己的收入；父项披露数原样抄进父项行。"
        "年报未单列不等于收入为零。真实零收入须有业务尚未开展、已退出等来源依据。"
        "若具名子项全部历史年都缺数据，不能伪造量价或把缺失称为真实零；"
        "以兜底零占位并逐格说明缺口，程序将在预测前撤销该无依据下钻。"
        "整个父项都无有效子项时恢复年报父项，按父项重新研究预测。\n"
        f"公司层差额（{REVENUE_SCOPE}减去各年报父项合计）只能写入「{company_slot}」。\n"
        f"年报抄得到就抄；券商表能对上已锁定子项也可以用。缺的年可以按结构推算。\n"
        "fill_method 必填，且只能是：官方抄录、卖方抄录、残差倒推、结构推算。\n"
        f"质量用：直接披露（年报原行）、有据可查（券商原表）、推算、倒推、兜底。why 逐格写取数依据，但不要重复套话。\n"
        "split_logic 具体解释采用哪些年报分部、为什么保留或下钻哪些业务，以及未单列业务的边界；不要抄逐年加总式。"
        "other_note 解释“其他”或父口径残差代表什么、哪些内容不能确定；不要重复逐年算式。"
        "source_documents 只列实际用于本次拆分或金额回填的检索文件，标题、机构、日期须与 hits 完全对应；"
        "年报接口记录无需填入，程序会单独列示。"
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
    retries=2,
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
    if len(segs) < 2:
        raise SystemExit("split_plan 分部不足两行")
    official = _load_official(run_dir, facts)
    split_pack_path = logs_dir(run_dir) / "evidence" / "split_pack.json"
    split_pack = load_json(split_pack_path) if split_pack_path.is_file() else {}
    hist = list(facts["hist_periods"])
    revenue = [float(item) for item in facts["income"][REVENUE_SCOPE]]
    fcst = [str(item)[:4] for item in (facts.get("forecast_periods") or [])]
    log_root = logs_dir(run_dir) / "mcp"
    log_root.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []

    with ComeinClient() as mcp:
        deps = HistFillDeps(
            company=str(facts.get("company") or plan.get("company") or ""),
            amount_unit=facts.get("unit") or "亿元",
            ticker=str(facts.get("ticker") or plan.get("ticker") or ""),
            hist_periods=hist,
            forecast_years=fcst,
            revenue=revenue,
            plan_names=[str(item["name"]).strip() for item in segs],
            plan_segments=segs,
            plan_caliber=str(plan.get("caliber") or "official"),
            official_names=[str(name).strip() for name in (plan.get("official_parents") or []) if str(name).strip()],
            official_rows=list(official.get("rows") or []),
            prior_hits=list(split_pack.get("vector_hits") or []),
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
        notes = notes_from_draft(facts, plan, official, draft, hits=[*deps.prior_hits, *deps.hits])
    except SchemaError as exc:
        raise SystemExit("历史拆分回填未通过规范表:\n  " + "\n  ".join(exc.errors)) from exc
    before = [str(item.get("name") or "") for item in plan.get("segments") or []]
    after = [str(item.get("name") or "") for item in notes.get("final_segments") or []]
    if before != after:
        _align_locked_names(run_dir, notes.get("final_segments") or [])
        removed = [name for name in before if name not in after]
        added = [name for name in after if name not in before]
        if removed:
            print(f"historical_fill: 历史口径收口，去掉 {'、'.join(removed)}", flush=True)
        if added:
            print(f"historical_fill: 历史口径收口，加入 {'、'.join(added)}", flush=True)
    return notes


def _align_locked_names(run_dir: Path, segments: list[dict[str, Any]]) -> None:
    """历史回填调整残差或撤销无依据下钻后，同步最终预测名单。"""
    rows = [
        {
            "name": str(item.get("name") or "").strip(),
            "parent": str(item.get("parent") or "").strip(),
        }
        for item in segments
        if str(item.get("name") or "").strip()
    ]
    path = spec_dir(run_dir) / "split_plan.json"
    if path.is_file():
        data = load_json(path)
        data["segments"] = rows
        dump_json(path, data)


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
    for attempt in range(1, 5):
        _log(deps, f"[hist] 写回填 第{attempt}次")
        result = fill_agent.run_sync(_fill_user(deps, hint), deps=deps, model=chat)
        _log_messages(deps, result.all_messages())
        draft = result.output
        last_errors = hist_errors(deps, draft)
        if not last_errors:
            return draft
        _log(deps, "[hist] 回填不合法：" + "；".join(last_errors))
        prev_json = json.dumps(draft.model_dump(), ensure_ascii=False)
        hint = f"上次回填草稿：{prev_json}\n回填不合法：{'；'.join(last_errors)}。请基于上次草稿直接修正上述问题。"
    raise SchemaError(last_errors or ["写历史回填失败"])


def notes_from_draft(
    facts: dict, plan: dict, official: dict, draft: HistFill, *, hits: list[dict[str, Any]] | None = None
) -> dict:
    hist = list(facts["hist_periods"])
    by_name = {str(item.name).strip(): item for item in draft.segments}
    last = hist[-1] if hist else ""
    last_total = float(facts["income"][REVENUE_SCOPE][-1]) if facts["income"][REVENUE_SCOPE] else 0.0
    sources: list[dict[str, Any]] = []
    seen_years: set[str] = set()
    for official_row in official.get("rows") or []:
        year = str(official_row.get("period") or "")[:4]
        if not year or year in seen_years:
            continue
        seen_years.add(year)
        sources.append({
            "source_id": f"S{len(sources) + 1}",
            "source_tool": "get_main_business_segments",
            "source_title": f"年报主营构成数据（{facts.get('company') or plan.get('company')} {year} 年度；接口未提供文件名）",
            "published": str(official_row.get("publish_date") or ""),
            "summary": official.get("item_classify") or "按产品",
            "segments_identified": [str(item.get("name") or "") for item in plan.get("segments") or []],
        })
    available = {
        (str(hit.get("title") or "").strip(), str(hit.get("date") or "").strip()): hit
        for hit in (hits or []) if hit.get("title") and hit.get("date")
    }
    seen_docs: set[tuple[str, str]] = set()
    for doc in draft.source_documents:
        key = (doc.title.strip(), doc.date.strip())
        hit = available.get(key)
        if hit is None or key in seen_docs:
            continue
        seen_docs.add(key)
        sources.append({
            "source_id": f"S{len(sources) + 1}",
            "source_tool": "searchComeinResource",
            "source_title": doc.title.strip(),
            "institution": str(hit.get("institution") or doc.institution).strip(),
            "published": doc.date.strip(),
            "segments_identified": [str(item.name) for item in draft.segments],
        })
    if not sources:
        sources.append({
            "source_id": "S1", "source_tool": "get_main_business_segments",
            "source_title": "年报主营构成数据（接口未提供文件名）", "published": "",
            "segments_identified": [str(item.name) for item in draft.segments],
        })
    final_segments = []
    for item in plan.get("segments") or []:
        name = str(item.get("name") or "").strip()
        row = by_name[name]
        revenue = {cell.year: float(cell.amount) for cell in row.years}
        quality = {cell.year: cell.quality for cell in row.years}
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
                "source_refs": [source["source_id"] for source in sources],
                "revenue_share_latest": share,
                "note": tag,
            }
        )
    official_parents = [str(name).strip() for name in (plan.get("official_parents") or []) if str(name).strip()]
    final_segments, resolutions = resolve_unsupported_children(
        final_segments, hist, list(official.get("rows") or []), official_parents,
        last_total=last_total,
    )
    before_names = [str(item.get("name") or "") for item in final_segments]
    final_segments, added = plug_parent_residuals(
        final_segments,
        hist,
        list(official.get("rows") or []),
        official_parents,
        last_total=last_total,
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
        "official_parents": official_parents,
        "final_segments": final_segments,
        "split_explanation": {
            "拆分逻辑": draft.split_logic,
            "结构变化": "由历史收入计算",
            "其他业务说明": draft.other_note,
            "来源文件": "见来源清单",
        },
        "final_rationale": draft.final_rationale,
        "sources": sources,
    }
    after_names = {str(item.get("name") or "") for item in final_segments}
    removed = [name for name in before_names if name not in after_names]
    clauses: list[str] = []
    if removed:
        clauses.append(f"{'、'.join(removed)}各历史年与父项差额为 0，已从名单删除。")
    if added:
        clauses.append(f"子项加总与年报父项不一致，已按精确差额补上{'、'.join(added)}。")
    if clauses:
        logic = str(notes["split_explanation"]["拆分逻辑"] or "").rstrip("。")
        notes["split_explanation"]["拆分逻辑"] = f"{logic}。{''.join(clauses)}" if logic else "".join(clauses)
        for source in sources:
            identified = source.get("segments_identified")
            if isinstance(identified, list):
                source["segments_identified"] = [item for item in identified if item not in removed]
                for name in added:
                    if name not in source["segments_identified"]:
                        source["segments_identified"].append(name)
    if resolutions:
        from valuation.segment_split.presentation import split_logic
        # 草稿仍会描述被撤销的零子项，按最终结构重建说明，避免旧口径流入研究。
        notes["split_explanation"]["拆分逻辑"] = ""
        explanation = split_logic(notes)
        for decision in resolutions:
            names = "、".join(decision["removed"])
            action = f"恢复年报父项{decision['parent']}" if decision["collapsed"] else "保留其余有依据子项并重算父项差额"
            explanation += f"{names}历史全为缺数据的兜底零，已撤销该下钻，{action}。"
        notes["split_explanation"]["拆分逻辑"] = explanation
        notes["split_explanation"]["其他业务说明"] = "仅保留有金额依据的父项差额及公司级收入差额；已撤销的子项不再独立预测。"
        notes["final_rationale"] = explanation
        notes["historical_resolution"] = resolutions
        final_names = [seg["name"] for seg in final_segments]
        for source in sources:
            source["segments_identified"] = final_names.copy()
    for key in EXPLAIN_KEYS:
        if not str(notes["split_explanation"].get(key) or "").strip():
            notes["split_explanation"][key] = "见历史数据及来源清单。"
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
        parts: list[float] = []
        missing = False
        for parent in official:
            amount = _official_amount(deps, year, parent)
            if amount is None:
                missing = True
                break
            parts.append(amount)
        if missing:
            continue
        official_sum = money_sum(parts)
        if not same_money(official_sum, reported):
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
        gap, gap_parts = _unplugged_parent_gap_detail(deps, year, amounts, parent_of, official)
        actual_sum = money_sum(list(amounts.values()))
        if actual_sum + gap != money(reported):
            company_res = next(
                (
                    name
                    for name in deps.plan_names
                    if is_residual_name(name, parent="", official_parents=official)
                    and not parent_of.get(name)
                ),
                "",
            )
            detail = ""
            if gap != 0:
                breakdown = "、".join(f"{p}={g}" for p, g in gap_parts)
                detail = f"加上未补的父项差额 {gap}（{breakdown}）"
            other_hint = ""
            if company_res and company_res in amounts:
                non_res = money_sum([v for k, v in amounts.items() if k != company_res])
                expected_other = money(reported) - non_res - gap
                other_hint = (
                    f"（未补父项差额将由程序自动补行，请勿计入「{company_res}」；"
                    f"若其余分部金额不变，「{company_res}」在 {year} 应填 {expected_other}）"
                )
            errors.append(
                f"{year} 分部加总 {actual_sum} {detail}对不上 {REVENUE_SCOPE} {reported}{other_hint}"
            )
        errors.extend(_parent_year_errors(deps, year, amounts, qualities, parent_of, official))
    if draft.fill_method not in FILL_METHODS:
        errors.append(f"fill_method 非法: {draft.fill_method}")
    available = {
        (str(hit.get("title") or "").strip(), str(hit.get("date") or "").strip())
        for hit in [*deps.prior_hits, *deps.hits]
    }
    for doc in draft.source_documents:
        if (doc.title.strip(), doc.date.strip()) not in available:
            errors.append(f"来源文件不在检索结果中: {doc.title}｜{doc.date}")
    if (deps.plan_caliber == "drilled" or any(
        cell.quality == "有据可查" for seg in draft.segments for cell in seg.years
    )) and not draft.source_documents:
        errors.append("细分口径或券商取数须列至少一份实际使用的检索文件")
    return errors


def _unplugged_parent_gap_detail(
    deps: HistFillDeps,
    year: str,
    amounts: dict[str, float],
    parent_of: dict[str, str],
    official: list[str],
) -> tuple[Decimal, list[tuple[str, Decimal]]]:
    """名单还没有父口径其他时，父项披露数减去具名子项，留待回填后补行。"""
    gap = Decimal(0)
    parts: list[tuple[str, Decimal]] = []
    for parent in official:
        members = [name for name in deps.plan_names if parent_of.get(name) == parent and name != parent]
        if not members:
            continue
        if any(is_residual_name(name, parent=parent, official_parents=official) for name in members):
            continue
        official_amt = _official_amount(deps, year, parent)
        if official_amt is None:
            continue
        named = [amounts[name] for name in members if name in amounts]
        diff = money(official_amt) - money_sum(named)
        gap += diff
        if diff != 0:
            parts.append((parent, diff))
    return gap, parts


def _unplugged_parent_gap(
    deps: HistFillDeps,
    year: str,
    amounts: dict[str, float],
    parent_of: dict[str, str],
    official: list[str],
) -> Decimal:
    gap, _ = _unplugged_parent_gap_detail(deps, year, amounts, parent_of, official)
    return gap


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
        present = [amounts[name] for name in members if name in amounts]
        has_plug = any(
            name != parent and is_residual_name(name, parent=parent, official_parents=official)
            for name in members
        )
        if has_plug and not same_money(money_sum(present), official_amt):
            errors.append(
                f"{year} {parent} 子项加总 {money_sum(present)} 对不上年报父项 {official_amt}"
            )
        elif not has_plug:
            named_children = [name for name in members if name != parent and name in amounts]
            if named_children:
                named_sum = money_sum([amounts[name] for name in named_children])
                gap = money(official_amt) - named_sum
                all_direct = all(qualities.get(name) == "直接披露" for name in named_children)
                if gap < 0:
                    errors.append(
                        f"{year} {parent} 子项加总 {named_sum} 超过年报父项 {official_amt}，请消化券商四舍五入尾差或核对口径"
                    )
                elif 0 < gap <= money(t_recon(official_amt)) and (not all_direct or gap <= Decimal("0.01")):
                    errors.append(
                        f"{year} {parent} 子项加总 {named_sum} 与年报父项 {official_amt} 仅相差 {gap}，属于四舍五入或推算尾差，请直接在子项中轧平"
                    )
        for name in members:
            if name == parent:
                continue
            if is_residual_name(name, parent=parent, official_parents=official):
                continue
            amount = amounts.get(name)
            if amount is None:
                continue
            if same_money(amount, official_amt):
                errors.append(f"{year} 不要把父口径 {parent} 总数填进子项 {name}")
            if abs(amount) <= 1e-12 and name not in disclosed and qualities.get(name) == "直接披露":
                errors.append(f"{year} {name} 年报未单列，0 不能标直接披露")
    return errors


def _load_official(run_dir: Path, facts: dict) -> dict[str, Any]:
    from valuation.segment_split.collect import _fetch_official, _market_and_code, _parse_official

    pack_path = logs_dir(run_dir) / "evidence" / "split_pack.json"
    if pack_path.is_file():
        pack = load_json(pack_path)
        official = pack.get("official_segments") or {}
        if official.get("rows"):
            if not any(row.get("publish_date") for row in official["rows"]):
                raw_path = logs_dir(run_dir) / "mcp" / "get_main_business_segments.json"
                if raw_path.is_file():
                    dated = _parse_official(load_json(raw_path), official.get("item_classify") or "按产品")
                    dates = {
                        (row["period"], row["name"]): row.get("publish_date")
                        for row in dated.get("rows") or []
                    }
                    for row in official["rows"]:
                        row["publish_date"] = dates.get((row.get("period"), row.get("name")), "")
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
    source_candidates = [
        {"title": hit.get("title"), "date": hit.get("date"), "institution": hit.get("institution")}
        for hit in [*deps.prior_hits, *deps.hits]
        if hit.get("title") and hit.get("date")
    ]
    source_candidates = list({(item["title"], item["date"]): item for item in source_candidates}.values())[:30]
    totals = "；".join(
        f"{year}={amount}" for year, amount in zip(deps.hist_periods, deps.revenue)
    )
    parent_map = "；".join(
        f"{item.get('name')}（挂靠父口径：{item.get('parent') or '无，属公司级残差'}）"
        for item in deps.plan_segments
        if item.get("name")
    )
    text = (
        f"检索结束。按已锁定名单拍出每个历史年的分部收入（{deps.amount_unit}）。\n"
        f"分部：{'、'.join(deps.plan_names)}\n"
        f"分部挂靠关系：{parent_map}\n"
        f"年报父口径名单：{'、'.join(deps.official_names) or '无'}\n"
        f"历史年：{'、'.join(deps.hist_periods)}\n"
        f"{REVENUE_SCOPE}对账：{totals}\n"
        f"年报：{_official_text(deps)}\n"
        f"hits={hits}\n"
        f"可选来源文件={json.dumps(source_candidates, ensure_ascii=False)}\n"
    )
    if hint:
        text += hint
    return text


def _query_errors(deps: HistFillDeps, query: str) -> list[str]:
    text = str(query or "").strip()
    errors: list[str] = []
    if not text:
        errors.append("问句不能为空")
        return errors
    if deps.company not in text and deps.ticker not in text:
        errors.append(f"问句须包含公司名或代码（{deps.label}）")
    named = any(name and name in text for name in deps.plan_names)
    topical = any(token in text for token in ("分业务", "分产品", "分部", "营收", "收入", "主营"))
    if not named and not topical:
        errors.append("问句须点已锁定分部，或写分产品/分业务收入")
    return errors


def _note_tag(quality: str) -> str:
    if quality == "兜底":
        return "[兜底]"
    if quality in QUALITY:
        return f"[{quality}]"
    return "[推算]"
