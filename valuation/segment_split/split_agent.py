"""收入拆分 agent：确认券商口径后直接锁定名单，历史金额由后续回填。"""

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
    hit_traces,
    is_residual_name,
    normalize_split_plan,
    require_split_plan,
)
from valuation.segment_split.schema import SchemaError

SearchFn = Callable[[str, bool, str], list[dict[str, Any]]]

MAX_CALIBER_SEARCHES = 3
_FORECAST_HINT = re.compile(r"展望|盈利预测|利润表|损益表|我们预计")
_PEEK_HINTS = ("分业务", "分产品", "营业收入", "营收", "亿元", "出货")
_DISCLOSURE = (
    "IMPORTANT DISCLOSURES",
    "conflict of interest",
    "52周收益率",
    "分析师：",
    "Rating and Target Price",
)


class SegmentPick(BaseModel):
    name: str = Field(description="券商拆分表里的一行，或年报父口径本身")
    parent: str = Field(default="", description="挂回的年报父口径")
    source_terms: list[str] = Field(
        default_factory=list,
        description="若合并多家券商近似拆法，填入切片原文出现过的原词",
    )


class SplitPlan(BaseModel):
    """拆分卡。不含历史金额或预测数字。"""

    caliber: Literal["official", "drilled"] = Field(
        description="停在年报口径，还是已用券商更细口径"
    )
    official_parents: list[str] = Field(default_factory=list, description="年报父口径")
    segments: list[SegmentPick] = Field(default_factory=list, description="确认后的拆分行")


# 保留别名以兼容旧导入
CaliberRow = SegmentPick
CaliberDraft = SplitPlan


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
    verbose: bool = True
    stage: Literal["caliber"] = "caliber"
    draft: CaliberDraft | None = None
    hits: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    search_seq: int = 0

    @property
    def stage_queries(self) -> list[dict[str, Any]]:
        return [item for item in self.queries if item.get("intent") == self.stage]

    @property
    def searches_left(self) -> int:
        return max(0, self.max_caliber_searches - len(self.stage_queries))

    @property
    def label(self) -> str:
        if self.company and self.ticker:
            return f"{self.company}（{self.ticker}）"
        return self.company or self.ticker


def query_errors(query: str, deps: SplitAgentDeps) -> list[str]:
    text = str(query or "").strip()
    errors: list[str] = []
    if not text:
        errors.append("问句不能为空")
        return errors
    if deps.company not in text and deps.ticker not in text:
        errors.append(f"问句须包含公司名或代码（{deps.label}）")
    if "父口径" in text:
        errors.append("问句不要写内部术语「父口径」")
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
    for i, hit in enumerate(_compact_hits(hits, limit=6), 1):
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
        "hits": _compact_hits(hits),
    }


def _official_lines(deps: SplitAgentDeps) -> tuple[str, str, str]:
    official = "、".join(deps.official_names) or "（无官方产品名）"
    rows = "；".join(
        f"{row.get('period')} {row.get('name')} {row.get('revenue_yi')}"
        for row in deps.official_rows
        if not row.get("is_total")
    )
    example = f"{deps.company} 主营业务收入拆分 分产品 分业务 营业收入构成"
    return official, rows, example


def _caliber_search_instructions(ctx: RunContext[SplitAgentDeps]) -> str:
    deps = ctx.deps
    official, rows, example = _official_lines(deps)
    return (
        f"你为{deps.label}写下一句检索问句，用来确认研报如何拆分主营收入，或决定停搜。\n"
        f"年报父口径只做回归，问句点公司名：{official}。往年改名不是并列分部。{rows or ''}\n"
        f"还可检索 {deps.searches_left} 次。\n"
        f"第一次检索公司主营业务或分产品收入拆分表。例如：{example}。"
        "看过 hits 后，若拆分表还不完整，用研报已经写出的细分业务名补搜。"
    )


def _caliber_instructions(ctx: RunContext[SplitAgentDeps]) -> str:
    deps = ctx.deps
    official, rows, _example = _official_lines(deps)
    return (
        f"你是{deps.label}的口径确认。这一拍只锁名单。\n"
        f"年报父口径只用来回归：{official}。往年改名不是并列分部。{rows or ''}\n"
        f"{CALIBER_RULES}"
    )


caliber_search_agent = Agent(
    output_type=SearchTurn,
    deps_type=SplitAgentDeps,
    retries=0,
    instructions=_caliber_search_instructions,
)

caliber_draft_agent = Agent(
    output_type=CaliberDraft,
    deps_type=SplitAgentDeps,
    retries=0,
    instructions=_caliber_instructions,
)


def _assign_parents(rows: list, official: list[str]) -> None:
    parent_set = set(official)
    for item in rows:
        if item.parent:
            continue
        if is_residual_name(item.name, parent="", official_parents=official):
            continue
        if item.name in parent_set:
            item.parent = item.name
        elif len(official) == 1:
            item.parent = official[0]


def _row_is_residual(item, official: list[str]) -> bool:
    return is_residual_name(item.name, parent=item.parent or "", official_parents=official)


def _has_company_residual(rows, official: list[str]) -> bool:
    for item in rows:
        if item.parent:
            continue
        if is_residual_name(item.name, parent="", official_parents=official):
            return True
    return False


def _apply_caliber_names(output: SplitPlan) -> SplitPlan:
    canon = normalize_split_plan(output.model_dump())
    output.caliber = canon["caliber"]
    output.official_parents = list(canon["official_parents"])
    output.segments = [
        SegmentPick(
            name=item["name"],
            parent=item["parent"],
            source_terms=list(item.get("source_terms") or []),
        )
        for item in canon["segments"]
    ]
    return output


