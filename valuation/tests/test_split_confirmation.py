"""业务名单确认后直接传给历史回填，不因数值证据不足再次删减。"""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from valuation.segment_split.schema import SchemaError
from valuation.segment_split.split_agent import (
    CaliberDraft,
    CaliberRow,
    SplitAgentDeps,
    compile_split_plan,
    query_errors,
    run_revenue_split,
)
from valuation.segment_split.split_plan import validate_split_plan


def make_deps(**overrides) -> SplitAgentDeps:
    values = {
        "company": "腾讯控股",
        "ticker": "hk00700",
        "forecast_years": ["2026", "2027", "2028"],
        "official_names": [],
        "official_rows": [],
        "search": Mock(return_value=[]),
        "verbose": False,
    }
    values.update(overrides)
    return SplitAgentDeps(**values)


class SplitConfirmationTests(unittest.TestCase):
    def test_english_businesses_survive_without_official_rows_or_chinese_traces(self):
        names = [
            "Online gaming business",
            "Social networks",
            "Marketing services",
            "FinTech and business services",
            "Others",
        ]
        hit = {"title": "Revenue mix", "snippet": "Revenue: " + "; ".join(names)}
        query = "腾讯控股近三年券商怎么拆主营收入？"
        # Both exhausting the search budget and stopping early must lock the same list.
        for budget in (1, 3):
            with self.subTest(search_budget=budget):
                search = Mock(return_value=[hit])
                deps = make_deps(search=search, max_caliber_searches=budget)
                responses = [{"query": query}]
                if budget > 1:
                    responses.append({"stop": True, "why": "口径已够"})
                responses.append({
                    "caliber": "official",
                    "segments": [{"name": name} for name in names],
                })
                model_calls = []

                def respond(messages, info):
                    self.assertLess(len(model_calls), len(responses), "确认后不应再请求查数或筛选名单")
                    payload = responses[len(model_calls)]
                    model_calls.append(messages)
                    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

                plan = run_revenue_split(deps, model=FunctionModel(respond))

                self.assertEqual([row.name for row in plan.segments], [*names, "其他"])
                self.assertEqual(plan.model_dump(), deps.draft.model_dump())
                self.assertEqual(len(model_calls), len(responses))
                self.assertEqual([item["intent"] for item in deps.queries], ["caliber"])
                search.assert_called_once_with(query, False, "search_1")

    def test_confirmed_parent_and_leaf_names_are_preserved(self):
        deps = make_deps(
            official_names=["增值服务", "营销服务"],
            draft=CaliberDraft(
                caliber="drilled",
                official_parents=["增值服务", "营销服务"],
                segments=[
                    CaliberRow(name="网络游戏", parent="增值服务"),
                    CaliberRow(name="社交网络", parent="增值服务"),
                    CaliberRow(name="营销服务", parent="营销服务"),
                    CaliberRow(name="其他"),
                ],
            ),
            hits=[{"snippet": "网络游戏、社交网络"}],
        )
        confirmed = deps.draft.model_dump()

        plan = compile_split_plan(deps)

        self.assertEqual(plan.model_dump(), confirmed)
        self.assertEqual(deps.draft.model_dump(), confirmed)
        deps.search.assert_not_called()

    def test_empty_or_residual_only_lists_fail_before_hist_fill(self):
        for names in ([], ["其他"], ["其他", "其他业务"]):
            with self.subTest(names=names):
                payload = {
                    "caliber": "official",
                    "official_parents": [],
                    "segments": [{"name": name, "parent": ""} for name in names],
                }
                errors = validate_split_plan(payload)
                self.assertTrue(errors)
                if names:
                    self.assertTrue(any("不能只有其他" in error for error in errors))

    def test_direct_plan_still_requires_sources_for_named_businesses(self):
        deps = make_deps(draft=CaliberDraft(
            caliber="official",
            segments=[CaliberRow(name="凭空业务"), CaliberRow(name="其他")],
        ))

        with self.assertRaisesRegex(SchemaError, "切片里没有出现"):
            compile_split_plan(deps)

    def test_plan_requires_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "需要已确认口径"):
            compile_split_plan(make_deps())

    def test_source_terms_allow_synonym_consolidation_and_query_allows_forecast_terms(self):
        deps = make_deps(
            official_names=["增值服务"],
            draft=CaliberDraft(
                caliber="drilled",
                official_parents=["增值服务"],
                segments=[
                    CaliberRow(name="国内及海外游戏", parent="增值服务", source_terms=["本土市场游戏", "国际市场游戏"]),
                    CaliberRow(name="社交网络", parent="增值服务"),
                    CaliberRow(name="其他"),
                ],
            ),
            hits=[{"snippet": "本土市场游戏收入增长，国际市场游戏及社交网络稳健"}],
        )
        plan = compile_split_plan(deps)
        self.assertEqual([s.name for s in plan.segments], ["国内及海外游戏", "社交网络", "其他"])
        self.assertEqual(query_errors("腾讯控股 卖方分业务收入拆分与2026盈利预测表", deps), [])


if __name__ == "__main__":
    unittest.main()
