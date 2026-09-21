from __future__ import annotations

import unittest

from valuation.comein.http import _sse_is_result
from valuation.historical_financials.company_info import build_company_info, market_rows, profile_rows
from valuation.historical_financials.fetch import _compact_balance, _compact_income, _fill_absent_cells
from valuation.operating_cost.schema import normalize_cost, validate_cost
from valuation.peer_valuation.peer_agent import _coverage_gaps
from valuation.peer_valuation.schema import normalize_comps, validate_comps
from valuation.research_dossier.attribution import _is_stale, _parse_date, _split_houses
from valuation.segment_research.methods import roll_segment
from valuation.segment_research.schema import normalize_forecast
from valuation.workbook.build import consensus_median_formula
from valuation.workbook.certify import formula_is_dangerous, median_range_is_empty


class FetchPartialYearTests(unittest.TestCase):
    def test_keeps_existing_years(self):
        notes: dict[str, str] = {}
        block = {"投资收益": [10.0, None, 20.0]}
        _fill_absent_cells(block, frozenset({"投资收益"}), 3, notes)
        self.assertEqual(block["投资收益"], [10.0, 0.0, 20.0])
        self.assertIn("已有值保留", notes["投资收益"])

    def test_income_and_balance_compact(self):
        notes: dict[str, str] = {}
        income = {"投资收益": [10.0, None, 20.0], "营业收入": [1.0, 2.0, 3.0]}
        _compact_income(income, 3, notes)
        self.assertEqual(income["投资收益"], [10.0, 0.0, 20.0])
        notes = {}
        balance = {"短期借款": [10.0, None, 20.0]}
        _compact_balance(balance, 3, notes)
        self.assertEqual(balance["短期借款"], [10.0, 0.0, 20.0])


class ConsensusMedianTests(unittest.TestCase):
    def test_empty_institutions(self):
        self.assertIsNone(consensus_median_formula(3, 2, "C"))
        self.assertTrue(median_range_is_empty("=MEDIAN(C3:C2)"))
        self.assertFalse(median_range_is_empty("=MEDIAN(C3:C5)"))

    def test_div_zero_is_dangerous(self):
        self.assertTrue(formula_is_dangerous("=1/0"))
        self.assertTrue(formula_is_dangerous("#DIV/0!"))
        self.assertFalse(formula_is_dangerous("=A1*100/B2"))


class HistRevenueLockTests(unittest.TestCase):
    def test_roll_keeps_locked_hist(self):
        forecast = {
            "historical_data": {
                "销量": {"2023A": 8.0, "2024A": 9.0, "2025A": 10.0},
                "单价": {"2023A": 10000.0, "2024A": 10000.0, "2025A": 10000.0},
            },
            "final_forecast": {
                "销量": {"2026E": 12.0, "2027E": 13.0, "2028E": 14.0},
                "单价": {"2026E": 10000.0, "2027E": 10000.0, "2028E": 10000.0},
            },
            "unit_meta": {"fx": 10000.0},
        }
        locked = {"2023A": 8.0, "2024A": 9.0, "2025A": 99.0}
        revenue, _ = roll_segment(
            "量价绝对值法",
            forecast,
            ["2023A", "2024A", "2025A"],
            ["2026E", "2027E", "2028E"],
            locked,
        )
        self.assertEqual(revenue["2025A"], 99.0)


class CostAndCompsValidationTests(unittest.TestCase):
    def test_cost_keeps_sources_and_rejects_bad_ratios(self):
        facts = {"forecast_periods": ["2026E"], "hist_periods": ["2025A"], "as_of": "2026-09-01", "income": {}}
        raw = {
            "gross_margin": {"2026E": 0.25},
            "sales_ratio": {"2026E": 0.02},
            "admin_ratio": {"2026E": 0.02},
            "rd_ratio": {"2026E": 0.03},
            "tax_rate": {"2026E": 0.15},
            "minority": {"2026E": 0.0},
            "fin_exp": {"2026E": -1.0},
            "nonop_inc": {"2026E": 0.0},
            "nonop_exp": {"2026E": 0.0},
            "other_op": {"2026E": 0.0},
            "rationale": "2026E毛利率0.25，费用沿用去年。",
            "sources": [{"source_title": "广发-2026-01-01", "house": "广发", "as_of": "2026-01-01", "horizon": "2026E"}],
        }
        card = normalize_cost(raw, facts)
        self.assertEqual(card["sources"][0]["as_of"], "2026-01-01")
        self.assertEqual(validate_cost(card, facts), [])
        bad = dict(card)
        bad["gross_margin"] = {"2026E": 1.2}
        self.assertTrue(any("gross_margin" in item for item in validate_cost(bad, facts)))
        bad = dict(card)
        bad["minority"] = {"2026E": 1.01}
        self.assertTrue(any("minority" in item for item in validate_cost(bad, facts)))

    def test_pe_adjust_and_currency(self):
        facts = {"as_of": "2026-09-01"}
        filled = [
            {
                "name": "中创新航（03931）",
                "ticker": "03931",
                "pe_y1": 17.39,
                "pe_ttm": 20.0,
                "mcap": 340.0,
                "currency": "HKD",
            }
        ]
        raw = {
            "core": [{"ticker": "03931", "note": "业务相近"}],
            "pe_adjust": 1.0,
            "rationale": "按业务选入。",
        }
        card = normalize_comps(raw, facts, filled)
        self.assertEqual(card["core"][0]["currency"], "HKD")
        first = dict(card["core"][0])
        extras = []
        for ticker, name in (("002594", "比亚迪"), ("300014", "亿纬锂能"), ("002074", "国轩高科")):
            extras.append({**first, "ticker": ticker, "name": name, "pe_y1": 15.0, "note": "业务相近"})
        card["core"] = [first, *extras]
        self.assertEqual(validate_comps(card), [])
        card["pe_adjust"] = -1
        self.assertTrue(any("pe_adjust" in item for item in validate_comps(card)))

    def test_coverage_gap(self):
        filled = [{"pe_y1": 10}, {"pe_y1": 12}, {"pe_y1": 14}, {"pe_y1": 16}]
        core = [{"pe_y1": 10}, {"pe_y1": 20}]
        gaps = _coverage_gaps(filled, core)
        self.assertTrue(any("覆盖不足" in item for item in gaps))
        self.assertTrue(any("离散度" in item for item in gaps))


