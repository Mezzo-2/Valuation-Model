"""DCF左侧估值、右侧WACC；通过行标签连接结果，不依赖固定输出坐标。"""
from __future__ import annotations

from datetime import date
import math
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter as L
from openpyxl.worksheet.views import Selection
from openpyxl.worksheet.datavalidation import DataValidation

from valuation.dcf.calculate import evidence_for, period_dates
from valuation.historical_financials.subjects import FS_RIGHT_COL, fs_label
from valuation.workbook.anchors import CellAnchor, build_link, cell_ref, find_label_position
from valuation.workbook.market_config import operating_key, ratio_formula
from valuation.workbook.styles import (AMT_FMT, PCT_FMT, PX_FMT, finish_sheet, mark_subtotal,
    set_col_widths, write_block_header, write_column_header_row, write_input, write_page_title, write_section_row)

DCF_LABELS = dict(wacc='WACC', growth='永续增长率g', valuation_date='估值日', terminal_margin='稳态经营利润率',
    explicit_pv='显性期现值', terminal_fcff='正常化FCFF', terminal_value='终值', terminal_pv='终值现值',
    enterprise_value='经营价值', terminal_share='终值占比', cash='现金余额', restricted_cash='减：受限现金',
    operating_cash='减：最低经营现金', non_operating_assets='加：非经营资产', debt='减：有息债务',
    minority='减：非控股权益', other_claims='减：其他权益索取权', equity_bridge='股权价值净调整',
    equity_value='归母股权价值', shares_mn='普通股总数（百万股）', price_factor='交易单位换算系数',
    current_price='当前股价', price='DCF每股价值', upside='DCF较现价空间', status='DCF可用性',
    wacc_step='WACC敏感性步长', growth_step='g敏感性步长')
FLOW = dict(revenue=3, original_ebit=4, adjustment=5, ebit=6, tax_rate=7, da_ratio=8, capex_ratio=9, nwc_ratio=10,
            tax=14, nopat=15, da=16, capex=17, dnwc=18, fcff=19, end=20, fraction=21, years=22, factor=23, pv=24)
WACC_START_COL = 8


def enabled(spec):
    return bool(spec.get('dcf_inputs')) and spec['dcf_inputs'].get('enabled') is not False


def result_address(ws, key):
    label = DCF_LABELS[key]
    for col, value_col in ((1, 2), (4, 6)):
        for row in range(1, ws.max_row + 1):
            if ws.cell(row, col).value == label:
                return ws.cell(row, value_col).coordinate
    raise ValueError(f'{ws.title}缺少结果标签：{label}')


def result_ref(ws, key):
    return f"'{ws.title}'!{result_address(ws, key)}"


def wacc_address(ws, label='WACC'):
    for row in range(1, ws.max_row+1):
        if ws.cell(row, WACC_START_COL).value == label:
            return ws.cell(row, WACC_START_COL+1).coordinate
    raise ValueError(f'{ws.title}缺少WACC参数：{label}')


def sensitivity_center(ws):
    row, _ = find_label_position(ws, 'WACC / g')
    return ws.cell(row+3,4).coordinate


def market_ref(ws, key):
    row, col = find_label_position(ws, fs_label(key))
    return build_link(CellAnchor(ws.title, row, col + 1))[1:]


def write_historical_cashflow(ws, spec, header_row):
    if not enabled(spec):
        return
    facts, inputs = spec['facts'], spec['dcf_inputs']
    periods = facts['hist_periods']
    start = FS_RIGHT_COL
    write_block_header(ws, header_row, start, ['现金流历史校准（亿元）', *periods])
    calibration = inputs.get('historical_cashflow') or {}
    for offset, (key, label) in enumerate((('da', 'D&A'), ('da_ratio', 'D&A收入占比'),
                                         ('capex', 'Capex'), ('capex_ratio', 'Capex收入占比')), 1):
        row = header_row + offset
        ws.cell(row, start, label)
        for col, period in enumerate(periods, start+1):
            if key.endswith('_ratio'):
                amount_row = row-1
                amount = f'{L(col)}{amount_row}'
                revenue = cell_ref(ws, '营业收入', period, same_sheet=True)
                ws.cell(row, col, ratio_formula(amount, revenue, guarded=True))
            else:
                value = (calibration.get(period) or {}).get(key)
                ws.cell(row, col, value if value is not None else '-')


