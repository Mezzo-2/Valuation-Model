from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from valuation.historical_financials.subjects import (
    ALIASES,
    BALANCE_FILL,
    BALANCE_ORDER,
    BS_QUERY,
    CORE_BALANCE,
    CORE_INCOME,
    INCOME_COMBINE,
    INCOME_FILL,
    INCOME_ORDER,
    INCOME_ZERO_IF_ABSENT,
    INDICATOR_QUERY,
    IS_QUERY,
    SOURCE_RANK,
    ZERO_IF_ABSENT,
)
from valuation.shared.io import dump_json, logs_dir
from valuation.comein.comein import ComeinClient

_UNIT = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(万亿|亿元|亿|万元|万|元|%)?\s*$"
)
_SKIP_NAME = re.compile(r"同比|环比|占比|占.+比重|增长率")
_LAYER_TOL = 0.01


def fetch_facts(run_dir: Path, *, ticker: str, company: str) -> dict[str, Any]:
    query = company or ticker
    years = _candidate_years()
    report_dates = [{"fiscalYear": y, "reportType": "A", "type": "year"} for y in years]
    log_root = logs_dir(run_dir) / "mcp"
    calls: list[dict[str, Any]] = []

    with ComeinClient() as mcp:
        finance_is = _logged(
            calls,
            "company_finance_search",
            {"querys": [query], "reportDates": report_dates, "searchData": IS_QUERY, "statementType": ["IS"]},
            lambda: mcp.finance_search([query], report_dates, IS_QUERY, ["IS"]),
        )
        finance_bs = _logged(
            calls,
            "company_finance_search",
            {"querys": [query], "reportDates": report_dates, "searchData": BS_QUERY, "statementType": ["BS"]},
            lambda: mcp.finance_search([query], report_dates, BS_QUERY, ["BS"]),
        )
        finance_ind = _logged(
            calls,
            "company_finance_search",
            {
                "querys": [query],
                "reportDates": report_dates,
                "searchData": INDICATOR_QUERY,
                "statementType": ["indicators"],
            },
            lambda: mcp.finance_search([query], report_dates, INDICATOR_QUERY, ["indicators"]),
        )
        price = _logged(
            calls,
            "pricePerformance",
            {
                "queries": [query],
                "include": [
                    "regular_market",
                    "market_value",
                    "valuation",
                    "standardized_info",
                    "period_change",
                ],
            },
            lambda: mcp.price_performance(
                [query],
                include=[
                    "regular_market",
                    "market_value",
                    "valuation",
                    "standardized_info",
                    "period_change",
                ],
            ),
        )
        try:
            details = _logged(
                calls,
                "get_stock_details",
                {
                    "queries": [query],
                    "include": [
                        "standardized_info",
                        "market_value",
                        "industry_concepts",
                        "management",
                        "business_profile",
                    ],
                },
                lambda: mcp.stock_details(
                    [query],
                    include=[
                        "standardized_info",
                        "market_value",
                        "industry_concepts",
                        "management",
                        "business_profile",
                    ],
                ),
            )
        except Exception:
            details = {}

    dump_json(log_root / "facts_calls.json", calls)
    dump_json(
        log_root / "company_finance_search.json",
        [finance_is, finance_bs, finance_ind],
    )
    dump_json(log_root / "pricePerformance.json", price)
    dump_json(log_root / "stock_details.json", details)

    records = (
        _finance_records(finance_is)
        + _finance_records(finance_bs)
        + _finance_records(finance_ind)
    )
    by_year = _index_by_year(records)
    hist_years = _pick_hist_years(by_year)
    hist_periods = [f"{y}A" for y in hist_years]
    last = hist_years[-1]
    forecast_periods = [f"{last + 1}E", f"{last + 2}E", f"{last + 3}E"]

    notes: dict[str, str] = {}
    income: dict[str, list[float | None]] = {}
    for label in CORE_INCOME:
        income[label] = [_require_income(by_year, y, label, notes) for y in hist_years]

    balance: dict[str, list[float | None]] = {}
    for label in CORE_BALANCE:
        balance[label] = [_require_balance(by_year, y, label, notes) for y in hist_years]

    shares = []
    for i, year in enumerate(hist_years):
        ni = income["归母净利润"][i]
        eps = income["EPS"][i]
        if eps is None or eps == 0:
            raise SystemExit(f"{year}A EPS 为 0，无法反推股本")
        shares.append(round(float(ni) * 100.0 / float(eps), 6))
    income["历史加权平均股本_百万股"] = shares

    other_op = []
    for i, year in enumerate(hist_years):
        ebit = float(income["息税前利润"][i])
        derived = (
            float(income["毛利"][i])
            - float(income["销售费用"][i])
            - float(income["管理费用"][i])
            - float(income["研发费用"][i])
        )
        other_op.append(round(ebit - derived, 6))

    _fill_optional(income, INCOME_FILL, by_year, hist_years)
    _fill_optional(balance, BALANCE_FILL, by_year, hist_years)
    _compact_income(income, len(hist_years), notes)
    _compact_balance(balance, len(hist_years), notes)
    incomplete = _drop_incomplete_optionals(income, balance)
    if incomplete:
        notes.setdefault("口径分层", "缺年已丢：" + "、".join(incomplete))

    from valuation.historical_financials.company_info import build_company_info

    info, market, as_of = _market_from_price(price, ticker=ticker, company=company)
    official = info.get("name") or company
    full_code = info.get("full_code") or _first_stock_code(records) or ticker
    company_info = build_company_info(details, price, ticker=ticker, company=official)

    optional_missing = _optional_gaps(income, balance, hist_years)
    payload = {
        "company": official,
        "ticker": ticker,
        "full_code": full_code,
        "source": "mcp",
        "as_of": as_of,
        "hist_periods": hist_periods,
        "forecast_periods": forecast_periods,
        "unit": "亿元",
        "income": income,
        "balance": balance,
        "market": market,
        "pnl_convention": "FinExp_below_Opex",
        "other_op_income": other_op,
        "notes": notes,
        "company_info": company_info,
        "coverage": {
            "income_rows": len(income),
            "balance_rows": len(balance),
            "optional_missing": optional_missing,
            "incomplete_dropped": incomplete,
        },
    }
    missing = _missing_core(payload)
    if missing:
        raise SystemExit("facts MCP 核心字段缺失:\n  " + "\n  ".join(missing))
    return payload


