"""研究分析师：先写底稿，再只从底稿抽封面。没有搜索工具。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from valuation.research_dossier.schema import (
    CHAPTERS,
    DISCLAIMER,
    require_summary_notes,
    validate_dossier,
)
from valuation.segment_split.split_agent import _log, _log_messages
from valuation.shared.env import build_chat_model
from valuation.shared.houses import strip_inline_cites

REWRITE = (
    ("【事件发酵】", "行业与订单"),
    ("【卖方假设】", "卖方怎么看"),
    ("【我们的数】", "本模型怎么取"),
    ("final_rationale", "分部说明"),
    ("pe_adjust", "目标PE调整系数"),
    ("open_gaps", "未披露"),
)


class DossierDraft(BaseModel):
    markdown: str = Field(description="完整 Markdown 底稿，含六个中文章节，文末只有一次免责声明")


class CoverPoint(BaseModel):
    title: str = Field(description="短标题，机制或结构，不超过16字")
    text: str = Field(description="一句带锁定数字的完整论据，必须能在底稿里找到")


class CoverDraft(BaseModel):
    thesis: list[CoverPoint] = Field(description="投资逻辑分点：短标题 + 论据")
    outlook: list[CoverPoint] = Field(description="未来展望分点：短标题 + 论据")
    risks: list[CoverPoint] = Field(description="主要风险分点：短标题 + 论据")


@dataclass
class AnalystDeps:
    company: str
    ticker: str
    snapshot: dict
    spec: dict
    pack_text: str
    run_dir: Any | None = None
    verbose: bool = True

    @property
    def label(self) -> str:
        if self.company and self.ticker:
            return f"{self.company}（{self.ticker}）"
        return self.company or self.ticker


def _dossier_instructions(ctx: RunContext[AnalystDeps]) -> str:
    deps = ctx.deps
    display = deps.snapshot["display"]
    gaps = display.get("material_gaps") or []
    if gaps:
        gap_line = "必须写清这些偏差年：" + "；".join(
            f"{item['year']}{item['metric']}我们{item['direction']}{item['gap_pct']}%"
            for item in gaps
        )
    else:
        gap_line = (
            "各年相对一致预期的绝对偏差都不大于 15%。"
            "投资逻辑里写清哪一年营收或利润略高还是略低。"
        )
    core = "、".join(deps.snapshot.get("core_segments") or []) or "核心分部"
    tail = "、".join(deps.snapshot.get("tail_segments") or []) or "其余分部"
    fcst_years = "、".join(deps.snapshot.get("forecast_periods") or []) or "预测年"
    houses = "、".join(item.get("name") or "" for item in deps.snapshot.get("bibliography") or []) or "一致预期样本券商"
    return f"""你是{deps.label}的卖方公司点评分析师。根据注入的模型结果写一篇有立场的点评。

数字已经锁死。营收、EPS、毛利率、费用率、目标PE、目标价、较现价空间、评级必须用注入的 display 数字。
评级必须是「{display['rating']}」，目标价 {display['target_price']} 元，预测首年 EPS {display['eps_y1']} 元，目标PE {display['target_pe']} 倍。

券商用简称，例如广发证券。正文只点名 {houses}，文末只留一次免责声明。
表最多三张：共识对照、盈利简表、同业 PE。营收亿元一位小数，EPS 两位，PE 一位，增速百分数一位。

