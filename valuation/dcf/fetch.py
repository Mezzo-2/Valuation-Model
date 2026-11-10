"""只读补取少量DCF校准值和市场参数，原始响应单独保存。"""
from __future__ import annotations

from datetime import date, timedelta
import re
from statistics import mean

from valuation.historical_financials.foreign import quantity, currency_of_unit
from valuation.shared.io import dump_json
from valuation.shared.markets import market_of

FIELDS = ["营业收入", "营业总收入", "经营活动现金流量净额", "经营活动现金流净额",
          "折旧与摊销", "折旧及摊销", "折旧损耗摊销", "资本性支出", "购建固定资产等支付现金", "购买固定资产",
          "货币资金", "现金及现金等价物", "期末现金及现金等价物余额", "受限现金", "短期借款", "长期借款", "应付债券",
          "一年内到期的非流动负债", "租赁负债", "短期债务及长期债务流动部分", "长期债务", "债务合计",
          "利息费用", "利息支出", "少数股东权益", "应收账款", "应收票据", "预付款项", "合同资产", "存货",
          "应付账款", "应付票据", "合同负债", "应付账款及应计", "所得税费用", "所得税", "利润总额", "税前利润",
          "长期股权投资", "投资性房地产", "其他权益工具", "应付债券:优先股"]


def canonical_code(value):
    code = str(value).lower().replace('.sz', '').replace('.sh', '')
    if re.fullmatch(r'\d{6}', code):
        return ('sh' if code.startswith(('6', '9')) else 'bj' if code.startswith(('4', '8')) else 'sz') + code
    if code.endswith('.hk'):
        return 'hk' + code[:-3].zfill(5)
    if code.endswith('.us'):
        return 'us' + code[:-3]
    if code.startswith(('sz', 'sh', 'bj', 'hk', 'us')):
        return code
    return 'us' + code


def financial_records(payload, code, as_of, currency=None):
    records = []
    if not isinstance(payload, dict):
        return records
    for row in payload.get('data') or []:
        if canonical_code(row.get('stockCode')) != canonical_code(code):
            continue
        raw_date = str(row.get('reportDate') or '')[:10]
        if not raw_date or raw_date > as_of.isoformat() or row.get('type') not in (None, '', '当年累计'):
            continue
        row_currency = currency_of_unit(row.get('unit') or '')
        if currency and row_currency and row_currency != currency:
            continue
        published = str(row.get('publishDate') or '')[:10]
        availability = 'published_after_valuation_date' if published and published > as_of.isoformat() else 'available' if published else 'unknown'
        for item in row.get('indicators') or []:
            amount = quantity(item.get('value'))
            if amount is not None:
                records.append({'code': row.get('stockCode'), 'period': row.get('reportPeriod'), 'report_date': raw_date,
                                'publish_date': row.get('publishDate'), 'unit': row.get('unit'), 'name': item.get('name'),
                                'raw': item.get('value'), 'amount_yi': amount, 'source': item.get('source'),
                                'currency': row_currency, 'availability': availability})
    return records


def parse_risk_free(payload, as_of):
    data = payload.get('data', {}) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        raise ValueError('无风险利率响应未含指标说明')
    description = data.get('indicator_description') or []
    meta = description[0] if description else {}
    if str(meta.get('单位')) != '%':
        raise ValueError('无风险利率单位未确认为百分数')
    cutoff = str(meta.get('终止日期') or '')
    if not re.fullmatch(r'\d{8}', cutoff):
        raise ValueError('无风险利率数据截止日缺失')
    end = min(as_of, date(int(cutoff[:4]), int(cutoff[4:6]), int(cutoff[6:])))
    values = [(date.fromisoformat(d), float(v) / 100) for d, v in re.findall(r'\|(\d{4}-\d{2}-\d{2})\|([+-]?[\d.]+)\|', data.get('answer') or '')]
    eligible = [(d, v) for d, v in values if d <= end and (as_of - d).days <= 14]
    if not eligible:
        raise ValueError('估值日前14天内没有可用无风险利率')
    chosen, value = max(eligible)
    return {'value': value, 'date': chosen.isoformat(), 'source': meta.get('数据来源'), 'name': meta.get('指标名称'),
            'metadata': meta, 'date_conflict': any(d > end for d, _ in values),
            'rationale': '只采用估值日与元数据截止日之前的近14天候选；仍需关注供应商时点口径。'}


def beta_from_prices(stock, index, as_of):
    info = (stock.get('data') or {})
    bars = info.get('bars') or []
    benchmark = index.get('data') or []
    if not isinstance(benchmark, list):
        raise ValueError('基准序列格式不匹配')
    s = {r['period_end_date']: float(r['close_price']) for r in bars if r.get('close_price') and r.get('period_end_date', '') <= as_of.isoformat()}
    m = {r['trading_day']: float(r['close']) for r in benchmark if r.get('close') and r.get('trading_day', '') <= as_of.isoformat()}
    dates = sorted(set(s) & set(m))
    if len(dates) < 31:
        raise ValueError('Beta回归共同收盘样本少于31个')
    pairs = [{'start': a, 'end': b, 'stock_return': s[b] / s[a] - 1, 'index_return': m[b] / m[a] - 1} for a, b in zip(dates, dates[1:])]
    x = [p['index_return'] for p in pairs]; y = [p['stock_return'] for p in pairs]
    mx, my = mean(x), mean(y)
    xx = sum((v - mx)**2 for v in x); yy = sum((v - my)**2 for v in y)
    xy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    if xx == 0 or yy == 0:
        raise ValueError('Beta回归样本方差为零')
    return {'value': xy / xx, 'r_squared': xy**2 / (xx * yy), 'pairs': pairs, 'observations': len(pairs),
            'start': dates[0], 'end': dates[-1], 'adjust': (info.get('meta') or {}).get('adjust', 'unknown'),
            'rationale': '两年周频价格收益率OLS含截距；未调整无风险超额收益，采用原始Beta；未来杠杆变化另行研究。'}


