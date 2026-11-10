from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from valuation.dcf.calculate import calculate_dcf, normalized_fcff
from valuation.dcf.certify import certify_dcf
from valuation.dcf.workbook import result_address, result_ref, sensitivity_center, wacc_address
from valuation.dcf.fetch import beta_from_prices, financial_records, parse_risk_free
from valuation.research_dossier.snapshot import snapshot_from_spec
from valuation.shared.operating import operating_from_spec
from valuation.tests.test_foreign_financials import integration_spec
from valuation.workbook.build import build_workbook, SHEET_ORDER
from valuation.shared.io import dump_json, load_json
from valuation.shared.state import new_state
from valuation.workbook.anchors import find_label_position


def case():
    spec = integration_spec('HK')
    facts = spec['facts']
    facts['as_of'] = '2025-09-27'
    facts['market']['快照日期'] = facts['as_of']
    periods = facts['forecast_periods']
    source = {'kind': 'judgment', 'source': '单元测试假设', 'date': facts['as_of'], 'rationale': '用于独立验证数值和边界'}
    inputs = {'enabled': True, 'company': facts['company'], 'ticker': facts['ticker'], 'currency': facts['currency'],
              'valuation_date': facts['as_of'],
              'period_end_dates': {p: f'{p[:4]}-09-27' for p in periods},
              'cashflow': {p: dict(da_ratio=.04, capex_ratio=.06, nwc_ratio=.1, tax_rate=.2, ebit_adjustment=0.) for p in periods},
              'terminal': dict(growth=.025, ebit_margin=.2, tax_rate=.2, da_ratio=.04, capex_ratio=.06, nwc_ratio=.1),
              'capital_cost': dict(risk_free_rate=.03, beta=1., erp=.05, tax_shield_rate=.2, kd_mode='spread', credit_spread=.02),
              'bridge': dict(cash=10., restricted_cash=1., operating_cash=2., non_operating_assets=3., debt=5., minority=1., other_claims=0.),
              'evidence': {k: deepcopy(source) for k in ('cashflow', 'terminal', 'capital_cost', 'bridge')}}
    return spec, inputs


