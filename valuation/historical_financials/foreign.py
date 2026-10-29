"""进门港美财务字段映射。只处理结构化数据，不用模型补历史值。"""
from __future__ import annotations

from datetime import date, datetime
from html import unescape
from pathlib import Path
import re

from valuation.comein.comein import ComeinClient
from valuation.shared.io import dump_json, logs_dir
from valuation.shared.markets import market_of, number

INCOME = {
    "营业收入": ["营业总收入", "营业收入"], "毛利": ["毛利润", "营业毛利", "毛利"],
    "销售费用": ["销售费用", "销售及分销费用"], "管理费用": ["管理费用", "一般及行政费用"],
    "研发费用": ["研发费用", "研发开支"], "SG&A": ["销售一般管理费用", "销售及一般管理费用", "SG&A"],
    "营业利润": ["营业利润", "SOPR"], "税前利润": ["税前利润", "利润总额", "除税前溢利"],
    "所得税费用": ["所得税", "所得税费用", "INCOME_TAX"], "净利润": ["净利润"],
    "归母净利润": ["归母净利润"], "少数股东损益": ["少数股东损益", "非控股权益应占利润"],
    "历史加权平均股本_百万股": ["基本加权平均股数", "加权平均普通股股数", "Weighted average shares basic"],
    "财务费用": ["财务费用"], "营业外收入": ["营业外收入"], "营业外支出": ["营业外支出"],
    "其他经营净收益": ["其他经营净收益", "其他经营净收入/费用"],
    "联营公司损益": ["应占联营公司利润及亏损", "应占联营公司损益", "联营公司损益"],
    "优先股股息": ["优先股股息", "优先股股利"],
}
BALANCE = {
    "货币资金": ["现金及现金等价物", "货币资金"], "存货": ["存货合计", "存货"],
    "固定资产": ["固定资产"], "无形资产": ["无形资产净额", "无形资产"],
    "流动资产合计": ["流动资产合计"], "非流动资产合计": ["非流动资产合计"],
    "资产总计": ["资产合计", "资产总计"], "流动负债合计": ["流动负债合计"],
    "非流动负债合计": ["非流动负债合计", "非流动负债", "t_ncl"],
    "总负债": ["负债合计", "总负债"],
    "归母股东权益": ["归母股东权益", "归母所有者权益合计", "归属于母公司所有者权益合计"],
    "少数股东权益": ["少数股东权益", "非控股权益"],
    "股东权益合计": ["股东权益合计", "所有者权益合计"],
}
_VALUE = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(万亿|十亿|百万|亿|万|千)?\s*(人民币|人民幣|美元|港元|港币|元|USD|CNY|HKD)?(?:/股|/ADS)?$", re.I)
_SCALE = {None: 1, "万亿": 1e12, "十亿": 1e9, "百万": 1e6, "亿": 1e8, "万": 1e4, "千": 1e3}
_UNIT_CODE = {
    "USD": "USD", "美元": "USD",
    "CNY": "CNY", "RMB": "CNY", "人民币": "CNY", "人民幣": "CNY",
    "HKD": "HKD", "港元": "HKD", "港币": "HKD", "港幣": "HKD",
}


def currency_of_unit(raw) -> str | None:
    """记录级 unit，如「美元-USD」。认不出就不猜测。"""
    text = str(raw or "").strip()
    if not text or text in {"无数据", "--", "—"}:
        return None
    found = set()
    for part in re.split(r"[\s\-_/]+", text):
        code = _UNIT_CODE.get(part) or _UNIT_CODE.get(part.upper())
        if code:
            found.add(code)
    if len(found) > 1:
        raise ValueError(f"财务记录 unit 含多个币种：{raw}")
    return next(iter(found), None)


