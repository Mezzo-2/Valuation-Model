"""cost_agent：公司级成本假设。历史轨迹已算好，只搜卖方、只拍预测年。"""

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
from valuation.operating_cost.schema import (
    AMOUNT_KEYS,
    RATIO_KEYS,
    format_hist_track,
    hist_cost_track,
    require_cost,
)
from valuation.segment_research.brief_agent import format_brief_for_forecast
from valuation.segment_split.collect import _mcp_typed_search
from valuation.segment_split.schema import SchemaError
from valuation.segment_split.split_agent import _log, _log_messages
from valuation.shared.env import build_chat_model
from valuation.shared.io import dump_json, load_json, logs_dir, spec_dir

MAX_SEARCHES = 4
PEEK = 800
CONTENT_LABEL = {
    "domestic_report": "内资研报",
    "foreign_report": "外资研报",
    "minutes": "纪要",
    "comment": "点评",
}
ContentType = Literal["domestic_report", "foreign_report", "minutes", "comment"]
TypedSearchFn = Callable[[str, bool, str, str], list[dict[str, Any]]]
_COST_HINTS = ("毛利率", "费用率", "税率", "财务费用", "销售费用", "管理费用", "研发费用")


class CostDraft(BaseModel):
    gross_margin: dict[str, float] = Field(
        description='预测年合并毛利率，小数如 {"2026E": 0.08}，不要写 8'
    )
    sales_ratio: dict[str, float] = Field(description="预测年销售费用率，小数")
    admin_ratio: dict[str, float] = Field(description="预测年管理费用率，小数")
    rd_ratio: dict[str, float] = Field(description="预测年研发费用率，小数")
    tax_rate: dict[str, float] = Field(description="预测年有效税率，小数，0–1")
    minority: dict[str, float] = Field(description="预测年少数股东比率，小数，常为 0")
    fin_exp: dict[str, float] = Field(description="预测年财务费用，亿元金额，可为负")
    nonop_inc: dict[str, float] = Field(description="预测年营业外收入，亿元")
    nonop_exp: dict[str, float] = Field(description="预测年营业外支出，亿元")
    other_op: dict[str, float] = Field(description="预测年其他经营净收益，亿元，可为负")
    rationale: str = Field(description="按理由模板三段写。我们的数从轨迹推，不抄卖方中位。")
    open_gaps: list[str] = Field(default_factory=list)
    sources: list[dict[str, str]] = Field(default_factory=list)


@dataclass
class CostDeps:
    company: str
    ticker: str
    facts: dict
    hist_track: dict
    forecast_periods: list[str]
    briefs_text: str
    search: TypedSearchFn
    max_searches: int = MAX_SEARCHES
    verbose: bool = True
    hits: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    search_seq: int = 0

    @property
    def searches_left(self) -> int:
        return max(0, self.max_searches - self.search_seq)

    @property
    def label(self) -> str:
        if self.company and self.ticker:
            return f"{self.company}（{self.ticker}）"
        return self.company or self.ticker


def _rationale_template(years: list[str]) -> str:
    street = "\n".join(
        (
            f"{year}：{{机构}} {{报告日}} {{目标期}} 毛利率/费用率/税率 {{数字}}，假设是{{…}}。下一家同理。"
            "只写对本公司这些指标有假设的机构。"
            if i == 0
            else f"{year}：…"
        )
        for i, year in enumerate(years)
    )
    ours = "\n".join(
        (
            f"{year}：毛利率 {{小数}}。销售/管理/研发 {{小数}}/{{小数}}/{{小数}}。"
            "税率 {{小数}}。财务费用 {{亿元}}。{{怎么走到这个数}}。"
            "不要写跟了谁，也不要抄谁的数字。"
            if i == 0
            else f"{year}：…"
        )
        for i, year in enumerate(years)
    )
    return (
        "【历史轨迹】\n"
        "用注入的近年毛利率、三费率、税率、财务费用，只写会改预测的拐点。不要改这些历史数。\n"
        "【卖方假设】\n"
        f"{street}\n"
        "没有材料的年份只写「年份：」，后面留空，不要写「没有」。\n"
        "【我们的数】\n"
        f"{ours}\n"
        "没有卖方就按最近一年外推或小幅经营杠杆。不要合成中位当答案。\n"
        "比率一律写小数（0.08 不是 8）。财务费用是亿元金额，历史为负也可以继续为负。"
    )