def _fill_caliber(deps: SplitAgentDeps, output: SplitPlan) -> SplitPlan:
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
            if not any(item.name == parent for item in items):
                filled.append(SegmentPick(name=parent, parent=parent))
            continue
        drilled.add(parent)

    kept: list[SegmentPick] = []
    for item in filled:
        if item.name == item.parent and item.parent in drilled:
            continue
        kept.append(item)
    if not _has_company_residual(kept, parents):
        kept.append(SegmentPick(name="其他", parent=""))
    output.segments = kept
    output.caliber = "drilled" if drilled else "official"
    return _apply_caliber_names(output)


def _invented_names(deps: SplitAgentDeps, rows: list[SegmentPick]) -> list[str]:
    official = list(deps.official_names)
    hist_official = {
        str(row.get("name") or "").strip()
        for row in deps.official_rows
        if str(row.get("name") or "").strip() and not row.get("is_total")
    }
    return [
        item.name
        for item in rows
        if item.name
        and not is_residual_name(item.name, official_parents=official)
        and item.name not in official
        and item.name not in hist_official
        and not _name_in_hits(item.name, deps.hits, source_terms=item.source_terms)
    ]


def _caliber_errors(deps: SplitAgentDeps, output: SplitPlan) -> list[str]:
    if not deps.stage_queries and not any(item.get("intent") == "caliber" for item in deps.queries):
        return ["还没有检索"]
    errors: list[str] = []
    invented = _invented_names(deps, output.segments)
    if invented:
        errors.append("这些名称无法回指切片或官方表：" + "、".join(invented))
    try:
        require_split_plan(output.model_dump(), hits=deps.hits, official_rows=deps.official_rows)
    except SchemaError as exc:
        errors.extend(exc.errors)
    return errors


def _name_in_hits(
    name: str,
    hits: list[dict[str, Any]],
    *,
    source_terms: list[str] | None = None,
) -> bool:
    candidates = [name.strip(), *(str(t).strip() for t in (source_terms or []) if str(t).strip())]
    candidates = [c for c in candidates if c]
    if not candidates:
        return False
    for hit in hits:
        blob = f"{hit.get('title') or ''} {hit.get('snippet') or ''}"
        specs = hit.get("specs") or []
        if any(needle in blob or needle in specs for needle in candidates):
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


def _peek_text(text: str, width: int = 1200) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for token in ("分业务", "分产品"):
        at = cleaned.find(token)
        if at >= 0:
            return cleaned[max(0, at - 48) : at - 48 + width]
    match = _LINE_INCOME.search(cleaned)
    if match:
        start = max(0, match.start() - 48)
        return cleaned[start : start + width]
    for token in _PEEK_HINTS:
        at = cleaned.find(token)
        if at >= 0:
            start = max(0, at - 64)
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
    """检索并确认一次业务名单，直接写入拆分计划，不再按取数情况删行。"""
    from valuation.shared.env import build_chat_model

    chat = model or build_chat_model()
    _log(deps, "[loop] 确认券商口径")
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
    _log(deps, "[loop] 已锁定口径：" + "、".join(item.name for item in deps.draft.segments))
    return compile_split_plan(deps)


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
        prev_json = json.dumps(draft.model_dump(), ensure_ascii=False)
        hint = f"上次草稿：{prev_json}\n口径不合法：{'；'.join(last_errors)}"
    raise SchemaError(last_errors or ["写口径失败"])


def compile_split_plan(deps: SplitAgentDeps) -> SplitPlan:
    """已确认的口径直接成为计划，只做结构及来源校验，不再调用模型。"""
    if deps.draft is None:
        raise RuntimeError("compile_split_plan 需要已确认口径")
    canonical = require_split_plan(
        deps.draft.model_dump(), hits=deps.hits, official_rows=deps.official_rows
    )
    return SplitPlan.model_validate(canonical)


def _run_search_turn(deps: SplitAgentDeps, model) -> SearchTurn:
    hint = ""
    last_errors: list[str] = []
    for attempt in range(1, 4):
        user_msg = _search_turn_user(deps, hint)
        _log(deps, f"[loop] 写问句  {deps.stage} 第{len(deps.stage_queries) + 1}拍 尝试{attempt}")
        result = caliber_search_agent.run_sync(user_msg, deps=deps, model=model)
        _log_messages(deps, result.all_messages())
        turn = result.output
        if turn.stop:
            if deps.stage_queries:
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
    hits = json.dumps(_compact_hits(deps.hits, limit=10), ensure_ascii=False)
    text = (
        f"已问过：{asked}\n"
        f"还可检索 {deps.searches_left} 次。\n"
        f"hits={hits}\n"
    )
    text += "先写一条问券商怎么拆主营收入的中文问句。" if not deps.stage_queries else "要补搜就写下一句；够了就把 stop 设为 true。"
    if hint:
        text += "\n" + hint
    return text


def _caliber_user(deps: SplitAgentDeps, hint: str) -> str:
    hits = json.dumps(_compact_hits(deps.hits, limit=18), ensure_ascii=False)
    _official, rows, _example = _official_lines(deps)
    text = (
        "检索已完成。照搬切片里的券商拆法，同意的合并，再挂回年报父口径。这一拍只锁名单。\n"
        f"最新年报父口径（回归用）：{'、'.join(deps.official_names) or '无'}\n"
        f"近三年年报主营构成明细：{rows or '无'}\n"
        f"hits={hits}\n"
    )
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
