"""brief_agent：检索并整理本分部有用信息。不拍我们的预测。"""

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
from valuation.segment_split.collect import _mcp_typed_search
from valuation.segment_split.split_agent import _log, _log_messages
from valuation.shared.env import build_chat_model
from valuation.shared.io import dump_json, load_json, logs_dir, spec_dir

CONTENT_TYPES = (
    "domestic_report",
    "foreign_report",
    "minutes",
    "comment",
)
CONTENT_LABEL = {
    "domestic_report": "内资研报",
    "foreign_report": "外资研报",
    "minutes": "纪要",
    "comment": "点评",
}
MAX_SEARCHES = 12
MAX_READS = 8
PEEK = 800
TypedSearchFn = Callable[[str, bool, str, str], list[dict[str, Any]]]
_LOG_TYPE = {
    "1": "domestic_report",
    "2": "foreign_report",
    "3": "minutes",
    "4": "comment",
}
_SOURCE_TYPE = {
    "innerReportResource": "domestic_report",
    "foreignReportResource": "foreign_report",
    "summaryResource": "minutes",
    "commentResource": "comment",
}

SourceType = Literal["domestic_report", "foreign_report", "minutes", "comment", "notes"]
ContentType = Literal["domestic_report", "foreign_report", "minutes", "comment"]


class FactRow(BaseModel):
    """已经发生的事。只放已实现期。"""

    period: str = Field("", description="已实现期，如 2023A、1H26")
    metric: str = Field("", description="收入、出货、单价、占比等")
    value: str = Field("", description="数字或区间，尽量带单位")
    yoy: str = Field("", description="原文给出的同比，如 -2.2%；没有则留空，不要推造")
    what: str = Field("", description="这条在说什么")
    house: str = Field("", description="谁披露或转述，有就写")
    as_of: str = Field("", description="资料日期，有就写")
    source_title: str = Field("", description="来源标题，有就写")
    source_ref: str = Field("", description="检索结果 ref，有就写，便于回看原片段")
    source_type: SourceType = "notes"


class SellsideCall(BaseModel):
    """某一家机构的预测，尽量点名。"""

    house: str = Field("", description="机构名，如 中金、国海、高盛")
    as_of: str = Field("", description="报告日期，有就写")
    horizon: str = Field("", description="预测针对哪一年，如 2026E")
    metric: str = Field("", description="收入增速、出货、ASP 等")
    value: str = Field("", description="该机构对本分部给出的具体数字或区间；没有则不要放入 sellside")
    view: str = Field("", description="该机构的核心假设，一两句")
    source_title: str = Field("", description="来源标题，有就写")
    source_ref: str = Field("", description="检索结果 ref，有就写")
    source_type: SourceType = "domestic_report"


class TimelineEvent(BaseModel):
    """会改量价或增速的事件。"""

    date: str = Field("", description="事件发生日或首次披露日")
    event: str = Field("", description="发生了什么")
    why_it_matters: str = Field("", description="对后续量价或增速的边际")
    house: str = Field("", description="谁说的或谁披露的，有就写")
    source_title: str = Field("", description="来源标题，有就写")
    source_ref: str = Field("", description="检索结果 ref，有就写")
    source_type: SourceType = "comment"


class ResearchBrief(BaseModel):
    """研究笔记。已实现、各家预测、事件分开写。只整理材料，增速留给下一步。"""

    facts: list[FactRow] = Field(default_factory=list)
    sellside: list[SellsideCall] = Field(default_factory=list)
    event_timeline: list[TimelineEvent] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list, description="筛选后的关键发现及为何重要，简短写")
    excluded: list[str] = Field(default_factory=list, description="容易误用、过旧或口径不合的材料及排除理由")
    gaps: list[str] = Field(default_factory=list)


