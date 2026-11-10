from __future__ import annotations

import calendar
from datetime import date
import hashlib
import json

from valuation.shared.markets import is_foreign, number, valuation_factor
from valuation.shared.operating import operating_from_spec

VERSION = 2
CF_KEYS = ("da_ratio", "capex_ratio", "nwc_ratio", "tax_rate", "ebit_adjustment")
TERMINAL_KEYS = ("growth", "ebit_margin", "tax_rate", "da_ratio", "capex_ratio", "nwc_ratio")
BRIDGE_KEYS = ("cash", "restricted_cash", "operating_cash", "non_operating_assets", "debt", "minority", "other_claims")


def evidence_for(inputs, path):
    sources = inputs.get("evidence") or {}
    value = sources.get(path) or sources.get(path.split(".")[0]) or {}
    return value if isinstance(value, dict) else {}


def input_fingerprint(spec, inputs):
    def financial_only(value):
        if isinstance(value, dict):
            return {k: financial_only(v) for k, v in value.items() if not str(k).startswith('_')}
        if isinstance(value, list):
            return [financial_only(v) for v in value]
        return value
    relevant = {k: spec.get(k) for k in ("facts", "split", "forecasts", "cost")}
    raw = json.dumps([VERSION, financial_only(relevant), inputs], sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def valuation_date(facts):
    raw = (facts.get("market") or {}).get("快照日期") or facts.get("as_of")
    return date.fromisoformat(str(raw)[:10])


def period_dates(facts, inputs):
    periods = facts["forecast_periods"]
    explicit = inputs.get("period_end_dates") or {}
    if is_foreign(facts) and any(p not in explicit for p in periods):
        raise ValueError("港美股须明确填写各预测财年的期末日期")
    ends = {p: date.fromisoformat(explicit[p]) if p in explicit else date(int(p[:4]), 12, 31) for p in periods}
    if any(ends[b] <= ends[a] for a, b in zip(periods, periods[1:])):
        raise ValueError("DCF 财年末日期须按预测期递增")
    starts = {}
    previous = None
    for p, end in ends.items():
        if previous is None:
            day = min(end.day, calendar.monthrange(end.year - 1, end.month)[1])
            previous = date(end.year - 1, end.month, day)
        starts[p] = previous
        previous = end
    return starts, ends


def validate_inputs(spec, inputs):
    facts = spec["facts"]
    errors = []
    if inputs.get("company") != facts.get("company") or inputs.get("ticker") != facts.get("ticker"):
        errors.append("DCF 输入的公司或证券代码与模型不一致")
    currency = facts.get("currency") or ("CNY" if not is_foreign(facts) else None)
    if not currency or inputs.get("currency") != currency:
        errors.append("DCF 现金流币种与财报币种不一致")
    try:
        as_of = date.fromisoformat(inputs["valuation_date"])
        if as_of != valuation_date(facts):
            errors.append("DCF 估值日与行情快照日期不一致")
        starts, ends = period_dates(facts, inputs)
        if ends[facts["forecast_periods"][-1]] <= as_of:
            errors.append("全部显性预测期已结束，须先更新经营预测")
    except (KeyError, ValueError, TypeError) as exc:
        errors.append(f"DCF 日期未确认：{exc}")

    def require(path, value, lo=None, hi=None):
        if not number(value):
            errors.append(f"缺少有效输入 {path}")
        elif (lo is not None and value < lo) or (hi is not None and value > hi):
            errors.append(f"{path} 超出适用范围")

    cf = inputs.get("cashflow") or {}
    for p in facts["forecast_periods"]:
        values = cf.get(p) or {}
        for key in CF_KEYS:
            limits = (0, 1) if key == "tax_rate" else ((0, None) if key in {"da_ratio", "capex_ratio"} else (None, None))
            require(f"cashflow.{key}", values.get(key), *limits)
    terminal = inputs.get("terminal") or {}
    for key in TERMINAL_KEYS:
        limits = (0, 1) if key == "tax_rate" else ((0, None) if key in {"da_ratio", "capex_ratio"} else (None, None))
        require(f"terminal.{key}", terminal.get(key), *limits)
    if number(terminal.get("growth")) and terminal["growth"] <= -1:
        errors.append("永续增长率必须大于-100%")
    cc = inputs.get("capital_cost") or {}
    for key in ("risk_free_rate", "beta", "erp", "tax_shield_rate"):
        require(f"capital_cost.{key}", cc.get(key), 0 if key in {"erp", "tax_shield_rate"} else None, 1 if key == "tax_shield_rate" else None)
    if cc.get("kd_mode") == "spread":
        require("capital_cost.credit_spread", cc.get("credit_spread"), 0)
    elif cc.get("kd_mode") == "direct":
        require("capital_cost.debt_cost", cc.get("debt_cost"), 0)
    else:
        errors.append("须指定债务成本口径 spread 或 direct")
    if cc.get("equity_value") is not None:
        require("capital_cost.equity_value", cc["equity_value"], 0)
        if cc["equity_value"] == 0:
            errors.append("股权市值须大于零")
    bridge = inputs.get("bridge") or {}
    for key in BRIDGE_KEYS:
        require(f"bridge.{key}", bridge.get(key), 0)
    if all(number(bridge.get(k)) for k in ("cash", "restricted_cash", "operating_cash")) and bridge["cash"] < bridge["restricted_cash"] + bridge["operating_cash"]:
        errors.append("受限现金与最低经营现金合计超过现金余额")
    if bridge.get("ordinary_shares_mn") is not None:
        require("bridge.ordinary_shares_mn", bridge["ordinary_shares_mn"], 0)
    market = facts.get("market") or {}
    if not number(market.get("当前股价")) or market["当前股价"] <= 0:
        errors.append("缺少有效当前股价")
    shares = bridge.get("ordinary_shares_mn")
    if shares is None:
        shares = market.get("当前总股本_百万股")
    if not number(shares) or shares <= 0:
        errors.append("缺少有效普通股总数")
    try:
        valuation_factor(facts)
    except ValueError as exc:
        errors.append(str(exc))
    if inputs.get("financial_company") is True:
        errors.append("金融企业暂不适用本版 FCFF DCF")
    return list(dict.fromkeys(errors))


def normalized_fcff(revenue, previous_revenue, margin, tax_rate, da_ratio, capex_ratio, nwc_ratio):
    ebit = revenue * margin
    tax = max(ebit, 0) * tax_rate
    return ebit - tax + revenue * (da_ratio - capex_ratio) - (revenue - previous_revenue) * nwc_ratio


def calculate_dcf(spec, inputs):
    result = {"version": VERSION, "company": spec["facts"].get("company"), "ticker": spec["facts"].get("ticker"),
              "valuation_date": inputs.get("valuation_date"), "currency": inputs.get("currency"),
              "status": "unavailable", "gaps": [], "warnings": []}
    if inputs.get("enabled") is False:
        result["status"] = "disabled"
        return result
    errors = validate_inputs(spec, inputs)
    result["gaps"] = errors
    if errors:
        return result
    result["input_hash"] = input_fingerprint(spec, inputs)
    facts = spec["facts"]
    revenue, pnl = operating_from_spec(spec)
    periods = facts["forecast_periods"]
    as_of = date.fromisoformat(inputs["valuation_date"])
    starts, ends = period_dates(facts, inputs)
    market = facts["market"]
    bridge, cc, terminal = inputs["bridge"], inputs["capital_cost"], inputs["terminal"]
    ratio = market.get("每交易单位普通股数", 1.0)
    factor = valuation_factor(facts)
    shares = bridge.get("ordinary_shares_mn")
    if shares is None:
        shares = market["当前总股本_百万股"] * ratio
    fx = factor / ratio
    equity = cc.get("equity_value")
    if equity is None:
        equity = market["当前股价"] * shares / ratio / 100 / fx
    debt = bridge["debt"]
    kd = cc["risk_free_rate"] + cc["credit_spread"] if cc["kd_mode"] == "spread" else cc["debt_cost"]
    ke = cc["risk_free_rate"] + cc["beta"] * cc["erp"]
    weight = debt / (debt + equity)
    wacc = (1 - weight) * ke + weight * kd * (1 - cc["tax_shield_rate"])
    result["capital_cost"] = {"ke": ke, "kd": kd, "equity_value": equity, "debt": debt, "debt_weight": weight, "wacc": wacc}
    if wacc <= terminal["growth"] or wacc <= -1:
        result["gaps"] = ["WACC 必须大于永续增长率且大于-100%"]
        return result
    previous_revenue = revenue[facts["hist_periods"][-1]]
    rows = []
    for p in periods:
        c = inputs["cashflow"][p]
        rev = revenue[p]
        ebit = pnl[p]["ebit"] + c["ebit_adjustment"]
        tax = max(ebit, 0) * c["tax_rate"]
        da, capex = rev * c["da_ratio"], rev * c["capex_ratio"]
        dnwc = (rev - previous_revenue) * c["nwc_ratio"]
        fcff = ebit - tax + da - capex - dnwc
        fraction = max(0, min(1, (ends[p] - max(as_of, starts[p])).days / (ends[p] - starts[p]).days))
        t = max(0, (ends[p] - as_of).days / 365.25)
        pv = fcff * fraction / (1 + wacc) ** t
        rows.append({"period": p, "end_date": ends[p].isoformat(), "revenue": rev, "unadjusted_ebit": pnl[p]["ebit"],
                     "ebit": ebit, "tax": tax, "nopat": ebit - tax, "da": da, "capex": capex,
                     "delta_nwc": dnwc, "fcff": fcff, "remaining_fraction": fraction, "discount_years": t, "pv": pv})
        previous_revenue = rev
    def value_at(rate, growth):
        if rate <= growth or rate <= -1 or growth <= -1:
            return None
        norm = normalized_fcff(previous_revenue * (1 + growth), previous_revenue, terminal["ebit_margin"],
                               terminal["tax_rate"], terminal["da_ratio"], terminal["capex_ratio"], terminal["nwc_ratio"])
        pv = sum(r["fcff"] * r["remaining_fraction"] / (1 + rate) ** r["discount_years"] for r in rows)
        pv_terminal = norm / (rate - growth) / (1 + rate) ** rows[-1]["discount_years"]
        equity_bridge = bridge["cash"] - bridge["restricted_cash"] - bridge["operating_cash"] + bridge["non_operating_assets"] - debt - bridge["minority"] - bridge["other_claims"]
        return {"fcff": norm, "explicit_pv": pv, "terminal_value": norm / (rate - growth), "terminal_pv": pv_terminal,
                "enterprise_value": pv + pv_terminal, "equity_bridge": equity_bridge,
                "equity_value": pv + pv_terminal + equity_bridge,
                "price": (pv + pv_terminal + equity_bridge) * 100 / shares * factor}
    base = value_at(wacc, terminal["growth"])
    sens = inputs.get("sensitivity") or {}
    rate_step, growth_step = sens.get("wacc_step", .005), sens.get("growth_step", .0025)
    if not number(rate_step) or rate_step <= 0 or not number(growth_step) or growth_step <= 0:
        result["gaps"] = ["敏感性步长须为正数"]
        return result
    rates = [wacc + k * rate_step for k in (-2, -1, 0, 1, 2)]
    growths = [terminal["growth"] + k * growth_step for k in (-2, -1, 0, 1, 2)]
    result.update(base)
    result.update(rows=rows, shares_mn=shares, price_factor=factor, growth=terminal['growth'], current_price=market["当前股价"],
                  upside=base["price"] / market["当前股价"] - 1,
                  terminal_share=base["terminal_pv"] / base["enterprise_value"] if base["enterprise_value"] else None,
                  sensitivity={"wacc": rates, "growth": growths, "prices": [[(value_at(r, g) or {}).get("price") for g in growths] for r in rates]})
    if any(r["remaining_fraction"] not in (0, 1) for r in rows):
        result["warnings"].append("首个未结束财年按剩余天数估计FCFF，未调整季节性；现金债务按披露日及显式调整估计。")
    if base["fcff"] <= 0:
        result["warnings"].append("稳态FCFF非正，请复核稳态盈利及再投资假设。")
    if result["terminal_share"] is not None and result["terminal_share"] > .8:
        result["warnings"].append("终值占经营价值超过80%，结果对稳态假设较敏感。")
    result['warnings'].extend(inputs.get('open_gaps') or [])
    # 未来现金流包含研究假设；来源和逐项理由选填，不把缺说明当缺数值。
    result["status"] = "estimated"
    return result


def narrative(result):
    if result.get("status") not in {"ready", "estimated"}:
        return "DCF暂不可计算：" + "；".join(result.get("gaps") or ["关键输入未确认"])
    cc = result["capital_cost"]
    return (f"DCF作为PE主估值的交叉验证：每股价值{result['price']:.2f}，相对现价{result['upside']:+.2%}；"
            f"WACC {cc['wacc']:.2%}，永续增长率{result.get('growth', result['sensitivity']['growth'][2]):.2%}，显性期{len(result['rows'])}年；终值占比"
            + (f"{result['terminal_share']:.2%}" if result['terminal_share'] is not None else "不适用")
            + "。现金流采用收入及增量收入比例法，参数含研究估计；PE主目标价保持独立，不对两种价格自动加权。"
            + " ".join(result.get("warnings") or []))