class DcfMathTests(unittest.TestCase):
    def test_three_year_fcff_wacc_and_bridge_against_independent_values(self):
        spec, inputs = case()
        result = calculate_dcf(spec, inputs)
        self.assertEqual(result['status'], 'estimated')
        self.assertEqual(len(result['rows']), 3)
        # Revenue chain is 100 -> 110 -> 121 -> 133.1. Operating margin is 45%.
        equity = 10 * 1000 / 100 / 1.1
        weight = 5 / (5 + equity)
        wacc = (1-weight)*.08 + weight*.05*.8
        fcffs = [110*.45*.8 + 110*(.04-.06) - 10*.1,
                 121*.45*.8 + 121*(.04-.06) - 11*.1,
                 133.1*.45*.8 + 133.1*(.04-.06) - 12.1*.1]
        self.assertAlmostEqual(result['capital_cost']['wacc'], wacc)
        for actual, expected in zip(result['rows'], fcffs):
            self.assertAlmostEqual(actual['fcff'], expected)
            self.assertEqual(actual['remaining_fraction'], 1)
        times = [(date(int(p[:4]), 9, 27)-date(2025,9,27)).days/365.25 for p in spec['facts']['forecast_periods']]
        normrev = 133.1*1.025
        norm = normrev*.2*.8 + normrev*(.04-.06) - (normrev-133.1)*.1
        ev = sum(f/(1+wacc)**t for f,t in zip(fcffs,times)) + norm/(wacc-.025)/(1+wacc)**times[-1]
        self.assertAlmostEqual(result['enterprise_value'], ev)
        self.assertAlmostEqual(result['equity_bridge'], 4.)
        self.assertAlmostEqual(result['price'], (ev+4)*100/1000*1.1)
        self.assertAlmostEqual(result['sensitivity']['prices'][2][2], result['price'])

    def test_uses_same_operating_chain_as_pe_snapshot(self):
        spec, inputs = case()
        result = calculate_dcf(spec, inputs)
        snapshot = snapshot_from_spec(spec)
        revenue, pnl = operating_from_spec(spec)
        for row in result['rows']:
            self.assertAlmostEqual(row['revenue'], revenue[row['period']])
            self.assertAlmostEqual(row['unadjusted_ebit'], pnl[row['period']]['ebit'])
        self.assertIn('display', snapshot)

    def test_missing_is_not_zero_and_nonfinite_is_rejected(self):
        spec, inputs = case()
        for value in (None, float('nan'), float('inf')):
            invalid = deepcopy(inputs)
            invalid['bridge']['other_claims'] = value
            result = calculate_dcf(spec, invalid)
            self.assertEqual(result['status'], 'unavailable')
            self.assertNotIn('price', result)
        inputs['bridge']['other_claims'] = 0
        self.assertIn('price', calculate_dcf(spec, inputs))

    def test_numeric_boundaries_and_optional_reasons(self):
        spec, inputs = case()
        invalid = deepcopy(inputs); invalid['cashflow'] = None
        self.assertEqual(calculate_dcf(spec, invalid)['status'], 'unavailable')
        inputs['evidence'] = {}
        self.assertIn('price',calculate_dcf(spec,inputs))
        for key, value in [('growth', .5), ('tax_rate', -0.1), ('capex_ratio', -0.1)]:
            invalid = deepcopy(inputs); invalid['terminal'][key] = value
            self.assertEqual(calculate_dcf(spec, invalid)['status'], 'unavailable')
        inputs['bridge']['operating_cash'] = 12
        self.assertTrue(any('超过现金余额' in x for x in calculate_dcf(spec, inputs)['gaps']))

    def test_stub_period_does_not_include_elapsed_cashflows(self):
        spec, inputs = case()
        inputs['valuation_date'] = '2026-03-27'
        spec['facts']['market']['快照日期'] = inputs['valuation_date']
        result = calculate_dcf(spec, inputs)
        self.assertAlmostEqual(result['rows'][0]['remaining_fraction'], 184/365)
        self.assertEqual(result['rows'][1]['remaining_fraction'], 1)
        inputs['valuation_date'] = '2026-10-01'
        spec['facts']['market']['快照日期'] = inputs['valuation_date']
        result = calculate_dcf(spec, inputs)
        self.assertEqual(result['rows'][0]['pv'], 0)
        self.assertTrue(0 < result['rows'][1]['remaining_fraction'] < 1)

    def test_losses_do_not_create_automatic_tax_refund(self):
        spec, inputs = case()
        inputs['cashflow'][spec['facts']['forecast_periods'][0]]['ebit_adjustment'] = -100
        row = calculate_dcf(spec, inputs)['rows'][0]
        self.assertLess(row['ebit'], 0)
        self.assertEqual(row['tax'], 0)

    def test_ads_fx_and_ordinary_share_override_applied_once(self):
        spec, inputs = case()
        spec['facts']['market']['每交易单位普通股数'] = 2
        inputs['bridge']['ordinary_shares_mn'] = 2500
        result = calculate_dcf(spec, inputs)
        self.assertAlmostEqual(result['capital_cost']['equity_value'], 10*2500/2/100/1.1)
        self.assertAlmostEqual(result['price'], result['equity_value']*100/2500*2*1.1)
        before = result['capital_cost']['wacc']
        spec['facts']['market']['当前股价'] *= 2
        self.assertGreater(calculate_dcf(spec, inputs)['capital_cost']['wacc'], before)

    def test_optional_null_overrides_and_explicit_equity_override(self):
        spec, inputs = case()
        inputs['bridge']['ordinary_shares_mn'] = None
        inputs['capital_cost']['equity_value'] = None
        self.assertIn('price', calculate_dcf(spec, inputs))
        inputs['capital_cost']['equity_value'] = 200
        self.assertEqual(calculate_dcf(spec, inputs)['capital_cost']['equity_value'], 200)
        inputs['capital_cost'].update(kd_mode='direct',debt_cost=.06,credit_spread=None)
        self.assertEqual(calculate_dcf(spec, inputs)['capital_cost']['kd'], .06)
        inputs['bridge']['debt'] = 0
        self.assertAlmostEqual(calculate_dcf(spec, inputs)['capital_cost']['wacc'],.08)

    def test_terminal_growth_recomputes_reinvestment(self):
        spec, inputs = case()
        result = calculate_dcf(spec, inputs)
        last = result['rows'][-1]['revenue']
        low = normalized_fcff(last*1.02,last,.2,.2,.04,.06,.1)
        high = normalized_fcff(last*1.03,last,.2,.2,.04,.06,.1)
        self.assertNotAlmostEqual(high, low*1.03/1.02)
        self.assertGreater(result['sensitivity']['prices'][2][4], result['sensitivity']['prices'][2][0])
        self.assertLess(result['sensitivity']['prices'][4][2], result['sensitivity']['prices'][0][2])

    def test_financial_company_is_unavailable_and_multi_listed_uses_current_market(self):
        spec, inputs = case()
        inputs['financial_company'] = True
        self.assertEqual(calculate_dcf(spec,inputs)['status'],'unavailable')
        inputs['financial_company'] = False
        inputs['multiple_share_classes'] = True
        self.assertEqual(calculate_dcf(spec,inputs)['status'],'estimated')
        self.assertIn('price',calculate_dcf(spec,inputs))
        inputs['bridge']['ordinary_shares_mn'] = 2000
        inputs['capital_cost']['equity_value'] = 200
        self.assertIn('price',calculate_dcf(spec,inputs))