def _note(ws, row, text, last_col):
    ws.cell(row, 1, text)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=last_col)
    ws.cell(row, 1).alignment = Alignment(wrap_text=True, vertical='center')
    width = sum(ws.column_dimensions[L(c)].width or 13 for c in range(1, last_col + 1))
    units = sum(2 if ord(c) > 127 else 1 for c in str(text))
    ws.row_dimensions[row].height = max(22, math.ceil(units / max(20, width-10))*18+8)


def _finish(ws):
    finish_sheet(ws)
    ws.freeze_panes = None
    ws.sheet_view.pane = None
    ws.sheet_view.selection = [Selection(activeCell='A1', sqref='A1')]
    ws.sheet_view.topLeftCell = 'A1'
    ws.sheet_view.zoomScale = 100
    for row in ws:
        for c in row:
            if isinstance(c.value, (int, float)) or c.data_type == 'f':
                c.number_format = AMT_FMT
            elif isinstance(c.value, date):
                c.number_format = 'yyyy-mm-dd'


def _wacc_header(ws, headers):
    start, last = WACC_START_COL, WACC_START_COL+3
    write_section_row(ws,2,['WACC 构建'],last,start_col=start)
    ws.cell(2,start).alignment=Alignment(horizontal='center',vertical='center')
    write_column_header_row(ws,3,headers,start_col=start)
    ws.column_dimensions[L(start-1)].width=3
    for col,width in ((start,32),(start+1,13),(start+2,13),(last,36)):
        ws.column_dimensions[L(col)].width=width


def _format_wacc(ws):
    value_col=WACC_START_COL+1
    for row in (4,6,7,11,12,13,14,19,20,22): ws.cell(row,value_col).number_format=PCT_FMT
    ws.cell(25,value_col).number_format=PX_FMT


def _write_wacc(wb, spec):
    ws, fs = wb['DCF估值'], wb['历史财务数据']
    f, i, result = spec['facts'], spec['dcf_inputs'], spec['dcf']
    cc = i['capital_cost']
    start, value_col, last = WACC_START_COL, WACC_START_COL+1, WACC_START_COL+3
    v=L(value_col)
    _wacc_header(ws,['股权成本','数值','单位','说明'])
    rows = {'risk_free_rate':4, 'beta':5, 'erp':6, 'ke':7, 'kd_mode':10, 'credit_spread':11,
            'debt_cost':12, 'kd':13, 'tax_shield_rate':14, 'equity_value':17, 'debt':18,
            'equity_weight':19, 'debt_weight':20, 'wacc':22, 'price':25, 'shares':26, 'fx':27, 'ratio':28}
    labels = {'risk_free_rate':'无风险利率 Rf', 'beta':'采用 Beta', 'erp':'股权风险溢价 ERP',
              'ke':'股权成本 Ke', 'kd_mode':'债务成本口径', 'credit_spread':'信用利差',
              'debt_cost':'直接融资成本', 'kd':'税前债务成本 Kd', 'tax_shield_rate':'税盾税率',
              'equity_value':'股权市值', 'debt':'有息债务', 'equity_weight':'股权权重',
              'debt_weight':'债务权重', 'wacc':'WACC', 'price':'当前股价', 'shares':'普通股总数',
              'fx':'报表币种兑交易币种', 'ratio':'每交易单位普通股数'}
    for row, title in ((9,'债务成本'),(16,'资本结构'),(24,'行情与换算输入')):
        write_section_row(ws, row, [title], last, start_col=start)
    for key, row in rows.items():
        ws.cell(row, start, labels[key])
    for key in ('risk_free_rate','beta','erp','credit_spread','debt_cost','tax_shield_rate'):
        row = rows[key]
        value = cc.get(key)
        if value is None:
            ws.cell(row, value_col, '-')
        else:
            write_input(ws, row, value_col, value)
    ws.cell(10,value_col,'利差法' if cc['kd_mode']=='spread' else '直接融资成本')
    dv = DataValidation(type='list', formula1='"利差法,直接融资成本"')
    ws.add_data_validation(dv); dv.add(ws.cell(10,value_col))
    ws.cell(7,value_col,f'=IF(AND(COUNT({v}4:{v}6)=3,{v}6>=0),{v}4+{v}5*{v}6,"-")')
    ws.cell(13,value_col,f'=IF({v}10="利差法",IF(AND(COUNT({v}4,{v}11)=2,{v}11>=0),{v}4+{v}11,"-"),IF(AND({v}10="直接融资成本",COUNT({v}12)=1,{v}12>=0),{v}12,"-"))')
    if cc.get('equity_value') is not None:
        write_input(ws,17,value_col,cc['equity_value'])
    else:
        ws.cell(17,value_col,f'=IF(AND(COUNT({v}25:{v}28)=4,{v}26>0,{v}27>0,{v}28>0),{v}25*{v}26/{v}28/100/{v}27,"-")')
    ws.cell(18,value_col,'='+result_address(ws,'debt'))
    ws.cell(19,value_col,f'=IF(AND(COUNT({v}17:{v}18)=2,{v}17>0,{v}18>=0),{v}17/({v}17+{v}18),"-")')
    ws.cell(20,value_col,f'=IF(COUNT({v}19)=1,1-{v}19,"-")')
    ws.cell(22,value_col,f'=IF(AND(COUNT({v}7,{v}13:{v}14,{v}19:{v}20)=5,{v}14>=0,{v}14<=1),{v}7*{v}19+{v}13*(1-{v}14)*{v}20,"-")')
    ws.cell(25,value_col,'='+market_ref(fs,'当前股价'))
    ws.cell(26,value_col,'='+result_address(ws,'shares_mn'))
    ratio = f['market'].get('每交易单位普通股数',1)
    write_input(ws,27,value_col,result['price_factor']/ratio)
    write_input(ws,28,value_col,ratio)
    notes = {4:'与现金流币种匹配的长期国债利率',5:'沿用股票与市场指数回归结果',6:'权益风险溢价假设',7:'Rf＋Beta×ERP',
             10:'选择利差法或直接融资成本',11:'利差法：Kd＝Rf＋信用利差',12:'直接融资成本模式采用此值',
             14:'与经营税率分开设置',17:'按当前股票行情与总股本计',18:'与DCF股权价值桥接使用同一债务',
             22:'按市值加权',25:'引用原模型行情',26:'按普通股口径计算',27:'每单位报表币种兑换的交易币种'}
    for row,text in notes.items(): ws.cell(row,last,text)
    for row in (17,18): ws.cell(row,start+2,f.get('unit') or '亿'+i['currency'])
    ws.cell(26,start+2,'百万股')
    mark_subtotal(ws,22,start,last,fill=True)
    for row in (4,5,6,10,11,12,14,17,18,25,26,27,28):
        ws.row_dimensions[row].height=22