def quantity(raw, *, kind="amount"):
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip().replace(",", "")
    text = re.sub(r"(万亿|十亿|百万|亿|万|千)元(?=人民币|人民幣|美元|港元|港币|USD|CNY|HKD)", r"\1", text, flags=re.I)
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    else:
        text = re.sub(r"^\(([^()]*)\)(.*)$", r"-\1\2", text)
    if kind == "shares":
        text = re.sub(r"(?:股|\s+shares)$", "", text, flags=re.I)
    if kind == "eps" and text.upper().endswith("/ADS"):
        return None  # 不将每 ADS 盈利静默标成每普通股。
    match = _VALUE.fullmatch(text)
    if not match:
        return None
    value = float(match[1]) * _SCALE[match[2]]
    if not number(value):
        return None
    if kind == "eps":
        return float(match[1]) if match[2] is None else None
    if kind == "shares":
        return value / 1e6
    # 无量纲裸数不冒充以亿计的金额。
    return value / 1e8 if match[2] or match[3] or value == 0 else None


def query_fields(market: str):
    aliases = dict(INCOME)
    aliases["营业成本"] = ["销售成本"] if market == "HK" else ["营业成本"]
    aliases["EPS"] = ["Basic EPS"] if market == "US" else ["基本每股收益", "基本每股盈利"]
    if market == "US":
        aliases["归母净利润"] = ["扣非归母净利润", "归母净利润（扣非）", "归母净利润"]
    return aliases