class DcfSourceTests(unittest.TestCase):
    def test_risk_free_rejects_future_rows_and_stale_metadata(self):
        payload = {'data': {'answer': '|2026-10-30|1.65|\n|2026-11-03|8.99|',
                           'indicator_description': [{'单位':'%', '终止日期':'20261030', '数据来源':'测试'}]}}
        result = parse_risk_free(payload, date(2026,11,2))
        self.assertEqual(result['date'], '2026-10-30')
        self.assertEqual(result['value'], .0165)
        self.assertTrue(result['date_conflict'])
        with self.assertRaises(ValueError): parse_risk_free(payload,date(2026,12,1))

    def test_company_period_and_accumulation_filters(self):
        row = dict(stockCode='sz002230',reportDate='2025-12-31',reportPeriod='2025A',type='当年累计',indicators=[dict(name='折旧与摊销',value='2亿元',source='现金流量表')])
        data = [row,dict(row,stockCode='sz000001'),dict(row,reportDate='2027-12-31'),dict(row,type='单季')]
        records = financial_records({'data':data},'002230',date(2026,11,2))
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['amount_yi'],2)
        late = dict(row,publishDate='2026-11-04',unit='人民币-CNY')
        wrong_currency = dict(row,unit='美元-USD')
        records = financial_records({'data':[late,wrong_currency]},'002230',date(2026,11,2),'CNY')
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['availability'],'published_after_valuation_date')

    def test_beta_uses_matched_dates_and_known_regression_slope(self):
        stock, index = [], []
        s, m = 100., 100.
        for i in range(40):
            r = .002*(i%5-2)
            s *= 1+1.5*r; m *= 1+r
            day = (date(2025,1,3)+timedelta(days=7*i)).isoformat()
            stock.append(dict(period_end_date=day,close_price=s))
            index.append(dict(trading_day=day,close=m))
        result = beta_from_prices({'data':{'bars':stock}},{'data':index},date(2026,1,1))
        self.assertAlmostEqual(result['value'],1.5)
        self.assertAlmostEqual(result['r_squared'],1)
        self.assertEqual(result['observations'],39)


