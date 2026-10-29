"""估值币种转换：进门 CFETS 中间价，财务原值不换币。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from valuation.comein.comein import ComeinClient
from valuation.shared.env import load_env
from valuation.shared.io import dump_json, load_json, logs_dir, spec_dir
from valuation.shared.markets import is_foreign, number

SOURCE = "中国外汇交易中心"
TOOL = "get_economic_indicator_data"
# 每一单位外币兑换的人民币；仅接入已明确方向的每日中间价序列。
INDICATORS = {"HKD": "中间价:港元兑人民币", "USD": "中间价:美元兑人民币"}
MAX_AGE_DAYS = 1


def run_date() -> date:
    """估值汇率以北京时间的运行日期为准，不沿用行情快照日期。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def parse_fixing_rates(raw: dict, indicator: str, as_of: date) -> dict[date, float]:
    if not isinstance(raw, dict) or raw.get("code") not in (1, "1"):
        raise ValueError("汇率接口未成功返回")
    body = raw.get("data") or {}
    if not isinstance(body, dict):
        raise ValueError("汇率响应结构无法识别")
    descriptions = body.get("indicator_description") or []
    if len(descriptions) != 1:
        raise ValueError("汇率序列元信息不唯一")
    description = descriptions[0]
    if (description.get("指标名称") != indicator or description.get("数据来源") != SOURCE
            or description.get("频率") != "D"):
        raise ValueError("汇率指标、来源或频度不匹配")
    # 不将百分比、每百单位外币等序列当作每一单位外币。
    unit = str(description.get("单位") or "")
    if any(token in unit for token in ("100", "百", "%")):
        raise ValueError("汇率单位与每一单位外币不符")
    answer = str(body.get("answer") or "")
    if f"|日期|{indicator}|" not in answer.replace(" ", ""):
        raise ValueError("汇率表头不匹配")
    rates = {}
    for line in answer.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 2:
            continue
        try:
            day = date.fromisoformat(cells[0])
            value = float(cells[1].replace(",", ""))
        except ValueError:
            continue
        # 当天优先，缺失仅回退前一个自然日；接口超范围返回的日期在此排除。
        if not 0 <= (as_of-day).days <= MAX_AGE_DAYS or not number(value) or value <= 0:
            continue
        if day in rates and rates[day] != value:
            raise ValueError("同日汇率值冲突")
        rates[day] = value
    if not rates:
        raise ValueError("运行当天及前一天均没有可用的日频中间价")
    return rates


def fetch_valuation_fx(mcp: ComeinClient, base: str, quote: str, as_of: date, log: Path) -> dict:
    return fetch_fx_conversions(mcp, [(base, quote)], as_of, log)[0]


def fetch_fx_conversions(
    mcp: ComeinClient,
    pairs: list[tuple[str, str]],
    as_of: date,
    log: Path,
) -> list[dict]:
    """按实际币对需求取日频报价，同次转换统一使用当天或前一天的同日数据。"""
    pairs = list(dict.fromkeys(pairs))
    if not pairs:
        return []
    if as_of > run_date():
        raise ValueError("估值基准日不能晚于今日")
    currencies = {currency for pair in pairs for currency in pair}
    if not currencies <= {"CNY", *INDICATORS}:
        raise ValueError("尚未接入币种汇率：" + "、".join(sorted(currencies - {"CNY", *INDICATORS})))
    legs, raw_sources = {}, []
    for currency in sorted(currencies-{"CNY"}):
        indicator = INDICATORS[currency]
        arguments = {"index_name": indicator, "data_source": SOURCE,
                     "start": (as_of-timedelta(days=MAX_AGE_DAYS)).strftime("%Y%m"),
                     "end": as_of.strftime("%Y%m")}
        raw = mcp.call(TOOL, arguments)
        dump_json(log / f"valuation_fx_{currency}.json", {"arguments": arguments, "response": raw})
        legs[currency] = parse_fixing_rates(raw, indicator, as_of)
    common = (set.intersection(*(set(series) for series in legs.values())) if legs else {as_of})
    if not common:
        raise ValueError("汇率转换各段没有同日可用报价")
    day = max(common)
    for currency, series in legs.items():
        raw_sources.append({"indicator": INDICATORS[currency], "base": currency,
                            "quote": "CNY", "rate": series[day], "date": day.isoformat()})
    result = []
    for base, quote in pairs:
        cny_per_base = 1.0 if base == "CNY" else legs[base][day]
        cny_per_quote = 1.0 if quote == "CNY" else legs[quote][day]
        result.append({"base": base, "quote": quote, "rate": cny_per_base/cny_per_quote,
            "date": day.isoformat(), "as_of": as_of.isoformat(), "source": SOURCE,
            "tool": TOOL, "method": "运行当天优先，缺失回退前一天；交叉汇率使用同日中间价",
            "raw_quotes": raw_sources,
            "retrieved_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")})
    return result


def ensure_valuation_fx(run_dir: Path, *, required: bool = True) -> dict | None:
    path = spec_dir(run_dir) / "facts.json"
    facts = load_json(path)
    if not is_foreign(facts):
        return None
    base, quote = facts.get("currency"), facts.get("trading_currency")
    market = facts.setdefault("market", {})
    if base and base == quote:
        # 币种变更为相同时，旧换算和旧说明均不再适用。
        had_fx = "valuation_fx" in facts or "报表币种兑交易币种" in market
        facts.pop("valuation_fx", None)
        market.pop("报表币种兑交易币种", None)
        if had_fx:
            dump_json(path, facts)
        return None
    # 已保存汇率仅用于追溯；每次调用都重新取数，刷新失败也不能继续用旧值。
    facts.pop("valuation_fx", None)
    market.pop("报表币种兑交易币种", None)
    as_of = run_date()
    error_path = logs_dir(run_dir) / "valuation_fx_error.json"
    try:
        if not base or not quote:
            raise ValueError("财务币种或交易币种未确认")
        load_env()
        with ComeinClient() as mcp:
            fx = fetch_valuation_fx(mcp, base, quote, as_of, logs_dir(run_dir)/"mcp")
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        dump_json(path, facts)
        dump_json(error_path, {"pair": f"{base}→{quote}", "as_of": as_of.isoformat(), "reason": reason})
        if required:
            raise SystemExit(f"估值汇率自动获取失败：{base}→{quote}；{reason}。仅接受运行当天或前一天的报价，可核对接口后重试。") from None
        return None
    market["报表币种兑交易币种"] = fx["rate"]
    facts["valuation_fx"] = fx
    dump_json(path, facts)
    error_path.unlink(missing_ok=True)
    print(f"valuation_fx: 1 {base} = {fx['rate']:.6f} {quote}；{fx['date']}；{fx['source']}")
    return fx
