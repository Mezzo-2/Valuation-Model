"""同业两拍：先提名，代码填 PE，再选核心池。"""

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
from valuation.peer_valuation.fetch import (
    _norm_ticker,
    fetch_subject_consensus,
    fill_candidates,
)
from valuation.peer_valuation.schema import CORE_MIN, require_comps
from valuation.segment_research.brief_agent import format_brief_for_forecast
from valuation.segment_split.collect import _mcp_typed_search
from valuation.segment_split.schema import SchemaError
from valuation.segment_split.split_agent import _log, _log_messages
from valuation.shared.env import build_chat_model
from valuation.shared.io import dump_json, load_json, logs_dir, spec_dir

PEEK = 400
ContentType = Literal["domestic_report", "foreign_report", "minutes", "comment"]
TypedSearchFn = Callable[[str, bool, str, str], list[dict[str, Any]]]
ScreenFn = Callable[[str], list[dict[str, Any]]]


class PeerPick(BaseModel):
    name: str = Field(description="公司名")
    ticker: str = Field(description="股票代码，如 300308")
    why: str = Field(description="为何可比，一句")


class NominateDraft(BaseModel):
    candidates: list[PeerPick] = Field(description="8 到 12 个候选")


class CorePick(BaseModel):
    ticker: str = Field(description="必须是已填表里有 pe_y1 的代码")
    note: str = Field(description="为何进核心池")


class ClassifyDraft(BaseModel):
    core: list[CorePick] = Field(description="至少 4 家，只能选自有 pe_y1 的行")
    pe_adjust: float = Field(1.0, description="默认 1。偏离必须在 rationale 写量化理由")
    rationale: str = Field(description="为何纳入或排除：业务和财务属性优先")
    open_gaps: list[str] = Field(default_factory=list)


@dataclass
class NominateDeps:
    company: str
    ticker: str
    facts: dict
    briefs_text: str
    search: TypedSearchFn
    screen: ScreenFn
    max_searches: int = 1
    max_screens: int = 1
    verbose: bool = True
    hits: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    search_seq: int = 0
    screen_seq: int = 0

    @property
    def searches_left(self) -> int:
        return max(0, self.max_searches - self.search_seq)

    @property
    def screens_left(self) -> int:
        return max(0, self.max_screens - self.screen_seq)

    @property
    def label(self) -> str:
        if self.company and self.ticker:
            return f"{self.company}（{self.ticker}）"
        return self.company or self.ticker


@dataclass
class ClassifyDeps:
    company: str
    ticker: str
    filled: list[dict[str, Any]]
    y1: str
    verbose: bool = True

    @property
    def label(self) -> str:
        if self.company and self.ticker:
            return f"{self.company}（{self.ticker}）"
        return self.company or self.ticker


def _nominate_instructions(ctx: RunContext[NominateDeps]) -> str:
    deps = ctx.deps
    return (
        f"你在为{deps.label}找可比公司。提名同业。\n"
        f"还可选股 {deps.screens_left} 次、检索 {deps.searches_left} 次。材料够了就停。\n"
        "优先业务相近、卖方覆盖会够的 A 股；港美股可以作补充。\n"
        "screen_peers 的 concept 用短词，例如 服务器、电子制造。\n"
        "交出 8 到 12 个候选：公司名、代码、为何可比。PE 由代码填。"
    )


nominate_agent = Agent(
    output_type=NominateDraft,
    deps_type=NominateDeps,
    retries=0,
    instructions=_nominate_instructions,
)


@nominate_agent.tool
def screen_peers(ctx: RunContext[NominateDeps], concept: str) -> str:
    """按概念补一批候选代码。只能用一次。"""
    deps = ctx.deps
    if _bad_concept(concept):
        return "概念用短词，例如 服务器、电子制造。这次不计数。"
    if deps.screens_left <= 0:
        return "选股次数已用完。"
    deps.screen_seq += 1
    _log(deps, f"[peer] 选股 {concept}")
    rows = deps.screen(concept)
    _log(deps, f"[peer] 选股回来 {len(rows)} 家")
    return json.dumps(rows[:20], ensure_ascii=False)


