"""收入拆分 agent：先确认券商口径，再按该口径查数、把过细行并进其他。不选预测方法。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from valuation.segment_split.split_plan import (
    CALIBER_RULES,
    RESIDUAL_NAMES,
    hit_traces,
    is_residual_name,
    line_traces,
    normalize_caliber,
    normalize_split_plan,
    parent_residual_name,
    require_caliber,
    require_split_plan,
)
from valuation.segment_split.schema import SchemaError

SearchFn = Callable[[str, bool, str], list[dict[str, Any]]]

MAX_CALIBER_SEARCHES = 3
MAX_EVIDENCE_SEARCHES = 3
_FORECAST_HINT = re.compile(r"展望|盈利预测|利润表|损益表|我们预计")
_PEEK_HINTS = ("分业务", "分产品", "营业收入", "营收", "亿元", "出货")
_DISCLOSURE = (
    "IMPORTANT DISCLOSURES",
    "conflict of interest",
    "52周收益率",
    "分析师：",
    "Rating and Target Price",
)


class CaliberRow(BaseModel):
    name: str = Field(description="券商拆分表里的一行，或年报父口径本身")
    parent: str = Field(default="", description="挂回的年报父口径")


class CaliberDraft(BaseModel):
    """确认后的拆分口径。不含方法、不含数字。"""

    caliber: Literal["official", "drilled"] = Field(
        description="停在年报口径，还是已用券商更细口径"
    )
    official_parents: list[str] = Field(default_factory=list, description="年报父口径")
    segments: list[CaliberRow] = Field(default_factory=list, description="确认后的拆分行")


class SegmentPick(BaseModel):
    name: str = Field(description="确认口径里留下的一行，或年报父口径本身")
    parent: str = Field(default="", description="挂回的年报父口径")


class SplitPlan(BaseModel):
    """拆分卡。不含历史金额或预测数字。"""

    caliber: Literal["official", "drilled"] = Field(
        description="停在年报口径，还是已用券商更细口径"
    )
    official_parents: list[str] = Field(default_factory=list, description="年报父口径")
    segments: list[SegmentPick] = Field(default_factory=list, description="收口后的分部")


class SearchTurn(BaseModel):
    """loop 里一拍：写下一句检索，或停下来写卡。"""

    stop: bool = Field(False, description="已够时为 True，不再检索")
    query: str = Field("", description="一句中文检索问题；stop 时留空")
    allow_image: bool = Field(False, description="分规格表在附图里时为 True")
    why: str = Field("", description="为何搜或为何停")


@dataclass
class SplitAgentDeps:
    company: str
    ticker: str
    forecast_years: list[str]
    official_names: list[str]
    official_rows: list[dict[str, Any]]
    search: SearchFn
    max_caliber_searches: int = MAX_CALIBER_SEARCHES
    max_evidence_searches: int = MAX_EVIDENCE_SEARCHES
    verbose: bool = True
    stage: Literal["caliber", "evidence"] = "caliber"
    draft: CaliberDraft | None = None
    hits: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    search_seq: int = 0

    @property
    def stage_queries(self) -> list[dict[str, Any]]:
        return [item for item in self.queries if item.get("intent") == self.stage]

    @property
    def searches_left(self) -> int:
        cap = self.max_caliber_searches if self.stage == "caliber" else self.max_evidence_searches
        return max(0, cap - len(self.stage_queries))

    @property
    def label(self) -> str:
        if self.company and self.ticker:
            return f"{self.company}（{self.ticker}）"
        return self.company or self.ticker


def _leaf_names(draft: CaliberDraft | None, official: list[str]) -> list[str]:
    if not draft:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in draft.segments:
        name = item.name.strip()
        if not name or name in seen or name in official:
            continue
        if is_residual_name(name, parent=item.parent, official_parents=official):
            continue
        seen.add(name)
        names.append(name)
    return names


def query_errors(query: str, deps: SplitAgentDeps) -> list[str]:
    text = str(query or "").strip()
    errors: list[str] = []
    if deps.company not in text and deps.ticker not in text:
        errors.append(f"问句须包含公司名或代码（{deps.label}）")
    if "卖方" in text:
        errors.append("问句写券商，不写卖方")
    if "父口径" in text:
        errors.append("问句不要写父口径")
    if _FORECAST_HINT.search(text):
        errors.append("问句不要写盈利预测或利润表")
    for year in deps.forecast_years:
        if year and year in text:
            errors.append(f"问句不要写入预测年 {year}")
    if deps.stage == "evidence":
        leaves = _leaf_names(deps.draft, deps.official_names)
        if leaves and not any(name in text for name in leaves):
            errors.append("问句须点已确认口径里的产品名")
    return errors


def execute_search(deps: SplitAgentDeps, query: str, allow_image: bool = False) -> dict[str, Any]:
    _log(deps, f"\n[loop] 检索  stage={deps.stage}  allow_image={allow_image}")
    _log(deps, f"[loop] 问句：{query}")
    if deps.searches_left <= 0:
        raise RuntimeError("检索次数已用完")
    deps.search_seq += 1
    query_id = f"search_{deps.search_seq}"
    _log(deps, f"[loop] 调用 searchComeinResource  {query_id}")
    hits = deps.search(query, allow_image, query_id)
    _log(deps, f"[loop] 回来 {len(hits)} 条（累计 {len(deps.hits) + len(hits)}）")
    for i, hit in enumerate(_compact_hits(hits, limit=6, names=_rank_names(deps)), 1):
        _log(
            deps,
            f"  hit[{i}] {hit.get('date')} {hit.get('institution')} {hit.get('title')}\n"
            f"         specs={hit.get('specs')} traces={hit.get('traces')} table={hit.get('has_table')}\n"
            f"         {hit.get('peek')}",
        )
    deps.queries.append(
        {
            "id": query_id,
            "intent": deps.stage,
            "query": query,
            "allow_image": allow_image,
            "anchor_segments": list(deps.official_names),
        }
    )
    deps.hits.extend(hits)
    return {
        "query_id": query_id,
        "searches_left": deps.searches_left,
        "new_hits": len(hits),
        "total_hits": len(deps.hits),
        "hits": _compact_hits(hits, names=_rank_names(deps)),
    }


def _official_lines(deps: SplitAgentDeps) -> tuple[str, str, str]:
    official = "、".join(deps.official_names) or "（无官方产品名）"
    rows = "；".join(
        f"{row.get('period')} {row.get('name')} {row.get('revenue_yi')}"
        for row in deps.official_rows[:12]
        if not row.get("is_total")
    )
    example = f"{deps.company}近三年券商怎么拆主营收入，分业务或分产品各块有没有收入或占比。"
    return official, rows, example


def _rank_names(deps: SplitAgentDeps) -> list[str]:
    if deps.stage == "evidence":
        return _leaf_names(deps.draft, deps.official_names)
    return []


def _caliber_search_instructions(ctx: RunContext[SplitAgentDeps]) -> str:
    deps = ctx.deps
    official, rows, example = _official_lines(deps)
    return (
        f"你为{deps.label}写下一句检索问句，用来确认券商怎么拆，或决定停搜。\n"
        f"年报父口径只做回归，不要写进问句：{official}。往年改名不是并列分部。{rows or ''}\n"
        f"还可检索 {deps.searches_left} 次。问句写券商，不写卖方。点公司名。不要问预测年。\n"
        f"第一次只问券商怎么拆近三年主营收入。例如：{example}"
        "看过 hits 后，若拆分表还不完整，用券商已经写出的行名补搜；"
        "不要自己猜年报大类下面是什么。这一拍不查某条产品的出货够不够。"
    )


def _evidence_search_instructions(ctx: RunContext[SplitAgentDeps]) -> str:
    deps = ctx.deps
    leaves = "、".join(_leaf_names(deps.draft, deps.official_names)) or "（无更细叶子）"
    example = f"{deps.company} {leaves} 近三年收入、出货或单价。"
    return (
        f"你为{deps.label}按已确认口径查数。不要再改名单、不要发明新产品。\n"
        f"已确认叶子：{leaves}。\n"
        f"还可检索 {deps.searches_left} 次。问句写券商，点公司名和已确认产品名。不要问预测年。\n"
        f"例如：{example}"
        "缺哪一行的收入或出货，就补搜那一行。"
    )


def _caliber_instructions(ctx: RunContext[SplitAgentDeps]) -> str:
    deps = ctx.deps
    official, rows, _example = _official_lines(deps)
    return (
        f"你是{deps.label}的口径确认。只锁定名单，不选方法，不填数字。\n"
        f"年报父口径只用来回归：{official}。往年改名不要单独成行。{rows or ''}\n"
        f"{CALIBER_RULES}"
    )


def _plan_instructions(ctx: RunContext[SplitAgentDeps]) -> str:
    deps = ctx.deps
    official, rows, _example = _official_lines(deps)
    leaves = "、".join(_leaf_names(deps.draft, deps.official_names)) or "（停在年报）"
    return (
        f"你是{deps.label}的拆分收口。口径已确认，按查数结果决定停在年报还是留下更细行，不选方法，不填数字。\n"
        f"年报父口径：{official}。{rows or ''}\n"
        f"已确认叶子：{leaves}。只能留下这些行，或把过细的并进该父口径其余部分，不要手写第二个「其他」，不能新增口径外的产品。"
    )


caliber_search_agent = Agent(
    output_type=SearchTurn,
    deps_type=SplitAgentDeps,
    retries=0,
    instructions=_caliber_search_instructions,
)

evidence_search_agent = Agent(
    output_type=SearchTurn,
    deps_type=SplitAgentDeps,
    retries=0,
    instructions=_evidence_search_instructions,
)

caliber_draft_agent = Agent(
    output_type=CaliberDraft,
    deps_type=SplitAgentDeps,
    retries=0,
    instructions=_caliber_instructions,
)

split_agent = Agent(
    output_type=SplitPlan,
    deps_type=SplitAgentDeps,
    retries=0,
    instructions=_plan_instructions,
)


def _assign_parents(rows: list, official: list[str]) -> None:
    parent_set = set(official)
    for item in rows:
        if item.parent:
            continue
        if item.name in parent_set:
            item.parent = item.name
        elif len(official) == 1:
            item.parent = official[0]


def _row_is_residual(item, official: list[str]) -> bool:
    return is_residual_name(item.name, parent=item.parent or "", official_parents=official)


def _has_parent_residual(rows, parent: str, official: list[str]) -> bool:
    for item in rows:
        if (item.parent or "") != parent:
            continue
        if is_residual_name(item.name, parent=parent, official_parents=official):
            return True
    return False


def _has_company_residual(rows, official: list[str]) -> bool:
    for item in rows:
        if item.parent:
            continue
        if is_residual_name(item.name, parent="", official_parents=official):
            return True
    return False


def _apply_caliber_names(output: CaliberDraft) -> CaliberDraft:
    canon = normalize_caliber(output.model_dump())
    output.caliber = canon["caliber"]
    output.official_parents = list(canon["official_parents"])
    output.segments = [CaliberRow(name=item["name"], parent=item["parent"]) for item in canon["segments"]]
    return output


def _apply_plan_names(output: SplitPlan) -> SplitPlan:
    canon = normalize_split_plan(output.model_dump())
    output.caliber = canon["caliber"]
    output.official_parents = list(canon["official_parents"])
    output.segments = [
        SegmentPick(name=item["name"], parent=item["parent"])
        for item in canon["segments"]
    ]
    return output


def _fill_caliber(deps: SplitAgentDeps, output: CaliberDraft) -> CaliberDraft:
    output.official_parents = list(deps.official_names)
    parents = list(output.official_parents)
    _assign_parents(output.segments, parents)
    by_parent: dict[str, list[CaliberRow]] = {}
    for item in output.segments:
        by_parent.setdefault(item.parent or item.name, []).append(item)

    filled = list(output.segments)
    drilled = set()
    for parent in parents:
        items = by_parent.get(parent) or []
        children = [item for item in items if item.name != parent and not _row_is_residual(item, parents)]
        if not children:
            if not any(item.name == parent for item in items):
                filled.append(CaliberRow(name=parent, parent=parent))
            continue
        drilled.add(parent)
        if not _has_parent_residual(filled, parent, parents):
            filled.append(CaliberRow(name=parent_residual_name(parent), parent=parent))

    kept: list[CaliberRow] = []
    for item in filled:
        if item.name == item.parent and item.parent in drilled:
            continue
        kept.append(item)
    if not _has_company_residual(kept, parents):
        kept.append(CaliberRow(name="其他", parent=""))
    output.segments = kept
    output.caliber = "drilled" if drilled else "official"
    return _apply_caliber_names(output)


def _fill_plan(deps: SplitAgentDeps, output: SplitPlan) -> SplitPlan:
    output.official_parents = list(deps.official_names)
    parents = list(output.official_parents)
    _assign_parents(output.segments, parents)

    by_parent: dict[str, list[SegmentPick]] = {}
    for item in output.segments:
        by_parent.setdefault(item.parent or item.name, []).append(item)

    filled = list(output.segments)
    drilled = set()
    for parent in parents:
        items = by_parent.get(parent) or []
        children = [item for item in items if item.name != parent and not _row_is_residual(item, parents)]
        if not children:
            filled = [
                item
                for item in filled
                if not (item.parent == parent and _row_is_residual(item, parents))
            ]
            if not any(item.name == parent and item.parent == parent for item in filled):
                filled.append(SegmentPick(name=parent, parent=parent))
            continue
        drilled.add(parent)
        if not _has_parent_residual(filled, parent, parents):
            filled.append(SegmentPick(name=parent_residual_name(parent), parent=parent))

    kept: list[SegmentPick] = []
    for item in filled:
        if item.name == item.parent and item.parent in drilled:
            continue
        kept.append(item)
    if not _has_company_residual(kept, parents):
        kept.append(SegmentPick(name="其他", parent=""))
    output.segments = kept
    output.caliber = "drilled" if drilled else "official"
    return _apply_plan_names(output)


def _invented_names(deps: SplitAgentDeps, names: list[str]) -> list[str]:
    official = list(deps.official_names)
    return [
        name
        for name in names
        if name
        and not is_residual_name(name, official_parents=official)
        and name not in official
        and not _name_in_hits(name, deps.hits)
    ]


def _caliber_errors(deps: SplitAgentDeps, output: CaliberDraft) -> list[str]:
    if not deps.stage_queries and not any(item.get("intent") == "caliber" for item in deps.queries):
        return ["还没有检索"]
    names = [item.name for item in output.segments]
    errors: list[str] = []
    invented = _invented_names(deps, names)
    if invented:
        errors.append("这些名称无法回指切片或官方表：" + "、".join(invented))
    try:
        require_caliber(output.model_dump(), hits=deps.hits)
    except SchemaError as exc:
        errors.extend(exc.errors)
    return errors


def _plan_errors(deps: SplitAgentDeps, output: SplitPlan) -> list[str]:
    if not deps.queries:
        return ["还没有检索"]
    names = [item.name for item in output.segments]
    errors: list[str] = []
    invented = _invented_names(deps, names)
    if invented:
        errors.append("这些名称无法回指切片或官方表：" + "、".join(invented))
    if deps.draft:
        allowed = {item.name for item in deps.draft.segments} | set(RESIDUAL_NAMES) | set(deps.official_names)
        allowed.update(parent_residual_name(name) for name in deps.official_names if name)
        extra = [name for name in names if name not in allowed]
        if extra:
            errors.append("收口不得新增口径外的行：" + "、".join(extra))
    try:
        require_split_plan(output.model_dump(), hits=deps.hits)
    except SchemaError as exc:
        errors.extend(exc.errors)
    return errors


def _name_in_hits(name: str, hits: list[dict[str, Any]]) -> bool:
    needle = name.strip()
    if not needle:
        return False
    for hit in hits:
        blob = f"{hit.get('title') or ''} {hit.get('snippet') or ''}"
        if needle in blob:
            return True
        if needle in (hit.get("specs") or []):
            return True
    return False


def _dedupe_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for hit in hits:
        key = hit.get("chunk_id") or hit.get("doc_id") or (
            hit.get("title"),
            hit.get("date"),
            str(hit.get("snippet") or "")[:80],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def _rank_hits(hits: list[dict[str, Any]], names: list[str] | None = None) -> list[dict[str, Any]]:
    names = names or []

    def key(hit: dict[str, Any]) -> tuple[int, ...]:
        text = f"{hit.get('title') or ''} {hit.get('snippet') or ''}"
        named = 1 if any(name and name in text for name in names) else 0
        income = 1 if _LINE_INCOME.search(text) or _SPEC_SPLIT.search(text) else 0
        section = 1 if any(token in text for token in ("分业务", "分产品", "分部", "业务线", "按传输速率")) else 0
        disclosure = 1 if any(token in text for token in _DISCLOSURE) else 0
        pnl = 1 if sum(token in text for token in ("销售费用", "管理费用", "研发费用")) >= 2 else 0
        return (named, income, section, -disclosure, -pnl)

    return sorted(hits, key=key, reverse=True)


_LINE_INCOME = re.compile(r".{0,20}(?:业务|设备|产品|器件|板块).{0,12}(?:营业收入|营收|收入)")
_SPEC_SPLIT = re.compile(r".{0,8}(?:\d+(?:\.\d+)?(?:G|T)).{0,20}(?:收入|营收|出货|单价|ASP)", re.I)


def _peek_text(text: str, width: int = 280) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for token in ("分业务", "分产品"):
        at = cleaned.find(token)
        if at >= 0:
            return cleaned[max(0, at - 24) : at - 24 + width]
    match = _LINE_INCOME.search(cleaned)
    if match:
        return cleaned[match.start() : match.start() + width]
    for token in _PEEK_HINTS:
        at = cleaned.find(token)
        if at >= 0:
            start = max(0, at - 48)
            return cleaned[start : start + width]
    return cleaned[:width]


def _compact_hits(
    hits: list[dict[str, Any]],
    limit: int = 8,
    names: list[str] | None = None,
) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for hit in _rank_hits(_dedupe_hits(hits), names)[:limit]:
        text = _peek_text(str(hit.get("snippet") or ""))
        compact.append(
            {
                "title": hit.get("title"),
                "institution": hit.get("institution"),
                "date": hit.get("date"),
                "has_table": hit.get("has_table"),
                "specs": hit.get("specs") or [],
                "traces": hit_traces(str(hit.get("snippet") or "")),
                "peek": text,
            }
        )
    return compact


def _log(deps: SplitAgentDeps, text: str) -> None:
    if deps.verbose:
        print(text, flush=True)


def run_revenue_split(deps: SplitAgentDeps, *, model=None) -> SplitPlan:
    """先确认口径，再按确认名单查数、收口。不选预测方法。"""
    from valuation.shared.env import build_chat_model

    chat = model or build_chat_model()
    _log(deps, "[loop] 第1拍：确认券商口径")
    deps.stage = "caliber"
    while deps.searches_left > 0:
        turn = _run_search_turn(deps, chat)
        if turn.stop:
            _log(deps, f"[loop] 停搜：{turn.why or '口径已够'}")
            break
        execute_search(deps, turn.query, turn.allow_image)
    if not any(item.get("intent") == "caliber" for item in deps.queries):
        raise RuntimeError("拆分 loop 没有检索")
    deps.draft = compile_caliber(deps, model=chat)
    _log(deps, "[loop] 已确认口径：" + "、".join(item.name for item in deps.draft.segments))

    _log(deps, "[loop] 第2拍：按确认口径查数")
    deps.stage = "evidence"
    if _leaf_names(deps.draft, deps.official_names):
        while deps.searches_left > 0:
            turn = _run_search_turn(deps, chat)
            if turn.stop:
                _log(deps, f"[loop] 停搜：{turn.why or '查数已够'}")
                break
            execute_search(deps, turn.query, turn.allow_image)
    else:
        _log(deps, "[loop] 停在年报父口径，不再查更细产品的数")
    return compile_split_plan(deps, model=chat)


def compile_caliber(deps: SplitAgentDeps, *, model=None) -> CaliberDraft:
    from valuation.shared.env import build_chat_model

    if not any(item.get("intent") == "caliber" for item in deps.queries):
        raise RuntimeError("compile_caliber 需要已有检索")
    chat = model or build_chat_model()
    hint = ""
    last_errors: list[str] = []
    for attempt in range(1, 4):
        user_msg = _caliber_user(deps, hint)
        _log(deps, f"[loop] 写口径 第{attempt}次新会话")
        result = caliber_draft_agent.run_sync(user_msg, deps=deps, model=chat)
        _log_messages(deps, result.all_messages())
        draft = _fill_caliber(deps, result.output)
        last_errors = _caliber_errors(deps, draft)
        if not last_errors:
            return draft
        _log(deps, "[loop] 口径不合法：" + "；".join(last_errors))
        hint = "口径不合法：" + "；".join(last_errors)
    raise SchemaError(last_errors or ["写口径失败"])


def compile_split_plan(deps: SplitAgentDeps, *, model=None) -> SplitPlan:
    """按已确认口径和查数结果收口。不带上一轮 Responses item。"""
    from valuation.shared.env import build_chat_model

    if not deps.queries:
        raise RuntimeError("compile_split_plan 需要已有检索")
    chat = model or build_chat_model()
    hint = ""
    last_errors: list[str] = []
    for attempt in range(1, 4):
        user_msg = _plan_user(deps, hint)
        _log(deps, f"[loop] 写卡 第{attempt}次新会话")
        result = split_agent.run_sync(user_msg, deps=deps, model=chat)
        _log_messages(deps, result.all_messages())
        plan = _fill_plan(deps, result.output)
        last_errors = _plan_errors(deps, plan)
        if not last_errors:
            return plan
        _log(deps, "[loop] 拆分卡不合法：" + "；".join(last_errors))
        hint = "拆分卡不合法：" + "；".join(last_errors)
    raise SchemaError(last_errors or ["写拆分卡失败"])


def _run_search_turn(deps: SplitAgentDeps, model) -> SearchTurn:
    agent = caliber_search_agent if deps.stage == "caliber" else evidence_search_agent
    hint = ""
    last_errors: list[str] = []
    for attempt in range(1, 4):
        user_msg = _search_turn_user(deps, hint)
        _log(deps, f"[loop] 写问句  {deps.stage} 第{len(deps.stage_queries) + 1}拍 尝试{attempt}")
        result = agent.run_sync(user_msg, deps=deps, model=model)
        _log_messages(deps, result.all_messages())
        turn = result.output
        if turn.stop:
            if deps.stage_queries or (deps.stage == "evidence" and deps.draft):
                return turn
            last_errors = ["还没有检索，不能停"]
            hint = last_errors[0] + "。先写一条问句。"
            continue
        last_errors = query_errors(turn.query, deps)
        if last_errors:
            hint = "问句不合规：" + "；".join(last_errors)
            _log(deps, "[loop] " + hint)
            continue
        return turn
    raise RuntimeError("写检索问句失败：" + "；".join(last_errors))


def _search_turn_user(deps: SplitAgentDeps, hint: str) -> str:
    asked = "；".join(item.get("query") or "" for item in deps.stage_queries) or "还没有检索"
    hits = json.dumps(_compact_hits(deps.hits, limit=10, names=_rank_names(deps)), ensure_ascii=False)
    text = (
        f"已问过：{asked}\n"
        f"还可检索 {deps.searches_left} 次。\n"
        f"hits={hits}\n"
    )
    if deps.stage == "caliber":
        text += "先写一条问券商怎么拆主营收入的中文问句。" if not deps.stage_queries else "要补搜就写下一句；够了就把 stop 设为 true。"
    else:
        leaves = "、".join(_leaf_names(deps.draft, deps.official_names))
        text += f"已确认口径：{leaves}。按这些名字查收入、出货或单价；够了就把 stop 设为 true。"
    if hint:
        text += "\n" + hint
    return text


def _caliber_user(deps: SplitAgentDeps, hint: str) -> str:
    hits = json.dumps(_compact_hits(deps.hits, limit=18), ensure_ascii=False)
    text = (
        "检索已完成，不要再检索。照搬切片里的券商拆法，同意的合并，再挂回年报父口径。"
        "不要选方法，不要填数字。\n"
        f"年报父口径（回归用）：{'、'.join(deps.official_names) or '无'}\n"
        f"hits={hits}\n"
    )
    if hint:
        text += hint
    return text


def _plan_user(deps: SplitAgentDeps, hint: str) -> str:
    hits = json.dumps(
        _compact_hits(deps.hits, limit=18, names=_leaf_names(deps.draft, deps.official_names)),
        ensure_ascii=False,
    )
    traces = []
    for name in _leaf_names(deps.draft, deps.official_names):
        found = line_traces(name, deps.hits)
        traces.append(f"{name}：{'、'.join(found) or '几乎没有可跟的数'}")
    text = (
        "口径已确认。过细、几乎没有可跟的数的行并进其他。不要选方法，不要填数字，不要新增产品。\n"
        f"年报父口径（回归用）：{'、'.join(deps.official_names) or '无'}\n"
        f"已确认口径：{'、'.join(item.name for item in (deps.draft.segments if deps.draft else [])) or '无'}\n"
    )
    if traces:
        text += "各行痕迹：" + "；".join(traces) + "。\n"
    text += f"hits={hits}\n"
    if hint:
        text += hint
    return text


def _log_messages(deps: SplitAgentDeps, messages) -> None:
    _log(deps, "[agent] 模型回合：")
    for msg in messages:
        kind = type(msg).__name__
        parts = getattr(msg, "parts", None) or []
        if not parts:
            _log(deps, f"  {kind}")
            continue
        for part in parts:
            name = type(part).__name__
            if hasattr(part, "tool_name"):
                args = getattr(part, "args", None) or getattr(part, "content", None)
                _log(deps, f"  {kind}.{name}  tool={part.tool_name}  args={args}")
            else:
                content = str(getattr(part, "content", "") or "")
                if content:
                    _log(deps, f"  {kind}.{name}: {content[:500]}")


def main(argv: list[str] | None = None) -> int:
    """单独测拆分 agent，不跑完整流水。"""
    import argparse

    from valuation.shared.env import describe
    from valuation.segment_split.collect import run_split_smoke

    parser = argparse.ArgumentParser(description="单独测试收入拆分")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--name", required=True, help="公司名，如 中际旭创")
    parser.add_argument("--out-dir", default="", help="默认写到 output/smoke/<ticker>")
    args = parser.parse_args(argv)
    print(describe())
    pack = run_split_smoke(args.name, args.ticker, out_dir=Path(args.out_dir) if args.out_dir else None)
    draft = pack.get("split_caliber") or {}
    if draft:
        print("caliber_draft=" + "、".join(item.get("name") or "" for item in draft.get("segments") or []))
    plan = pack.get("split_plan") or {}
    print(f"caliber={plan.get('caliber')}")
    for item in plan.get("segments") or []:
        print(f"  {item.get('name')}  parent={item.get('parent')}")
    print(f"queries={len(pack.get('vector_queries') or [])}")
    for query in pack.get("vector_queries") or []:
        print(f"  {query.get('id')}[{query.get('intent')}]: {query.get('query')}")
    print(f"plan: {pack.get('plan_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