def decode_financials(payloads: list[dict], *, ticker: str, company: str, metadata: dict | None = None) -> dict:
    """公开纯解析入口，供存档回放测试和实际取数共用。"""
    market = market_of(ticker)
    aliases = query_fields(market)
    metadata = dict(metadata or {})
    profit_basis = ({"净利润": "reported", "归母净利润": "reported"} if market == "HK"
                    else {"净利润": "adjusted", "归母净利润": None})
    profit_basis.update(metadata.get("profit_basis") or {})
    indexed, dates, currencies, provenance, basis_by_year = {}, {}, set(), {}, {}
    stock_name, stock_code = company, ticker
    today = date.today().isoformat()
    for payload in payloads:
        if str(payload.get("code")) not in {"0", "200"}:
            raise ValueError("港美财务接口未成功返回")
        for row in payload.get("data") or []:
            code = str(row.get("stockCode") or "")
            if code and _code(code) != _code(ticker):
                continue
            period = str(row.get("reportPeriod") or "")
            if not re.fullmatch(r"\d{4}Q4", period) or row.get("type") not in (None, "", "当年累计"):
                continue
            if str(row.get("publishDate") or "")[:10] > today:
                continue
            year = int(period[:4])
            report_date = str(row.get("reportDate") or "")[:10]
            if year in dates and dates[year] != report_date:
                raise ValueError(f"{year} 财年存在不同截止日期，不能合并")
            dates[year] = report_date
            stock_name, stock_code = row.get("stockName") or stock_name, code or stock_code
            unit_code = currency_of_unit(row.get("unit"))
            if unit_code:
                currencies.add(unit_code)
            bucket = indexed.setdefault(year, {})
            for item in row.get("indicators") or []:
                name, raw = item.get("name"), item.get("value")
                if name == "币种" and raw in {"USD", "CNY", "HKD"}:
                    currencies.add(raw)
                token = str(raw)
                if "美元" in token or "USD" in token:
                    currencies.add("USD")
                elif "人民币" in token or "人民幣" in token or "CNY" in token:
                    currencies.add("CNY")
                elif "港元" in token or "港币" in token or "HKD" in token:
                    currencies.add("HKD")
                rank = 0 if item.get("source") in {"利润表", "资产负债表"} else 1
                previous = bucket.get(name)
                if raw in (None, "", "无数据", "--", "—"):
                    continue
                if previous and previous[0] == rank and previous[1] != raw:
                    raise ValueError(f"{stock_code} {year} {name} 同优先级来源冲突")
                if previous is None or rank < previous[0]:
                    bucket[name] = (rank, raw, item.get("source"))
    if len(currencies) > 1:
        raise ValueError(f"历史财务存在多币种 {sorted(currencies)}，不能合并")
    currency = next(iter(currencies), metadata.get("currency"))
    def lookup(year, key, mapping):
        for alias in mapping[key]:
            hit = indexed[year].get(alias)
            if hit:
                kind = "eps" if key == "EPS" else "shares" if key == "历史加权平均股本_百万股" else "amount"
                value = quantity(hit[1], kind=kind)
                if value is not None:
                    if key == "归母净利润" and "扣非" in alias:
                        basis_by_year.setdefault(year, dict(profit_basis))["归母净利润"] = "adjusted"
                    provenance[f"{year}A/{key}"] = {"field": alias, "source": hit[2], "raw": hit[1]}
                    return value
        return None
    years = sorted(y for y in indexed if lookup(y, "营业收入", aliases) is not None)
    years = years[-3:]
    if len(years) < 3 or years != list(range(years[0], years[-1]+1)):
        raise ValueError(f"港美历史财务不足三个连续完整财年：{years}")
    income = {key: [lookup(y, key, aliases) for y in years] for key in aliases}
    balance = {key: [lookup(y, key, BALANCE) for y in years] for key in BALANCE}
    derived, checks = [], []
    for i, year in enumerate(years):
        net, parent = income["净利润"][i], income["归母净利润"][i]
        basis = basis_by_year.get(year, profit_basis)
        same_basis = basis.get("净利润") is not None and basis.get("净利润") == basis.get("归母净利润")
        if income["少数股东损益"][i] is None and net is not None and parent is not None and same_basis:
            income["少数股东损益"][i] = round(net-parent, 8)
            derived.append(f"{year}A/少数股东损益=净利润-归母净利润")
        elif income["少数股东损益"][i] is None and net is not None and parent is not None:
            checks.append(f"{year}A 合并与归母利润的调整范围未确认一致，少数股东损益暂缺；不把未知利润差额当作非控股损益")
        pre, tax = income["税前利润"][i], income["所得税费用"][i]
        if all(v is not None for v in (pre, tax, net)):
            gap = pre-tax-net
            if abs(gap) > .02:
                checks.append(f"{year}A 税前利润-所得税-净利润={gap:.6f}；须核对未返回项目与调整范围")
    fee_names = ["SG&A"] if market == "US" else ["销售费用", "管理费用"]
    if any(v is not None for v in income["研发费用"]):
        fee_names.append("研发费用")
    for i, year in enumerate(years):
        gp, op = income["毛利"][i], income["营业利润"][i]
        fees = [income[name][i] for name in fee_names]
        if gp is None or op is None or any(v is None for v in fees):
            continue
        reported = income["其他经营净收益"][i]
        gap = round(op - (gp - sum(fees) + (reported or 0)), 6)
        if reported is None and abs(gap) > .02:
            income["其他经营净收益"][i] = gap
            derived.append(f"{year}A/其他经营净收益=营业利润-(毛利-适用费用)")
        elif reported is not None and abs(gap) > .02:
            checks.append(f"{year}A 毛利减适用费用加其他经营净收益与营业利润相差{gap:.6f}")
    ca, cl = balance["流动资产合计"], balance["流动负债合计"]
    balance["营运资本"] = [None if a is None or b is None else round(a-b, 8) for a, b in zip(ca, cl)]
    unit = {"CNY": "亿元人民币", "USD": "亿美元", "HKD": "亿港元"}.get(currency, "亿（币种待确认）")
    return {"company": stock_name, "ticker": ticker, "full_code": stock_code, "market_type": market,
            "source": "mcp", "as_of": datetime.now().isoformat(timespec="seconds"),
            "hist_periods": [f"{y}A" for y in years],
            "forecast_periods": [f"{years[-1]+i}E" for i in (1, 2, 3)],
            "report_dates": {f"{y}A": dates[y] for y in years}, "currency": currency, "unit": unit,
            "eps_unit": f"{currency or '币种待确认'}/普通股", "eps_scope": "SBCOC" if market == "US" else "basic",
            "income": income, "balance": balance,
            "market": {}, "provenance": provenance, "derived": derived, "profit_basis": profit_basis,
            "profit_basis_by_period": {f"{y}A": basis_by_year.get(y, dict(profit_basis)) for y in years},
            "notes": {"profit_checks": checks}, "coverage": {"optional_missing": [
                f"{key} {years[i]}A" for block in (income, balance) for key, values in block.items()
                for i, value in enumerate(values) if value is None]}}


