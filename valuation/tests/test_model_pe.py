"""独立PE判断、旧任务复现及三情景公式的回归。"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from valuation.peer_valuation.context import load_operating_context, operating_context
from valuation.peer_valuation.fetch import _fill_one
from valuation.peer_valuation.judgment import resolve_model_pe
from valuation.peer_valuation.peer_agent import (
    ClassifyDeps, ClassifyDraft, CorePick, _run_classify,
    read_valuation_research, search_valuation_research,
)
from valuation.peer_valuation.schema import require_comps
from valuation.research_dossier.analyst_agent import AnalystDeps, _dossier_instructions, format_pack
from valuation.research_dossier.snapshot import snapshot_from_spec
from valuation.segment_split.schema import SchemaError
from valuation.shared.io import dump_json
from valuation.shared.markets import register_labels, valuation_factor
from valuation.tests.test_foreign_financials import integration_spec
from valuation.workbook.anchors import find_row_by_label
from valuation.workbook.build import build_workbook
from valuation.workbook.certify import _relative_valuation_errors, certify_workbook
from valuation.workbook.styles import INPUT_BLUE, MULTIPLE_FMT


REASON = "主要参考品牌及音频业务相近的同业，结合本公司盈利增长和风险取30倍；高倍数样本仍保留作业务观察。"


def case(market="HK"):
    spec = deepcopy(integration_spec("HK" if market == "A" else market))
    if market == "A":
        spec["facts"].update(market_type="A", ticker="300001")
        income = spec["facts"]["income"]
        for key in ("营业外收入", "营业外支出"):
            income[key] = [0. if value is None else value for value in income[key]]
        income["税前利润"] = [op + inc - exp for op, inc, exp in zip(
            income["营业利润"], income["营业外收入"], income["营业外支出"])]
        for key in ("fin_exp", "nonop_inc", "nonop_exp", "other_op"):
            spec["cost"].setdefault(key, {year: 0. for year in spec["facts"]["forecast_periods"]})
    spec["comps"].update(schema_version=2, model_pe=30., model_pe_reason=REASON)
    for item, pe in zip(spec["comps"]["core"], (14.54, 21.25, 145.75, 460.72)):
        item["pe_y1"] = pe
    return spec


def draft(spec, **extra):
    return ClassifyDraft(
        core=[CorePick(ticker=i["ticker"], note=i["note"]) for i in spec["comps"]["core"]],
        model_pe=30., model_pe_reason=REASON, rationale="业务可比，高倍数保留；模型结合基本面独立取值。",
        **extra,
    )


class ModelPEValidationTests(unittest.TestCase):
    def test_new_contract_is_preserved_and_legacy_is_explicit(self):
        spec = case()
        card = require_comps(spec["comps"], spec["facts"], require_model_pe=True)
        self.assertEqual(resolve_model_pe(card), (30., REASON, True))
        legacy = integration_spec("HK")["comps"]
        value, reason, independent = resolve_model_pe(legacy)
        self.assertEqual(value, 11.5)
        self.assertFalse(independent)
        self.assertIn("历史任务", reason)
        with self.assertRaises(SchemaError):
            require_comps(legacy, spec["facts"], require_model_pe=True)

    def test_invalid_or_missing_new_pe_never_falls_back(self):
        spec = case()
        for value in (None, True, 0, -10, float("nan"), float("inf"), "30"):
            with self.subTest(value=value):
                bad = {**spec["comps"], "model_pe": value}
                with self.assertRaises(ValueError):
                    resolve_model_pe(bad)
                with self.assertRaises(SchemaError):
                    require_comps(bad, spec["facts"])
        for missing in ("model_pe", "model_pe_reason"):
            bad = dict(spec["comps"])
            del bad[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                resolve_model_pe(bad)
        with self.assertRaises(ValueError):
            resolve_model_pe({"schema_version": 2, "core": spec["comps"]["core"]})
        with self.assertRaises(ValueError):
            resolve_model_pe({**spec["comps"], "model_pe_reason": "  "})

    def test_snapshot_uses_model_pe_despite_extreme_peer_and_manual_coefficient(self):
        for market in ("A", "HK", "US"):
            spec = case(market)
            spec["comps"]["pe_adjust"] = 1.1
            before = snapshot_from_spec(spec)
            spec["comps"]["core"][-1]["pe_y1"] = 9999.
            after = snapshot_from_spec(spec)
            with self.subTest(market=market):
                self.assertNotEqual(before["valuation"]["pe_mean"], after["valuation"]["pe_mean"])
                self.assertEqual(after["valuation"]["target_pe"], 33.)
                self.assertEqual(before["valuation"]["target_price"], after["valuation"]["target_price"])
                expected = 33 * after["valuation"]["eps_y1"] * valuation_factor(spec["facts"])
                self.assertAlmostEqual(after["valuation"]["target_price"], expected)
                pack = json.loads(format_pack(after, spec))
                self.assertEqual(pack["必须使用的结论数字"]["模型给定PE"], 30.)
                self.assertEqual(pack["模型给定PE依据"], REASON)
                self.assertEqual(pack["必须使用的结论数字"]["PE基础来源"], "模型独立判断")

    def test_ads_conversion_happens_once(self):
        spec = case("US")
        before = snapshot_from_spec(spec)["valuation"]["target_price"]
        spec["facts"]["market"]["每交易单位普通股数"] = 2
        after = snapshot_from_spec(spec)["valuation"]["target_price"]
        # 相同快照交易单位股数换为更多普通股：EPS分母与价格换算相抵。
        self.assertAlmostEqual(before, after)

    def test_dossier_explains_only_the_actual_valuation_mode(self):
        for spec, expected in (
            (case(), "本次采用估值Agent独立给定的基础PE"),
            (integration_spec("HK"), "本次为旧版任务复现"),
        ):
            snap = snapshot_from_spec(spec)
            deps = AnalystDeps("样例", "TEST", snap, spec, "")
            instructions = _dossier_instructions(SimpleNamespace(deps=deps))
            self.assertIn(expected, instructions)
            self.assertNotIn("历史均值规则", instructions)


class ValuationContextTests(unittest.TestCase):
    def test_operating_summary_uses_same_profit_chain_without_comps(self):
        for market in ("A", "HK", "US"):
            spec = case(market)
            expected = snapshot_from_spec(spec)
            del spec["comps"]
            context = operating_context(spec["facts"], spec["split"], spec["forecasts"], spec["cost"])
            with self.subTest(market=market):
                for year in spec["facts"]["forecast_periods"]:
                    self.assertEqual(context["pnl"][year]["eps"], expected["pnl"][year]["eps"])
                self.assertNotIn("当前股价", json.dumps(context, ensure_ascii=False))

    def test_load_context_does_not_require_comps_or_snapshot_files(self):
        spec = case()
        notes = {"final_segments": [{"name": seg["name"], "parent": "",
                    "historical_revenue": dict(zip(spec["facts"]["hist_periods"], seg["hist_revenue"]))}
                    for seg in spec["split"]["final_segments"]]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dump_json(root / "segment_split_notes.json", notes)
            dump_json(root / "cost_assumptions.json", spec["cost"])
            for name, card in spec["forecasts"].items():
                dump_json(root / f"forecast_notes_{name}.json", card)
            context = load_operating_context(root, spec["facts"])
        self.assertIn("2026E", context["pnl"])

    def test_peer_financials_preserve_period_unit_and_source(self):
        filled = _fill_one({"ticker": "002888", "query": "002888"},
            {"standardizedInfo": {"report_type": "2026Q3", "publishDate": "2026-10-27"},
             "snapshotItems": [{"name_cn": "归母净利润同比", "value": "-83.8", "unit": "%"}]},
            {"valuation": {"peForward": 460.72}})
        financials = filled["financials"]
        self.assertEqual(financials["period"], "2026Q3")
        self.assertEqual(financials["metrics"][0], {"name": "归母净利润同比", "value": -83.8, "unit": "%"})
        self.assertEqual(financials["source"], "get_financial_snapshot")


class ValuationAgentTests(unittest.TestCase):
    def test_new_agent_writes_model_pe_and_initializes_coefficient_once(self):
        spec = case()
        deps = ClassifyDeps("样例", "TEST", spec["comps"]["core"], "2026E", verbose=False)
        result = SimpleNamespace(output=draft(spec), all_messages=lambda: [])
        with patch("valuation.peer_valuation.peer_agent.classify_agent.run_sync", return_value=result):
            card = _run_classify(spec["facts"], deps.filled, None, deps=deps)
        self.assertEqual(card["model_pe"], 30.)
        self.assertEqual(card["pe_adjust"], 1.)
        self.assertEqual(card["schema_version"], 2)

    def test_sources_must_be_read_and_research_budget_is_bounded(self):
        spec = case()
        hit = {"title": "样例经营分析", "institution": "样例机构", "date": "2026-11-01", "snippet": "原片段的盈利与估值分析"}
        deps = ClassifyDeps("样例", "TEST", spec["comps"]["core"], "2026E",
                            verbose=False, search=lambda *args: [hit], max_searches=1, max_reads=1)
        ctx = SimpleNamespace(deps=deps)
        search_valuation_research(ctx, "样例估值")
        ref = deps.hits[0]["ref"]
        unread = SimpleNamespace(output=draft(spec, model_pe_sources=[ref]), all_messages=lambda: [])
        with patch("valuation.peer_valuation.peer_agent.classify_agent.run_sync", return_value=unread):
            with self.assertRaises(SchemaError):
                _run_classify(spec["facts"], deps.filled, None, deps=deps)
        self.assertIn("次数已用完", search_valuation_research(ctx, "再次检索"))
        self.assertIn("原片段", read_valuation_research(ctx, ref))
        self.assertIn("次数已用完", read_valuation_research(ctx, ref))
        with patch("valuation.peer_valuation.peer_agent.classify_agent.run_sync", return_value=unread):
            card = _run_classify(spec["facts"], deps.filled, None, deps=deps)
        self.assertEqual(card["model_pe_sources"][0]["title"], hit["title"])


class ModelPEWorkbookTests(unittest.TestCase):
    def test_new_row_drives_original_scenarios_and_certification_catches_mean(self):
        for market in ("A", "HK", "US"):
            spec = case(market)
            with self.subTest(market=market), tempfile.TemporaryDirectory() as directory:
                with patch("valuation.workbook.build.load_spec", return_value=spec):
                    path = build_workbook(Path(directory))
                wb = load_workbook(path)
                register_labels(wb, spec["facts"])
                ws = wb["相对估值预测"]
                row = find_row_by_label(ws, "模型给定PE")
                self.assertEqual(row, find_row_by_label(ws, "核心池中位数") + 1)
                self.assertEqual(ws.cell(row, 2).value, 30.)
                self.assertEqual(ws.cell(row, 3).value, REASON)
                self.assertTrue(ws.cell(row, 3).alignment.wrap_text)
                self.assertIn(f"C{row}:F{row}", {str(r) for r in ws.merged_cells.ranges})
                self.assertEqual(ws.cell(row, 2).number_format, MULTIPLE_FMT)
                self.assertEqual(ws.cell(row, 2).font.color.rgb[-6:], INPUT_BLUE)
                self.assertEqual(_relative_valuation_errors(wb, spec), [])
                with patch("valuation.workbook.certify.load_spec", return_value=spec):
                    report = certify_workbook(Path(directory), path)
                self.assertTrue(report["pass"], report["fails"])
                neutral = find_row_by_label(ws, "中性")
                ws.cell(neutral, 3, f"=B{neutral}*B{find_row_by_label(ws, '核心池均值')}")
                self.assertTrue(any("未引用模型给定PE" in error for error in _relative_valuation_errors(wb, spec)))

    def test_legacy_row_is_a_labeled_mean_link(self):
        spec = integration_spec("HK")
        with tempfile.TemporaryDirectory() as directory:
            with patch("valuation.workbook.build.load_spec", return_value=spec):
                path = build_workbook(Path(directory))
            wb = load_workbook(path)
            ws = wb["相对估值预测"]
            row = find_row_by_label(ws, "模型给定PE")
            self.assertEqual(ws.cell(row, 2).value, f"=B{find_row_by_label(ws, '核心池均值')}")
            self.assertIn("历史任务", ws.cell(row, 3).value)
            self.assertEqual(_relative_valuation_errors(wb, spec), [])


if __name__ == "__main__":
    unittest.main()
