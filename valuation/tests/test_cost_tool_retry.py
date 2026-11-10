"""回放真实参数漏填：模型可纠正，错误调用不执行检索。"""
import unittest
from unittest.mock import Mock

from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from valuation.operating_cost.cost_agent import CostDeps, compile_cost, cost_agent


def deps(search):
    return CostDeps(company="泡泡玛特",ticker="hk09992",facts={"market_type":"HK"},
                    hist_track={},forecast_periods=["2026E"],briefs_text="",search=search,verbose=False)


class CostToolRetryTests(unittest.TestCase):
    def test_missing_type_can_be_corrected_before_search_executes(self):
        search=Mock(return_value=[])
        calls=[]
        def respond(messages, info):
            calls.append(messages)
            if len(calls)==1:
                return ModelResponse(parts=[ToolCallPart("search_research",{"query":"泡泡玛特费用率"})])
            if len(calls)==2:
                self.assertTrue(any(isinstance(p,RetryPromptPart) for m in messages for p in m.parts))
                search.assert_not_called()
                return ModelResponse(parts=[ToolCallPart("search_research",{"query":"泡泡玛特费用率","content_type":"domestic_report"})])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,{
                "gross_margin":{"2026E":.7},"tax_rate":{"2026E":.2},
                "minority":{"2026E":.01},"other_op":{"2026E":0},"rationale":"测试"})])
        result=cost_agent.run_sync("测试参数纠错",deps=deps(search),model=FunctionModel(respond))
        self.assertEqual(result.output.gross_margin,{"2026E":.7})
        search.assert_called_once_with("泡泡玛特费用率",False,"cost_1","domestic_report")

    def test_repeated_invalid_arguments_stop_at_retry_limit(self):
        search=Mock(return_value=[])
        def respond(messages, info):
            return ModelResponse(parts=[ToolCallPart("search_research",{"query":"泡泡玛特费用率"})])
        with self.assertRaises(UnexpectedModelBehavior):
            cost_agent.run_sync("测试纠错上限",deps=deps(search),model=FunctionModel(respond))
        search.assert_not_called()

    def test_compile_cost_retry_preserves_hits_in_prompt(self):
        search=Mock(return_value=[{"title":"毛利率研报","institution":"中金","date":"2026-08-01","snippet":"泡泡玛特2026E毛利率预计70%"}])
        d = CostDeps(
            company="泡泡玛特", ticker="hk09992",
            facts={"market_type":"HK", "forecast_periods":["2026E"], "hist_periods":["2025A"],
                   "income":{"营业收入":[100.0], "毛利":[65.0], "销售费用":[20.0], "管理费用":[10.0], "营业利润":[35.0], "税前利润":[35.0], "所得税费用":[7.0], "净利润":[28.0], "归母净利润":[28.0], "少数股东损益":[0.0]},
                   "market":{"当前总股本_百万股":1000.0, "每交易单位普通股数":1.0}},
            hist_track={}, forecast_periods=["2026E"], briefs_text="", search=search, verbose=False,
        )
        prompts = []
        def respond(messages, info):
            user_text = " ".join(str(getattr(p, "content", "")) for m in messages for p in getattr(m, "parts", []))
            prompts.append(user_text)
            if len(prompts) == 1:
                return ModelResponse(parts=[ToolCallPart("search_research", {"query":"泡泡玛特毛利率", "content_type":"domestic_report"})])
            if len(prompts) == 2:
                # First draft fails validation because rationale is empty
                return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                    "gross_margin":{"2026E":0.7}, "sales_ratio":{"2026E":0.2}, "admin_ratio":{"2026E":0.1},
                    "tax_rate":{"2026E":0.2}, "minority":{"2026E":0.0}, "rationale":""
                })])
            # Second attempt in compile_cost should see the retrieved hits and previous draft
            self.assertIn("毛利率研报", user_text)
            self.assertIn("上次草稿", user_text)
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                "gross_margin":{"2026E":0.7}, "sales_ratio":{"2026E":0.2}, "admin_ratio":{"2026E":0.1},
                "tax_rate":{"2026E":0.2}, "minority":{"2026E":0.0}, "rationale":"2026E毛利率0.70"
            })])
        draft = compile_cost(d, model=FunctionModel(respond))
        self.assertEqual(draft.gross_margin, {"2026E": 0.7})


if __name__=="__main__":unittest.main()
