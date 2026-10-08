"""研究分析师：一次生成底稿与 Excel 摘要。没有搜索工具。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from valuation.research_dossier.schema import (
    DISCLAIMER,
    require_summary_notes,
    validate_dossier,
)
from valuation.segment_split.split_agent import _log, _log_messages
from valuation.shared.env import build_chat_model
from valuation.shared.houses import strip_inline_cites
from valuation.shared.io import load_json, spec_dir

REWRITE = (
    ("【事件发酵】", "行业与订单"),
    ("【卖方假设】", "卖方怎么看"),
    ("【我们的数】", "本模型怎么取"),
    ("final_rationale", "分部说明"),
    ("pe_adjust", "目标PE调整系数"),
    ("open_gaps", "未披露"),
)


class CoverPoint(BaseModel):
    title: str = Field(description="短标题，机制或结构，不超过16字")
    text: str = Field(description="一句带锁定数字的完整论据，必须能在对应底稿章节找到")


class DossierDraft(BaseModel):
    markdown: str = Field(description="完整 Markdown 底稿，含八个中文章节，文末只有一次免责声明")
    thesis: list[CoverPoint] = Field(description="Excel 投资逻辑，取自底稿的投资逻辑或估值与评级")
    outlook: list[CoverPoint] = Field(description="Excel 未来展望，取自底稿的业务展望或盈利预测")
    risks: list[CoverPoint] = Field(description="Excel 主要风险，取自底稿的主要风险")


@dataclass
class AnalystDeps:
    company: str
    ticker: str
    snapshot: dict
    spec: dict
    pack_text: str
    run_dir: Path | None = None
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
    missing_consensus = [
        year
        for year in deps.snapshot.get("forecast_periods") or []
        if year not in (display.get("consensus_revenue") or {})
        or year not in (display.get("consensus_eps") or {})
    ]
    if gaps:
        gap_line = "必须写清这些偏差年：" + "；".join(
            f"{item['year']}{item['metric']}我们{item['direction']}{item['gap_pct']}%"
            for item in gaps
        )
    elif missing_consensus:
        gap_line = "部分期间缺少可比一致预期，不能据此判断模型与市场接近。"
    else:
        gap_line = (
            "各年相对一致预期的绝对偏差都不大于 15%。"
            "投资逻辑里写清哪一年营收或利润略高还是略低。"
        )
    if missing_consensus:
        gap_line += " 缺少可比收入或 EPS 一致预期的年份：" + "、".join(missing_consensus) + "。"
    core = "、".join(deps.snapshot.get("core_segments") or []) or "核心分部"
    tail = "、".join(deps.snapshot.get("tail_segments") or []) or "其余分部"
    fcst_years = "、".join(deps.snapshot.get("forecast_periods") or []) or "预测年"
    houses = "、".join(item.get("name") or "" for item in deps.snapshot.get("bibliography") or []) or "一致预期样本券商"
    return f"""你是{deps.label}的公司研究分析师。根据注入的模型结果和已有研究材料，写一篇能复核关键判断的底稿，并在同一次输出中填写 thesis、outlook、risks。

数字已经锁死。营收、EPS、毛利率、费用率、目标PE、目标价、较现价空间、评级必须用注入的 display 数字。
评级必须是「{display['rating']}」，目标价 {display['target_price']} 元，预测首年 EPS {display['eps_y1']} 元，目标PE {display['target_pe']} 倍。

券商用简称，例如广发证券；只点名材料里确有来源的机构，现有券商包括 {houses}。来源、日期、适用期间只从注入材料取，不要编造原文页码或报告日期。关键的最新实际、机构预测或有争议的事实，在相关判断旁用括号写明材料标题或披露者、资料日期和适用期间；不另列证据台账或数据来源章。材料不足时写出具体待验证事项，不用确定语气补足。
表最多三张：共识对照、盈利简表、同业 PE。营收亿元一位小数，EPS 两位，PE 一位，增速百分数一位。诊断用的同业 PE 中位数不得替代已经锁定的目标 PE。