def _first_number(payload: Any, keys: tuple[str, ...]) -> float | None:
    lowered = {key.lower() for key in keys}
    if isinstance(payload, dict):
        for key, raw in payload.items():
            if str(key).lower() in lowered:
                parsed = parse_plain(raw)
                if parsed is not None:
                    return parsed
            if isinstance(raw, (dict, list)):
                nested = _first_number(raw, keys)
                if nested is not None:
                    return nested
        name = str(payload.get("name") or payload.get("indicator") or "").lower()
        if name in lowered or any(key in name for key in lowered):
            return parse_plain(payload.get("value") or payload.get("val"))
    if isinstance(payload, list):
        for item in payload:
            parsed = _first_number(item, keys)
            if parsed is not None:
                return parsed
    return None


def parse_plain(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"无数据", "NaN", "nan", "--", "-"}:
        return None
    matched = _UNIT.match(text)
    if not matched:
        return None
    return float(matched.group(1))


def parse_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"无数据", "NaN", "nan", "--", "-"}:
        return None
    matched = _UNIT.match(text)
    if not matched:
        return None
    number = float(matched.group(1))
    unit = matched.group(2) or ""
    if unit == "万亿":
        return number * 10000.0
    if unit in {"亿", "亿元"}:
        return number
    if unit in {"万", "万元"}:
        return number / 10000.0
    if unit == "元":
        return number / 1e8
    if unit == "%":
        return number
    return number


def _logged(calls: list[dict[str, Any]], tool: str, arguments: dict[str, Any], fn):
    try:
        result = fn()
    except Exception as exc:
        calls.append({"tool": tool, "arguments": arguments, "ok": False, "error": str(exc)})
        raise
    calls.append({"tool": tool, "arguments": arguments, "ok": True})
    return result


def _finance_records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise SystemExit(f"company_finance_search 返回异常: {type(payload)}")
    code = payload.get("code")
    if str(code) not in {"0", "200"}:
        raise SystemExit(f"company_finance_search 失败: {payload.get('msg') or payload}")
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise SystemExit("company_finance_search data 不是列表")
    return [row for row in data if isinstance(row, dict)]


