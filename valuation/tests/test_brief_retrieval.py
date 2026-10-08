from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from valuation.segment_research.brief_agent import (
    BriefDeps,
    format_brief_for_forecast,
    read_research_hit,
    search_research,
)


class BriefRetrievalTests(unittest.TestCase):
    def test_partial_year_yoy_reaches_forecast(self):
        text = format_brief_for_forecast(
            {
                "facts": [
                    {
                        "period": "1H26",
                        "metric": "智能硬件收入",
                        "value": "198.93亿元",
                        "yoy": "-2.2%",
                        "source_title": "中报点评",
                    }
                ]
            }
        )
        self.assertIn("1H26 智能硬件收入=198.93亿元 同比=-2.2%", text)

    def test_all_hits_are_visible_and_repeated_hits_are_skipped(self):
        def search(*_args):
            return [
                {
                    "doc_id": f"doc-{i}",
                    "chunk_id": "1",
                    "title": f"公司分部报告 {i}",
                    "date": "2026-09-01",
                    "snippet": "公司 目标分部 " + "背景" * 3600 + f"关键判断{i}",
                }
                for i in range(15)
            ]

        deps = BriefDeps(
            company="公司",
            ticker="000001",
            segment="目标分部",
            hist_periods=["2025A"],
            forecast_periods=["2026E"],
            notes_revenue={"2025A": 10.0},
            search=search,
            verbose=False,
        )
        ctx = SimpleNamespace(deps=deps)
        first = json.loads(search_research(ctx, "分部收入", "domestic_report"))
        self.assertEqual(len(first), 15)
        self.assertEqual(deps.queries[0]["new_hits"], 15)

        ref = next(hit["ref"] for hit in first if hit["ref"].endswith(":15"))
        expanded = json.loads(read_research_hit(ctx, ref, "关键判断14"))
        self.assertIn("关键判断14", expanded["snippet"])
        beginning = json.loads(read_research_hit(ctx, ref))
        continuation = json.loads(read_research_hit(ctx, ref, offset=beginning["next_offset"]))
        self.assertIn("关键判断14", continuation["snippet"])
        self.assertEqual(len(deps.reads), 3)

        second = search_research(ctx, "分部盈利预测", "domestic_report")
        self.assertIn("没有新增", second)
        self.assertEqual(deps.queries[1]["new_hits"], 0)


if __name__ == "__main__":
    unittest.main()