class ForecastSourceTests(unittest.TestCase):
    def test_keeps_as_of(self):
        facts = {
            "hist_periods": ["2023A", "2024A", "2025A"],
            "forecast_periods": ["2026E", "2027E", "2028E"],
            "unit": "亿元",
            "as_of": "2026-09-01",
        }
        notes = {
            "name": "消费电子",
            "historical_revenue": {"2023A": 8.0, "2024A": 9.0, "2025A": 10.0},
        }
        raw = {
            "segment": "消费电子",
            "method": "收入增速法",
            "historical_data": {"分部收入": {"2023A": 8.0, "2024A": 9.0, "2025A": 10.0}},
            "final_forecast": {"收入增速": {"2026E": 0.1, "2027E": 0.1, "2028E": 0.1}},
            "final_rationale": "2026E按0.1外推。",
            "sources": [
                {
                    "source_title": "西南-动力-2025-04-25",
                    "house": "西南",
                    "as_of": "2025-04-25",
                    "horizon": "2026E",
                }
            ],
        }
        card = normalize_forecast(raw, facts, notes)
        self.assertEqual(card["sources"][0]["as_of"], "2025-04-25")
        self.assertEqual(card["sources"][0]["horizon"], "2026E")


class AttributionHelperTests(unittest.TestCase):
    def test_stale_house(self):
        as_of = _parse_date("2026-09-01")
        stale, fresh = _split_houses(
            [
                {"name": "国信", "published": "2024-07-21"},
                {"name": "广发", "published": "2026-08-01"},
            ],
            as_of,
        )
        self.assertTrue(any("国信" in item for item in stale))
        self.assertTrue(any("广发" in item for item in fresh))
        self.assertTrue(_is_stale(_parse_date("2024-07-21"), as_of))
        self.assertFalse(_is_stale(_parse_date("2026-08-01"), as_of))


class CompanyInfoTests(unittest.TestCase):
    def test_omits_missing_fields_and_open_gaps(self):
        details = {
            "data": [
                {
                    "standardized_info": {
                        "stock_short_name": "宁德时代",
                        "stock_code": "300750",
                        "stock_full_code": "300750.SZ",
                        "snapshotDate": "2026-09-21",
                    },
                    "industry_concepts": {
                        "industry_standard": "申万",
                        "level1_industry": "电力设备",
                        "level2_industry": "电池",
                        "level3_industry": "锂电池",
                    },
                    "business_profile": {"main_business": "动力电池系统"},
                    "management": {"legal_representative": "曾毓群"},
                    "market_value": {"totalMarketValue": "13856亿"},
                }
            ]
        }
        price = {
            "data": [
                {
                    "standardized_info": {"snapshotDate": "2026-09-21"},
                    "regular_market": {"latestPrice": "372.00"},
                    "period_change": {
                        "pctChange": "-0.83%",
                        "periodReturn1w": "2.10%",
                        "periodReturn1m": "5.00%",
                        "periodReturn3m": "8.00%",
                        "periodReturn6m": "12.00%",
                        "periodReturnYtd": "20.00%",
                    },
                    "market_value": {
                        "totalMarketValue": 1385579108262,
                        "floatMarketValue": 1200000000000,
                        "freeFloatMarketValue": "无数据",
                    },
                }
            ]
        }
        info = build_company_info(details, price, ticker="300750", company="宁德时代")
        self.assertNotIn("open_gaps", info)
        profile = info["profile"]
        market = info["market"]
        self.assertEqual(profile["short_name"], "宁德时代")
        self.assertEqual(profile["legal_representative"], "曾毓群")
        self.assertEqual(profile["sw_industry"], "电力设备 / 电池 / 锂电池")
        for key in (
            "legal_name",
            "listing_date",
            "controller",
            "registered_place",
            "global_position",
        ):
            self.assertNotIn(key, profile)
        self.assertNotIn("free_float_mcap_yi", market)
        self.assertAlmostEqual(market["chg_1d"], -0.0083)
        self.assertAlmostEqual(market["mcap_yi"], 13855.7911)
        labels = [label for label, _ in profile_rows(info)]
        self.assertEqual(labels, ["公司简称", "A股代码", "申万行业", "主营业务", "法定代表人"])
        self.assertTrue(all(not row[2] or isinstance(row[1], float) for row in market_rows(info)))


class SseTests(unittest.TestCase):
    def test_skips_notification(self):
        self.assertFalse(_sse_is_result({"method": "notifications/progress"}, 1))
        self.assertFalse(_sse_is_result({"id": 2, "result": {}}, 1))
        self.assertTrue(_sse_is_result({"id": 1, "result": {"ok": True}}, 1))


if __name__ == "__main__":
    unittest.main()
