"""市场科目与计算规则；不创建工作表、不指定布局或样式。"""
from valuation.historical_financials.subjects import INCOME_ORDER, BALANCE_ORDER, HIST_METRICS
from valuation.shared.markets import expenses, is_foreign, market_of, profit_items, PROFIT_ITEMS, number


def fee_rows(facts):
    if is_foreign(facts):
        return expenses(facts)
    return [("销售费用", "sales_ratio"), ("管理费用", "admin_ratio"), ("研发费用", "rd_ratio")]


def deductible_fees(facts):
    return [key for key, _ in fee_rows(facts) if key != "研发费用"
            or not (facts.get("rd_in_admin") or facts.get("rd_in_sga"))]


def historical_rows(facts):
    if not is_foreign(facts):
        return INCOME_ORDER, BALANCE_ORDER, HIST_METRICS
    income = ["营业收入", "营业成本", "毛利", *(k for k, _ in fee_rows(facts)), "营业利润",
              "税前利润", "所得税费用", "净利润", "归母净利润", "少数股东损益", "EPS"]
    for stage, before in [("operating", "营业利润"), ("pretax", "税前利润"), ("common", "EPS")]:
        for name, group, _ in PROFIT_ITEMS.values():
            if group == stage and any(number(v) for v in facts["income"].get(name, [])):
                income.insert(income.index(before), name)
    if any(v is not None for v in facts["income"].get("历史加权平均股本_百万股", [])):
        income.append("历史加权平均股本_百万股")
    balance = [key for key in BALANCE_ORDER if key in facts["balance"]
               and (key != "少数股东权益" or any(v is not None for v in facts["balance"][key]))]
    metrics = [("营业收入增速", "营业收入", "营业收入"), ("毛利率", "毛利", "营业收入"),
               *[(ratio_name(facts, ratio), key, "营业收入") for key, ratio in fee_rows(facts)],
               (operating_margin_name(facts), "营业利润", "营业收入"),
               ("税费/税前利润", "所得税费用", "税前利润")]
    return income, balance, metrics


def ratio_name(facts, key):
    names = {"gross_margin": "合并毛利率", "sales_ratio": "销售费用率", "admin_ratio": "管理费用率",
             "rd_ratio": "研发费用率", "sga_ratio": "SG&A费用率", "tax_rate": "有效税率",
             "minority": "少数股东比率"}
    if is_foreign(facts) and key == "tax_rate":
        return "税费/税前利润"
    return names[key]


def assumption_rows(facts, cost=None):
    rows = [("gross_margin", "合并毛利率", "毛利", "营业收入"),
            *[(ratio, ratio_name(facts, ratio), key, "营业收入") for key, ratio in fee_rows(facts)],
            ("tax_rate", ratio_name(facts, "tax_rate"), "所得税费用", "税前利润")]
    if is_foreign(facts):
        rows += [("minority", "少数股东比率", "少数股东损益", "净利润")]
        rows += [(key, "财务费用假设" if key == "fin_exp" else name, name, None)
                 for key, name, _ in profit_items(facts, cost)]
    else:
        rows += [("fin_exp", "财务费用假设", "财务费用", None),
                 ("nonop_inc", "营业外收入", "营业外收入", None),
                 ("nonop_exp", "营业外支出", "营业外支出", None),
                 ("minority", "少数股东比率", None, None),
                 ("other_op", "其他经营净收益", None, None)]
    return rows


def pnl_rows(facts, cost=None):
    if not is_foreign(facts):
        return ["营业收入", "营业收入同比", "营业成本", "合并毛利率", "毛利", "销售费用", "管理费用", "研发费用",
                "息税前利润", "财务费用", "营业外收入", "营业外支出", "税前利润", "所得税费用", "少数股东损益",
                "归母净利润", "归母净利润同比", "EPS"]
    return ["营业收入", "营业收入同比", "营业成本", "合并毛利率", "毛利", *(k for k, _ in fee_rows(facts)),
            "营业利润", *[name for _, name, _ in profit_items(facts, cost, stage="pretax")],
            "税前利润", "所得税费用", "净利润", "少数股东损益", "归母净利润", "归母净利润同比",
            *[name for _, name, _ in profit_items(facts, cost, stage="common")], "EPS"]


def operating_key(facts):
    return "营业利润" if is_foreign(facts) else "息税前利润"


def operating_margin_name(facts):
    return "SOPR利润率" if market_of(facts) == "US" else "营业利润率" if is_foreign(facts) else "息税前利润率"


def ratio_formula(num, den, *, growth=False, guarded=False):
    body = f"{num}/{den}" + ("-1" if growth else "")
    return (f'=IF(AND(ISNUMBER({num}),ISNUMBER({den}),{den}<>0),{body},"-")'
            if guarded else "="+body)


def foreign_pnl_formulas(facts, cost, ref, assume, revenue, build, shares):
    fees = "+".join(ref(k) for k in deductible_fees(facts))
    def terms(stage, lookup):
        return "".join(("+" if sign > 0 else "-") + lookup(key, name)
                       for key, name, sign in profit_items(facts, cost, stage=stage))
    return {"营业收入": revenue, "营业成本": build("营业成本"),
            "毛利": build("毛利"),
            **{key: build(key) for key, _ in fee_rows(facts)},
            "营业利润": f"{ref('毛利')}-({fees})"+terms("operating", lambda key, name: assume(key)),
            **{name: assume(key) for stage in ("pretax", "common")
               for key, name, _ in profit_items(facts, cost, stage=stage)},
            "税前利润": ref('营业利润')+terms("pretax", lambda key, name: ref(name)),
            "所得税费用": f"{ref('税前利润')}*{assume('tax_rate')}",
            "净利润": f"{ref('税前利润')}-{ref('所得税费用')}",
            "少数股东损益": f"{ref('净利润')}*{assume('minority')}",
            "归母净利润": f"{ref('净利润')}-{ref('少数股东损益')}",
            "EPS": f"({ref('归母净利润')}"+terms("common", lambda key, name: ref(name))+f")*100/({shares})"}


def pnl_note(facts):
    if is_foreign(facts):
        return ("历史列沿用披露值；预测按适用费用和有依据的经营项目推导营业利润，再计算税前利润、净利润和归母净利润。"
                "预测EPS沿用财务快照股本，假设股本持平；ADS先按已核实比例换成普通股数；其中项费用不重复扣减。")
    return ("历史列沿用历史财务数据口径。预测列：息税前利润＝毛利－销售费用－管理费用－研发费用＋其他经营净收益；"
            "税前利润＝息税前利润－财务费用＋营业外收入－营业外支出。"
            "历史披露或接口的息税前利润口径可能与上述模型定义不同。")


def assumption_history_formula(facts, key, numerator, denominator, ref):
    if numerator:
        return (ratio_formula(ref(numerator), ref(denominator), guarded=is_foreign(facts))
                if denominator else "="+ref(numerator))
    if not is_foreign(facts) and key == "minority":
        return f"={ref('少数股东损益')}/({ref('归母净利润')}+{ref('少数股东损益')})"
    if not is_foreign(facts) and key == "other_op":
        return f"={ref('息税前利润')}-({ref('毛利')}-{ref('销售费用')}-{ref('管理费用')}-{ref('研发费用')})"
    return "-"
