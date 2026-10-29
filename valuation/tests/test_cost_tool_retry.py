"""回放真实参数漏填：模型可纠正，错误调用不执行检索。"""
import unittest
from unittest.mock import Mock

from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from valuation.operating_cost.cost_agent import CostDeps, cost_agent


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


if __name__=="__main__":unittest.main()
