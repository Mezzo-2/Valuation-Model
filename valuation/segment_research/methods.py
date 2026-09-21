"""七种预测方法的单一模板：恒等式、编表行、snapshot 滚动共用。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MethodSpec:
    hist_fields: tuple[str, ...]
    fcst_fields: tuple[str, ...]
    needs_fx: bool
    assume: tuple[str, ...]
    assume_anchor: str
    build: tuple[str, ...]
    fcst_mode: str
    grow_from: dict[str, str]
    identity: str
    certify_need: tuple[str, ...]


SPECS: dict[str, MethodSpec] = {
    "量价增速法": MethodSpec(
        hist_fields=("销量", "单价"),
        fcst_fields=("销量增速", "单价增速"),
        needs_fx=True,
        assume=("销量增速", "单价增速"),
        assume_anchor="销量增速",
        build=("销量", "单价"),
        fcst_mode="grow",
        grow_from={"销量": "销量增速", "单价": "单价增速"},
        identity="qty_price",
        certify_need=("*", "/"),
    ),
    "量价绝对值法": MethodSpec(
        hist_fields=("销量", "单价"),
        fcst_fields=("销量", "单价"),
        needs_fx=True,
        assume=("销量", "单价"),
        assume_anchor="销量",
        build=("销量", "单价"),
        fcst_mode="abs",
        grow_from={},
        identity="qty_price",
        certify_need=("*", "/"),
    ),
    "市场渗透法": MethodSpec(
        hist_fields=("市场规模", "渗透率", "市占率"),
        fcst_fields=("市场规模", "渗透率", "市占率"),
        needs_fx=False,
        assume=("市场规模", "渗透率", "市占率"),
        assume_anchor="市场规模",
        build=("市场规模", "渗透率", "市占率"),
        fcst_mode="abs",
        grow_from={},
        identity="market",
        certify_need=("*",),
    ),
    "订单转化法": MethodSpec(
        hist_fields=("期初在手订单", "新签订单", "转化率"),
        fcst_fields=("期初在手订单", "新签订单", "转化率"),
        needs_fx=False,
        assume=("期初在手订单", "新签订单", "转化率"),
        assume_anchor="期初在手订单",
        build=("期初在手订单", "新签订单", "转化率"),
        fcst_mode="abs",
        grow_from={},
        identity="order",
        certify_need=("+", "*"),
    ),
    "门店坪效法": MethodSpec(
        hist_fields=("门店数", "单店产出"),
        fcst_fields=("门店数", "单店产出"),
        needs_fx=True,
        assume=("门店数", "单店产出"),
        assume_anchor="门店数",
        build=("门店数", "单店产出"),
        fcst_mode="abs",
        grow_from={},
        identity="store",
        certify_need=("*", "/"),
    ),
    "用户单价法": MethodSpec(
        hist_fields=("订阅用户", "单用户收入"),
        fcst_fields=("订阅用户", "单用户收入"),
        needs_fx=True,
        assume=("订阅用户", "单用户收入"),
        assume_anchor="订阅用户",
        build=("订阅用户", "单用户收入"),
        fcst_mode="abs",
        grow_from={},
        identity="user",
        certify_need=("*", "/"),
    ),
    "收入增速法": MethodSpec(
        hist_fields=("分部收入",),
        fcst_fields=("收入增速",),
        needs_fx=False,
        assume=("收入增速",),
        assume_anchor="收入增速",
        build=(),
        fcst_mode="rev_grow",
        grow_from={},
        identity="none",
        certify_need=("(1+",),
    ),
}

HIST_FIELDS = {name: spec.hist_fields for name, spec in SPECS.items()}
FORECAST_FIELDS = {name: spec.fcst_fields for name, spec in SPECS.items()}
QTY_METHODS = frozenset({"量价增速法", "量价绝对值法"})
FX_METHODS = frozenset(name for name, spec in SPECS.items() if spec.needs_fx)
GROW_FROM_LAST = frozenset({"收入增速法", "量价增速法"})
GROWTH_FIELDS = frozenset({"收入增速", "销量增速", "单价增速"})
SHARE_FIELDS = frozenset({"渗透率", "市占率", "转化率"})
RATE_FIELDS = GROWTH_FIELDS | SHARE_FIELDS
GROWTH_ABS_CAP = 5.0


def as_growth_rate(value: Any) -> float:
    """内部一律用小数。18 就是 1800%，不会自动除以 100。"""
    return float(value)


def method_spec(method: str) -> MethodSpec:
    spec = SPECS.get(method)
    if spec is None:
        raise KeyError(method)
    return spec


def fx_errors(method: str, unit_meta: dict | None) -> list[str]:
    spec = SPECS.get(method)
    if spec is None or not spec.needs_fx:
        return []
    raw = (unit_meta or {}).get("fx")
    if raw is None or raw == "":
        return [f"{method} 必须自报 unit_meta.fx，不要缺省"]
    if not _is_number(raw):
        return [f"unit_meta.fx 不是数字: {raw}"]
    if float(raw) == 0:
        return ["unit_meta.fx 不能为 0"]
    return []


def read_fx(method: str, unit_meta: dict | None) -> float | None:
    if fx_errors(method, unit_meta):
        return None
    spec = SPECS.get(method)
    if spec is None or not spec.needs_fx:
        return None
    return float((unit_meta or {})["fx"])


def implied_revenue(
    method: str,
    data: dict,
    year: str,
    unit_meta: dict | None,
) -> float | None:
    spec = SPECS.get(method)
    if spec is None or spec.identity == "none":
        return None
    if spec.needs_fx:
        fx = read_fx(method, unit_meta)
        if fx is None:
            return None
    else:
        fx = None
    get = lambda field: (data.get(field) or {}).get(year)
    if spec.identity == "qty_price":
        vol, px = get("销量"), get("单价")
        if not _is_number(vol) or not _is_number(px) or fx is None:
            return None
        return float(vol) * float(px) / fx
    if spec.identity == "market":
        size, pen, share = get("市场规模"), get("渗透率"), get("市占率")
        if not _is_number(size) or not _is_number(pen) or not _is_number(share):
            return None
        return float(size) * float(pen) * float(share)
    if spec.identity == "order":
        backlog, new, conv = get("期初在手订单"), get("新签订单"), get("转化率")
        if not _is_number(backlog) or not _is_number(new) or not _is_number(conv):
            return None
        return (float(backlog) + float(new)) * float(conv)
    if spec.identity == "store":
        stores, prod = get("门店数"), get("单店产出")
        if not _is_number(stores) or not _is_number(prod) or fx is None:
            return None
        return float(stores) * float(prod) / fx
    if spec.identity == "user":
        users, arpu = get("订阅用户"), get("单用户收入")
        if not _is_number(users) or not _is_number(arpu) or fx is None:
            return None
        return float(users) * float(arpu) / fx
    return None


def identity_errors(
    method: str,
    hist_data: dict,
    hist: list[str],
    notes_rev: dict,
    unit_meta: dict | None,
) -> list[str]:
    errors = fx_errors(method, unit_meta)
    spec = SPECS.get(method)
    if spec is None or spec.identity == "none":
        return errors
    from valuation.segment_split.schema import t_recon

    for year in hist:
        got = implied_revenue(method, hist_data, year, unit_meta)
        want = notes_rev.get(year)
        if got is None or not _is_number(want):
            continue
        if abs(got - float(want)) > t_recon(float(want)):
            errors.append(f"{year} 驱动测算={got:.4f}，对不上分部收入 {want}")
    return errors


def roll_segment(
    method: str,
    forecast: dict,
    hist: list[str],
    fcst: list[str],
    hist_rev: dict[str, float],
) -> tuple[dict[str, float], dict[str, Any]]:
    spec = method_spec(method)
    unit_meta = forecast.get("unit_meta") or {}
    hist_data = forecast.get("historical_data") or {}
    fcst_data = forecast.get("final_forecast") or {}
    revenue: dict[str, float] = {}
    series: dict[str, dict[str, float]] = {field: {} for field in spec.build}
    if spec.fcst_mode == "rev_grow":
        for i, year in enumerate(hist):
            if _is_number((hist_data.get("分部收入") or {}).get(year)):
                revenue[year] = float(hist_data["分部收入"][year])
            else:
                revenue[year] = float(hist_rev[year])
        grow = fcst_data.get("收入增速") or {}
        for i, year in enumerate(fcst):
            prev = hist[-1] if i == 0 else fcst[i - 1]
            revenue[year] = revenue[prev] * (1 + as_growth_rate(grow[year]))
        return revenue, {"revenue_growth": {year: as_growth_rate(grow[year]) for year in fcst}}
    for year in hist:
        for field in spec.build:
            series[field][year] = float((hist_data.get(field) or {})[year])
        locked = hist_rev.get(year)
        if _is_number(locked):
            revenue[year] = float(locked)
        else:
            got = implied_revenue(method, hist_data, year, unit_meta)
            revenue[year] = float(got if got is not None else 0.0)
    if spec.fcst_mode == "grow":
        for i, year in enumerate(fcst):
            prev = hist[-1] if i == 0 else fcst[i - 1]
            for field in spec.build:
                rate = as_growth_rate((fcst_data.get(spec.grow_from[field]) or {})[year])
                series[field][year] = series[field][prev] * (1 + rate)
            packed = {field: {year: series[field][year]} for field in spec.build}
            revenue[year] = float(implied_revenue(method, packed, year, unit_meta) or 0.0)
        drivers = {field: dict(values) for field, values in series.items()}
        if spec.needs_fx:
            drivers["fx"] = read_fx(method, unit_meta)
        for field, grow in spec.grow_from.items():
            drivers[f"{field}_growth"] = {
                year: as_growth_rate((fcst_data.get(grow) or {})[year]) for year in fcst
            }
        return revenue, drivers
    for year in fcst:
        packed = {field: {year: float((fcst_data.get(field) or {})[year])} for field in spec.build}
        for field in spec.build:
            series[field][year] = packed[field][year]
        revenue[year] = float(implied_revenue(method, packed, year, unit_meta) or 0.0)
    drivers = {field: dict(values) for field, values in series.items()}
    if spec.needs_fx:
        drivers["fx"] = read_fx(method, unit_meta)
    return revenue, drivers


def formula_errors(method: str, formula: str) -> list[str]:
    spec = SPECS.get(method)
    if spec is None:
        return [f"预测方法不在白名单: {method}"]
    text = formula.replace(" ", "")
    missing = [token for token in spec.certify_need if token not in text]
    if missing:
        return [f"分部收入公式缺少 {'、'.join(missing)}: {formula}"]
    return []


def assume_unit(field: str) -> str:
    if field in RATE_FIELDS:
        return "%"
    return ""


def fx_unit_note(unit_meta: dict) -> str:
    qty = (
        unit_meta.get("销量")
        or unit_meta.get("销量单位")
        or unit_meta.get("volume_unit")
        or unit_meta.get("门店数")
        or unit_meta.get("store_unit")
        or unit_meta.get("订阅用户")
    )
    price = (
        unit_meta.get("单价")
        or unit_meta.get("单价单位")
        or unit_meta.get("price_unit")
        or unit_meta.get("单店产出")
        or unit_meta.get("productivity_unit")
        or unit_meta.get("单用户收入")
    )
    amount = (
        unit_meta.get("分部收入")
        or unit_meta.get("收入单位")
        or unit_meta.get("amount_unit")
        or "亿元"
    )
    if qty and price and not _is_number(qty) and not _is_number(price):
        return f"{qty}×{price}→{amount}"
    return "倍"


def build_unit(field: str, unit_meta: dict) -> str:
    if field in RATE_FIELDS:
        return "%"
    keys = {
        "销量": "volume_unit",
        "单价": "price_unit",
        "订阅用户": "volume_unit",
        "单用户收入": "price_unit",
        "门店数": "store_unit",
        "单店产出": "productivity_unit",
        "市场规模": "amount_unit",
        "期初在手订单": "amount_unit",
        "新签订单": "amount_unit",
    }
    key = keys.get(field)
    if key and unit_meta.get(key):
        return str(unit_meta[key])
    for raw in (unit_meta.get(field), unit_meta.get(f"{field}单位")):
        if raw not in (None, "") and not _is_number(raw):
            return str(raw)
    if field in {"市场规模", "期初在手订单", "新签订单"}:
        return str(unit_meta.get("amount_unit") or "亿元")
    return ""


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