@dataclass
class BriefDeps:
    company: str
    ticker: str
    segment: str
    hist_periods: list[str]
    forecast_periods: list[str]
    notes_revenue: dict[str, float]
    search: TypedSearchFn
    as_of: str = ""
    sibling_names: list[str] = field(default_factory=list)
    seed_hits: list[dict[str, Any]] = field(default_factory=list)
    max_searches: int = MAX_SEARCHES
    max_reads: int = MAX_READS
    verbose: bool = True
    hits: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    reads: list[dict[str, Any]] = field(default_factory=list)
    search_seq: int = 0
    read_seq: int = 0
    seen_hits: set[str] = field(default_factory=set)

    @property
    def searches_left(self) -> int:
        return max(0, self.max_searches - self.search_seq)

    @property
    def reads_left(self) -> int:
        return max(0, self.max_reads - self.read_seq)

    @property
    def label(self) -> str:
        if self.company and self.ticker:
            return f"{self.company}（{self.ticker}）/{self.segment}"
        return f"{self.company or self.ticker}/{self.segment}"


def _brief_instructions(ctx: RunContext[BriefDeps]) -> str:
    deps = ctx.deps
    rev = "；".join(f"{year}={amount}" for year, amount in deps.notes_revenue.items()) or "无"
    others = "、".join(deps.sibling_names) or "无"
    return (
        f"你在为{deps.label}做分部研究，资料截止日为{deps.as_of or '本次建模日'}。"
        "你的工作是多角度检索、辨别和筛选，预测数字留给下一步分析师。\n"
        "分轮检索：第一轮最多发两条查询，分别寻找最近已实现的分部数据和分部预测；"
        "看到结果后再判断缺什么、哪里冲突，下一轮才补搜纪要、点评或外资材料。"
        "每轮最多两条 search_research，不要在首轮一次发完四类查询。"
        "每次查询聚焦一个问题，变化关键词、时间和资料类型；结果重复或不对口径时及时换问法。"
        "可以查盈利预测表里的分部出货和单价，也要留意最新季度/半年实际。\n"
        f"还可检索 {deps.searches_left} 次、展开阅读 {deps.reads_left} 条命中。"
        "通常需要约6-10次不同角度的检索。若只有公司总量、还没有本分部的数值预测，"
        "至少再用‘分业务收入/盈利预测表/Smart Hardware revenue’等不同词组专项补搜；"
        "较旧的分部预测可以保留作情景参照，并用最新实际检验。"
        "优先展开可能影响判断的原片段；确认近期实际、主要分部预测及反方材料后再停。\n"
        f"已锁定历史分部收入（亿元，请写进已实现）：{rev}。\n"
        f"公司还有这些分部，写到它们时说清不是「{deps.segment}」：{others}。\n"
        "筛选时优先看是否真是本分部、指标和单位是否同口径、资料日期是否仍适用。"
        "行业总量、公司总量、其他分部和旧预测可以留下作背景，但不要冒充本分部的当前证据。"
        "同一指标有不同说法时保留主要分歧并说明取舍，不要重复罗列近似摘录。"
        "关键数字、份额或争议观点如果要进入 key_findings，先用 read_research_hit 展开原片段核对上下文。\n"
        "facts 只放已实现；sellside 只放点名机构对本分部给出明确数值的预测，"
        "无数值的机构观点和行业总量要另作背景；"
        "event_timeline 按事件发生日整理。"
        "季度、半年收入如有同比，要在对应 facts 的 yoy 字段保留，方便下一步推算剩余期间。"
        "key_findings 用少量句子告诉下一步分析师哪些材料最有用、为什么；"
        "excluded 写最容易误用的材料及原因；gaps 写仍缺什么。"
    )


brief_agent = Agent(
    output_type=ResearchBrief,
    deps_type=BriefDeps,
    retries=0,
    instructions=_brief_instructions,
)


@brief_agent.tool
def search_research(
    ctx: RunContext[BriefDeps],
    query: str,
    content_type: ContentType,
    allow_image: bool = False,
) -> str:
    """检索一类资料。问句尽量含公司名和分部，按缺口选类型。"""
    deps = ctx.deps
    if deps.searches_left <= 0:
        return "检索次数已用完，根据已有材料整理笔记。"
    deps.search_seq += 1
    query_id = f"brief_{deps.segment}_{deps.search_seq}"
    _log(deps, f"[brief] {CONTENT_LABEL[content_type]} {query}")
    hits = deps.search(query, allow_image, query_id, content_type)
    novel = []
    for i, hit in enumerate(hits, 1):
        hit["content_type"] = content_type
        hit["ref"] = f"{query_id}:{i}"
        key = _hit_key(hit)
        if key not in deps.seen_hits:
            deps.seen_hits.add(key)
            novel.append(hit)
    deps.queries.append(
        {
            "id": query_id,
            "content_type": content_type,
            "query": query,
            "allow_image": allow_image,
            "hits": len(hits),
            "new_hits": len(novel),
        }
    )
    deps.hits.extend(hits)
    view = _brief_hits_view(novel, deps.segment, company=deps.company)
    _log(deps, f"[brief] 回来 {len(hits)} 条，新材料 {len(novel)} 条，给模型 {len(view)} 条")
    for i, hit in enumerate(view[:5], 1):
        _log(
            deps,
            f"  hit[{i}] {hit.get('date')} {hit.get('institution')} {hit.get('title')}\n"
            f"         {hit.get('peek')}",
        )
    if not view:
        return "本次没有新增可读片段。请换问题、关键词或资料类型。"
    return json.dumps(view, ensure_ascii=False)