def _index_by_year(records: list[dict[str, Any]]) -> dict[int, dict[str, tuple[int, float, str]]]:
    out: dict[int, dict[str, tuple[int, float, str]]] = {}
    for row in records:
        if not _is_annual(row):
            continue
        year = _row_year(row)
        if year is None:
            continue
        bucket = out.setdefault(year, {})
        for ind in row.get("indicators") or []:
            if not isinstance(ind, dict):
                continue
            name = str(ind.get("name") or "").strip()
            source = str(ind.get("source") or "")
            number = parse_amount(ind.get("value"))
            if not name or number is None or _SKIP_NAME.search(name):
                continue
            if source == "财务指标" and name not in {"毛利", "息税前利润"}:
                continue
            if source not in SOURCE_RANK:
                continue
            rank = SOURCE_RANK[source]
            prev = bucket.get(name)
            if prev is None or rank < prev[0]:
                bucket[name] = (rank, number, source)
    return out


def _is_annual(row: dict[str, Any]) -> bool:
    period = str(row.get("reportPeriod") or "")
    report_date = str(row.get("reportDate") or "")
    kind = str(row.get("type") or "")
    if kind and kind != "当年累计":
        return False
    if period.endswith("Q4"):
        return True
    return report_date.endswith("-12-31")


def _row_year(row: dict[str, Any]) -> int | None:
    report_date = str(row.get("reportDate") or "")
    if len(report_date) >= 4 and report_date[:4].isdigit():
        return int(report_date[:4])
    period = str(row.get("reportPeriod") or "")
    if len(period) >= 4 and period[:4].isdigit():
        return int(period[:4])
    return None


def _pick_hist_years(by_year: dict[int, dict[str, tuple[int, float, str]]]) -> list[int]:
    years = sorted(y for y, fields in by_year.items() if _lookup(fields, "营业收入") is not None)
    if not years:
        raise SystemExit("MCP 没有拉到任何年度营业收入")
    consec = [years[-1]]
    for year in reversed(years[:-1]):
        if year == consec[0] - 1:
            consec.insert(0, year)
        else:
            break
    if len(consec) < 3:
        raise SystemExit(f"不足 3 个连续完整年度，仅有 {consec}")
    return consec[-3:]


def _lookup(fields: dict[str, tuple[int, float, str]], label: str) -> float | None:
    best: tuple[int, float] | None = None
    for alias in ALIASES.get(label, [label]):
        hit = fields.get(alias)
        if hit is None:
            continue
        if best is None or hit[0] < best[0]:
            best = (hit[0], hit[1])
    return None if best is None else best[1]


def _require_income(
    by_year: dict[int, dict[str, tuple[int, float, str]]],
    year: int,
    label: str,
    notes: dict[str, str],
) -> float:
    fields = by_year[year]
    value = _lookup(fields, label)
    if value is not None:
        return value
    if label == "营业成本":
        revenue = _lookup(fields, "营业收入")
        gross = _lookup(fields, "毛利")
        if revenue is not None and gross is not None:
            notes.setdefault(label, "由营业收入 − 毛利派生（接口未返回营业成本行）")
            return round(revenue - gross, 6)
    if label == "毛利":
        revenue = _lookup(fields, "营业收入")
        cost = _lookup(fields, "营业成本")
        if revenue is not None and cost is not None:
            notes.setdefault(label, "由营业收入 − 营业成本派生")
            return round(revenue - cost, 6)
    if label == "息税前利润":
        op = _lookup(fields, "营业利润")
        fin = _lookup(fields, "财务费用")
        if op is not None and fin is not None:
            notes.setdefault(label, "由营业利润 + 财务费用近似")
            return round(op + fin, 6)
    raise SystemExit(f"{year}A 缺少核心科目 {label}")


def _require_balance(
    by_year: dict[int, dict[str, tuple[int, float, str]]],
    year: int,
    label: str,
    notes: dict[str, str],
) -> float:
    fields = by_year[year]
    value = _lookup(fields, label)
    if value is not None:
        return value
    if label == "营运资本":
        current_assets = _lookup(fields, "流动资产")
        current_liab = _lookup(fields, "流动负债")
        if current_assets is not None and current_liab is not None:
            notes.setdefault(label, "由流动资产合计 − 流动负债合计派生")
            return round(current_assets - current_liab, 6)
    raise SystemExit(f"{year}A 缺少核心科目 {label}")