正文按这个目录，大标题写成「## 一、投资要点」这种中文序号。小标题写主题或分部名，三年路径写在同一段。
1. 投资要点：先写评级、目标价、相对现价空间，再三句为什么是这个判断，数字放进三年模型营收/EPS vs 一致预期的小表。可用 ### 评级与判断。
2. 研究口径与最新经营验证：按注入的 YYYY-MM-DD 原样写资料截止日，并写明预测基期、业务和单位口径；使用已有的最新季度/半年实际，算清全年预测对剩余期间的要求。若没有可比的最新实际，写明具体缺失，不推造。区分公司披露、机构预测和本模型假设。
3. 投资逻辑：必须有 ### 主线、### 分歧归因、### 与市场的分歧、### 目标价口径。主线、分歧和目标价口径用编号分点：每条先写不超过16字的短标题，再写一句带锁定数字的论据。短标题是机制或结构（谁赚钱、哪块放量、和一致预期差在哪、估值口径），一条一个完整判断。分歧归因先写资料时效和口径，再写收入或利润差在哪个分部或利润率；{fcst_years} 的数字写进句子里。{gap_line}
4. 业务展望：按分部设小标题。先 ### 收入结构。核心分部是 {core}，每个核心分部一个 ### 标题，说明量价、订单、产能或其他适用驱动怎样传到收入；尾部分部 {tail} 合成一个 ###。把没有验证的驱动说成假设。
5. 盈利预测：先放一张历史+预测简表，再用 ### 拐点与斜率 按机制写：毛利率、费用率、利润是否快于收入、相对一致预期。区分计算关系与尚未建立桥接的解释。
6. 估值与评级：### 同业选取、### 目标价与评级。同业为什么这几家（业务优先）、目标 PE 取均值（或调整）的理由、目标价是当前合理价格（首年 EPS × 目标 PE），并对照现价隐含 EPS。数字必须等于注入结果。若同业倍数明显分散，明说均值受哪些样本影响，不把高倍数自动删除。
7. 反证与跟踪：必须有 ### 替代解释 和 ### 跟踪指标。至少讨论一项最可能推翻主线的相反解释或材料，说明若成立先修改哪个销量、价格、毛利率或估值假设，再影响哪年 EPS 或目标价；给出下一次可观察指标、时间窗口和触发调整的条件。不要只是重复风险名称。
8. 主要风险：分点写能打穿上述逻辑的判断。每条「短标题。触发情景和对盈利或目标价的方向」。八章写完即止。

小标题写主题，例如主线、云计算、拐点与斜率。买入/观察是结论，理由写业务和盈利。

thesis、outlook、risks 是 Excel 总结页文字：条数按材料重要性确定，每条 {{title, text}}；title 不超过16字，text 为简短的完整句，带关键锁定数字。thesis 只从投资逻辑或估值与评级章节逐字取句，outlook 只从业务展望或盈利预测逐字取句，risks 只从主要风险逐字取句。投资逻辑优先覆盖利润驱动、与一致预期的实质差异和估值依据；未来展望优先覆盖近期兑现条件与中期利润路径；风险写具体触发条件及影响方向。底稿与 Excel 总结不能互相矛盾。不要在总结点里写“数据缺口”。

