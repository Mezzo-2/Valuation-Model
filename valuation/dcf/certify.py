from __future__ import annotations

import math

from valuation.dcf.calculate import calculate_dcf
from valuation.dcf.workbook import enabled, result_address, wacc_address, sensitivity_center


def certify_dcf(wb, spec):
    if not enabled(spec):
        return []
    fails = []
    result = calculate_dcf(spec, spec['dcf_inputs'])
    if result.get('status') not in {'ready', 'estimated'}:
        if wb['DCF估值'][result_address(wb['DCF估值'],'status')].value != '关键输入未确认':
            fails.append('DCF缺少关键输入时未明确显示原因')
        return fails
    center = result['sensitivity']['prices'][2][2]
    if center is None or abs(center - result['price']) > 1e-9:
        fails.append('DCF敏感性中心与基准价格不一致')
    d = wb['DCF估值']
    for name, address in [('DCF估值', result_address(d,'price')), ('DCF估值', sensitivity_center(d)), ('DCF估值', wacc_address(d))]:
        value = wb[name][address].value
        if not isinstance(value, str) or not value.startswith('='):
            fails.append(f'{name}!{address}必须是联动公式')
    if d[result_address(d,'wacc')].value != '='+wacc_address(d):
        fails.append('DCF未引用WACC计算结果')
    if len(result['rows']) != len(spec['facts']['forecast_periods']):
        fails.append('DCF显性期未复用现有预测期间')
    for key in ('price', 'equity_value', 'enterprise_value'):
        if not math.isfinite(result[key]):
            fails.append(f'DCF {key}不是有限数')
    if spec.get('dcf', {}).get('input_hash') != result.get('input_hash'):
        fails.append('DCF结果使用了过期输入')
    for ws in (d,):
        if ws.freeze_panes is not None or ws.sheet_view.pane is not None:
            fails.append(f'{ws.title}未沿用原模板的无冻结视图')
    return fails