def build_dcf_sheets(wb, spec):
    f, i, result = spec['facts'], spec['dcf_inputs'], spec['dcf']
    ws, fs = wb['DCF估值'], wb['历史财务数据']
    periods = f['forecast_periods']
    first = 2; last = first+len(periods)-1; tc=last+1; nc=tc+1
    end, steady = L(last), L(tc)
    set_col_widths(ws,nc,first=32,other=13); ws.column_dimensions[L(nc)].width=26
    write_page_title(ws,f["company"]+'-DCF估值',WACC_START_COL+3)
    write_section_row(ws,2,['现金流假设',*periods,'稳态','备注'],nc)
    labels = {3:'营业收入',4:'原模型经营利润',5:'经营口径调整',6:'经营EBIT',7:'经营税率',8:'D&A/收入',
              9:'Capex/收入',10:'ΔONWC/Δ收入',13:'经营EBIT（现金流起点）',14:'经营现金税',15:'NOPAT',
              16:'D&A',17:'Capex',18:'ΔONWC',19:'FCFF',20:'财年末',21:'剩余期间比例',
              22:'折现期限（年）',23:'折现因子',24:'FCFF现值'}
    for row,label in labels.items(): ws.cell(row,1,label)
    write_section_row(ws,12,['FCFF与折现'],nc)
    write_section_row(ws,26,['终值与归母股权价值'],nc)
    left_keys = ['explicit_pv','terminal_fcff','terminal_value','terminal_pv','enterprise_value','terminal_share',
                 'cash','restricted_cash','operating_cash','non_operating_assets','debt','minority','other_claims',
                 'equity_bridge','equity_value','shares_mn','price_factor','current_price','price','upside','status']
    for row,key in enumerate(left_keys,27):
        ws.cell(row,1,DCF_LABELS[key])
        ws.merge_cells(start_row=row,start_column=2,end_row=row,end_column=3)
    for row,key in enumerate(('wacc','growth','valuation_date','terminal_margin','wacc_step','growth_step'),27):
        ws.cell(row,4,DCF_LABELS[key]); ws.merge_cells(start_row=row,start_column=4,end_row=row,end_column=5)
    if result.get('status') not in {'ready','estimated'}:
        for key in left_keys:
            ws[result_address(ws,key)]='关键输入未确认' if key=='status' else '-'
        for key in ('wacc','growth','terminal_margin','valuation_date','wacc_step','growth_step'):
            ws[result_address(ws,key)]='-'
        _note(ws,50,'；'.join(result.get('gaps') or ['关键输入未确认']),nc)
        _wacc_header(ws,['参数','状态','单位','说明'])
        ws.cell(4,WACC_START_COL,'WACC'); ws.cell(4,WACC_START_COL+1,'-')
        ws.cell(4,WACC_START_COL+3,'关键输入未确认')
        _finish(ws)
        return

    _write_wacc(wb,spec)
    def addr(key): return result_address(ws,key)
    def ref(key): return '$'+addr(key)[0]+'$'+addr(key)[1:]
    cc, bridge, terminal = i['capital_cost'], i['bridge'], i['terminal']
    ws[addr('wacc')] = '='+wacc_address(ws)
    for key,value in [('growth',terminal['growth']),('terminal_margin',terminal['ebit_margin']),
                      ('wacc_step',(i.get('sensitivity') or {}).get('wacc_step',.005)),
                      ('growth_step',(i.get('sensitivity') or {}).get('growth_step',.0025))]:
        cell=ws[addr(key)]; write_input(ws,cell.row,cell.column,value)
    ws[addr('valuation_date')]=date.fromisoformat(i['valuation_date'])
    for key in ('cash','restricted_cash','operating_cash','non_operating_assets','debt','minority','other_claims'):
        cell=ws[addr(key)]; write_input(ws,cell.row,cell.column,bridge[key])
    starts, ends = period_dates(f,i)
    for col,p in enumerate(periods,first):
        c,prev=L(col),L(col-1); v=i['cashflow'][p]
        previous_revenue = (cell_ref(fs, '营业收入', f['hist_periods'][-1]) if col == first else f'{prev}3')
        start = starts[p]
        previous_end = f'DATE({start.year},{start.month},{start.day})' if col == first else f'{prev}20'
        ws.cell(3,col,'='+cell_ref(wb['利润表预测'],'营业收入',p))
        ws.cell(4,col,'='+cell_ref(wb['利润表预测'],operating_key(f),p))
        for row,key in ((5,'ebit_adjustment'),(7,'tax_rate'),(8,'da_ratio'),(9,'capex_ratio'),(10,'nwc_ratio')):
            write_input(ws,row,col,v[key])
        forms={6:f'IF(COUNT({c}4:{c}5)=2,{c}4+{c}5,"-")',13:f'{c}6',
               14:f'IF(AND(COUNT({c}6:{c}7)=2,{c}7>=0,{c}7<=1),MAX({c}6,0)*{c}7,"-")',
               15:f'IF(COUNT({c}6,{c}14)=2,{c}6-{c}14,"-")',
               16:f'IF(AND(COUNT({c}3,{c}8)=2,{c}8>=0),{c}3*{c}8,"-")',
               17:f'IF(AND(COUNT({c}3,{c}9)=2,{c}9>=0),{c}3*{c}9,"-")',
               18:f'IF(COUNT({c}3,{previous_revenue},{c}10)=3,({c}3-{previous_revenue})*{c}10,"-")',
               19:f'IF(COUNT({c}15:{c}18)=4,{c}15+{c}16-{c}17-{c}18,"-")',
               21:f'MAX(0,MIN(1,({c}20-MAX({ref("valuation_date")},{previous_end}))/({c}20-{previous_end})))',
               22:f'MAX(0,({c}20-{ref("valuation_date")})/365.25)',
               23:f'IF(AND(COUNT({ref("wacc")})=1,{ref("wacc")}>-1),1/(1+{ref("wacc")})^{c}22,"-")',
               24:f'IF(COUNT({c}19,{c}21,{c}23)=3,{c}19*{c}21*{c}23,"-")'}
        for row,formula in forms.items(): ws.cell(row,col,'='+formula)
        ws.cell(20,col,ends[p])
    for row,key in ((7,'tax_rate'),(8,'da_ratio'),(9,'capex_ratio'),(10,'nwc_ratio')):
        write_input(ws,row,tc,terminal[key])
    forms={3:f'IF(COUNT({end}3,{ref("growth")})=2,{end}3*(1+{ref("growth")}),"-")',
           6:f'IF(COUNT({steady}3,{ref("terminal_margin")})=2,{steady}3*{ref("terminal_margin")},"-")',
           13:f'{steady}6',
           14:f'IF(AND(COUNT({steady}6:{steady}7)=2,{steady}7>=0,{steady}7<=1),MAX({steady}6,0)*{steady}7,"-")',
           15:f'IF(COUNT({steady}6,{steady}14)=2,{steady}6-{steady}14,"-")',
           16:f'IF(AND(COUNT({steady}3,{steady}8)=2,{steady}8>=0),{steady}3*{steady}8,"-")',
           17:f'IF(AND(COUNT({steady}3,{steady}9)=2,{steady}9>=0),{steady}3*{steady}9,"-")',
           18:f'IF(COUNT({steady}3,{end}3,{steady}10)=3,({steady}3-{end}3)*{steady}10,"-")',
           19:f'IF(COUNT({steady}15:{steady}18)=4,{steady}15+{steady}16-{steady}17-{steady}18,"-")'}
    for row,formula in forms.items(): ws.cell(row,tc,'='+formula)
    if bridge.get('ordinary_shares_mn') is not None:
        cell=ws[addr('shares_mn')]; write_input(ws,cell.row,cell.column,result['shares_mn'])
    else: ws[addr('shares_mn')]='='+market_ref(fs,'当前总股本_百万股')+'*'+wacc_address(ws,'每交易单位普通股数')
    ws[addr('price_factor')]='='+wacc_address(ws,'报表币种兑交易币种')+'*'+wacc_address(ws,'每交易单位普通股数')
    formulas={
        'explicit_pv':f'IF(COUNT({L(first)}24:{end}24)={len(periods)},SUM({L(first)}24:{end}24),"-")',
        'terminal_fcff':f'{steady}19',
        'terminal_value':f'IF(AND(COUNT({ref("wacc")},{ref("growth")},{ref("terminal_fcff")})=3,{ref("wacc")}>{ref("growth")},{ref("growth")}>-1),{ref("terminal_fcff")}/({ref("wacc")}-{ref("growth")}),"-")',
        'terminal_pv':f'IF(COUNT({ref("terminal_value")},{end}23)=2,{ref("terminal_value")}*{end}23,"-")',
        'enterprise_value':f'IF(COUNT({ref("explicit_pv")},{ref("terminal_pv")})=2,{ref("explicit_pv")}+{ref("terminal_pv")},"-")',
        'terminal_share':f'IF(AND(COUNT({ref("terminal_pv")},{ref("enterprise_value")})=2,{ref("enterprise_value")}<>0),{ref("terminal_pv")}/{ref("enterprise_value")},"-")',
        'equity_bridge':f'IF(AND(COUNT({ref("cash")}:{ref("other_claims")})=7,MIN({ref("cash")}:{ref("other_claims")})>=0,{ref("cash")}>={ref("restricted_cash")}+{ref("operating_cash")}),{ref("cash")}-{ref("restricted_cash")}-{ref("operating_cash")}+{ref("non_operating_assets")}-{ref("debt")}-{ref("minority")}-{ref("other_claims")},"-")',
        'equity_value':f'IF(COUNT({ref("enterprise_value")},{ref("equity_bridge")})=2,{ref("enterprise_value")}+{ref("equity_bridge")},"-")',
        'current_price':market_ref(fs,'当前股价'),
        'price':f'IF(AND(COUNT({ref("equity_value")},{ref("shares_mn")},{ref("price_factor")})=3,{ref("shares_mn")}>0,{ref("price_factor")}>0),{ref("equity_value")}*100/{ref("shares_mn")}*{ref("price_factor")},"-")',
        'upside':f'IF(AND(COUNT({ref("price")},{ref("current_price")})=2,{ref("current_price")}>0),{ref("price")}/{ref("current_price")}-1,"-")',
        'status':f'IF(COUNT({ref("price")})=1,"采用估计","关键输入未确认")'}
    for key,formula in formulas.items(): ws[addr(key)]='='+formula
    price_unit={'CNY':'元/股','USD':'美元/交易单位','HKD':'港元/股'}.get(f.get('trading_currency') or i['currency'],i['currency'])
    ws.cell(3,nc,'金额：'+(f.get('unit') or '亿'+i['currency'])+'；价格：'+price_unit)
    ws.cell(9,nc,'Capex判断见本页下方')
    ws.cell(21,nc,'首年按剩余天数折算')
    _note(ws,48,'显性期现值为负时，终值占比可超过100%',nc)
    ws.row_dimensions[48].height=22
    write_section_row(ws,49,['WACC × g 每股价值敏感性'],nc)
    write_column_header_row(ws,50,['WACC / g',*[f'=IF(AND(COUNT({ref("growth")},{ref("growth_step")})=2,{ref("growth_step")}>0),{ref("growth")}+({k})*{ref("growth_step")},"-")' for k in (-2,-1,0,1,2)]])
    write_column_header_row(ws,57,['稳态现金流校准',*[f'={L(c)}50' for c in range(2,7)]])
    for row,label in ((58,'稳态收入'),(59,'稳态NOPAT'),(60,'稳态净再投资'),(61,'正常化FCFF（敏感性辅助）')): ws.cell(row,1,label)
    for col in range(2,7):
        c=L(col)
        ws.cell(58,col,f'=IF(COUNT({end}$3,{c}50)=2,{end}$3*(1+{c}50),"-")')
        ws.cell(59,col,f'=IF(AND(COUNT({c}58,{ref("terminal_margin")},{steady}$7)=3,{steady}$7>=0,{steady}$7<=1),{c}58*{ref("terminal_margin")}-MAX({c}58*{ref("terminal_margin")},0)*{steady}$7,"-")')
        ws.cell(60,col,f'=IF(AND(COUNT({c}58,{steady}$9,{steady}$8,{end}$3,{steady}$10)=5,{steady}$9>=0,{steady}$8>=0),{c}58*({steady}$9-{steady}$8)+({c}58-{end}$3)*{steady}$10,"-")')
        ws.cell(61,col,f'=IF(COUNT({c}59:{c}60)=2,{c}59-{c}60,"-")')
    for row,k in enumerate((-2,-1,0,1,2),51):
        ws.cell(row,1,f'=IF(AND(COUNT({ref("wacc")},{ref("wacc_step")})=2,{ref("wacc_step")}>0),{ref("wacc")}+({k})*{ref("wacc_step")},"-")')
        terms=','.join(f'{L(c)}$19*{L(c)}$21/(1+$A{row})^{L(c)}$22' for c in range(first,last+1))
        for col in range(2,7):
            c=L(col)
            ws.cell(row,col,f'=IF(AND(COUNT($A{row},{c}$50,{ref("equity_bridge")},{ref("shares_mn")},{ref("price_factor")},{c}$61)=6,COUNT({L(first)}$19:{end}$19)={len(periods)},$A{row}>{c}$50,$A{row}>-1,{c}$50>-1,{ref("shares_mn")}>0),(SUM({terms})+{c}$61/($A{row}-{c}$50)/(1+$A{row})^{end}$22+{ref("equity_bridge")})*100/{ref("shares_mn")}*{ref("price_factor")},"-")')
    mark_subtotal(ws,53,4,4,fill=True)
    write_section_row(ws,63,['Capex判断与主要来源'],nc)
    note=i.get('capex_note') or evidence_for(i,'cashflow.capex_ratio').get('rationale') or 'Capex采用已保存的预测假设。'
    _note(ws,64,note,nc)
    row=66
    for source in i.get('capex_references') or []:
        _note(ws,row,'来源：'+str(source.get('institution') or '')+' '+str(source.get('date') or '')+' '+str(source.get('title') or ''),nc)
        if source.get('url'): ws.cell(row,1).hyperlink=source['url']
        row+=1
    for row in (6,15,19,24): mark_subtotal(ws,row,1,tc,fill=True)
    for key in ('enterprise_value','equity_value','price'): mark_subtotal(ws,ws[addr(key)].row,1,3,fill=True)
    _finish(ws)
    _format_wacc(ws)
    for row in (7,8,9,10,21):
        for col in range(2,tc+1): ws.cell(row,col).number_format=PCT_FMT
    for col in range(2,last+1): ws.cell(20,col).number_format='yyyy-mm-dd'
    for key in ('wacc','growth','terminal_margin','wacc_step','growth_step','terminal_share','upside'): ws[addr(key)].number_format=PCT_FMT
    ws[addr('valuation_date')].number_format='yyyy-mm-dd'
    for key in ('price','current_price'): ws[addr(key)].number_format=PX_FMT
    for row in (50,57):
        for col in range(2,7): ws.cell(row,col).number_format=PCT_FMT
    for row in range(51,56):
        ws.cell(row,1).number_format=PCT_FMT
        for col in range(2,7): ws.cell(row,col).number_format=PX_FMT