def _cost_instructions(ctx: RunContext[CostDeps]) -> str:
    deps = ctx.deps
    return (
        f"你是{deps.label}的分析师，拍公司合并成本费用假设。只输出每年一个合并毛利率，不要拆分部毛利率。\n"
        f"历史轨迹已经算好，禁止改。还可检索 {deps.searches_left} 次。\n"
        "用 search_research 找卖方对合并毛利率、三费率、有效税率、财务费用的假设。"
        "材料够了就停搜。搜不到就外推，缺口写进 open_gaps，不要假装有搜证。\n"
        "只写预测年。比率用小数。rationale 按模板写。我们的数从轨迹和机制推，不抄卖方。\n"
        f"{_rationale_template(deps.forecast_periods)}"
    )


cost_agent = Agent(
    output_type=CostDraft,
    deps_type=CostDeps,
    retries=0,
    instructions=_cost_instructions,
)


@cost_agent.tool
def search_research(
    ctx: RunContext[CostDeps],
    query: str,
    content_type: ContentType,
    allow_image: bool = False,
) -> str:
    """检索一类资料。问句尽量含公司名和毛利率/费用率/税率。"""
    deps = ctx.deps
    if deps.searches_left <= 0:
        return "检索次数已用完，根据已有材料写假设。"
    deps.search_seq += 1
    query_id = f"cost_{deps.search_seq}"
    _log(deps, f"[cost] {CONTENT_LABEL[content_type]} {query}")
    hits = deps.search(query, allow_image, query_id, content_type)
    for hit in hits:
        hit["content_type"] = content_type
    deps.queries.append(
        {
            "id": query_id,
            "content_type": content_type,
            "query": query,
            "allow_image": allow_image,
            "hits": len(hits),
        }
    )
    deps.hits.extend(hits)
    view = _cost_hits_view(hits, deps.company)
    _log(deps, f"[cost] 回来 {len(hits)} 条，给模型 {len(view)} 条")
    for i, hit in enumerate(view[:5], 1):
        _log(
            deps,
            f"  hit[{i}] {hit.get('date')} {hit.get('institution')} {hit.get('title')}\n"
            f"         {hit.get('peek')}",
        )
    return json.dumps(view, ensure_ascii=False)


def run_live_cost(run_dir: Path) -> dict:
    facts = load_json(spec_dir(run_dir) / "facts.json")
    hist_track = hist_cost_track(facts)
    log_root = logs_dir(run_dir) / "mcp"
    log_root.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []
    chat = build_chat_model(api="chat")
    with ComeinClient() as mcp:
        deps = CostDeps(
            company=str(facts.get("company") or ""),
            ticker=str(facts.get("ticker") or ""),
            facts=facts,
            hist_track=hist_track,
            forecast_periods=[str(year) for year in (facts.get("forecast_periods") or [])],
            briefs_text=_load_cost_context(run_dir),
            search=_mcp_typed_search(mcp, calls, log_root, start=_start_from_hist(facts)),
        )
        draft = compile_cost(deps, model=chat)
        dump_json(log_root / "cost_raw.json", draft.model_dump())
        dump_json(log_root / "cost_hits.json", {"queries": deps.queries, "hits": deps.hits})
    dump_json(logs_dir(run_dir) / "search_log_cost.json", calls or deps.queries)
    payload = draft.model_dump()
    payload["ts"] = datetime.now().isoformat(timespec="seconds")
    try:
        card = require_cost(payload, facts)
    except SchemaError as exc:
        raise SystemExit("成本假设未通过规范表:\n  " + "\n  ".join(exc.errors)) from exc
    dump_json(spec_dir(run_dir) / "cost_assumptions.json", card)
    return card


def compile_cost(deps: CostDeps, *, model=None) -> CostDraft:
    chat = model or build_chat_model(api="chat")
    hint = ""
    last_errors: list[str] = []
    for attempt in range(1, 4):
        _log(deps, f"[cost] 写假设 第{attempt}次")
        result = cost_agent.run_sync(_cost_user(deps, hint), deps=deps, model=chat)
        _log_messages(deps, result.all_messages())
        draft = result.output
        last_errors = _cost_try_errors(deps, draft)
        if not last_errors:
            return draft
        _log(deps, "[cost] 不合法：" + "；".join(last_errors))
        hint = "不合法：" + "；".join(last_errors)
    raise SchemaError(last_errors or ["写成本假设失败"])


