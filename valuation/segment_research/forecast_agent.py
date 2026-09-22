"""forecast_agent：只读底稿，作为分析师给出我们的预测。不再检索。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from valuation.segment_research.methods import FORECAST_FIELDS, FX_METHODS, HIST_FIELDS, QTY_METHODS
from valuation.segment_research.schema import METHOD_PICK_RULES, SchemaError, require_forecast
from valuation.segment_split.schema import FALLBACK_METHOD, METHOD_ORDER, canonical_method
from valuation.segment_split.split_agent import _log, _log_messages
from valuation.segment_split.split_plan import is_residual_name
from valuation.shared.env import build_chat_model
from valuation.shared.io import dump_json, load_json, spec_dir


class ForecastDraft(BaseModel):
    method: str = Field(
        default="",
        description="预测方法白名单之一。残差由代码指定为收入增速法",
    )
    why_method: str = Field(
        default="",
        description="为何用这个方法。量价写清哪侧有搜证、哪侧用锁定收入回推",
    )
    historical_data: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description="外层是方法字段，内层是年份。收入增速法如 {\"分部收入\": {\"2023A\": 3311}}",
    )
    final_forecast: dict[str, dict[str, float]] = Field(
        description="外层是方法字段，内层是年份。收入增速法如 {\"收入增速\": {\"2026E\": 0.18}}，用小数"
    )
    final_rationale: str = Field(
        description="按理由模板三段写：事件发酵、卖方假设、我们的数"
    )
    open_gaps: list[str] = Field(default_factory=list)
    sources: list[dict[str, str]] = Field(default_factory=list)
    unit_meta: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ForecastDeps:
    company: str
    ticker: str
    segment: str
    brief: dict
    notes_seg: dict
    facts: dict
    method: str = ""
    verbose: bool = True

    @property
    def label(self) -> str:
        return f"{self.company}（{self.ticker}）/{self.segment}"

    @property
    def forecast_periods(self) -> list[str]:
        return [str(year) for year in (self.facts.get("forecast_periods") or [])]


def _rationale_template(years: list[str]) -> str:
    ev = "\n".join(
        (
            f"{year}：{{哪件事、何时开始贡献、先改量还是价还是结构}}。只写会进本分部该年驱动的事。"
            if i == 0
            else f"{year}：…"
        )
        for i, year in enumerate(years)
    ) or "（按预测年逐行写）"
    street = "\n".join(
        (
            f"{year}：{{机构}} {{报告日}} {{目标期}} {{数字}}，假设是{{…}}。下一家同理。只写对本分部该指标有假设的机构。"
            if i == 0
            else f"{year}：…"
        )
        for i, year in enumerate(years)
    )
    ours = "\n".join(
        (
            f"{year}：{{数字}}。{{量、价或结构各自怎么变，怎么走到这个数}}。"
            if i == 0
            else f"{year}：…"
        )
        for i, year in enumerate(years)
    )
    return (
        "【事件发酵】\n"
        f"{ev}\n"
        "【卖方假设】\n"
        f"{street}\n"
        "没有材料的年份只写「年份：」，后面留空。\n"
        "【我们的数】\n"
        f"{ours}\n"
        "卖方假设只作对照。我们的数写本分部该年的量、价或结构路径。"
        "没有事件可推的年份，写回落或外推。"
        "材料只写会进本分部该指标的事实。"
    )


def _field_map() -> str:
    lines = []
    for method in METHOD_ORDER:
        hist = "、".join(HIST_FIELDS.get(method, ()))
        fcst = "、".join(FORECAST_FIELDS.get(method, ()))
        lines.append(f"{method}：historical_data 写 {hist}；final_forecast 写 {fcst}。")
    return (
        "字段名用销量、单价这些汉字。"
        + "".join(lines)
        + "量价对账：每年 销量×单价/unit_meta.fx = 锁定分部收入。"
        "有搜证的一侧留下，另一侧用收入回推到对得上。"
        "量价、用户单价、门店坪效须自报 unit_meta.fx。"
        "渗透、订单没有 fx。量价增速法的预测年写销量增速、单价增速，用小数。"
    )


def _driver_hint(method: str, hist_need: str) -> str:
    if method in QTY_METHODS:
        return (
            "历史销量×单价/fx 对上已锁定的分部收入。"
            "先用笔记里已实现的量或价；缺的一侧用锁定收入回推。"
            "unit_meta.fx 自报。两侧都没有，缺口写进 open_gaps，仍须交出能对账的数。"
        )
    if method in FX_METHODS:
        return (
            f"历史驱动（{hist_need}）从笔记的已实现里拆。"
            "有的就用，缺的用锁定收入回推。unit_meta.fx 自报。"
            "笔记没有的，缺口写进 open_gaps。"
        )
    if method != "收入增速法":
        return (
            f"历史驱动（{hist_need}）从笔记的已实现里拆。"
            "有的就用，缺的用锁定收入回推。笔记没有的，缺口写进 open_gaps。"
        )
    return ""


def _forecast_instructions(ctx: RunContext[ForecastDeps]) -> str:
    deps = ctx.deps
    methods = "、".join(METHOD_ORDER)
    if deps.method:
        hist_need = "、".join(HIST_FIELDS.get(deps.method, ()))
        fcst_need = "、".join(FORECAST_FIELDS.get(deps.method, ()))
        extra = _driver_hint(deps.method, hist_need)
        pick = (
            f"预测方法已指定为{deps.method}。"
            f"历史驱动要写：{hist_need}。未来要写：{fcst_need}。{extra}"
        )
    else:
        pick = (
            f"按下面规则自己选方法，写出 method 和 why_method，再按该方法填数。"
            f"方法属于：{methods}。{METHOD_PICK_RULES}"
            f"{_field_map()}"
        )
    return (
        f"你是{deps.label}的分析师。笔记是材料，用锁定分部收入对账。\n"
        f"{pick}\n"
        "historical_data 只写历史年。final_rationale 按模板写；卖方假设只作对照，我们的数写量、价或结构路径。\n"
        f"{_rationale_template(deps.forecast_periods)}"
    )


forecast_agent = Agent(
    output_type=ForecastDraft,
    deps_type=ForecastDeps,
    retries=0,
    instructions=_forecast_instructions,
)


def run_live_forecast(run_dir: Path, segment: str, brief: dict) -> dict:
    from valuation.segment_research.brief_agent import _load_segment

    facts, notes_seg = _load_segment(run_dir, segment)
    locked = _forced_method(notes_seg, segment)
    deps = ForecastDeps(
        company=str(facts.get("company") or brief.get("company") or ""),
        ticker=str(facts.get("ticker") or brief.get("ticker") or ""),
        segment=segment,
        method=locked,
        brief=brief,
        notes_seg=notes_seg,
        facts=facts,
    )
    draft = compile_forecast(deps)
    payload = _forecast_payload(deps, draft)
    try:
        card = require_forecast(payload, facts, notes_seg=notes_seg)
    except SchemaError as exc:
        raise SystemExit("分部预测未通过规范表:\n  " + "\n  ".join(exc.errors)) from exc
    dump_json(spec_dir(run_dir) / f"forecast_notes_{segment}.json", card)
    write_method_to_notes(
        run_dir,
        segment,
        str(card.get("method") or ""),
        str(draft.why_method or ""),
    )
    return card


def _forced_method(notes_seg: dict, segment: str) -> str:
    parent = str(notes_seg.get("parent") or "")
    if is_residual_name(segment, parent=parent):
        return FALLBACK_METHOD
    return ""


def write_method_to_notes(run_dir: Path, segment: str, method: str, why_method: str = "") -> None:
    path = spec_dir(run_dir) / "segment_split_notes.json"
    notes = load_json(path)
    for seg in notes.get("final_segments") or []:
        if str(seg.get("name") or "").strip() != segment:
            continue
        seg["预测方法"] = method
        if why_method:
            seg["why_method"] = why_method
        break
    dump_json(path, notes)


def compile_forecast(deps: ForecastDeps, *, model=None) -> ForecastDraft:
    chat = model or build_chat_model(api="chat")
    hint = ""
    last_errors: list[str] = []
    for attempt in range(1, 4):
        _log(deps, f"[forecast] 写预测 第{attempt}次")
        result = forecast_agent.run_sync(_forecast_user(deps, hint), deps=deps, model=chat)
        _log_messages(deps, result.all_messages())
        draft = result.output
        if deps.method:
            draft.method = deps.method
        last_errors = _forecast_try_errors(deps, draft)
        if not last_errors:
            return draft
        _log(deps, "[forecast] 不合法：" + "；".join(last_errors))
        hint = _retry_hint(deps, draft, last_errors)
    raise SchemaError(last_errors or ["写预测失败"])


def _forecast_payload(deps: ForecastDeps, draft: ForecastDraft) -> dict:
    payload = draft.model_dump()
    payload["segment"] = deps.segment
    payload["method"] = deps.method or draft.method
    payload["ts"] = datetime.now().isoformat(timespec="seconds")
    return payload


def _forecast_try_errors(deps: ForecastDeps, draft: ForecastDraft) -> list[str]:
    payload = _forecast_payload(deps, draft)
    try:
        require_forecast(payload, deps.facts, notes_seg=deps.notes_seg)
    except SchemaError as exc:
        return list(exc.errors)
    return []


def _forecast_user(deps: ForecastDeps, hint: str) -> str:
    from valuation.segment_research.brief_agent import format_brief_for_forecast

    hist = "、".join(deps.facts["hist_periods"])
    fcst = "、".join(deps.facts["forecast_periods"])
    rev = deps.notes_seg.get("historical_revenue") or {}
    if deps.method:
        hist_need = "、".join(HIST_FIELDS.get(deps.method, ()))
        fcst_need = "、".join(FORECAST_FIELDS.get(deps.method, ()))
        method_line = (
            f"方法已指定：{deps.method}。historical_data 外层写 {hist_need}，"
            f"final_forecast 外层写 {fcst_need}，内层写对应年份。"
        )
    else:
        method_line = (
            "先写 method 和 why_method，再按该方法填 historical_data / final_forecast。"
            f"{_field_map()}"
        )
    text = (
        "检索已结束。final_rationale 按下面模板写。\n"
        f"{_rationale_template(deps.forecast_periods)}\n"
        f"{method_line}\n"
        f"历史年：{hist}  预测年：{fcst}\n"
        f"已锁定分部收入：{json.dumps(rev, ensure_ascii=False)}\n"
        f"{format_brief_for_forecast(deps.brief)}\n"
    )
    if hint:
        text += hint
    return text


def _retry_hint(deps: ForecastDeps, draft: ForecastDraft, errors: list[str]) -> str:
    method = canonical_method(deps.method or draft.method)
    hist_need = "、".join(HIST_FIELDS.get(method, ())) or "先选白名单方法"
    fcst_need = "、".join(FORECAST_FIELDS.get(method, ())) or "先选白名单方法"
    extra = ""
    if method in QTY_METHODS:
        extra = (
            "字段名是销量、单价。"
            "每年销量×单价/unit_meta.fx 等于锁定分部收入；"
            "有搜证的一侧留下，另一侧用收入回推。unit_meta.fx 自报。"
        )
        if method == "量价增速法":
            extra += "final_forecast 写销量增速、单价增速，用小数。"
    elif method in FX_METHODS:
        extra = "unit_meta.fx 自报，和单位对得上。"
    elif method not in {"", "收入增速法"}:
        extra = "驱动测算对上锁定分部收入。"
    return (
        f"不合法：{'；'.join(errors)}。"
        f"你选了{method}。historical_data 外层只能写 {hist_need}。"
        f"final_forecast 外层只能写 {fcst_need}。{extra}"
    )


def run_live_research(run_dir: Path, segment: str, *, reuse_search: bool = False) -> dict:
    from valuation.segment_research.brief_agent import run_live_brief

    brief = run_live_brief(run_dir, segment, reuse_search=reuse_search)
    card = run_live_forecast(run_dir, segment, brief)
    return card