def _code(code: str) -> str:
    hk = code.lower().startswith("hk") or code.upper().endswith(".HK")
    if code.startswith(("hk", "us")) and len(code) > 2:
        code = code[2:]
    code = code.upper().removesuffix(".HK").removesuffix(".US").removesuffix(".NYSE").removesuffix(".NASDAQ")
    code = code[2:] if re.fullmatch(r"HK\d{5}", code) else code
    return code.zfill(5) if hk and code.isdigit() else code


def report_metadata(hits: list[dict], *, ticker: str, fiscal_year: str) -> dict:
    """只识别明确的列报币种句和证券登记行；不清洗年报财务金额。"""
    currencies, ratios, sources = set(), set(), []
    chinese_year = "".join("零一二三四五六七八九"[int(n)] for n in fiscal_year)
    currency_names = {"RENMINBI": "CNY", "RMB": "CNY", "人民币": "CNY", "人民幣": "CNY",
                      "USD": "USD", "UNITED STATES DOLLARS": "USD", "US DOLLARS": "USD",
                      "HKD": "HKD", "HONG KONG DOLLARS": "HKD", "港元": "HKD",
                      "美元": "USD", "港币": "HKD", "港幣": "HKD"}
    for hit in hits:
        raw = hit.get("viewpoint") or hit.get("text") or hit.get("mainText") or ""
        text = re.sub(r"<[^>]+>", " ", unescape(raw))
        title = str(hit.get("title") or "")
        used = False
        if fiscal_year in title or chinese_year in title:
            patterns = [r"(?:consolidated financial statements|financial statements)[^.\n]{0,65}presented in\s+(Renminbi|RMB|United States Dollars|US Dollars|Hong Kong Dollars|USD|HKD)",
                        r"(?:合併|合并|綜合|综合)[財财][務务][報报]表[^。\n]{0,50}(?:以|採用|采用)(人民币|人民幣|美元|港元|港币|港幣)[^。\n]{0,15}(?:列報|列报|呈列)"]
            for pattern in patterns:
                for match in re.finditer(pattern, text, re.I):
                    currencies.add(currency_names[match[1].upper()])
                    used = True
        for row in re.findall(r"<tr\b[^>]*>.*?</tr>", raw, re.S | re.I):
            flat = unescape(re.sub(r"<[^>]+>", " ", row))
            if re.search(r"\b(?:common stock|ordinary shares)\b", flat, re.I) and re.search(r"\b"+re.escape(_code(ticker))+r"\b", flat, re.I) and not re.search(r"\bADS|depositary", flat, re.I):
                ratios.add(1.0)
                used = True
        for match in re.finditer(r"(?:each ADS|each American depositary share)[^.\n]{0,30}represent(?:s|ing)?\s+(\d+(?:\.\d+)?|eight|two|four|one)\s+(?:Class [A-Z]\s+)?ordinary shares", text, re.I):
            token = match[1].lower()
            ratios.add({"eight": 8., "two": 2., "four": 4., "one": 1.}.get(token, float(token) if token[0].isdigit() else 0))
            used = True
        if used:
            sources.append({"title": title, "url": hit.get("docUrl") or hit.get("url"), "chunk_id": hit.get("chunkId")})
    result = {"sources": sources}
    if len(currencies) == 1:
        result["reporting_currency"] = next(iter(currencies))
    if len(ratios) == 1:
        result["ordinary_per_quote"] = next(iter(ratios))
    return result


