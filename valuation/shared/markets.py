"""港美市场的少量共用规则；未指定市场的旧事实文件仍走 A 股路径。"""
from __future__ import annotations

import math
import re


def market_of(value: dict | str) -> str:
    if isinstance(value, dict):
        if value.get("market_type") in {"A", "HK", "US"}:
            return value["market_type"]
        value = str(value.get("full_code") or value.get("ticker") or "")
    token = value.strip().upper()
    if re.fullmatch(r"HK\d{1,5}", token) or token.endswith(".HK") or re.fullmatch(r"\d{5}", token):
        return "HK"
    if token.startswith("US") or token.endswith((".US", ".NASDAQ", ".NYSE")) or re.fullmatch(r"[A-Z][A-Z.\-]*", token):
        return "US"
    return "A"


def is_foreign(facts: dict) -> bool:
    return market_of(facts) != "A"


def expenses(facts: dict) -> list[tuple[str, str]]:
    result = [("SG&A", "sga_ratio")] if market_of(facts) == "US" else [
        ("销售费用", "sales_ratio"), ("管理费用", "admin_ratio")]
    if any(v is not None for v in (facts.get("income", {}).get("研发费用") or [])):
        result.append(("研发费用", "rd_ratio"))
    return result


def label(facts: dict, key: str) -> str:
    if not is_foreign(facts):
        from valuation.historical_financials.subjects import fs_label
        return fs_label(key)
    us = market_of(facts) == "US"
    market_unit = {"CNY": "亿元", "USD": "亿美元", "HKD": "亿港元"}.get(facts.get("trading_currency"), "亿（币种待确认）")
    names = {"EPS": "基本每股收益（扣非）" if us else "基本每股收益",
             "营业利润": "营业利润（SOPR）" if us else "营业利润",
             "息税前利润": "营业利润（SOPR）" if us else "营业利润",
             "净利润": "净利润（扣非）" if us else "净利润",
             "SG&A": "销售及一般管理费用（SG&A）",
             "货币资金": "现金及现金等价物",
             "营业成本": "营业成本" if us else "销售成本",
             "当前股价": "当前股价", "当前总股本_百万股": "当前总股本（百万股）",
             "历史加权平均股本_百万股": "基本加权平均股数（百万股）",
             "流通股本_百万股": "流通股本（百万股）",
             "总市值_亿": f"总市值（{market_unit}）", "流通市值_亿": f"流通市值（{market_unit}）"}
    if key == "研发费用" and (facts.get("rd_in_admin") or facts.get("rd_in_sga")):
        return "其中：研发费用"
    return names.get(key, key)


def register_labels(wb, facts: dict) -> None:
    if not is_foreign(facts):
        return
    from valuation.historical_financials.subjects import fs_label
    keys = ["EPS", "营业利润", "息税前利润", "净利润", "SG&A", "货币资金", "营业成本",
            "研发费用", "当前股价", "当前总股本_百万股", "流通股本_百万股", "历史加权平均股本_百万股", "总市值_亿", "流通市值_亿"]
    aliases = {name: label(facts, key) for key in keys for name in (key, fs_label(key))}
    aliases["有效税率"] = "税费/税前利润"
    for sheet in wb:
        sheet._subject_labels = aliases


def validate_pe_basis(facts: dict, comps: dict) -> None:
    if not is_foreign(facts):
        return
    core = comps.get("core") or []
    if not core:
        raise ValueError("没有可比盈利口径的同业PE")
    year = facts["forecast_periods"][0]
    if not facts.get("eps_scope"):
        raise ValueError("本公司基本EPS口径未确认")
    for item in core:
        if item.get("eps_scope") and item["eps_scope"] != facts["eps_scope"]:
            raise ValueError(f"{item.get('name')} 同业PE的盈利口径与本公司明确不一致")
        if item.get("forecast_period") and str(item["forecast_period"]) not in {year, year[:4]}:
            raise ValueError(f"{item.get('name')} 同业PE的预测财年与本公司明确不一致")
        if not number(item.get("pe_y1")) or item["pe_y1"] <= 0:
            raise ValueError(f"{item.get('name')} 缺少有效同业PE")


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def at(facts: dict, key: str, i: int):
    values = facts.get("income", {}).get(key) or []
    return values[i] if i < len(values) and number(values[i]) else None


def safe_ratio(num, den):
    return num / den if number(num) and number(den) and den != 0 else None


def valuation_factor(facts: dict) -> float:
    """将每普通股、报表币种的 EPS 转为每交易单位、交易币种。"""
    if not is_foreign(facts):
        return 1.0
    m = facts.get("market") or {}
    report, trade = facts.get("currency"), facts.get("trading_currency")
    fx = 1.0 if report and report == trade else m.get("报表币种兑交易币种")
    shares = m.get("每交易单位普通股数")
    if not number(fx) or fx <= 0:
        raise ValueError(f"缺少 {report or '?'}→{trade or '?'} 汇率（每单位报表币种兑换交易币种）")
    if not number(shares) or shares <= 0:
        raise ValueError("缺少已核实的每交易单位普通股数（普通股为 1，ADS 按实际比例）")
    return fx * shares