def _fill_optional(
    block: dict[str, list[float | None]],
    order: list[str],
    by_year: dict[int, dict[str, tuple[int, float, str]]],
    hist_years: list[int],
) -> None:
    for label in order:
        if label in block:
            continue
        vals = [_lookup(by_year[year], label) for year in hist_years]
        if any(v is not None for v in vals):
            block[label] = vals


def _compact_income(
    income: dict[str, list[float | None]],
    hist_n: int,
    notes: dict[str, str],
) -> None:
    for label, parts in INCOME_COMBINE.items():
        if _is_complete(income.get(label)):
            continue
        composed, used = _sum_available(income, parts)
        if composed is not None:
            income[label] = composed
            notes.setdefault(label, "由" + " + ".join(used) + "加总")
    _fill_absent_cells(income, INCOME_ZERO_IF_ABSENT, hist_n, notes)
    keep = set(INCOME_ORDER)
    for key in list(income):
        if key not in keep:
            del income[key]


def _compact_balance(
    balance: dict[str, list[float | None]],
    hist_n: int,
    notes: dict[str, str],
) -> None:
    if (
        not _is_complete(balance.get("非流动资产合计"))
        and _is_complete(balance.get("资产总计"))
        and _is_complete(balance.get("流动资产合计"))
    ):
        balance["非流动资产合计"] = [
            round(float(total) - float(current), 6)
            for total, current in zip(balance["资产总计"], balance["流动资产合计"])
        ]
        notes.setdefault("非流动资产合计", "由资产总计 − 流动资产合计派生")
    if (
        not _is_complete(balance.get("非流动负债合计"))
        and _is_complete(balance.get("总负债"))
        and _is_complete(balance.get("流动负债合计"))
    ):
        balance["非流动负债合计"] = [
            round(float(total) - float(current), 6)
            for total, current in zip(balance["总负债"], balance["流动负债合计"])
        ]
        notes.setdefault("非流动负债合计", "由总负债 − 流动负债合计派生")

    _fill_absent_cells(balance, ZERO_IF_ABSENT, hist_n, notes)

    if (
        not _is_complete(balance.get("归母股东权益"))
        and _is_complete(balance.get("股东权益合计"))
        and _is_complete(balance.get("少数股东权益"))
    ):
        balance["归母股东权益"] = [
            round(float(eq) - float(mi), 6)
            for eq, mi in zip(balance["股东权益合计"], balance["少数股东权益"])
        ]
        notes.setdefault("归母股东权益", "由股东权益合计 − 少数股东权益派生")

    if _is_complete(balance.get("总负债")) and _is_complete(balance.get("股东权益合计")):
        balance["负债和股东权益总计"] = [
            round(float(liab) + float(eq), 6)
            for liab, eq in zip(balance["总负债"], balance["股东权益合计"])
        ]
        if _is_complete(balance.get("资产总计")) and _series_close(
            balance["负债和股东权益总计"], balance["资产总计"]
        ):
            notes.setdefault("负债和股东权益总计", "总负债 + 股东权益合计，与资产总计勾稽")
        else:
            notes.setdefault("负债和股东权益总计", "由总负债 + 股东权益合计派生")

    keep = set(BALANCE_ORDER)
    for key in list(balance):
        if key not in keep:
            del balance[key]


def _sum_available(
    block: dict[str, list[float | None]],
    parts: list[str],
) -> tuple[list[float | None] | None, list[str]]:
    used = [name for name in parts if _is_complete(block.get(name))]
    if not used:
        return None, []
    width = len(block[used[0]])
    return [round(sum(float(block[name][i]) for name in used), 6) for i in range(width)], used


def _drop_incomplete_optionals(
    income: dict[str, list[float | None]],
    balance: dict[str, list[float | None]],
) -> list[str]:
    core = set(CORE_INCOME) | set(CORE_BALANCE) | {"历史加权平均股本_百万股"}
    dropped: list[str] = []
    for block in (income, balance):
        for label in list(block):
            if label in core:
                continue
            if not _is_complete(block[label]):
                del block[label]
                dropped.append(label)
    return dropped


def _fill_absent_cells(
    block: dict[str, list[float | None]],
    labels: frozenset[str],
    hist_n: int,
    notes: dict[str, str],
) -> None:
    for label in labels:
        series = list(block.get(label) or [])
        while len(series) < hist_n:
            series.append(None)
        series = series[:hist_n]
        had_gap = any(v is None for v in series)
        had_value = any(v is not None for v in series)
        block[label] = [0.0 if v is None else float(v) for v in series]
        if had_gap and had_value:
            notes.setdefault(label, "部分年度缺失，缺失位按 0；已有值保留")
        elif had_gap:
            notes.setdefault(label, "接口未返回，按 0 计入大类")


