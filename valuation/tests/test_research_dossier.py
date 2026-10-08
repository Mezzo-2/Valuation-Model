from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from valuation.research_dossier.analyst_agent import (
    AnalystDeps,
    CoverPoint,
    DossierDraft,
    _brief_context,
    _dossier_instructions,
    compile_dossier,
    prepare_dossier,
)
from valuation.research_dossier.schema import (
    require_summary_notes,
    validate_dossier,
)


THESIS = "2026E核心业务收入达到100.0亿元，订单转化是主要增长来源。"
OUTLOOK = "2026E交付达到计划时，归母利润预计为20.0亿元。"
RISK = "若订单验收延后，2026E收入100.0亿元预测需下修。"


def _draft() -> DossierDraft:
    logic = "公司需要先完成交付和验收，才能把订单转化为收入；这也是判断增长持续性的关键。" * 10
    outlook = "产能、交付和客户验收共同决定收入确认节奏；费用与成本仍需用季度结果检验。" * 15
    counter = "另一种解释是订单仍在增加，但验收时间比预测更晚，使收入和利润跨期确认。" * 4
    markdown = f"""# 样例公司研究底稿

## 一、投资要点
维持观察评级，目标价20.00元；2026E EPS为2.00元。

## 二、研究口径与最新经营验证
资料截止日为2026-09-24。最近完整财年为2025A；最新季度数据尚待核实。

## 三、投资逻辑
### 主线
1. 订单转化。{THESIS}
{logic}
### 分歧归因
2026E收入判断与市场不同。
### 与市场的分歧
模型更关注验收节奏。
### 目标价口径
目标价采用2026E EPS。

## 四、业务展望
### 收入结构
业务A为核心。
### 业务A
{OUTLOOK}
{outlook}

## 五、盈利预测
2026E归母利润预计为20.0亿元。

## 六、估值与评级
### 同业选取
同业按业务相近程度选择。
### 目标价与评级
2026E EPS为2.00元，目标PE为10.0倍。

## 七、反证与跟踪
### 替代解释
{counter}
若这一解释成立，先下调2026E收入，再重算EPS与目标价。
### 跟踪指标
下次季度报告核对验收金额和交付数量；低于模型要求时调整预测。

## 八、主要风险
1. 验收推迟。{RISK}
"""
    return DossierDraft(
        markdown=markdown,
        thesis=[CoverPoint(title="订单转化", text=THESIS)],
        outlook=[CoverPoint(title="交付兑现", text=OUTLOOK)],
        risks=[CoverPoint(title="验收推迟", text=RISK)],
    )


def _snapshot() -> dict:
    return {
        "as_of": "2026-09-24",
        "forecast_periods": ["2026E"],
        "material_gaps": [],
        "display": {"rating": "观察", "target_price": 20.0, "eps_y1": 2.0},
    }


class ResearchDossierTests(unittest.TestCase):
    def test_new_chapters_and_summary_use_same_research_output(self):
        draft = _draft()
        snapshot = _snapshot()
        markdown = prepare_dossier(draft.markdown, snapshot)
        notes = require_summary_notes(draft.model_dump(exclude={"markdown"}))
        self.assertEqual(validate_dossier(markdown, snapshot, notes), [])

        notes["outlook"][0]["text"] = THESIS
        self.assertIn("对应底稿章节", "；".join(validate_dossier(markdown, snapshot, notes)))

    def test_dossier_and_summary_are_generated_in_one_model_call(self):
        draft = _draft()
        deps = AnalystDeps(
            company="样例公司",
            ticker="000001",
            snapshot=_snapshot(),
            spec={},
            pack_text="已核对的模型输入",
            verbose=False,
        )
        result = SimpleNamespace(output=draft, all_messages=lambda: [])
        with patch("valuation.research_dossier.analyst_agent.dossier_agent.run_sync", return_value=result) as run:
            with patch("valuation.research_dossier.analyst_agent._log_messages"):
                markdown, notes = compile_dossier(deps, model=object())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(notes["thesis"][0]["text"], THESIS)
        self.assertIn("## 七、反证与跟踪", markdown)

    def test_brief_context_uses_existing_sources_without_new_ledger(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "research_brief_业务A.json"
            path.write_text(
                json.dumps(
                    {
                        "facts": [
                            {
                                "period": "1H26",
                                "metric": "收入",
                                "value": "50亿元",
                                "as_of": "2026-09-01",
                                "source_title": "半年报",
                                "source_ref": "brief_业务A_1:2",
                            }
                        ],
                        "sellside": [],
                        "event_timeline": [],
                        "excluded": ["旧报告口径不一致"],
                        "gaps": ["缺全年公司指引"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            context = _brief_context(
                {"core_segments": ["业务A"], "segments": [{"name": "业务A", "share_latest": 1}]},
                Path(root),
            )
        self.assertEqual(context[0]["近期实际"][0]["source_ref"], "brief_业务A_1:2")
        self.assertEqual(context[0]["降低权重的材料"], ["旧报告口径不一致"])
        self.assertEqual(context[0]["仍需核实"], ["缺全年公司指引"])

    def test_missing_consensus_is_not_described_as_a_small_gap(self):
        snapshot = _snapshot()
        snapshot["display"].update(
            {"target_pe": 10.0, "consensus_revenue": {}, "consensus_eps": {}}
        )
        deps = AnalystDeps("样例公司", "000001", snapshot, {}, "", verbose=False)
        instructions = _dossier_instructions(SimpleNamespace(deps=deps))
        self.assertIn("缺少可比收入或 EPS 一致预期的年份：2026E", instructions)
        self.assertNotIn("各年相对一致预期的绝对偏差都不大于 15%", instructions)

    def test_small_current_segment_with_large_forecast_increment_is_included(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("核心", "成熟", "新增", "其他"):
                (Path(root) / f"research_brief_{name}.json").write_text("{}", encoding="utf-8")
            snapshot = {
                "core_segments": ["核心"],
                "last_actual": "2025A",
                "forecast_periods": ["2026E"],
                "segments": [
                    {"name": "核心", "share_latest": 0.69, "revenue": {"2025A": 69, "2026E": 89}},
                    {"name": "成熟", "share_latest": 0.20, "revenue": {"2025A": 20, "2026E": 20}},
                    {"name": "新增", "share_latest": 0.01, "revenue": {"2025A": 1, "2026E": 101}},
                    {"name": "其他", "share_latest": 0.10, "revenue": {"2025A": 10, "2026E": 10}},
                ],
            }
            names = [item["分部"] for item in _brief_context(snapshot, Path(root))]
        self.assertEqual(names, ["核心", "新增", "成熟"])


if __name__ == "__main__":
    unittest.main()
