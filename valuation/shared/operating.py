"""共用经营计算；PE研究快照和DCF读取同一条利润预测链。"""
from __future__ import annotations
from valuation.shared.markets import is_foreign, foreign_pnl
from valuation.segment_research.methods import roll_segment

def _income_at(income, name, index):
    values=income.get(name) or []
    value = values[index] if index < len(values) else None
    return float(value) if value is not None else None

def _pnl_roll(
    facts: dict,
    cost: dict,
    revenue: dict[str, float],
    hist: list[str],
    fcst: list[str],
) -> dict[str, dict[str, float | None]]:
    if is_foreign(facts):
        return foreign_pnl(facts, cost, revenue)
    income = facts.get("income") or {}
    shares = float((facts.get("market") or {})["当前总股本_百万股"])
    out: dict[str, dict[str, float | None]] = {}
    periods = hist + fcst
    for i, year in enumerate(hist):
        rev = _income_at(income, "营业收入", i)
        gp = _income_at(income, "毛利", i)
        if gp is None and rev is not None:
            cogs = _income_at(income, "营业成本", i)
            if cogs is not None:
                gp = rev - cogs
        sales = _income_at(income, "销售费用", i)
        admin = _income_at(income, "管理费用", i)
        rd = _income_at(income, "研发费用", i)
        ebit = _income_at(income, "息税前利润", i)
        ni = _income_at(income, "归母净利润", i)
        eps = _income_at(income, "EPS", i)
        tax = _income_at(income, "所得税费用", i)
        pre = _income_at(income, "税前利润", i)
        row = {
            "revenue": rev,
            "gp": gp,
            "gm": None if not rev else (gp or 0) / rev,
            "sales": sales,
            "admin": admin,
            "rd": rd,
            "sales_ratio": None if not rev else (sales or 0) / rev,
            "admin_ratio": None if not rev else (admin or 0) / rev,
            "rd_ratio": None if not rev else (rd or 0) / rev,
            "ebit": ebit,
            "ebit_margin": None if not rev else (ebit or 0) / rev,
            "ni": ni,
            "ni_margin": None if not rev else (ni or 0) / rev,
            "eps": eps,
            "tax_rate": None if not pre else (tax or 0) / pre,
            "rev_yoy": None,
        }
        if i:
            prev = _income_at(income, "营业收入", i - 1)
            if prev:
                row["rev_yoy"] = rev / prev - 1
        out[year] = row
    for year in fcst:
        rev = revenue[year]
        gm = float(cost["gross_margin"][year])
        sr = float(cost["sales_ratio"][year])
        ar = float(cost["admin_ratio"][year])
        dr = float(cost["rd_ratio"][year])
        tax_rate = float(cost["tax_rate"][year])
        fin = float(cost["fin_exp"][year])
        nonop_inc = float(cost["nonop_inc"][year])
        nonop_exp = float(cost["nonop_exp"][year])
        minority = float((cost.get("minority") or {}).get(year) or 0.0)
        other_op = float((cost.get("other_op") or {}).get(year) or 0.0)
        gp = rev * gm
        sales = rev * sr
        admin = rev * ar
        rd = rev * dr
        ebit = gp - sales - admin - rd + other_op
        pre = ebit - fin + nonop_inc - nonop_exp
        tax = pre * tax_rate
        mino = (pre - tax) * minority
        ni = pre - tax - mino
        eps = ni * 100 / shares
        out[year] = {
            "revenue": rev,
            "gp": gp,
            "gm": gm,
            "sales": sales,
            "admin": admin,
            "rd": rd,
            "sales_ratio": sr,
            "admin_ratio": ar,
            "rd_ratio": dr,
            "ebit": ebit,
            "ebit_margin": ebit / rev if rev else None,
            "ni": ni,
            "ni_margin": ni / rev if rev else None,
            "eps": eps,
            "tax_rate": tax_rate,
            "fin": fin,
            "other_op": other_op,
            "rev_yoy": None,
        }
    for i, year in enumerate(periods):
        if i == 0:
            continue
        prev = periods[i - 1]
        cur = out[year]["revenue"]
        pri = out[prev]["revenue"]
        if cur is not None and pri:
            out[year]["rev_yoy"] = cur / pri - 1
    return out



def operating_from_spec(spec):
    facts=spec['facts']; hist=facts['hist_periods']; fcst=facts['forecast_periods']
    revenues=[]
    for seg in spec['split']['final_segments']:
        forecast=spec['forecasts'][seg['name']]
        raw=seg.get('hist_revenue') or {}
        hist_rev=dict(raw) if isinstance(raw,dict) else dict(zip(hist,raw))
        fallback=(forecast.get('historical_data') or {}).get('分部收入') or {}
        for period in hist:
            if period not in hist_rev and period in fallback: hist_rev[period]=fallback[period]
        values,_=roll_segment(seg['method'],forecast,hist,fcst,hist_rev)
        revenues.append(values)
    revenue={p:sum(item[p] for item in revenues) for p in hist+fcst}
    return revenue,_pnl_roll(facts,spec['cost'],revenue,hist,fcst)