@brief_agent.tool
def read_research_hit(
    ctx: RunContext[BriefDeps], ref: str, focus: str = "", offset: int = 0
) -> str:
    """展开 search_research 的 ref；长片段可按 next_offset 继续读取。"""
    deps = ctx.deps
    if deps.reads_left <= 0:
        return "展开次数已用完，根据已读材料筛选。"
    hit = next((item for item in deps.hits + deps.seed_hits if item.get("ref") == ref), None)
    if hit is None:
        return f"找不到 ref={ref}。请使用搜索结果中的 ref。"
    deps.read_seq += 1
    full_text = str(hit.get("snippet") or "")
    focus_at = full_text.find(focus) if focus else -1
    start = max(0, focus_at - 900) if focus_at >= 0 else max(0, offset)
    end = min(len(full_text), start + 5000)
    deps.reads.append({"ref": ref, "focus": focus, "window_start": start, "window_end": end})
    return json.dumps(
        {
            "ref": ref,
            "title": hit.get("title"),
            "institution": hit.get("institution"),
            "date": hit.get("date"),
            "doc_id": hit.get("doc_id"),
            "chunk_id": hit.get("chunk_id"),
            "pointer": hit.get("pointer"),
            "snippet": full_text[start:end],
            "total_chars": len(full_text),
            "window_start": start,
            "window_end": end,
            "next_offset": end if end < len(full_text) else None,
        },
        ensure_ascii=False,
    )


def run_live_brief(run_dir: Path, segment: str, *, reuse_search: bool = False) -> dict:
    facts, notes_seg = _load_segment(run_dir, segment)
    hist = list(facts["hist_periods"])
    notes_rev = {
        year: float((notes_seg.get("historical_revenue") or {}).get(year))
        for year in hist
        if (notes_seg.get("historical_revenue") or {}).get(year) is not None
    }
    log_root = logs_dir(run_dir) / "mcp"
    log_root.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []
    start = _start_from_hist(hist)
    chat = build_chat_model(api="chat")
    hits_path = log_root / f"research_hits_{segment}.json"
    with ComeinClient() as mcp:
        deps = BriefDeps(
            company=str(facts.get("company") or ""),
            ticker=str(facts.get("ticker") or ""),
            segment=segment,
            hist_periods=hist,
            forecast_periods=list(facts.get("forecast_periods") or []),
            notes_revenue=notes_rev,
            search=_mcp_typed_search(mcp, calls, log_root, start=start),
            as_of=str(facts.get("as_of") or "")[:10],
            sibling_names=_sibling_names(run_dir, segment),
        )
        if reuse_search:
            _seed_previous_hits(deps, log_root, segment)
            _log(deps, f"[brief] 已读上次材料 {len(deps.seed_hits)} 条，仍可再搜")
        draft = run_brief(deps, model=chat)
        dump_json(hits_path, {"queries": deps.queries, "hits": deps.hits, "seed_hits": deps.seed_hits})
    dump_json(log_root / f"research_brief_raw_{segment}.json", draft.model_dump())
    brief = _brief_payload(deps, draft)
    dump_json(logs_dir(run_dir) / f"search_log_research_{segment}.json", calls or deps.queries)
    dump_json(logs_dir(run_dir) / "evidence" / f"research_brief_{segment}.json", brief)
    dump_json(spec_dir(run_dir) / f"research_brief_{segment}.json", brief)
    return brief


