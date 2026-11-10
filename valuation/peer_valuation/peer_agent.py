"""先提名、代码填 PE，再选择核心池并独立判断模型 PE。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean, median
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
from valuation.segment_research.brief_agent import format_brief_for_forecast, read_research_hit as _read_brief_hit
from valuation.peer_valuation.context import load_operating_context
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
    ticker: str = Field(description="带明确市场的证券代码：港股 hk00700、美股 usMAT、日股 jpn8136；A股可用300308")
    market: Literal["A", "HK", "US", "JP", "OTHER"] | None = Field(default=None, description="候选证券的上市市场；跨市场同业必须明确")
    why: str = Field(description="为何可比，一句")


class NominateDraft(BaseModel):
    candidates: list[PeerPick] = Field(description="8 到 12 个候选")


class CorePick(BaseModel):
    ticker: str = Field(description="必须是已填表里有 pe_y1 的代码")
    note: str = Field(description="一至两句说明业务可比点、主要差异及预测首年PE水平；高倍数离散项说明原因")


class ClassifyDraft(BaseModel):
    core: list[CorePick] = Field(description="至少 4 家，只能选自有 pe_y1 的行")
    model_pe: float = Field(gt=0, allow_inf_nan=False, description="独立判断的合理PE，对应本公司预测首年EPS；不得默认取同业均值")
    model_pe_reason: str = Field(min_length=1, description="一至两句简要理由，点出主要同业参照及本公司为何适用该倍数，供Excel展示")
    model_pe_sources: list[str] = Field(default_factory=list, description="补查材料中实际影响判断且已展开读取的ref；只用注入的已有研究时可留空")
    rationale: str = Field(description="同业纳入/排除和合理PE判断的详细解释，区分已知事实和推测")
    open_gaps: list[str] = Field(default_factory=list)


@dataclass
class NominateDeps:
    company: str
    ticker: str
    facts: dict
    briefs_text: str
    search: TypedSearchFn
    screen: ScreenFn
    max_searches: int = 2
    max_screens: int = 2
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
    operating: dict = field(default_factory=dict)
    briefs_text: str = ""
    search: TypedSearchFn | None = None
    seed_hits: list[dict[str, Any]] = field(default_factory=list)
    hits: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    reads: list[dict[str, Any]] = field(default_factory=list)
    search_seq: int = 0
    read_seq: int = 0
    max_searches: int = 4
    max_reads: int = 4

    @property
    def searches_left(self) -> int:
        return max(0, self.max_searches - self.search_seq) if self.search else 0

    @property
    def reads_left(self) -> int:
        return max(0, self.max_reads - self.read_seq)

    @property
    def label(self) -> str:
        if self.company and self.ticker:
            return f"{self.company}（{self.ticker}）"
        return self.company or self.ticker


def _nominate_instructions(ctx: RunContext[NominateDeps]) -> str:
    deps = ctx.deps
    from valuation.shared.markets import is_foreign, market_of
    preference = (f"优先业务相近的{'美股' if market_of(deps.facts) == 'US' else '港股'}，其他市场作为补充。\n"
                  if is_foreign(deps.facts) else "优先业务相近、卖方覆盖会够的 A 股；港美股可以作补充。\n")
    return (
        f"你在为{deps.label}找可比公司。提名同业。\n"
        f"还可选股 {deps.screens_left} 次、检索 {deps.searches_left} 次。材料够了就停。\n"
        + preference +
        "港股代码用hk加五位数字，美股用us加代码，日股用jpn加四位数字；market填写实际上市市场，不能把日股当成港股。\n"
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
    """按概念补一批候选代码。"""
    deps = ctx.deps
    if _bad_concept(concept):
        return "概念用短词，例如 锂电池、服务器、医疗器械。这次不计数。"
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
    """检索同业或可比估值相关材料。"""
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
        f"你是{deps.label}的估值分析师，选择核心同业并独立给出合理PE。预测首年是{deps.y1}。\n"
        "核心池只能选已填表中有 pe_y1 的公司，至少 4 家；不能补造证券身份或PE。\n"
        f"pe_y1来自接口最新预测年，展示为{deps.y1} PE；财年或盈利口径明确冲突时不得用于定价，元数据缺失不能当作已确认一致。\n"
        "证券身份以接口返回的公司名称和代码为准，提名理由不能覆盖证券身份；名称代码错配时不能入池。\n"
        "先按业务再看财务。高低倍数都可以保留在核心池，但留在池中不意味着其倍数必须等权参与定价。\n"
        "备注统一用一至两句写业务可比点、主要差异及当年PE水平；不要重复接口名称、字段名或缺少元数据等通用说明。\n"
        "小样本可以保留。覆盖不够写缺口，核心池只放业务可比的名字。\n"
        "model_pe是本公司合理的基础PE。先指出主要参考哪些同业和估值区间，再结合本公司增长质量、利润率、商业模式、现金回报及风险说明定位。\n"
        "均值、中位数仅作分布参考，不能默认采用，也不能为了压低估值而机械删除高倍数。合理PE可以接近统计值，但必须有针对本公司的依据。\n"
        "异常倍数可能与低基数、亏损恢复、一次性损益或增长预期有关；原因须核实，未核实时只能表述为可能性。历史和预测、季度累计和全年不能混比。\n"
        f"可按需检索 {deps.searches_left} 次、展开 {deps.reads_left} 次。重点补查主要参照和重大疑点，已有材料够用即可停止。\n"
        "补查材料中的关键数字或异常成因须用read_valuation_research展开核实，model_pe_sources只填实际已读且影响判断的ref。\n"
        "model_pe_reason用一至两句简要说明主要参照及给定倍数的原因，详细取舍放rationale；来源不足写缺口，不编造。\n"
        "只判断一个基础PE，情景系数由原模板计算。不能为了贴近现价或得到买入评级倒推PE。"
    )


classify_agent = Agent(
    output_type=ClassifyDraft,
    deps_type=ClassifyDeps,
    retries=0,
    instructions=_classify_instructions,
)


@classify_agent.tool
def search_valuation_research(
    ctx: RunContext[ClassifyDeps], query: str,
    content_type: ContentType = "domestic_report", allow_image: bool = False,
) -> str:
    """按需核实主要同业的业务、盈利状态和合理估值依据。"""
    deps = ctx.deps
    if deps.searches_left <= 0:
        return "检索次数已用完，请使用已有研究并明确缺口。"
    deps.search_seq += 1
    query_id = f"peer_valuation_{deps.search_seq}"
    hits = deps.search(query, allow_image, query_id, content_type)
    hits = [{**hit, "ref": f"{query_id}:{i}"} for i, hit in enumerate(hits, 1)]
    deps.hits.extend(hits)
    deps.queries.append({"id": query_id, "query": query, "content_type": content_type, "hits": len(hits)})
    _log(deps, f"[peer] 估值补查 {query}：{len(hits)}条")
    return json.dumps(_valuation_hits_view(hits), ensure_ascii=False)


@classify_agent.tool
def read_valuation_research(
    ctx: RunContext[ClassifyDeps], ref: str, focus: str = "", offset: int = 0,
) -> str:
    """展开检索命中的原片段，核实关键事实及其期间。"""
    return _read_brief_hit(ctx, ref, focus, offset)


def _valuation_hits_view(hits: list[dict]) -> list[dict]:
    return [{key: hit.get(key) for key in ("ref", "title", "institution", "date")}
            | {"peek": _peek(str(hit.get("snippet") or ""))} for hit in hits[:10]]


def run_live_peers(run_dir: Path) -> tuple[dict, dict]:
    facts = load_json(spec_dir(run_dir) / "facts.json")
    operating = load_operating_context(run_dir, facts)
    log_root = logs_dir(run_dir) / "mcp"
    log_root.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []
    chat = build_chat_model()
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
                _supplement_filled(mcp, facts, filled, calls, log_root, deps.briefs_text),
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
        valuation_deps = ClassifyDeps(
            company=deps.company, ticker=deps.ticker, filled=filled,
            y1=str(facts["forecast_periods"][0]), operating=operating,
            briefs_text=deps.briefs_text, search=deps.search,
            seed_hits=[{**hit, "ref": f"nominate:{i}"} for i, hit in enumerate(deps.hits, 1)],
        )
        try:
            card = _run_classify(facts, filled, chat, deps=valuation_deps)
        finally:
            dump_json(log_root / "peer_valuation_research.json", {
                "operating": operating, "queries": valuation_deps.queries,
                "reads": valuation_deps.reads, "hits": valuation_deps.seed_hits + valuation_deps.hits,
            })
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
    from valuation.shared.markets import is_foreign
    preference = ("优先同市场、业务相近且盈利口径可比的公司。" if is_foreign(facts)
                  else "优先与本公司实际业务相近且卖方覆盖充分的 A 股。")
    market = str(facts.get("market_type")) if is_foreign(facts) else "A 股"
    prompt = (
        f"本公司 {deps.label}。已填表：{have}。\n"
        f"有可比预测首年 PE 的还不到 4 家。再提名 6 到 8 个尚未出现的 {market} 同业。\n"
        + preference
    )
    _log(deps, f"[peer] 补提名 {market} 同业")
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


def _run_classify(facts: dict, filled: list[dict[str, Any]], chat, *, deps: ClassifyDeps | None = None) -> dict:
    y1 = str((facts.get("forecast_periods") or ["2026E"])[0])
    deps = deps or ClassifyDeps(
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
            "schema_version": 2,
            "model_pe": draft.model_pe,
            "model_pe_reason": draft.model_pe_reason,
            "pe_adjust": 1.0,
            "rationale": draft.rationale,
            "open_gaps": draft.open_gaps,
        }
        try:
            read_refs = {item["ref"] for item in deps.reads}
            missing = set(draft.model_pe_sources) - read_refs
            if missing:
                raise SchemaError(["估值来源必须实际展开读取：" + "、".join(sorted(missing))])
            selected = set(draft.model_pe_sources)
            payload["model_pe_sources"] = [
                {key: hit.get(key) for key in ("ref", "title", "institution", "date", "doc_id", "chunk_id")}
                for hit in deps.seed_hits + deps.hits if hit.get("ref") in selected
            ]
            card = require_comps(payload, facts, filled, require_model_pe=True)
            for gap in _coverage_gaps(filled, card.get("core") or []):
                if gap not in card["open_gaps"]:
                    card["open_gaps"].append(gap)
            return card
        except SchemaError as exc:
            last_errors = list(exc.errors)
            _log(deps, "[peer] 不合法：" + "；".join(last_errors))
            prev_json = json.dumps(draft.model_dump(), ensure_ascii=False)
            hint = f"上次草稿：{prev_json}\n不合法：{'；'.join(last_errors)}"
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
        "本公司经营预测（金额、期间及来源按原记录，比例为小数）：\n"
        f"{json.dumps(deps.operating, ensure_ascii=False)}\n"
        f"已有分部研究：\n{deps.briefs_text}\n"
        f"预测首年 {deps.y1}。有 pe_y1 的才能进核心池，下面这些可用：\n"
        f"{json.dumps(usable, ensure_ascii=False)}\n"
        "其余候选：\n"
        f"{json.dumps([item for item in deps.filled if item.get('pe_y1') is None], ensure_ascii=False)}\n"
        "核心至少 4 家。先按业务再看财务；不进核心须写业务或财务理由。覆盖不够写缺口。\n"
    )
    pes = [float(item["pe_y1"]) for item in usable]
    if pes:
        text += "全部可用候选的统计背景（不是选定核心池的统计，也不是目标PE）：\n"
        text += json.dumps({"mean": mean(pes), "median": median(pes), "min": min(pes), "max": max(pes)}, ensure_ascii=False) + "\n"
    if deps.seed_hits:
        text += "提名阶段已有检索线索，可按ref展开核实：\n" + json.dumps(_valuation_hits_view(deps.seed_hits), ensure_ascii=False) + "\n"
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


def _supplement_concepts(facts: dict, briefs_text: str = "") -> list[str]:
    """从公司申万行业及分部名称动态提取选股概念词，避免跨行业硬编码污染。"""
    concepts: list[str] = []
    profile = (facts.get("company_info") or {}).get("profile") or {}
    sw = str(profile.get("sw_industry") or "")
    for part in reversed([p.strip() for p in sw.split("/") if p.strip()]):
        if not _bad_concept(part) and part not in concepts:
            concepts.append(part)
    for line in (briefs_text or "").splitlines():
        if line.startswith("—— ") and line.endswith(" ——"):
            seg = line.strip("— ").strip()
            if seg and seg not in {"其他", "其他业务"} and not _bad_concept(seg) and seg not in concepts:
                concepts.append(seg)
    return concepts[:4]


def _supplement_filled(
    mcp: ComeinClient,
    facts: dict,
    filled: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    log_root: Path,
    briefs_text: str = "",
) -> list[dict[str, Any]]:
    from valuation.shared.markets import is_foreign
    if is_foreign(facts):
        return []  # A 股选股器补池仅用于 A 股。
    concepts = _supplement_concepts(facts, briefs_text)
    if not concepts:
        return []
    have = {_norm_ticker(item.get("ticker")) for item in filled}
    have.add(_norm_ticker(facts.get("ticker")))
    have.discard("")
    screen = _screen_fn(mcp, calls, log_root)
    picks: list[dict[str, Any]] = []
    for concept in concepts:
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