@nominate_agent.tool
def search_research(
    ctx: RunContext[NominateDeps],
    query: str,
    content_type: ContentType = "domestic_report",
    allow_image: bool = False,
) -> str:
    """检索同业或可比估值相关材料。只能用一次。"""
    deps = ctx.deps
    if deps.searches_left <= 0:
        return "检索次数已用完。"
    deps.search_seq += 1
    query_id = f"peer_{deps.search_seq}"
    _log(deps, f"[peer] 检索 {query}")
    hits = deps.search(query, allow_image, query_id, content_type)
    deps.queries.append({"id": query_id, "query": query, "hits": len(hits)})
    deps.hits.extend(hits)
    view = []
    for hit in hits[:10]:
        view.append(
            {
                "title": hit.get("title"),
                "institution": hit.get("institution"),
                "peek": _peek(str(hit.get("snippet") or "")),
            }
        )
    return json.dumps(view, ensure_ascii=False)


def _classify_instructions(ctx: RunContext[ClassifyDeps]) -> str:
    deps = ctx.deps
    return (
        f"你在为{deps.label}选核心同业池。预测首年是{deps.y1}。\n"
        "只能读已填好的表。有 pe_y1 的才能进核心池，至少 4 家。\n"
        "先按业务再看财务。高倍数若业务可比可留在核心池，离散度写进 note 或 rationale，必要时用 pe_adjust。\n"
        "小样本可以保留。覆盖不够写缺口，核心池只放业务可比的名字。\n"
        "pe_adjust 默认 1。要偏离须写出量化理由。"
    )


classify_agent = Agent(
    output_type=ClassifyDraft,
    deps_type=ClassifyDeps,
    retries=0,
    instructions=_classify_instructions,
)


def run_live_peers(run_dir: Path) -> tuple[dict, dict]:
    facts = load_json(spec_dir(run_dir) / "facts.json")
    log_root = logs_dir(run_dir) / "mcp"
    log_root.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []
    chat = build_chat_model(api="chat")
    with ComeinClient() as mcp:
        cons = fetch_subject_consensus(mcp, facts, log_root, calls)
        dump_json(spec_dir(run_dir) / "consensus_estimates.json", cons)
        deps = NominateDeps(
            company=str(facts.get("company") or ""),
            ticker=str(facts.get("ticker") or ""),
            facts=facts,
            briefs_text=_load_briefs(run_dir),
            search=_mcp_typed_search(mcp, calls, log_root),
            screen=_screen_fn(mcp, calls, log_root),
        )
        nominated = _run_nominate(deps, chat)
        dump_json(log_root / "peer_nominate_raw.json", nominated.model_dump())
        filled = fill_candidates(
            mcp,
            [item.model_dump() for item in nominated.candidates],
            facts,
            log_root,
            calls,
        )
        usable = [item for item in filled if item.get("pe_y1") is not None]
        if len(usable) < 4:
            more = _run_nominate_more(facts, filled, chat)
            dump_json(log_root / "peer_nominate_more.json", more.model_dump())
            filled = _merge_filled(
                filled,
                fill_candidates(
                    mcp,
                    [item.model_dump() for item in more.candidates],
                    facts,
                    log_root,
                    calls,
                    log_prefix="peers_more",
                ),
            )
            usable = [item for item in filled if item.get("pe_y1") is not None]
        if len(usable) < 4:
            filled = _merge_filled(
                filled,
                _supplement_filled(mcp, facts, filled, calls, log_root),
            )
            usable = [item for item in filled if item.get("pe_y1") is not None]
        dump_json(log_root / "peer_filled.json", filled)
        if len(usable) < 4:
            raise SystemExit(
                "同业覆盖不足：有预测首年 PE 的不足 4 家。"
                + "；".join(
                    f"{item.get('name')} pe_y1={item.get('pe_y1')} thin={item.get('thin')}"
                    for item in filled
                )
            )
        card = _run_classify(facts, filled, chat)
    dump_json(logs_dir(run_dir) / "search_log_peer.json", calls)
    dump_json(spec_dir(run_dir) / "comps_spec.json", card)
    return cons, card