文末单独引用块，且全文只出现一次：
> {DISCLAIMER}
"""


dossier_agent = Agent(
    output_type=DossierDraft,
    deps_type=AnalystDeps,
    retries=0,
    instructions=_dossier_instructions,
)

def compile_dossier(deps: AnalystDeps, *, model=None) -> tuple[str, dict]:
    chat = model or build_chat_model(api="chat")
    hint = ""
    last_errors: list[str] = []
    for attempt in range(1, 4):
        _log(deps, f"[analyst] 写底稿与总结 第{attempt}次")
        result = dossier_agent.run_sync(_dossier_user(deps, hint), deps=deps, model=chat)
        _log_messages(deps, result.all_messages())
        markdown = _prepare_markdown(result.output.markdown, deps.snapshot)
        try:
            notes = require_summary_notes(
                {
                    "thesis": [point.model_dump() for point in result.output.thesis],
                    "outlook": [point.model_dump() for point in result.output.outlook],
                    "risks": [point.model_dump() for point in result.output.risks],
                    "sources": [],
                }
            )
        except ValueError as exc:
            last_errors = [str(exc)]
            _log(deps, "[analyst] 总结不合法：" + "；".join(last_errors))
            hint = "底稿与总结须一次输出，且总结要有投资逻辑、展望、风险。不合法：" + "；".join(last_errors)
            continue
        last_errors = validate_dossier(markdown, deps.snapshot, notes)
        if last_errors:
            _log(deps, "[analyst] 底稿或总结不合法：" + "；".join(last_errors))
            hint = "不合法：" + "；".join(last_errors)
            continue
        _persist_draft(deps, markdown)
        return markdown, notes
    raise SystemExit("研究底稿未通过校验:\n  " + "\n  ".join(last_errors or ["写底稿失败"]))


def format_pack(snapshot: dict, spec: dict, run_dir: Path | None = None) -> str:
    display = snapshot["display"]
    peer_pes = [float(item["pe_y1"]) for item in snapshot["valuation"].get("core") or []]
    payload = {
        "公司": snapshot["company"],
        "代码": snapshot.get("ticker"),
        "资料截止日": str(snapshot.get("as_of") or (spec.get("facts") or {}).get("as_of") or "")[:10],
        "最近完整财年": snapshot.get("last_actual"),
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
        "同业PE中位数_仅作分散度诊断": round(median(peer_pes), 1) if peer_pes else None,
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
        "核心业务近期材料_直接引自已有brief": _brief_context(snapshot, run_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _brief_context(snapshot: dict, run_dir: Path | None) -> list[dict[str, Any]]:
    if run_dir is None:
        return []
    core = list(snapshot.get("core_segments") or [])
    last_actual = snapshot.get("last_actual")
    last_forecast = (snapshot.get("forecast_periods") or [None])[-1]

    def increment(item: dict) -> float:
        revenue = item.get("revenue") or {}
        start = float(revenue.get(last_actual) or 0)
        end = float(revenue.get(last_forecast) or 0)
        return max(0.0, end - start)

    all_segments = snapshot.get("segments") or []
    total_increment = sum(increment(item) for item in all_segments) or 1.0
    tails = sorted(
        (item for item in all_segments if item.get("name") not in core),
        key=lambda item: -max(
            float(item.get("share_latest") or 0), increment(item) / total_increment
        ),
    )
    names = core + [item["name"] for item in tails[:2]]
    out: list[dict[str, Any]] = []
    for name in dict.fromkeys(names):
        path = spec_dir(run_dir) / f"research_brief_{name}.json"
        if not path.is_file():
            continue
        brief = load_json(path)
        facts = [item for item in brief.get("facts") or [] if isinstance(item, dict)]
        sellside = [item for item in brief.get("sellside") or [] if isinstance(item, dict)]
        events = [item for item in brief.get("event_timeline") or [] if isinstance(item, dict)]
        out.append(
            {
                "分部": name,
                "关键发现": (brief.get("key_findings") or [])[:3],
                "近期实际": [
                    _select(item, "period", "metric", "value", "yoy", "what", "house", "as_of", "source_title", "source_ref", "source_type")
                    for item in facts[-4:]
                ],
                "机构预测": [
                    _select(item, "house", "as_of", "horizon", "metric", "value", "view", "source_title", "source_ref", "source_type")
                    for item in sorted(sellside, key=lambda item: str(item.get("as_of") or ""), reverse=True)[:3]
                ],
                "近期事件": [
                    _select(item, "date", "event", "why_it_matters", "house", "source_title", "source_ref", "source_type")
                    for item in events[-2:]
                ],
                "降低权重的材料": (brief.get("excluded") or [])[:1],
                "仍需核实": (brief.get("gaps") or [])[:2],
            }
        )
    return out


def _select(row: dict, *keys: str) -> dict[str, Any]:
    return {key: row[key] for key in keys if row.get(key) not in (None, "")}


def _dossier_user(deps: AnalystDeps, hint: str) -> str:
    text = (
        "用下面这份材料写底稿。数字已经锁死。\n"
        f"{deps.pack_text}\n"
    )
    if hint:
        text += "\n" + hint
    return text


def _persist_draft(deps: AnalystDeps, markdown: str) -> None:
    if not deps.run_dir:
        return
    from valuation.shared.io import dossier_path

    path = dossier_path(deps.run_dir, deps.company)
    draft = path.with_name("research_dossier.draft.md")
    draft.write_text(markdown, encoding="utf-8")


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