def run_brief(deps: BriefDeps, *, model=None) -> ResearchBrief:
    chat = model or build_chat_model(api="chat")
    _log(deps, f"[brief] 研究 {deps.segment}")
    result = brief_agent.run_sync(_brief_user(deps), deps=deps, model=chat)
    _log_messages(deps, result.all_messages())
    return result.output


def _brief_user(deps: BriefDeps) -> str:
    rev = "；".join(f"{year}={amount}" for year, amount in deps.notes_revenue.items()) or "无"
    hist = "、".join(deps.hist_periods)
    fcst = "、".join(deps.forecast_periods)
    text = (
        f"研究「{deps.segment}」。先想清楚哪些材料最能支持下一步预测，再有针对性地搜索和筛选。\n"
        f"历史年：{hist}  预测年：{fcst}\n"
        f"已锁定分部收入（亿元）：{rev}\n"
        "用 search_research 检索，对关键命中可用 read_research_hit 展开。"
        "交付 facts / sellside / event_timeline，以及 key_findings / excluded / gaps。\n"
        "只整理材料，增速留给下一步。\n"
    )
    if deps.seed_hits:
        text += "上次已读材料，可直接用，也可再搜：\n"
        text += json.dumps(_brief_hits_view(deps.seed_hits, deps.segment, company=deps.company, limit=16), ensure_ascii=False)
        text += "\n"
    return text


def _brief_payload(deps: BriefDeps, draft: ResearchBrief) -> dict[str, Any]:
    return {
        "company": deps.company,
        "ticker": deps.ticker,
        "segment": deps.segment,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "notes_revenue": deps.notes_revenue,
        "hist_periods": deps.hist_periods,
        "forecast_periods": deps.forecast_periods,
        "facts": [item.model_dump() for item in draft.facts],
        "sellside": [item.model_dump() for item in draft.sellside],
        "event_timeline": [item.model_dump() for item in draft.event_timeline],
        "key_findings": list(draft.key_findings),
        "excluded": list(draft.excluded),
        "gaps": list(draft.gaps),
        "vector_queries": deps.queries,
        "vector_reads": deps.reads,
    }


def format_brief_for_forecast(brief: dict[str, Any]) -> str:
    """把研究笔记排成给 forecast_agent 读的文本。"""
    lines = ["【筛选后的关键发现】"]
    lines.extend(f"- {item}" for item in brief.get("key_findings") or [])
    lines.append("【已实现事实】")
    facts = brief.get("facts") or []
    if not facts:
        lines.append("（无）")
    for item in facts:
        lines.append(
            f"- {item.get('period')} {item.get('metric')}={item.get('value')} "
            + (f"同比={item.get('yoy')} " if item.get("yoy") else "")
            + f"| {item.get('what')} | 来源={item.get('house') or '—'} {item.get('source_title')}"
            f" {item.get('source_ref') or ''}"
        )
    lines.append("【各家假设】每一条是某一家机构怎么看，不是我们的数")
    sells = brief.get("sellside") or []
    if not sells:
        lines.append("（无）")
    for item in sells:
        lines.append(
            f"- {item.get('house')} {item.get('as_of')} → {item.get('horizon')} "
            f"{item.get('metric')}={item.get('value')} | {item.get('view')} "
            f"| {item.get('source_title')} {item.get('source_ref') or ''}"
        )
    lines.append("【可能发酵的事】")
    events = list(brief.get("event_timeline") or [])
    if not events:
        lines.append("（无）")
    for item in events:
        extra = item.get("why_it_matters") or ""
        lines.append(
            f"- {item.get('date')} {item.get('event')}"
            + (f" | 边际：{extra}" if extra else "")
            + f" | {item.get('house') or ''} {item.get('source_title')} {item.get('source_ref') or ''}"
        )
    excluded = brief.get("excluded") or []
    if excluded:
        lines.append("【已降低权重的材料】" + "；".join(str(item) for item in excluded))
    gaps = brief.get("gaps") or []
    if gaps:
        lines.append("【缺口】" + "；".join(str(item) for item in gaps))
    return "\n".join(lines)