def valuation_fx_note(facts: dict) -> str:
    fx = facts.get("valuation_fx") or {}
    if not fx:
        return ""
    ratio = (facts.get("market") or {}).get("每交易单位普通股数", facts.get("ordinary_per_quote"))
    return (f"1 {fx['base']}＝{fx['rate']:.6f} {fx['quote']}；汇率日期 {fx['date']}；"
            f"来源 {fx['source']}；每交易单位普通股数 {ratio}；汇率和股份比例各换算一次")


# 复用 A 股金额字段，仅为实际适用的联营损益、优先股股息增加字段。
PROFIT_ITEMS = {
    "other_op": ("其他经营净收益", "operating", 1),
    "fin_exp": ("财务费用", "pretax", -1),
    "nonop_inc": ("营业外收入", "pretax", 1),
    "nonop_exp": ("营业外支出", "pretax", -1),
    "associates": ("联营公司损益", "pretax", 1),
    "preferred_dividends": ("优先股股息", "common", -1),
}


def profit_items(facts: dict, cost: dict | None = None, *, stage: str | None = None):
    """只列有实际历史金额或非零预测的收支，不为模板预留零行。"""
    cost = cost or {}
    return [(key, name, sign) for key, (name, group, sign) in PROFIT_ITEMS.items()
            if (stage is None or stage == group) and any(number(v) and v != 0 for v in
                [*(facts.get("income", {}).get(name) or []), *((cost.get(key) or {}).values())])]


def forecast_keys(facts: dict, cost: dict | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ratios = ("gross_margin", *(k for _, k in expenses(facts)), "tax_rate", "minority")
    amounts = tuple(key for key, (name, _, _) in PROFIT_ITEMS.items()
                    if (cost or {}).get(key) or any(number(v) for v in facts.get("income", {}).get(name, [])))
    return ratios, amounts


def snapshot_ordinary_shares(facts: dict) -> float:
    market = facts.get("market") or {}
    shares, ratio = market.get("当前总股本_百万股"), market.get("每交易单位普通股数")
    if not number(shares) or shares <= 0 or not number(ratio) or ratio <= 0:
        raise ValueError("缺少有效快照股本或已核实的普通股/ADS比例")
    return shares * ratio


def foreign_pnl(facts: dict, cost: dict, revenue: dict) -> dict:
    """与 Excel 公式相同的港美盈利链，供研究摘要及回归使用。"""
    hist, fcst = facts["hist_periods"], facts["forecast_periods"]
    result = {}
    for i, year in enumerate(hist):
        rev, gp, op = at(facts, "营业收入", i), at(facts, "毛利", i), at(facts, "营业利润", i)
        ni = at(facts, "归母净利润", i)
        row = {"revenue": rev, "gp": gp, "gm": safe_ratio(gp, rev), "ebit": op,
               "ebit_margin": safe_ratio(op, rev), "ni": ni, "ni_margin": safe_ratio(ni, rev),
               "eps": at(facts, "EPS", i), "tax_rate": safe_ratio(at(facts, "所得税费用", i), at(facts, "税前利润", i)),
               "sales": at(facts, "销售费用", i), "admin": at(facts, "管理费用", i),
               "rd": at(facts, "研发费用", i), "rev_yoy": None}
        for key, ratio in [("销售费用", "sales_ratio"), ("管理费用", "admin_ratio"),
                           ("研发费用", "rd_ratio"), ("SG&A", "sga_ratio")]:
            row[ratio] = safe_ratio(at(facts, key, i), rev)
        result[year] = row
    shares = snapshot_ordinary_shares(facts)
    for year in fcst:
        rev = revenue[year]
        gp = rev * cost["gross_margin"][year]
        fees = {key: rev * cost[ratio][year] for key, ratio in expenses(facts)}
        deductible = sum(v for key, v in fees.items() if key != "研发费用" or not (facts.get("rd_in_admin") or facts.get("rd_in_sga")))
        amount = lambda stage: sum(sign * cost[key][year] for key, _, sign in profit_items(facts, cost, stage=stage))
        op = gp - deductible + amount("operating")
        pre = op + amount("pretax")
        tax = pre * cost["tax_rate"][year]
        net = pre - tax
        minority = net * cost["minority"][year]
        parent = net - minority
        common = parent + amount("common")
        result[year] = {"revenue": rev, "gp": gp, "gm": cost["gross_margin"][year],
                        "ebit": op, "ebit_margin": safe_ratio(op, rev), "pre": pre, "tax": tax,
                        "net": net, "minority": minority, "ni": parent,
                        "ni_margin": safe_ratio(parent, rev), "eps": common * 100 / shares,
                        "tax_rate": cost["tax_rate"][year], "rev_yoy": None,
                        "other_op": amount("operating"),
                        "sales": fees.get("销售费用"), "admin": fees.get("管理费用"), "rd": fees.get("研发费用")}
        for key in ("sales_ratio", "admin_ratio", "rd_ratio", "sga_ratio"):
            result[year][key] = (cost.get(key) or {}).get(year)
    periods = hist + fcst
    for i, year in enumerate(periods[1:], 1):
        ratio = safe_ratio(result[year]["revenue"], result[periods[i-1]]["revenue"])
        result[year]["rev_yoy"] = None if ratio is None else ratio - 1
    return result