def _cost_try_errors(deps: CostDeps, draft: CostDraft) -> list[str]:
    try:
        require_cost(draft.model_dump(), deps.facts)
    except SchemaError as exc:
        return list(exc.errors)
    return []


def _cost_user(deps: CostDeps, hint: str) -> str:
    years = "、".join(deps.forecast_periods)
    ratio = "、".join(RATIO_KEYS)
    amount = "、".join(AMOUNT_KEYS)
    text = (
        "rationale 按下面模板写。\n"
        f"{_rationale_template(deps.forecast_periods)}\n"
        f"预测年：{years}\n"
        f"比率字段（小数）：{ratio}\n"
        f"金额字段（亿元）：{amount}\n"
        "每个字段外层写年份。不要把历史年写进这些字段。\n"
        "【已算好的历史轨迹】不要改：\n"
        f"{format_hist_track(deps.hist_track)}\n"
    )
    if deps.briefs_text:
        text += "【分部收入与研究笔记】只当产品结构背景。合并毛利率可写组合/单位盈利/其他，不要拆分部毛利率：\n"
        text += deps.briefs_text + "\n"
    if hint:
        text += hint
    return text


def _load_cost_context(run_dir: Path) -> str:
    return "\n\n".join(item for item in (_load_segment_revenue(run_dir), _load_briefs(run_dir)) if item)


def _load_segment_revenue(run_dir: Path) -> str:
    root = spec_dir(run_dir)
    lines: list[str] = []
    for path in sorted(root.glob("forecast_notes_*.json")):
        card = load_json(path)
        name = str(card.get("segment") or path.stem.replace("forecast_notes_", ""))
        hist = (card.get("historical_data") or {}).get("分部收入") or {}
        fcst = card.get("final_forecast") or {}
        lines.append(f"{name} 锁定历史收入 {hist}；预测驱动 {fcst}")
    if not lines:
        return ""
    return "【已落盘分部收入】只作合并毛利率背景，不要据此另写分部毛利率：\n" + "\n".join(lines)


def _load_briefs(run_dir: Path) -> str:
    root = spec_dir(run_dir)
    blocks: list[str] = []
    for path in sorted(root.glob("research_brief_*.json")):
        brief = load_json(path)
        name = str(brief.get("segment") or path.stem.replace("research_brief_", ""))
        blocks.append(f"—— {name} ——\n{format_brief_for_forecast(brief)}")
    return "\n\n".join(blocks)


def _cost_hits_view(hits: list[dict[str, Any]], company: str, limit: int = 12) -> list[dict[str, Any]]:
    def score(hit: dict[str, Any]) -> tuple:
        text = f"{hit.get('title') or ''} {hit.get('snippet') or ''}"
        named = 1 if company and company in text else 0
        costed = 1 if any(word in text for word in _COST_HINTS) else 0
        has_num = 1 if re.search(r"\d", text) else 0
        inst = 1 if hit.get("institution") else 0
        return (costed, named, has_num, inst, str(hit.get("date") or ""))

    ranked = sorted(hits, key=score, reverse=True)
    view: list[dict[str, Any]] = []
    for hit in ranked[:limit]:
        view.append(
            {
                "title": hit.get("title"),
                "institution": hit.get("institution"),
                "date": hit.get("date"),
                "content_type": hit.get("content_type"),
                "peek": _peek_cost(str(hit.get("snippet") or ""), company),
            }
        )
    return view


def _peek_cost(text: str, company: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    anchors = [company, *_COST_HINTS]
    for name in anchors:
        if name and name in cleaned:
            at = cleaned.find(name)
            start = max(0, at - 80)
            return cleaned[start : start + PEEK]
    return cleaned[:PEEK]


def _start_from_hist(facts: dict) -> str:
    for period in facts.get("hist_periods") or []:
        year = str(period)[:4]
        if year.isdigit():
            return f"{int(year) - 1}-01-01"
    return f"{datetime.now().year - 3}-01-01"