def _brief_hits_view(
    hits: list[dict[str, Any]],
    segment: str,
    company: str = "",
    limit: int = 15,
) -> list[dict[str, Any]]:
    def score(hit: dict[str, Any]) -> tuple:
        text = f"{hit.get('title') or ''} {hit.get('snippet') or ''}"
        named = 1 if segment and segment in text else 0
        company_named = 1 if company and company in text else 0
        has_num = 1 if re.search(r"\d", text) else 0
        inst = 1 if hit.get("institution") else 0
        return (named, company_named, has_num, inst, str(hit.get("date") or ""))

    ranked = sorted(hits, key=score, reverse=True)
    view: list[dict[str, Any]] = []
    for hit in ranked[:limit]:
        view.append(
            {
                "ref": hit.get("ref"),
                "title": hit.get("title"),
                "institution": hit.get("institution"),
                "date": hit.get("date"),
                "content_type": hit.get("content_type"),
                "peek": _peek_brief(str(hit.get("snippet") or ""), [segment]),
            }
        )
    return view


def _hit_key(hit: dict[str, Any]) -> str:
    chunk = str(hit.get("chunk_id") or "").strip()
    if chunk:
        document = str(hit.get("doc_id") or hit.get("pointer") or hit.get("title") or "").strip()
        return f"chunk:{document}:{chunk}"
    return "text:" + "|".join(
        str(hit.get(key) or "").strip() for key in ("title", "date", "institution")
    ) + "|" + str(hit.get("snippet") or "")[:160]


def _peek_brief(text: str, names: list[str]) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for name in names:
        if name and name in cleaned:
            at = cleaned.find(name)
            start = max(0, at - 80)
            return cleaned[start : start + PEEK]
    return cleaned[:PEEK]


def _seed_previous_hits(deps: BriefDeps, log_root: Path, segment: str) -> None:
    packed = log_root / f"research_hits_{segment}.json"
    if packed.is_file():
        data = load_json(packed)
        seeded = list(data.get("hits") or []) or list(data.get("seed_hits") or [])
        if seeded:
            deps.seed_hits = _number_seed_hits(seeded)
            deps.seen_hits.update(_hit_key(hit) for hit in deps.seed_hits)
            return
    _, seeded = hits_from_mcp_logs(log_root, segment)
    deps.seed_hits = _number_seed_hits(seeded)
    deps.seen_hits.update(_hit_key(hit) for hit in deps.seed_hits)


def _number_seed_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for i, hit in enumerate(hits, 1):
        hit["ref"] = f"seed:{i}"
    return hits


def hits_from_mcp_logs(log_root: Path, segment: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from valuation.segment_split.collect import _parse_vector

    queries: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []
    for path in sorted(log_root.glob(f"searchComeinResource_brief_{segment}_*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        seq = path.stem.rsplit("_", 1)[-1]
        content_type = _LOG_TYPE.get(seq, "comment")
        first = raw[0] if isinstance(raw, list) and raw else {}
        if isinstance(first, dict):
            content_type = _SOURCE_TYPE.get(str(first.get("source") or ""), content_type)
        parsed = _parse_vector(raw, path.stem, content_type=content_type)
        queries.append(
            {
                "id": path.stem,
                "content_type": content_type,
                "query": "(reuse)",
                "hits": len(parsed),
            }
        )
        hits.extend(parsed)
    return queries, hits


def _sibling_names(run_dir: Path, segment: str) -> list[str]:
    notes = load_json(spec_dir(run_dir) / "segment_split_notes.json")
    names: list[str] = []
    for item in notes.get("final_segments") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name and name != segment:
            names.append(name)
    return names


def _load_segment(run_dir: Path, segment: str) -> tuple[dict, dict]:
    root = spec_dir(run_dir)
    facts = load_json(root / "facts.json")
    notes = load_json(root / "segment_split_notes.json")
    notes_seg = _find_seg(notes.get("final_segments") or [], segment, "name")
    if not notes_seg:
        raise SystemExit(f"segment_split_notes 没有分部 {segment}")
    return facts, notes_seg


def _find_seg(rows: list, name: str, key: str) -> dict:
    for item in rows:
        if isinstance(item, dict) and str(item.get(key) or "").strip() == name:
            return item
    return {}


def _start_from_hist(hist: list[str]) -> str:
    for period in hist:
        year = str(period)[:4]
        if year.isdigit():
            return f"{int(year) - 1}-01-01"
    return f"{datetime.now().year - 3}-01-01"