正文按这个目录，大标题写成「## 一、投资要点」这种中文序号。小标题写主题或分部名，三年路径写在同一段。
1. 投资要点：先写评级、目标价、相对现价空间，再三句为什么是这个判断，数字放进三年模型营收/EPS vs 一致预期的小表。可用 ### 评级与判断。
2. 投资逻辑：必须有 ### 主线、### 分歧归因、### 与市场的分歧、### 目标价口径。主线、分歧和目标价口径用编号分点：每条先写不超过16字的短标题，再写一句带锁定数字的论据。短标题是机制或结构（谁赚钱、哪块放量、和一致预期差在哪、估值口径），一条一个完整判断。分歧归因先写资料时效和口径，再写收入或利润差在哪个分部或利润率；{fcst_years} 的数字写进句子里。{gap_line}
3. 业务展望：按分部设小标题。先 ### 收入结构。核心分部是 {core}，每个核心分部一个 ### 标题，下面用同样的「短标题 + 一句带数字的三年路径」。尾部分部 {tail} 合成一个 ###。
4. 盈利预测：先放一张历史+预测简表，再用 ### 拐点与斜率 按机制写：毛利率、费用率、利润是否快于收入、相对一致预期。
5. 估值与评级：### 同业选取、### 目标价与评级。同业为什么这几家（业务优先）、目标 PE 取均值（或调整）的理由、目标价是当前合理价格（首年 EPS × 目标 PE），并对照现价隐含 EPS。数字必须等于注入结果。
6. 主要风险：分点写能打穿上述逻辑的判断。每条「短标题。触发情景和对盈利或目标价的方向」。六章写完即止。

小标题写主题，例如主线、云计算、拐点与斜率。买入/观察是结论，理由写业务和盈利。