def historical_cashflow(facts, records):
    """已有年度D&A、购建支出及收入占比；不拼算历史ONWC或FCFF。"""
    fields = {'da': ('折旧与摊销', '折旧及摊销', '折旧损耗摊销'),
              'capex': ('购建固定资产等支付现金', '资本性支出', '购买固定资产')}
    result = {}
    revenues = (facts.get('income') or {}).get('营业收入') or []
    for index, p in enumerate(facts['hist_periods']):
        year = p[:4]
        annual = [r for r in records if str(r.get('period')) in (year+'Q4',year+'A')]
        row = {}
        for key, names in fields.items():
            item = next((r for name in names for r in annual if r['name']==name), None)
            if item is not None:
                value = item['amount_yi']
                # 此处仅规范已识别的购建支出现金流出符号。
                row[key] = abs(value) if key=='capex' else value
                revenue = revenues[index] if index < len(revenues) else None
                if isinstance(revenue, (int, float)) and revenue > 0:
                    row[key+'_ratio'] = row[key] / revenue
        if row: result[p] = row
    return result


def collect(facts, mcp, log_dir):
    from valuation.dcf.calculate import valuation_date
    code = canonical_code(facts.get('full_code') or facts['ticker'])
    as_of = valuation_date(facts)
    errors = []
    def call(name, tool, args):
        dump_json(log_dir / f'{name}_request.json', {'tool': tool, 'arguments': args})
        try:
            result = mcp.call(tool, args)
            dump_json(log_dir / f'{name}_response.json', result)
            return result
        except Exception as exc:
            errors.append(f'{name}: {type(exc).__name__}')
            return {}
    annual = [{'fiscalYear': int(p[:4]), 'reportType': 'A', 'type': 'year'} for p in facts['hist_periods']]
    periods = annual + [{'fiscalYear': y, 'reportType': t, 'type': 'year'} for y in {as_of.year, as_of.year + 1} for t in ('Q1', 'S1', 'Q3')]
    raw = call('financials', 'company_finance_search', {'querys': [code], 'reportDates': periods, 'searchData': FIELDS})
    market = market_of(facts)
    currency = facts.get('currency') or ('CNY' if market == 'A' else None)
    records = financial_records(raw, code, as_of, currency)
    rf = None
    series = {'CNY': ('中债国债到期收益率:10年', '中国', '中国债券信息网'), 'USD': ('美国:国债收益率:10年', '美国', '美联储')}
    if currency in series:
        name, country, source = series[currency]
        payload = call('risk_free', 'get_economic_indicator_data', {'index_name': name, 'country': country, 'data_source': source,
                       'start': (as_of - timedelta(days=30)).strftime('%Y%m'), 'end': as_of.strftime('%Y%m')})
        try:
            rf = parse_risk_free(payload, as_of)
        except ValueError as exc:
            errors.append(str(exc))
    beta = None
    end = as_of - timedelta(days=1)
    # 只使用最近已完成的周线，避免盘中/未完结周混入。
    end -= timedelta(days=(end.weekday() - 4) % 7)
    start = end - timedelta(days=731)
    stock = call('beta_stock', 'get_stock_kline', {'query': code, 'freq': '1w', 'startDateTime': start.isoformat(), 'endDateTime': end.isoformat(),
                 'limit': 200, 'order': 'asc', 'fields': ['stock_full_code', 'currency', 'period_end_date', 'close_price']})
    benchmark = {'A': '沪深300', 'HK': '恒生指数', 'US': '标普500'}[market]
    index = call('beta_index', 'searchIndexQuotation', {'queries': [benchmark], 'mode': 'kline', 'period_type': 'W',
                 'start_date': start.isoformat(), 'end_date': end.isoformat(), 'limit': 200})
    try:
        info = (stock.get('data') or {}).get('standardized_info') or {}
        if canonical_code(info.get('stock_full_code')) != code:
            raise ValueError('Beta股票序列证券代码不一致')
        beta = beta_from_prices(stock, index, end)
        beta['benchmark'] = benchmark
    except (ValueError, TypeError, KeyError) as exc:
        errors.append(str(exc))
    announcements = call('notes', 'searchAnnouncementReport', {'fullCode': code, 'labelList': ','.join(p[:4]+'A' for p in facts['hist_periods'][-2:]),
                         'query': '资本开支计划与折旧摊销 有息借款租赁及受限现金 经营税率优惠及研发资本化', 'topK': 4})
    docs = []
    for hit in announcements if isinstance(announcements, list) else []:
        docs.append({k: hit.get(k) for k in ('title', 'url', 'businessTime', 'locations')})
        docs[-1]['text'] = (hit.get('chunkText') or hit.get('viewpoint') or '')[:6500]
    result = {'queried_on': date.today().isoformat(), 'valuation_date': as_of.isoformat(), 'code': code, 'currency': currency,
              'financial_records': records, 'historical_cashflow': historical_cashflow(facts, records),
              'risk_free': rf, 'beta': beta, 'disclosures': docs, 'errors': errors}
    dump_json(log_dir / 'collected.json', result)
    return result