def _is_complete(series: list[float | None] | None) -> bool:
    return bool(series) and all(v is not None for v in series)


def _series_close(left: list[float | None], right: list[float | None]) -> bool:
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if a is None or b is None:
            return False
        if abs(a - b) > max(_LAYER_TOL, abs(b) * 1e-4):
            return False
    return True


def _optional_gaps(
    income: dict[str, list[float | None]],
    balance: dict[str, list[float | None]],
    hist_years: list[int],
) -> list[str]:
    gaps: list[str] = []
    wanted = [
        (INCOME_ORDER, income),
        (BALANCE_ORDER, balance),
    ]
    core = set(CORE_INCOME) | set(CORE_BALANCE) | {"历史加权平均股本_百万股"}
    for order, block in wanted:
        for label in order:
            if label in core:
                continue
            series = block.get(label)
            if not series or any(v is None for v in series):
                years = []
                if series:
                    years = [f"{hist_years[i]}A" for i, v in enumerate(series) if v is None]
                else:
                    years = [f"{y}A" for y in hist_years]
                gaps.append(f"{label} ({', '.join(years)})")
    return gaps


def _market_from_price(
    payload: Any, *, ticker: str, company: str
) -> tuple[dict[str, str], dict[str, float], str]:
    if not isinstance(payload, dict):
        raise SystemExit("pricePerformance 返回异常")
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"pricePerformance 无数据: {payload.get('msg') or payload.get('failures')}")
    row = rows[0] if isinstance(rows[0], dict) else {}
    info = row.get("standardized_info") or {}
    regular = row.get("regular_market") or {}
    mcap = row.get("market_value") or {}
    valuation = row.get("valuation") or {}
    price = parse_plain(regular.get("latestPrice"))
    shares = parse_plain(mcap.get("totalShare"))
    if price is None or shares is None:
        raise SystemExit("pricePerformance 缺少最新价或总股本")
    as_of = str(info.get("dataTime") or info.get("snapshotDate") or datetime.now().isoformat(timespec="seconds"))
    meta = {
        "name": str(info.get("stockShortName") or company),
        "full_code": str(info.get("stockFullCode") or ticker),
    }
    market = {
        "当前股价": price,
        "当前总股本_百万股": round(shares / 1e6, 6),
        "快照日期": str(info.get("snapshotDate") or as_of)[:10],
    }
    float_share = parse_plain(mcap.get("floatShare"))
    if float_share is not None:
        market["流通股本_百万股"] = round(float_share / 1e6, 6)
    pe = _first_number(valuation, ("peTtm", "PE_TTM", "pe_ttm", "peTTM", "ttmPe", "市盈率"))
    if pe is not None:
        market["PE_TTM"] = pe
    pb = _first_number(valuation, ("pb", "PB", "pbLatest", "市净率"))
    if pb is not None:
        market["PB"] = pb
    beta = _first_number(valuation, ("beta", "Beta", "BETA"))
    if beta is None:
        beta = _first_number(row, ("beta", "Beta", "BETA"))
    if beta is not None:
        market["Beta"] = beta
    return meta, market, as_of


def _first_stock_code(records: list[dict[str, Any]]) -> str:
    for row in records:
        code = row.get("stockCode")
        if code:
            return str(code)
    return ""


def _candidate_years() -> list[int]:
    year = datetime.now().year
    return [year - 1, year - 2, year - 3, year - 4]


def _missing_core(payload: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    hist = payload["hist_periods"]
    for label in CORE_INCOME + ["历史加权平均股本_百万股"]:
        series = payload["income"].get(label)
        for i, year in enumerate(hist):
            if not series or i >= len(series) or series[i] is None:
                missing.append(f"{label} {year}")
    for label in CORE_BALANCE:
        series = payload["balance"].get(label)
        for i, year in enumerate(hist):
            if not series or i >= len(series) or series[i] is None:
                missing.append(f"{label} {year}")
    if payload["market"].get("当前股价") is None:
        missing.append("当前股价")
    if payload["market"].get("当前总股本_百万股") is None:
        missing.append("当前总股本_百万股")
    return missing