文末单独引用块，且全文只出现一次：
> {DISCLAIMER}
"""


dossier_agent = Agent(
    output_type=DossierDraft,
    deps_type=AnalystDeps,
    retries=0,
    instructions=_dossier_instructions,
)

cover_agent = Agent(
    output_type=CoverDraft,
    deps_type=AnalystDeps,
    retries=0,
    instructions=(
        "你只读已经写好的研究底稿，抽出 Excel 总结页要用的封面字段。"
        "thesis、outlook、risks 都写成 {title, text} 列表。条数按材料来。"
        "title 是机制短标题，不超过16字；text 是一句带锁定数字的完整论据，必须能在底稿里找到。"
        "按判断拆点，不要按句号把一段话切成碎句。"
        "thesis 从投资逻辑抽：谁赚钱、哪块弹性、和一致预期差在哪、目标价怎么取。"
        "outlook 从业务展望或盈利预测抽：按分部或毛利率机制。"
        "risks 从主要风险抽：保留风险名和对盈利或目标价的方向。"
        f"章节名是：{'、'.join(CHAPTERS)}。"
    ),
)


def compile_dossier(deps: AnalystDeps, *, model=None) -> tuple[str, dict]:
    chat = model or build_chat_model(api="chat")
    hint = ""
    last_errors: list[str] = []
    markdown = ""
    notes: dict[str, Any] = {}
    for attempt in range(1, 4):
        _log(deps, f"[analyst] 写底稿 第{attempt}次")
        result = dossier_agent.run_sync(_dossier_user(deps, hint), deps=deps, model=chat)
        _log_messages(deps, result.all_messages())
        markdown = _prepare_markdown(result.output.markdown, deps.snapshot)
        last_errors = validate_dossier(markdown, deps.snapshot)
        if last_errors:
            _log(deps, "[analyst] 底稿不合法：" + "；".join(last_errors))
            hint = "不合法：" + "；".join(last_errors)
            continue
        _persist_draft(deps, markdown)
        notes, cover_errors = _extract_cover(deps, markdown, chat)
        if notes and not cover_errors:
            return markdown, notes
        last_errors = cover_errors
        _log(deps, "[analyst] 封面不合法：" + "；".join(last_errors))
        raise SystemExit("研究底稿未通过轻校验:\n  " + "\n  ".join(last_errors or ["抽封面失败"]))
    raise SystemExit("研究底稿未通过轻校验:\n  " + "\n  ".join(last_errors or ["写底稿失败"]))


def _extract_cover(deps: AnalystDeps, markdown: str, chat) -> tuple[dict, list[str]]:
    from pydantic_ai.exceptions import ModelHTTPError

    from valuation.research_dossier.schema import validate_cover_against

    hint = ""
    last_errors: list[str] = []
    notes: dict[str, Any] = {}
    models = [chat]
    try:
        alt = build_chat_model()
        if alt is not chat:
            models.append(alt)
    except Exception:
        pass
    for model in models:
        for attempt in range(1, 3):
            _log(deps, f"[analyst] 抽封面 第{attempt}次")
            user = _cover_user(markdown)
            if hint:
                user += "\n" + hint
            try:
                cover = cover_agent.run_sync(user, deps=deps, model=model)
            except ModelHTTPError as exc:
                _log(deps, f"[analyst] 抽封面 HTTP {exc.status_code}，缩短输入或换接口")
                last_errors = [f"抽封面失败: {exc.status_code}"]
                continue
            _log_messages(deps, cover.all_messages())
            try:
                notes = require_summary_notes(cover.output.model_dump())
                notes["sources"] = []
            except Exception as exc:
                last_errors = [str(exc)]
                hint = "封面不合法：" + "；".join(last_errors)
                continue
            last_errors = validate_cover_against(markdown, notes)
            if not last_errors:
                return notes, []
            hint = "每条写短标题加一句带数字的论据，按判断拆点。不合法：" + "；".join(last_errors)
    notes = require_summary_notes(_fallback_cover(markdown, deps.snapshot))
    last_errors = validate_cover_against(markdown, notes)
    if not last_errors:
        _log(deps, "[analyst] 抽封面改用底稿原句压缩")
        return notes, []
    return notes, last_errors


def format_pack(snapshot: dict, spec: dict) -> str:
    display = snapshot["display"]
    payload = {
        "公司": snapshot["company"],
        "代码": snapshot.get("ticker"),
        "公司档案": spec.get("company_info") or {},
        "必须使用的结论数字": {
            "评级": display["rating"],
            "目标价_元": display["target_price"],
            "现价_元": display["price"],
            "较现价空间_百分数": display["upside_pct"],
            "目标PE": display["target_pe"],
            "核心池PE均值": display["pe_mean"],
            "预测首年": snapshot["y1"],
            "预测首年EPS": display["eps_y1"],
            "目标PE是否取均值不调整": snapshot["valuation"].get("pe_adjust_is_one"),
        },
        "核心分部": snapshot.get("core_segments"),
        "尾部分部": snapshot.get("tail_segments"),
        "分部收入_亿元一位": display["segment_revenue"],
        "盈利简表": {
            year: {
                "营收": display["revenue"].get(year),
                "同比_百分数": display["rev_yoy_pct"].get(year),
                "毛利率_百分数": display["gm_pct"].get(year),
                "销售费用率_百分数": _pct(snapshot["pnl"][year].get("sales_ratio")),
                "管理费用率_百分数": _pct(snapshot["pnl"][year].get("admin_ratio")),
                "研发费用率_百分数": _pct(snapshot["pnl"][year].get("rd_ratio")),
                "归母": _r1(snapshot["pnl"][year].get("ni")),
                "EPS": display["eps"].get(year),
            }
            for year in snapshot["hist_periods"] + snapshot["forecast_periods"]
        },
        "一致预期": {
            year: {
                "营收": display["consensus_revenue"].get(year),
                "EPS": display["consensus_eps"].get(year),
                "营收偏差_百分数": display["rev_gap_pct"].get(year),
                "EPS偏差_百分数": display["eps_gap_pct"].get(year),
            }
            for year in snapshot["forecast_periods"]
        },
        "需要解释的偏差": display.get("material_gaps") or [],
        "分歧归因": snapshot.get("attribution") or {},
        "目标价期限": display.get("target_horizon"),
        "现价隐含EPS": display.get("implied_eps"),
        "同业核心池": display.get("core_pe"),
        "同业选取说明": _rewrite(snapshot.get("comps_rationale") or ""),
        "拆分逻辑": snapshot.get("split_logic") or "",
        "成本怎么取": _rewrite(snapshot.get("cost_rationale") or ""),
        "各分部说明": [
            {
                "名称": item["name"],
                "方法": item["method"],
                "最近一年占比说明写进核心或尾部即可": item["name"] in (snapshot.get("core_segments") or []),
                "说明": _rewrite(item.get("rationale") or item.get("note") or ""),
            }
            for item in snapshot["segments"]
        ],
        "可用券商": [item.get("name") for item in snapshot.get("bibliography") or snapshot.get("sources") or []],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _dossier_user(deps: AnalystDeps, hint: str) -> str:
    text = (
        "用下面这份材料写底稿。数字已经锁死。\n"
        f"{deps.pack_text}\n"
    )
    if hint:
        text += "\n" + hint
    return text


def _cover_user(markdown: str) -> str:
    from valuation.research_dossier.schema import CHAPTERS, _chapter

    chunks = []
    for title in CHAPTERS:
        body = (_chapter(markdown, title) or "").strip()
        cap = 2200 if title in {"投资逻辑", "业务展望", "盈利预测"} else 1400
        if len(body) > cap:
            body = body[:cap]
        chunks.append(f"## {title}\n{body}")
    return "只根据下面这篇底稿抽封面。每条 {title, text}，短标题是机制，正文是带数字的论据：\n\n" + "\n\n".join(chunks)


def _persist_draft(deps: AnalystDeps, markdown: str) -> None:
    if not deps.run_dir:
        return
    from valuation.shared.io import dossier_path

    path = dossier_path(deps.run_dir, deps.company)
    draft = path.with_name("research_dossier.draft.md")
    draft.write_text(markdown, encoding="utf-8")


def _fallback_cover(markdown: str, snapshot: dict) -> dict:
    from valuation.research_dossier.schema import _as_point, _chapter

    def points(title: str) -> list[dict[str, str]]:
        rows = []
        for raw in re.split(r"\n+", _chapter(markdown, title)):
            line = raw.strip()
            if not line or line.startswith("|") or line.startswith("#") or line.startswith(">"):
                continue
            point = _as_point(line)
            if point and (point["title"] or point["text"]):
                rows.append(point)
        return rows

    logic = points("投资逻辑")
    outlook = points("业务展望") or points("盈利预测")
    risks = points("主要风险")
    thesis = [item for item in logic if any(word in item["text"] for word in ("分歧", "主线", "收入", "目标价"))]
    if not thesis:
        thesis = logic
    if not any("目标价" in item["text"] or "目标PE" in item["text"] for item in thesis):
        thesis.extend(item for item in points("估值与评级") if "目标价" in item["text"])
    return {
        "thesis": thesis,
        "outlook": outlook,
        "risks": risks,
        "sources": [],
    }


def prepare_dossier(text: str, snapshot: dict) -> str:
    markdown = strip_inline_cites(_strip_fence(text))
    return _drop_sources(markdown)


def _prepare_markdown(text: str, snapshot: dict) -> str:
    return prepare_dossier(text, snapshot)


def _drop_sources(markdown: str) -> str:
    text = re.sub(
        r"(?:\n)?#+\s*(?:[一二三四五六七八九十0-9]+[、.．]?\s*)?数据来源\b[\s\S]*\Z",
        "",
        markdown,
    )
    text = text.replace(f"> {DISCLAIMER}", "").rstrip()
    text = re.sub(rf"(?:^|\n){re.escape(DISCLAIMER)}\s*$", "", text).rstrip()
    return text + f"\n\n> {DISCLAIMER}\n"


def _strip_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:markdown|md)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()
    if not raw.endswith("\n"):
        raw += "\n"
    return raw


def _rewrite(text: str) -> str:
    out = text or ""
    for old, new in REWRITE:
        out = out.replace(old, new)
    out = re.sub(r"\bthin\b", "样本不足", out)
    return out.strip()


def _pct(value: object) -> float | None:
    if value is None:
        return None
    return round(float(value) * 100, 1)


def _r1(value: object) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)