class DcfWorkbookTests(unittest.TestCase):
    def test_historical_cashflow_is_on_financials_and_forecast_starts_with_previous_revenue(self):
        spec, inputs = case()
        inputs['historical_cashflow'] = {spec['facts']['hist_periods'][-1]: {'da': 4, 'capex': 0}}
        spec['dcf_inputs'] = inputs
        spec['dcf'] = calculate_dcf(spec, inputs)
        with tempfile.TemporaryDirectory() as tmp, patch('valuation.workbook.build.load_spec',return_value=spec):
            wb = load_workbook(build_workbook(Path(tmp)))
            fs, d = wb['历史财务数据'], wb['DCF估值']
            da_row, col = find_label_position(fs, 'D&A')
            capex_row, _ = find_label_position(fs, 'Capex')
            self.assertEqual(fs.cell(da_row, col+3).value, 4)
            self.assertEqual(fs.cell(capex_row, col+3).value, 0)
            self.assertEqual(fs.cell(da_row, col+1).value, '-')
            self.assertIn('/D', fs.cell(da_row+1, col+3).value)
            self.assertIn("'历史财务数据'!D", d['B18'].value)
            self.assertIn('DATE(2025,9,27)', d['B21'].value)
            self.assertEqual(d['C18'].value, '=IF(COUNT(C3,B3,C10)=3,(C3-B3)*C10,"-")')
            self.assertEqual(d[result_address(d,'growth_step')].value, .0025)
            self.assertFalse(any('ONWC' in str(c.value) for row in fs for c in row))

    def test_eight_sheets_with_inline_wacc_and_unavailable_fallback(self):
        spec, inputs = case()
        spec['dcf_inputs'] = inputs
        spec['dcf'] = calculate_dcf(spec,inputs)
        with tempfile.TemporaryDirectory() as tmp, patch('valuation.workbook.build.load_spec',return_value=spec):
            path = build_workbook(Path(tmp)); wb = load_workbook(path)
            self.assertEqual(wb.sheetnames,SHEET_ORDER[:-1]+['DCF估值',SHEET_ORDER[-1]])
            self.assertEqual(certify_dcf(wb,spec),[])
            d=wb['DCF估值']
            self.assertNotIn('WACC',wb.sheetnames)
            self.assertIn('A1:K1',str(d.merged_cells))
            self.assertEqual(d['H2'].value,'WACC 构建')
            self.assertEqual([d.cell(3,c).value for c in range(8,12)],['股权成本','数值','单位','说明'])
            self.assertEqual(d[wacc_address(d,'普通股总数')].value,'='+result_address(d,'shares_mn'))
            self.assertEqual(d[result_address(d,'wacc')].value,'='+wacc_address(d))
            self.assertIn('COUNT(B$19:D$19)=3',d[sensitivity_center(d)].value)
            self.assertFalse(wb['DCF估值'].sheet_view.showGridLines)
            self.assertEqual(d['B19'].number_format, '#,##0.00;[Red]-#,##0.00;0.00;@')
            self.assertIsNone(d.freeze_panes)
            self.assertEqual([d.cell(2,c).value for c in range(2,6)],spec['facts']['forecast_periods']+['稳态'])
            self.assertEqual(d.max_column,11)
            self.assertEqual(d.column_dimensions['G'].width,3)
            self.assertTrue(all((d.row_dimensions[r].height or 18)<=22 for r in range(3,62)))
            summary = [c.value for row in wb['总结'] for c in row]
            self.assertIn('='+result_ref(d,'price'), summary)
            inputs['bridge']['cash'] = None
            spec['dcf'] = calculate_dcf(spec,inputs)
            path = build_workbook(Path(tmp)); wb = load_workbook(path)
            self.assertEqual(wb['DCF估值'][result_address(wb['DCF估值'],'status')].value,'关键输入未确认')
            self.assertEqual(wb['DCF估值'][result_address(wb['DCF估值'],'price')].value,'-')
            self.assertEqual(certify_dcf(wb,spec),[])

    def test_old_pe_model_stays_seven_sheets(self):
        spec, inputs = case()
        with tempfile.TemporaryDirectory() as tmp, patch('valuation.workbook.build.load_spec',return_value=spec):
            self.assertEqual(load_workbook(build_workbook(Path(tmp))).sheetnames,SHEET_ORDER)
            spec['dcf_inputs'] = {**inputs,'enabled':False}
            spec['dcf'] = calculate_dcf(spec,spec['dcf_inputs'])
            self.assertEqual(spec['dcf']['status'],'disabled')
            self.assertEqual(load_workbook(build_workbook(Path(tmp))).sheetnames,SHEET_ORDER)


class DcfResumeTests(unittest.TestCase):
    def test_enabling_old_task_preserves_upstream_work_and_invalidates_outputs(self):
        from valuation.pipeline import main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = new_state('old',ticker='002230',company='科大讯飞')
            state.pop('dcf_enabled')
            state['completed'] = ['historical_financials','segment_split','peer_valuation','research_dossier','build','certify']
            dump_json(root/'run_state.json',state)
            with patch('valuation.pipeline._find_run',return_value=root), patch('valuation.pipeline._advance',return_value=0):
                main(['--resume','old','--with-dcf'])
            saved = load_json(root/'run_state.json')
            self.assertTrue(saved['dcf_enabled'])
            self.assertEqual(saved['completed'],['historical_financials','segment_split','peer_valuation'])


if __name__ == '__main__':
    unittest.main()
