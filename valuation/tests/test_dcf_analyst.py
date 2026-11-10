import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from valuation.dcf.assumptions import DcfDeps, search_capex, read_source, propose, _searcher, _source_errors
from valuation.dcf.fetch import historical_cashflow
from valuation.tests.test_dcf import case


class DcfAnalystTests(unittest.TestCase):
    def test_search_cutoff_and_readable_references(self):
        search = Mock(return_value=[
            dict(title='可用计划',date='2025-08-01',snippet='明年投入10亿元'),
            dict(title='未来报告',date='2026-08-01',snippet='不能进入基准'),
            dict(title='无日期',snippet='不能确认时点')])
        deps = DcfDeps('样例','2025-09-27',search,max_searches=1)
        ctx = SimpleNamespace(deps=deps)
        hits = json.loads(search_capex(ctx,'资本开支'))
        self.assertEqual([h['title'] for h in hits],['可用计划'])
        ref = hits[0]['ref']
        self.assertTrue(_source_errors({'capex_note':'按计划估计','capex_sources':[ref]},deps))
        self.assertIn('10亿元',read_source(ctx,ref))
        self.assertEqual(_source_errors({'capex_note':'按计划估计','capex_sources':[ref]},deps),[])
        search_capex(ctx,'再次查询')
        self.assertEqual(search.call_count,1)

    def test_one_agent_searches_capex_and_outputs_complete_dcf_without_per_field_reasons(self):
        spec, inputs = case()
        hits = [dict(title='资本开支计划',date='2025-08-01',snippet='投入计划支持收入的7%')]
        search = Mock(return_value=hits)
        turns=[]
        def respond(messages,info):
            turns.append(messages)
            if len(turns)==1:
                prompt = next(part.content for message in messages for part in message.parts if isinstance(part,UserPromptPart))
                package = json.loads(prompt.split('\n',1)[1])
                self.assertEqual(package['collected']['historical_cashflow']['2025A']['da_ratio'],.04)
                self.assertEqual(package['collected']['historical_cashflow']['2025A']['capex_ratio'],.06)
                return ModelResponse(parts=[ToolCallPart('search_capex',{'query':'样例Capex'})])
            if len(turns)==2:
                return ModelResponse(parts=[ToolCallPart('read_source',{'ref':'capex_1:1'})])
            draft={k:inputs[k] for k in ('cashflow','terminal','capital_cost','bridge','period_end_dates')}
            draft['cashflow']={p:{**v,'capex_ratio':.07} for p,v in inputs['cashflow'].items()}
            draft.update(capex_note='按投入计划预测三年Capex，稳态沿用正常化比例。',capex_sources=['capex_1:1'])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,draft)])
        with TemporaryDirectory() as tmp, patch('valuation.dcf.assumptions._searcher',return_value=search):
            collected = {'currency':spec['facts']['currency'], 'financial_records':[
                dict(period='2025Q4',name='折旧与摊销',amount_yi=4),
                dict(period='2025Q4',name='购建固定资产等支付现金',amount_yi=6)]}
            result=propose(spec,collected,mcp=object(),run_dir=Path(tmp),model=FunctionModel(respond))
            self.assertEqual(len(turns),3)
            self.assertEqual(result['evidence'],{})
            self.assertEqual(result['capital_cost'],inputs['capital_cost'])
            self.assertEqual(result['terminal'],inputs['terminal'])
            self.assertEqual(result['bridge'],inputs['bridge'])
            self.assertTrue(all(v['capex_ratio']==.07 for v in result['cashflow'].values()))
            self.assertEqual(result['capex_references'][0]['title'],'资本开支计划')
            self.assertTrue((Path(tmp)/'logs/dcf/capex_search.json').is_file())

    def test_capex_research_retains_company_financial_forecast_tables(self):
        mcp=Mock()
        table='<table>营业收入 资本开支 销售费用 管理费用 研发费用 资产总计</table>'
        mcp.search_comein_resource.return_value=[dict(title='盈利预测表',businessTime='2025-08-01',chunkText=table)]
        spec,_=case()
        with TemporaryDirectory() as tmp:
            hits=_searcher(mcp,spec['facts'],Path(tmp))('Capex','domestic_report','capex_1')
        self.assertEqual(hits[0]['snippet'],table)
        self.assertEqual(mcp.search_comein_resource.call_args.kwargs['contentTypes'],['domestic_report'])

    def test_historical_capex_normalizes_only_identified_cash_outflow(self):
        records=[dict(period='2025Q4',name='购买固定资产',amount_yi=-10),
                 dict(period='2025Q4',name='折旧与摊销',amount_yi=4),
                 dict(period='2026Q3',name='购买固定资产',amount_yi=-20)]
        result=historical_cashflow({'hist_periods':['2025A']},records)
        self.assertEqual(result,{'2025A':{'capex':10,'da':4}})

    def test_historical_ratios_use_reported_revenue_without_constructing_onwc(self):
        records = [dict(period='2025Q4',name='折旧与摊销',amount_yi=4),
                   dict(period='2025Q4',name='购建固定资产等支付现金',amount_yi=0),
                   dict(period='2025Q4',name='应收账款',amount_yi=99)]
        facts = {'hist_periods':['2025A'], 'income':{'营业收入':[100]}}
        result = historical_cashflow(facts, records)
        self.assertEqual(result, {'2025A':{'da':4,'da_ratio':.04,'capex':0,'capex_ratio':0}})
        facts['income']['营业收入'] = [0]
        self.assertEqual(historical_cashflow(facts, records), {'2025A':{'da':4,'capex':0}})


if __name__=='__main__': unittest.main()