def fetch_foreign(run_dir: Path, *, ticker: str, company: str) -> dict:
    from valuation.historical_financials.company_info import build_company_info
    from valuation.historical_financials.fetch import _market_from_price
    market = market_of(ticker)
    aliases = query_fields(market)
    years = range(date.today().year, date.today().year-5, -1)
    periods = [{"fiscalYear": y, "reportType": "A", "type": "year"} for y in years]
    log = logs_dir(run_dir) / "mcp"
    payloads = []
    with ComeinClient() as mcp:
        for table, mapping in [("IS", aliases), ("BS", BALANCE)]:
            terms = list(dict.fromkeys([a for names in mapping.values() for a in names] + ["币种", "列报币种"]))
            result = mcp.finance_search([ticker], periods, terms, [table])
            dump_json(log / f"foreign_{table}.json", result)
            payloads.append(result)
        try:
            price = mcp.price_performance([ticker], include=["regular_market", "market_value", "valuation", "standardized_info"])
        except Exception as exc:
            price = {"data": []}
            dump_json(log / "price_error.json", {"error_type": type(exc).__name__})
        dump_json(log / "pricePerformance.json", price)
        try:
            details = mcp.stock_details([ticker], include=["standardized_info", "market_value", "business_profile", "industry_concepts"])
        except Exception as exc:
            details = {}
            dump_json(log / "details_error.json", {"error_type": type(exc).__name__})
        dump_json(log / "stock_details.json", details)
        facts = decode_financials(payloads, ticker=ticker, company=company)
        metadata = {}
        if not facts.get("currency") or market == "US":
            try:
                hits = mcp.call("searchAnnouncementReport", {"fullCode": facts["full_code"],
                    "labelList": facts["hist_periods"][-1],
                    "query": ("列报货币 presentation currency consolidated financial statements presented in" if not facts.get("currency")
                              else "Securities registered Title of each class Trading symbol Common Stock ordinary shares ADS each representing"), "topK": 4})
                dump_json(log / "report_metadata.json", hits)
                if isinstance(hits, list):
                    metadata = report_metadata(hits, ticker=ticker, fiscal_year=facts["hist_periods"][-1][:4])
            except Exception as exc:
                dump_json(log / "report_metadata_error.json", {"error_type": type(exc).__name__})
    if metadata.get("reporting_currency"):
        facts["reporting_currency"] = metadata["reporting_currency"]
        if not facts.get("currency"):
            facts["currency"] = metadata["reporting_currency"]
            facts["unit"] = {"CNY": "亿元人民币", "USD": "亿美元", "HKD": "亿港元"}[facts["currency"]]
            facts["eps_unit"] = f"{facts['currency']}/普通股"
    facts["metadata_sources"] = metadata.get("sources", [])
    try:
        _, facts["market"], facts["as_of"] = _market_from_price(price, ticker=ticker, company=company)
    except (SystemExit, ValueError):
        facts["notes"]["market_missing"] = "行情或当前股数缺失；财务底稿保留，完整估值需补齐。"
    quote = next((r for r in price.get("data", []) if _code(r.get("standardized_info", {}).get("stockFullCode", r.get("query", ""))) == _code(ticker)), {})
    std = quote.get("standardized_info") or {}
    facts["trading_currency"] = std.get("currency")
    facts["company_info"] = build_company_info(details, price, ticker=ticker, company=company)
    # 比例和原币种均需要来源支持；缺少时底稿可生成，估值环节明确指出缺口。
    ratio = std.get("ordinarySharesPerADS") or std.get("adsRatio")
    if ratio is not None:
        try:
            facts["market"]["每交易单位普通股数"] = float(ratio)
        except (TypeError, ValueError):
            pass
    elif metadata.get("ordinary_per_quote"):
        facts["market"]["每交易单位普通股数"] = metadata["ordinary_per_quote"]
    elif market == "HK" or std.get("isADR") is False:
        facts["market"]["每交易单位普通股数"] = 1.0
    ratio = facts["market"].get("每交易单位普通股数")
    shares = facts["market"].get("当前总股本_百万股")
    if number(ratio) and ratio > 0 and number(shares):
        facts["market"]["当前普通股总数_百万股"] = ratio * shares
    return facts
