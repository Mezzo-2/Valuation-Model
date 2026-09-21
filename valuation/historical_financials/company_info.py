"""第一步公司档案。只保留进门能取到的字段，缺的不写。"""

from __future__ import annotations

from typing import Any

YUAN_PER_YI = 100_000_000.0

PROFILE_LABELS = (
    ("short_name", "公司简称"),
    ("ticker", "A股代码"),
    ("sw_industry", "申万行业"),
    ("main_business", "主营业务"),
    ("legal_representative", "法定代表人"),
)

MARKET_LABELS = (
    ("price", "当前股价（元）"),
    ("as_of", "数据日期"),
    ("chg_1d", "1日涨跌幅"),
    ("chg_1w", "近1周涨跌幅"),
    ("chg_1m", "1月涨跌幅"),
    ("chg_3m", "3月涨跌幅"),
    ("chg_6m", "6月涨跌幅"),
    ("chg_ytd", "年初至今涨跌幅"),
    ("mcap_yi", "总市值（亿元）"),
    ("float_mcap_yi", "流通市值（亿元）"),
)

PCT_MARKET_KEYS = frozenset(
    {"chg_1d", "chg_1w", "chg_1m", "chg_3m", "chg_6m", "chg_ytd"}
)


def build_company_info(
    details: Any,
    price: Any,
    *,
    ticker: str,
    company: str,
) -> dict[str, Any]:
    detail = _first_row(details)
    quote = _first_row(price)
    std = _block(detail, "standardized_info") or _block(quote, "standardized_info")
    industry = _block(detail, "industry_concepts")
    profile = _block(detail, "business_profile")
    management = _block(detail, "management")
    regular = _block(quote, "regular_market")
    change = _block(quote, "period_change")
    mcap = _block(quote, "market_value") or _block(detail, "market_value")

    out_profile = _omit(
        {
            "short_name": _text(std.get("stock_short_name") or std.get("stockShortName") or company),
            "ticker": _text(std.get("stock_code") or std.get("stockCode") or ticker),
            "full_code": _text(std.get("stock_full_code") or std.get("stockFullCode")),
            "sw_industry": _sw_industry(industry),
            "main_business": _text(profile.get("main_business") or profile.get("mainBusiness")),
            "legal_representative": _text(
                management.get("legal_representative") or management.get("legalRepresentative")
            ),
        }
    )
    out_market = _omit(
        {
            "price": _plain(regular.get("latestPrice") or regular.get("latest_price")),
            "as_of": _text(std.get("snapshotDate") or std.get("snapshot_date") or std.get("dataTime"))[:10],
            "chg_1d": _pct(change.get("pctChange") or change.get("pct_change")),
            "chg_1w": _pct(change.get("periodReturn1w") or change.get("period_return_1w")),
            "chg_1m": _pct(change.get("periodReturn1m") or change.get("period_return_1m")),
            "chg_3m": _pct(change.get("periodReturn3m") or change.get("period_return_3m")),
            "chg_6m": _pct(change.get("periodReturn6m") or change.get("period_return_6m")),
            "chg_ytd": _pct(change.get("periodReturnYtd") or change.get("period_return_ytd")),
            "mcap_yi": _yi(mcap.get("totalMarketValue") or mcap.get("total_market_value")),
            "float_mcap_yi": _yi(mcap.get("floatMarketValue") or mcap.get("float_market_value")),
        }
    )
    payload: dict[str, Any] = {
        "query": company or ticker,
        "source": {
            "profile": "get_stock_details",
            "market": "pricePerformance",
        },
    }
    if out_profile:
        payload["profile"] = out_profile
    if out_market:
        payload["market"] = out_market
    as_of = out_market.get("as_of") if out_market else ""
    if as_of:
        payload["ts"] = as_of
    return payload


def profile_rows(info: dict[str, Any]) -> list[tuple[str, str]]:
    profile = info.get("profile") or {}
    rows: list[tuple[str, str]] = []
    for key, label in PROFILE_LABELS:
        value = profile.get(key)
        if value in (None, ""):
            continue
        rows.append((label, str(value)))
    return rows


def market_rows(info: dict[str, Any]) -> list[tuple[str, Any, bool]]:
    market = info.get("market") or {}
    rows: list[tuple[str, Any, bool]] = []
    for key, label in MARKET_LABELS:
        value = market.get(key)
        if value in (None, ""):
            continue
        rows.append((label, value, key in PCT_MARKET_KEYS))
    return rows


def _sw_industry(block: dict[str, Any]) -> str:
    parts = [
        _text(block.get("level1_industry") or block.get("level1Industry")),
        _text(block.get("level2_industry") or block.get("level2Industry")),
        _text(block.get("level3_industry") or block.get("level3Industry")),
    ]
    return " / ".join(part for part in parts if part)


def _first_row(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    data = raw.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    return {}


def _block(row: dict[str, Any], key: str) -> dict[str, Any]:
    value = row.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"无数据", "None", "null", "--"}:
        return ""
    return text


def _plain(value: Any) -> float | None:
    from valuation.historical_financials.fetch import parse_plain

    return parse_plain(value)


def _pct(value: Any) -> float | None:
    if value is None or value == "":
        return None
    text = str(value).strip().replace(",", "")
    if text in {"无数据", "--", "-"}:
        return None
    if text.endswith("%"):
        number = _plain(text[:-1])
        return None if number is None else number / 100.0
    return _plain(text)


def _yi(value: Any) -> float | None:
    text = str(value or "").strip()
    if "亿" in text:
        number = _plain(text.replace("亿", ""))
        return number
    number = _plain(value)
    if number is None:
        return None
    if abs(number) >= YUAN_PER_YI:
        return round(number / YUAN_PER_YI, 4)
    return number


def _omit(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if value not in (None, "")}
