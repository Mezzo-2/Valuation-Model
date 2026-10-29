"""本公司一致预期 + 候选 Forward PE。同业 PE 直接取 pricePerformance.peForward。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import math
import re
from typing import Any

from valuation.comein.comein import ComeinClient
from valuation.historical_financials.fx import fetch_fx_conversions, run_date
from valuation.segment_split.schema import require_consensus
from valuation.shared.houses import short_house_name
from valuation.shared.io import dump_json
from valuation.shared.markets import is_foreign

YUAN_PER_YI = 100_000_000.0


def fetch_subject_consensus(
    mcp: ComeinClient,
    facts: dict,
    log_root: Path,
    calls: list[dict[str, Any]],
) -> dict:
    query = _subject_query(facts)
    years = _forecast_years(facts)
    raw = _call(
        calls,
        "getStockProfitForecast",
        {"queries": [query], "forecastYears": years, "size": 20},
        lambda: mcp.profit_forecast([query], forecastYears=years, size=20),
        log_root / "profit_forecast_subject.json",
    )
    # 模型币种已在财务阶段确定；这里只按机构明细的原币种准备转换。
    inputs = list(_revenue_inputs(raw, facts))
    target = _model_currency(facts)
    if inputs and not target:
        raise ValueError("模型财务币种尚未确定，不能处理机构营收预测")
    sources = {_revenue_currency(line, item, block, facts) for block, item, line in inputs}
    pairs = sorted((source, target) for source in sources
                   if source in {"CNY", "HKD", "USD"} and source != target)
    as_of = run_date()
    parse_facts = {**facts, "revenue_fx": []}
    # 本阶段独立重新查询，不把旧的目标价汇率当作机构预测换算的输入。
    parse_facts.pop("valuation_fx", None)
    fx_gaps = []
    if pairs:
        try:
            parse_facts["revenue_fx"] = fetch_fx_conversions(mcp, pairs, as_of, log_root / "consensus_fx")
            calls.append({"tool": "get_economic_indicator_data", "purpose": "机构营收换算", "pairs": pairs, "ok": True})
            dump_json(log_root / "consensus_fx.json", {"as_of": as_of.isoformat(), "rates": parse_facts["revenue_fx"]})
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            fx_gaps.append(f"机构营收换算汇率获取失败：{reason}")
            calls.append({"tool": "get_economic_indicator_data", "purpose": "机构营收换算", "pairs": pairs, "ok": False, "error": reason})
            dump_json(log_root / "consensus_fx.json", {"as_of": as_of.isoformat(), "pairs": pairs, "error": reason})
    houses, gaps = parse_institution_estimates(raw, parse_facts, shares=_subject_shares(facts))
    gaps = fx_gaps + gaps
    if inputs and not any(estimate.get("revenue") is not None
                          for house in houses for estimate in house["estimates"].values()):
        reason = "；".join(dict.fromkeys(gap for gap in gaps if "收入币种" in gap or "汇率" in gap))
        raise ValueError("机构营收处理失败：接口有预测金额，但未得到任何可写入的营收值。" + reason)
    payload = {
        "company": facts.get("company"),
        "ticker": facts.get("ticker"),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "forecast_periods": list(facts.get("forecast_periods") or []),
        "revenue_unit": "亿元",
        "revenue_scope": "营业收入",
        "eps_unit": "元/股",
        "eps_scope": "EPS",
        "institution_estimates": houses,
        "provider_aggregates": [],
        "open_gaps": gaps,
    }
    from valuation.shared.markets import is_foreign
    if is_foreign(facts):
        payload.update(revenue_unit=facts.get("unit"), eps_unit=facts.get("eps_unit"), eps_scope=facts.get("eps_scope"))
    if not houses and not gaps:
        payload["open_gaps"] = ["getStockProfitForecast 未返回可识别机构"]
        payload["coverage_status"] = "unavailable"
    return require_consensus(payload, facts)


def _model_currency(facts: dict) -> str:
    value = facts.get("currency") or ("CNY" if not is_foreign(facts) else "")
    return _currency(value)


def _currency(value: Any) -> str:
    token = str(value or "").strip().upper()
    return "CNY" if token == "RMB" else token


def _revenue_currency(line: dict, item: dict, block: dict, facts: dict) -> str:
    explicit = _currency(line.get("currency"))
    unit = _currency(line.get("unit"))
    inherited = _currency(item.get("currency") or block.get("currency") or
                          (block.get("standardizedInfo") or {}).get("currency"))
    inherited_unit = _currency(item.get("unit") or block.get("unit") or
                               (block.get("standardizedInfo") or {}).get("unit"))
    source = explicit or (unit if re.fullmatch(r"[A-Z]{3}", unit) else "") or inherited
    source = source or (inherited_unit if re.fullmatch(r"[A-Z]{3}", inherited_unit) else "")
    # A股历史路径默认人民币；港美明细没有币种时不猜测。
    return source or ("CNY" if not is_foreign(facts) else "")


def _revenue_inputs(raw: Any, facts: dict):
    block = _first_block(raw)
    years = set(_forecast_years(facts))
    for item in block.get("forecastList") or []:
        if not isinstance(item, dict):
            continue
        for line in item.get("forecastItems") or []:
            if (isinstance(line, dict) and _as_int(line.get("year")) in years
                    and line.get("revenue") is not None):
                yield block, item, line


def fill_candidates(
    mcp: ComeinClient,
    candidates: list[dict[str, Any]],
    facts: dict,
    log_root: Path,
    calls: list[dict[str, Any]],
    log_prefix: str = "peers",
) -> list[dict[str, Any]]:
    subject = _norm_ticker(facts.get("ticker") or "")
    rows = []
    seen: set[str] = set()
    for item in candidates:
        query = _candidate_query(item, facts)
        ticker = _norm_ticker(query)
        if not ticker or ticker == subject or ticker in seen:
            continue
        seen.add(ticker)
        rows.append(
            {
                "name": str(item.get("name") or "").strip(),
                "ticker": ticker,
                "query": query,
                "why": str(item.get("why") or "").strip(),
            }
        )
    if not rows:
        return []
    queries = [row["query"] for row in rows]
    snap_map: dict[str, dict[str, Any]] = {}
    price_map: dict[str, dict[str, Any]] = {}
    for i, chunk in enumerate(_chunks(queries, 10)):
        suffix = "" if i == 0 else f"_{i + 1}"
        snap_map.update(
            _by_ticker(
                _call(
                    calls,
                    "get_financial_snapshot",
                    {"queries": chunk},
                    lambda chunk=chunk: mcp.financial_snapshot(chunk),
                    log_root / f"financial_snapshot_{log_prefix}{suffix}.json",
                )
            )
        )
        price_map.update(
            _price_by_ticker(
                _call(
                    calls,
                    "pricePerformance",
                    {
                        "queries": chunk,
                        "include": [
                            "regular_market",
                            "market_value",
                            "valuation",
                            "standardized_info",
                        ],
                    },
                    lambda chunk=chunk: mcp.price_performance(
                        chunk,
                        include=[
                            "regular_market",
                            "market_value",
                            "valuation",
                            "standardized_info",
                        ],
                    ),
                    log_root / f"price_performance_{log_prefix}{suffix}.json",
                )
            )
        )
    filled = []
    for row in rows:
        ticker = row["ticker"]
        item = _fill_one(row, snap_map.get(ticker) or {}, price_map.get(ticker) or {})
        from valuation.shared.markets import is_foreign, validate_pe_basis
        if is_foreign(facts) and item.get("pe_y1") is not None:
            try:
                validate_pe_basis(facts, {"core": [item]})
            except ValueError as exc:
                item.update(source_pe_y1=item["pe_y1"], pe_y1=None, thin=True, basis_gap=str(exc))
        if is_foreign(facts) and not (snap_map.get(ticker) or price_map.get(ticker)):
            item["data_gap"] = "接口未返回该证券的行情或财务快照"
        filled.append(item)
    return filled


def parse_institution_estimates(
    raw: Any,
    facts: dict,
    *,
    shares: float | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    gaps: list[str] = []
    block = _first_block(raw)
    if not block:
        return [], ["getStockProfitForecast 无本公司数据"]
    fcst = [str(year) for year in (facts.get("forecast_periods") or [])]
    want = {int(year[:4]) for year in fcst if str(year)[:4].isdigit()}
    company = str(facts.get("company") or "")
    houses: list[dict[str, Any]] = []
    for i, item in enumerate(block.get("forecastList") or [], 1):
        if not isinstance(item, dict):
            continue
        inst = short_house_name(item.get("orgNameAbbr") or item.get("institution") or "")
        date = _date(item.get("publishDate") or item.get("report_date"))
        if not inst or not date:
            continue
        estimates: dict[str, dict[str, Any]] = {year: {"revenue": None, "eps": None} for year in fcst}
        for line in item.get("forecastItems") or []:
            if not isinstance(line, dict):
                continue
            year_n = _as_int(line.get("year"))
            if year_n is None or year_n not in want:
                continue
            label = f"{year_n}E"
            if label not in estimates:
                continue
            rev = _to_yi(line.get("revenue"), line.get("unit"))
            profit = _to_yuan(line.get("parentNetProfit"), line.get("unit"))
            revenue_source = None
            revenue_currency = _revenue_currency(line, item, block, facts)
            if is_foreign(facts):
                # 来源未说明 EPS 调整范围/币种/股份单位时，不由通用归母利润硬算。
                def metadata(key):
                    return line.get(key) or item.get(key) or block.get(key) or (block.get("standardizedInfo") or {}).get(key)
                exact = bool(facts.get("eps_scope") and facts.get("eps_unit")) and metadata("epsScope") == facts.get("eps_scope") and metadata("epsUnit") == facts.get("eps_unit")
                source_eps = line.get("basicEps") if line.get("basicEps") is not None else line.get("eps")
                eps = _as_float(source_eps) if exact else None
                currency = metadata("currency")
                rev, revenue_source = _reporting_revenue(rev, revenue_currency, facts)
                if line.get("revenue") is not None and rev is None:
                    gaps.append(f"{inst} {label} 收入币种无法确认或缺少报表币种换算汇率")
                if currency != facts.get("currency"):
                    eps = None
                if eps is None:
                    gaps.append(f"{inst} {label} 缺少可比基本EPS或其口径说明")
            else:
                if revenue_currency != _model_currency(facts):
                    rev, revenue_source = _reporting_revenue(rev, revenue_currency, facts)
                    profit, _ = _reporting_revenue(profit, revenue_currency, facts)
                    if line.get("revenue") is not None and rev is None:
                        gaps.append(f"{inst} {label} 收入币种无法确认或缺少报表币种换算汇率")
                eps = (profit / shares) if profit is not None and shares else None
            estimates[label] = {"revenue": rev, "eps": eps}
            if revenue_source is not None:
                estimates[label]["revenue_source"] = {
                    "value": line.get("revenue"), "unit": line.get("unit"),
                    "report_date": date, "source_title": f"{inst}-{company}-{date}",
                    "source_record_index": i, **revenue_source}
        houses.append(
            {
                "institution": inst,
                "report_date": date,
                "source_id": f"C{i}",
                "source_tool": "getStockProfitForecast",
                "source_title": f"{inst}-{company}-{date}",
                "estimates": estimates,
            }
        )
    houses = _dedup_houses(houses)
    if shares is None:
        gaps.append("缺股本，机构 EPS 未回推")
    if not houses:
        gaps.append("盈利预测没有可点名机构")
    return houses, gaps


def _reporting_revenue(value: float | None, currency: Any, facts: dict) -> tuple[float | None, dict | None]:
    """将机构原币种金额统一到已确定的模型币种，收入与EPS覆盖独立。"""
    source = _currency(currency)
    target = _model_currency(facts)
    if value is None or not math.isfinite(value) or not source or not target:
        return None, None
    provenance = {"currency": source}
    if source == target:
        return value, provenance
    # 显式提供本阶段汇率时，包含空列表也不能回退到旧估值汇率。
    rates = facts.get("revenue_fx") if "revenue_fx" in facts else [facts.get("valuation_fx") or {}]
    for fx in rates or []:
        rate = _as_float(fx.get("rate"))
        if rate is None or not math.isfinite(rate) or rate <= 0:
            continue
        if (source, target) == (fx.get("base"), fx.get("quote")):
            conversion = rate
        elif (source, target) == (fx.get("quote"), fx.get("base")):
            conversion = 1 / rate
        else:
            continue
        return value * conversion, {**provenance, "fx": {"base": source, "quote": target,
            "rate": conversion, "date": fx.get("date"), "source": fx.get("source")}}
    return None, None


def _fill_one(
    row: dict[str, Any],
    snapshot: dict,
    price: dict,
) -> dict[str, Any]:
    ticker = row["ticker"]
    snapshot = _aligned_block(snapshot, ticker, row.get("query"))
    price = _aligned_block(price, ticker, row.get("query"))
    info = snapshot.get("standardizedInfo") or {}
    price_info = price.get("standardized_info") or {}
    name = (
        info.get("assetName")
        or price_info.get("stockShortName")
        or row.get("name")
        or row["ticker"]
    )
    currency = str(price_info.get("currency") or "CNY").upper()
    _shares, last = _shares_and_last(snapshot, price)
    mcap = _to_yi((price.get("market_value") or {}).get("totalMarketValue"), currency)
    if mcap is None:
        mcap = _snapshot_number(snapshot, "总市值")
        if mcap is not None and mcap > 1000:
            mcap = mcap / YUAN_PER_YI
    valuation = price.get("valuation") or {}
    pe_y1 = _as_float(valuation.get("peForward"))
    if pe_y1 is not None and pe_y1 <= 0:
        pe_y1 = None
    pe_ttm = _as_float(valuation.get("peTtm"))
    if pe_ttm is None:
        pe_ttm = _snapshot_number(snapshot, "市盈率TTM")
    result = {
        "name": _display_name(str(name), ticker),
        "ticker": ticker,
        "why": row.get("why") or "",
        "pe_y1": pe_y1,
        "pe_ttm": pe_ttm,
        "mcap": mcap,
        "thin": pe_y1 is None,
        "currency": currency,
        "price": last,
        "eps_scope": valuation.get("peForwardBasis"),
        "forecast_period": valuation.get("forwardFiscalYear"),
        "pe_source": "pricePerformance.valuation.peForward" if pe_y1 is not None else None,
    }
    requested = set(re.findall(r"[\u4e00-\u9fff]", str(row.get("name") or "")))
    actual = set(re.findall(r"[\u4e00-\u9fff]", str(info.get("assetName") or price_info.get("stockShortName") or "")))
    if requested and actual and requested.isdisjoint(actual):
        result.update(source_pe_y1=pe_y1, pe_y1=None, thin=True,
                      data_gap=f"候选名称{row['name']}与接口证券{name}（{ticker}）不匹配")
    return result


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _subject_query(facts: dict) -> str:
    return str(facts.get("ticker") or facts.get("company") or "")


def _subject_shares(facts: dict) -> float | None:
    million = _as_float((facts.get("market") or {}).get("当前总股本_百万股"))
    if million:
        return million * 1_000_000.0
    return None


def _forecast_years(facts: dict) -> list[int]:
    years = []
    for period in facts.get("forecast_periods") or []:
        year = str(period)[:4]
        if year.isdigit():
            years.append(int(year))
    return years


def _call(calls: list[dict[str, Any]], tool: str, arguments: dict[str, Any], fn, path: Path) -> Any:
    try:
        result = fn()
    except Exception as exc:
        calls.append({"tool": tool, "arguments": arguments, "ok": False, "error": str(exc)})
        dump_json(path, {"error": str(exc)})
        return {"error": str(exc)}
    calls.append({"tool": tool, "arguments": arguments, "ok": True})
    dump_json(path, result)
    return result


def _first_block(raw: Any) -> dict[str, Any]:
    data = _payload_data(raw)
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, dict) and (value.get("forecastList") or value.get("standardizedInfo")):
                return value
        if data.get("forecastList"):
            return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def _by_ticker(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    data = _payload_data(raw)
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                ticker = _norm_ticker(
                    key
                    or (value.get("standardizedInfo") or {}).get("ticker")
                    or (value.get("standardizedInfo") or {}).get("fullCode")
                )
                if ticker:
                    out[ticker] = value
    elif isinstance(data, list):
        for value in data:
            if not isinstance(value, dict):
                continue
            ticker = _norm_ticker(
                value.get("query")
                or (value.get("standardizedInfo") or {}).get("ticker")
            )
            if ticker:
                out[ticker] = value
    return out


def _price_by_ticker(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    data = _payload_data(raw)
    rows = data if isinstance(data, list) else []
    if isinstance(data, dict):
        rows = list(data.values()) if not data.get("query") else [data]
    for value in rows:
        if not isinstance(value, dict):
            continue
        ticker = _norm_ticker(
            value.get("query")
            or (value.get("standardized_info") or {}).get("stockCode")
        )
        if ticker:
            out[ticker] = value
    return out


def _payload_data(raw: Any) -> Any:
    if not isinstance(raw, dict) or raw.get("error"):
        return {}
    code = raw.get("code")
    if code not in (None, "", 0, "0", 200, "200"):
        return {}
    return raw.get("data")


def _aligned_block(block: dict, ticker: str, query: Any) -> dict:
    if not block:
        return {}
    std = block.get("standardizedInfo") or block.get("standardized_info") or {}
    std_t = _norm_ticker(std.get("stockFullCode") or std.get("fullCode") or std.get("ticker") or std.get("stockCode") or "")
    aliases = {_norm_ticker(ticker), _norm_ticker(query)}
    aliases.discard("")
    if std_t and aliases and std_t not in aliases:
        return {}
    return block


def _shares_and_last(snapshot: dict, price: dict) -> tuple[float | None, float | None]:
    shares = _as_float((price.get("market_value") or {}).get("totalShare"))
    last = _as_float((price.get("regular_market") or {}).get("latestPrice"))
    if shares is None:
        revenue = _snapshot_number(snapshot, "营业收入")
        per_share = _snapshot_number(snapshot, "每股营业收入")
        if revenue and per_share and per_share > 0:
            shares = revenue / per_share
    if last is None and shares and shares > 0:
        mcap_yuan = _snapshot_number(snapshot, "总市值")
        if mcap_yuan and mcap_yuan > 0:
            last = mcap_yuan / shares
    return shares, last


def _snapshot_number(snapshot: dict, label: str) -> float | None:
    for item in snapshot.get("snapshotItems") or []:
        if isinstance(item, dict) and str(item.get("name_cn") or "") == label:
            return _as_float(item.get("value"))
    return None


def _dedup_houses(houses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in houses:
        key = str(item.get("institution") or "")
        grouped.setdefault(key, []).append(item)
    out = []
    for records in grouped.values():
        ordered = sorted(records, key=lambda item: str(item.get("report_date") or ""), reverse=True)
        populated = [record for record in ordered if any(
            estimate.get(metric) is not None
            for estimate in (record.get("estimates") or {}).values() for metric in ("revenue", "eps"))]
        # 行发布日期为最近实际使用的报告；空白新报告不能抹掉已有有效预测。
        merged = deepcopy((populated or ordered)[0])
        years = dict.fromkeys(year for record in ordered for year in (record.get("estimates") or {}))
        merged["estimates"] = {}
        for year in years:
            cell = {"revenue": None, "eps": None}
            for metric in ("revenue", "eps"):
                for record in ordered:
                    estimate = (record.get("estimates") or {}).get(year) or {}
                    if estimate.get(metric) is None:
                        continue
                    cell[metric] = estimate[metric]
                    source = estimate.get(metric + "_source")
                    if source is not None or len(records) > 1:
                        cell[metric + "_source"] = {
                            "report_date": record.get("report_date"),
                            "source_title": record.get("source_title"), **deepcopy(source or {})}
                    break
            merged["estimates"][year] = cell
        out.append(merged)
    for i, item in enumerate(out, 1):
        item["source_id"] = f"C{i}"
    return out


def _display_name(name: str, ticker: str) -> str:
    name = name.replace("（", "(").replace("）", ")")
    if f"({ticker})" in name:
        return name.replace("(", "（").replace(")", "）")
    return f"{name}（{ticker}）"


def _norm_ticker(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace(".XSHG", "").replace(".XSHE", "").replace(".SS", "").replace(".SZ", "")
    if re.fullmatch(r"hk\d{1,5}", text, re.I):
        return text[2:].zfill(5)
    if text.lower().startswith(("sh", "sz", "bj")) and text[2:].isdigit():
        return text[2:]
    if text.startswith("us") and len(text)>2 and text[2].isupper():
        return text[2:]
    for suffix in (".HK", ".XHKG", ".US", ".NYSE", ".NASDAQ"):
        if text.upper().endswith(suffix):
            text = text[:-len(suffix)]
            return text.zfill(5) if suffix in {".HK", ".XHKG"} and text.isdigit() else text
    return text.zfill(5) if text.isdigit() and len(text) < 5 else text


def _candidate_query(item: dict, facts: dict) -> str:
    """四位代码不能不带市场发送；显式市场优先于目标公司的市场。"""
    text = str(item.get("ticker") or item.get("code") or "").strip()
    market = str(item.get("market") or "").upper()
    if re.fullmatch(r"hk\d{1,5}", text, re.I):
        return "hk" + text[2:].zfill(5)
    if text.upper().endswith((".HK", ".XHKG")):
        digits = text.rsplit(".", 1)[0]
        return "hk" + digits.zfill(5) if digits.isdigit() else text
    if text.isdigit():
        if len(text) == 6:  # 保持已有 A 股查询方式。
            return text
        if market == "JP":
            return "jpn" + text
        from valuation.shared.markets import market_of
        if market == "HK" or (not market and (len(text) == 5 or market_of(facts) == "HK")):
            return "hk" + text.zfill(5)
        # 未知四位代码不作跨市场模糊匹配。
        return ""
    if text.upper().endswith(".T"):
        return "jpn" + text[:-2]
    if market == "US" and not text.startswith("us"):
        return "us" + _norm_ticker(text)
    return text


def _to_yi(value: Any, unit: Any) -> float | None:
    number = _to_yuan(value, unit)
    if number is None:
        return None
    return number / YUAN_PER_YI


def _to_yuan(value: Any, unit: Any) -> float | None:
    number = _as_float(value)
    if number is None:
        return None
    text = str(unit or "").upper()
    if "亿" in str(unit or ""):
        return number * YUAN_PER_YI
    if "万" in str(unit or ""):
        return number * 10_000.0
    if text in {"", "CNY", "RMB", "元", "YUAN"}:
        return number
    return number


def _date(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("%", "")
        if not text or text in {"无数据", "盈利为负"}:
            return None
        value = text
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