def _run_nominate_more(facts: dict, filled: list[dict[str, Any]], chat) -> NominateDraft:
    deps = NominateDeps(
        company=str(facts.get("company") or ""),
        ticker=str(facts.get("ticker") or ""),
        facts=facts,
        briefs_text="",
        search=lambda *args, **kwargs: [],
        screen=lambda concept: [],
        max_searches=0,
        max_screens=0,
    )
    have = "、".join(
        f"{item.get('name')} pe_y1={item.get('pe_y1')}" for item in filled
    )
    prompt = (
        f"本公司 {deps.label}。已填表：{have}。\n"
        "有预测首年 PE 的还不到 4 家。再提名 6 到 8 个尚未出现的 A 股同业。\n"
        "优先服务器、ODM、电子制造，卖方覆盖要够。"
    )
    _log(deps, "[peer] 补提名 A 股")
    result = nominate_agent.run_sync(prompt, deps=deps, model=chat)
    _log_messages(deps, result.all_messages())
    return result.output


def _merge_filled(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {str(item.get("ticker") or "") for item in base}
    out = list(base)
    for item in extra:
        ticker = str(item.get("ticker") or "")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        out.append(item)
    return out


def _run_nominate(deps: NominateDeps, chat) -> NominateDraft:
    _log(deps, "[peer] 提名候选")
    result = nominate_agent.run_sync(_nominate_user(deps), deps=deps, model=chat)
    _log_messages(deps, result.all_messages())
    return result.output


def _run_classify(facts: dict, filled: list[dict[str, Any]], chat) -> dict:
    y1 = str((facts.get("forecast_periods") or ["2026E"])[0])
    deps = ClassifyDeps(
        company=str(facts.get("company") or ""),
        ticker=str(facts.get("ticker") or ""),
        filled=filled,
        y1=y1,
    )
    hint = ""
    last_errors: list[str] = []
    for attempt in range(1, 4):
        _log(deps, f"[peer] 分类 第{attempt}次")
        result = classify_agent.run_sync(_classify_user(deps, hint), deps=deps, model=chat)
        _log_messages(deps, result.all_messages())
        draft = result.output
        payload = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "core": [item.model_dump() for item in draft.core],
            "pe_adjust": draft.pe_adjust,
            "rationale": draft.rationale,
            "open_gaps": draft.open_gaps,
        }
        try:
            card = require_comps(payload, facts, filled)
            for gap in _coverage_gaps(filled, card.get("core") or []):
                if gap not in card["open_gaps"]:
                    card["open_gaps"].append(gap)
            return card
        except SchemaError as exc:
            last_errors = list(exc.errors)
            _log(deps, "[peer] 不合法：" + "；".join(last_errors))
            hint = "不合法：" + "；".join(last_errors)
    raise SchemaError(last_errors or ["同业分类失败"])


def _nominate_user(deps: NominateDeps) -> str:
    segs = "、".join(_brief_segment_hint(deps))
    text = (
        f"本公司 {deps.label}。提名同业。\n"
        f"已有分部：{segs or '未知'}。\n"
        "用 screen_peers 或 search_research 补名单也可以，够了就写 candidates。\n"
    )
    if deps.briefs_text:
        text += "分部研究笔记（只当业务背景）：\n" + deps.briefs_text + "\n"
    return text


def _classify_user(deps: ClassifyDeps, hint: str) -> str:
    usable = [item for item in deps.filled if item.get("pe_y1") is not None]
    text = (
        f"预测首年 {deps.y1}。有 pe_y1 的才能进核心池，下面这些可用：\n"
        f"{json.dumps(usable, ensure_ascii=False)}\n"
        "其余候选：\n"
        f"{json.dumps([item for item in deps.filled if item.get('pe_y1') is None], ensure_ascii=False)}\n"
        "核心至少 4 家。先按业务再看财务；不进核心须写业务或财务理由。覆盖不够写缺口。\n"
    )
    if hint:
        text += hint
    return text


