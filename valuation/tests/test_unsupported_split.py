from copy import deepcopy
import unittest

from valuation.segment_split.schema import SchemaError, unsupported_zero_history, validate_split
from valuation.segment_split.hist_agent import HistCell, HistFill, HistSeg, notes_from_draft
from valuation.segment_split.split_plan import resolve_unsupported_children, plug_parent_residuals


class UnsupportedSplitTests(unittest.TestCase):
    years = ["2023A", "2024A", "2025A"]

    def child(self, name="待拆业务", amount=0, quality="兜底"):
        return {"name": name, "parent": "父项",
                "historical_revenue": dict.fromkeys(self.years, amount),
                "data_quality": dict.fromkeys(self.years, quality)}

    def official(self):
        return [{"name": "父项", "period": year, "revenue_yi": amount}
                for year, amount in zip(self.years, [80, 90, 100])]

    def test_all_unsupported_children_collapse_to_disclosed_parent(self):
        rows = [self.child(), self.child("父项其他", 100, "倒推")]
        original = deepcopy(rows)
        fixed, decisions = resolve_unsupported_children(rows, self.years, self.official(), ["父项"], last_total=200)
        self.assertEqual(rows, original)
        self.assertEqual([r["name"] for r in fixed], ["父项"])
        self.assertEqual(list(fixed[0]["historical_revenue"].values()), [80, 90, 100])
        self.assertEqual(fixed[0]["revenue_share_latest"], .5)
        self.assertTrue(decisions[0]["collapsed"])
        self.assertFalse(unsupported_zero_history(fixed[0], self.years))

    def test_supported_child_survives_and_residual_is_replaced_once(self):
        rows = [self.child(), self.child("已披露业务", 50, "直接披露"), self.child("父项其他", 50, "倒推")]
        fixed, decisions = resolve_unsupported_children(rows, self.years, self.official(), ["父项"])
        self.assertFalse(decisions[0]["collapsed"])
        fixed, _ = plug_parent_residuals(fixed, self.years, self.official(), ["父项"])
        self.assertEqual([r["name"] for r in fixed], ["已披露业务", "父项其他"])
        self.assertEqual(list(fixed[1]["historical_revenue"].values()), [30, 40, 50])
        again, _ = plug_parent_residuals(fixed, self.years, self.official(), ["父项"])
        self.assertEqual(again, fixed)

    def test_real_zero_and_nonzero_history_are_retained(self):
        for amount, quality in [(0, "直接披露"), (0, "有据可查"), (20, "兜底")]:
            with self.subTest(amount=amount, quality=quality):
                rows = [self.child(amount=amount, quality=quality)]
                fixed, decisions = resolve_unsupported_children(rows, self.years, self.official(), ["父项"])
                self.assertEqual(fixed, rows)
                self.assertEqual(decisions, [])

    def test_missing_parent_data_is_not_filled_with_zero(self):
        with self.assertRaisesRegex(SchemaError, "缺少年报父项历史收入"):
            resolve_unsupported_children([self.child()], self.years, self.official()[1:], ["父项"])

    def test_unmapped_zero_cannot_be_silently_deleted(self):
        row = self.child()
        row["parent"] = ""
        with self.assertRaisesRegex(SchemaError, "无法退回"):
            resolve_unsupported_children([row], self.years, self.official(), ["父项"])

    def test_hist_fill_output_uses_restored_parent_and_matching_sources(self):
        facts = {"company": "测试公司", "ticker": "123456", "hist_periods": self.years,
                 "unit": "亿元", "income": {"营业收入": [90, 100, 110]}}
        plan = {"caliber": "drilled", "official_parents": ["父项"],
                "segments": [{"name": "待拆业务", "parent": "父项"}, {"name": "其他", "parent": ""}]}
        draft = HistFill(fill_method="结构推算", split_logic="旧草稿保留待拆业务零值",
                         other_note="旧父项余额未拆分", segments=[
            HistSeg(name=name, years=[HistCell(year=y, amount=amount, quality=quality, why="测试依据")
                                     for y in self.years])
            for name, amount, quality in [("待拆业务", 0, "兜底"), ("其他", 10, "倒推")]])
        notes = notes_from_draft(facts, plan, {"rows": self.official()}, draft)
        self.assertEqual([s["name"] for s in notes["final_segments"]], ["父项", "其他"])
        self.assertEqual(validate_split(notes, facts), [])
        self.assertIn("撤销", notes["split_explanation"]["拆分逻辑"])
        self.assertNotIn("旧草稿", notes["final_rationale"])
        for source in notes["sources"]:
            self.assertEqual(source["segments_identified"], ["父项", "其他"])
        notes["final_segments"][0] = self.child()
        self.assertTrue(any("不能进入预测" in e for e in validate_split(notes, facts)))


if __name__ == "__main__":
    unittest.main()