def _screen_fn(mcp: ComeinClient, calls: list[dict[str, Any]], log_root: Path) -> ScreenFn:
    def screen(concept: str) -> list[dict[str, Any]]:
        arguments = {
            "enumConditions": [
                {"key": "concept", "values": [concept]},
                {"key": "marketType", "values": ["sh", "sz"]},
            ],
            "page": 1,
            "size": 20,
            "sort": [{"field": "marketCap", "order": "desc"}],
        }
        try:
            raw = mcp.screener_stock(**arguments)
            ok = True
            err = ""
        except Exception as exc:
            raw = {"error": str(exc)}
            ok = False
            err = str(exc)
        calls.append({"tool": "screenerStock", "arguments": arguments, "ok": ok, "error": err})
        dump_json(log_root / "screenerStock_peer.json", raw)
        return _parse_screener(raw)

    return screen


def _parse_screener(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("error"):
        return []
    data = raw.get("data")
    if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
        data = data.get("data")
    if isinstance(data, dict):
        concept = ((data.get("enumConditionCount") or {}).get("concept") or {})
        if concept.get("totalCount") == 0:
            return []
    rows: list[Any]
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = (
            data.get("stockList")
            or data.get("list")
            or data.get("items")
            or data.get("records")
            or []
        )
        if not rows and data.get("name"):
            rows = [data]
    else:
        rows = []
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("stockName") or item.get("assetName")
        ticker = item.get("ticker") or item.get("code") or item.get("stockCode")
        if name and ticker:
            out.append({"name": str(name), "ticker": str(ticker), "mcap": item.get("marketCap")})
    return out


def _supplement_filled(
    mcp: ComeinClient,
    facts: dict,
    filled: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    log_root: Path,
) -> list[dict[str, Any]]:
    have = {_norm_ticker(item.get("ticker")) for item in filled}
    have.add(_norm_ticker(facts.get("ticker")))
    have.discard("")
    screen = _screen_fn(mcp, calls, log_root)
    picks: list[dict[str, Any]] = []
    for concept in ("消费电子", "云计算", "算力"):
        for row in screen(concept):
            ticker = _norm_ticker(row.get("ticker"))
            if not ticker or ticker in have:
                continue
            have.add(ticker)
            picks.append(
                {
                    "name": row.get("name") or ticker,
                    "ticker": ticker,
                    "why": f"选股补名单：{concept}",
                }
            )
            if len(picks) >= 8:
                break
        if len(picks) >= 8:
            break
    if not picks:
        return []
    extra = fill_candidates(mcp, picks, facts, log_root, calls, log_prefix="peers_supp")
    return [item for item in extra if item.get("pe_y1") is not None]


def _bad_concept(concept: str) -> bool:
    text = str(concept or "").strip()
    if not text or len(text) > 8:
        return True
    return any(ch in text for ch in "，,。；;、 ")


def _load_briefs(run_dir: Path) -> str:
    root = spec_dir(run_dir)
    blocks = []
    for path in sorted(root.glob("research_brief_*.json")):
        brief = load_json(path)
        name = str(brief.get("segment") or path.stem.replace("research_brief_", ""))
        blocks.append(f"—— {name} ——\n{format_brief_for_forecast(brief)}")
    return "\n\n".join(blocks)


def _brief_segment_hint(deps: NominateDeps) -> list[str]:
    names = []
    for line in deps.briefs_text.splitlines():
        if line.startswith("—— ") and line.endswith(" ——"):
            names.append(line.strip("— ").strip())
    return names


def _coverage_gaps(filled: list[dict[str, Any]], core: list[dict[str, Any]]) -> list[str]:
    usable = [item for item in filled if item.get("pe_y1") is not None]
    gaps: list[str] = []
    if len(usable) <= CORE_MIN:
        gaps.append("核心池仅满足最低家数，覆盖不足")
    pes = []
    for item in core:
        try:
            pes.append(float(item["pe_y1"]))
        except (TypeError, ValueError, KeyError):
            continue
    if len(pes) >= 2 and min(pes) > 0 and max(pes) / min(pes) >= 1.5:
        gaps.append("核心池 PE 离散度大")
    return gaps


def _peek(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:PEEK]
