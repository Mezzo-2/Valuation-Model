"""分部研究规范表。预测数字是我们拍的，不要求和卖方摘录一致。"""

from __future__ import annotations

from typing import Any

from valuation.segment_research.methods import (
    FORECAST_FIELDS,
    FX_METHODS,
    GROW_FROM_LAST,
    GROWTH_ABS_CAP,
    GROWTH_FIELDS,
    HIST_FIELDS,
    RATE_FIELDS,
    SHARE_FIELDS,
    as_growth_rate,
    identity_errors,
)
from valuation.segment_split.schema import (
    FALLBACK_METHOD,
    METHODS,
    SchemaError,
    canonical_method,
    t_recon,
)
from valuation.segment_split.split_plan import is_residual_name

METHOD_PICK_RULES = (
    "公司级或父口径残差必须用收入增速法，不要自己改。"
    "非残差：本分部 brief 已有出货/销量或单价/ASP → 用量价；"
    "有同比或增速用 量价增速法，否则用 量价绝对值法。"
    "有市场规模且有渗透率或市占率 → 市场渗透法；"
    "有在手订单、新签订单或转化率 → 订单转化法；"
    "有门店且有单店产出或坪效 → 门店坪效法；"
    "有订阅用户或用户数且有单用户收入或ARPU → 用户单价法；"
    "量价、单价痕迹都没有、只跟得上收入时，才允许收入增速法。"
    "年报分部收入是锚。卖方出货×ASP 对不上年报收入时仍用量价："
    "留下更可靠的一侧（通常是出货），另一侧用锁定收入回推。不是对不齐就改回收入增速。"
    "只有量、价两侧都没有搜证时，才不能用量价。"
    "分部收入 = 销量 × 单价 / fx，历史年也要成立。"
    "量价、用户单价、门店坪效必须自报 unit_meta.fx，不要缺省。"
)


def require_forecast(
    payload: dict,
    facts: dict,
    *,
    notes_seg: dict,
) -> dict:
    canonical = normalize_forecast(payload, facts, notes_seg)
    errors = validate_forecast(canonical, facts, notes_seg)
    if errors:
        raise SchemaError(errors)
    return canonical


def normalize_forecast(
    raw: dict,
    facts: dict,
    notes_seg: dict,
) -> dict:
    name = str(notes_seg.get("name") or raw.get("segment") or "").strip()
    method = canonical_method(raw.get("method") or raw.get("预测方法"))
    hist = list(facts["hist_periods"])
    fcst = list(facts["forecast_periods"])
    notes_rev = {
        year: float((notes_seg.get("historical_revenue") or {}).get(year))
        for year in hist
        if _is_number((notes_seg.get("historical_revenue") or {}).get(year))
    }
    hist_data = _copy_series(raw.get("historical_data") or {}, HIST_FIELDS.get(method, ()))
    hist_data["分部收入"] = dict(notes_rev)
    fcst_data = _copy_series(raw.get("final_forecast") or {}, FORECAST_FIELDS.get(method, ()))
    unit_meta = dict(raw.get("unit_meta") or {})
    if method in FX_METHODS:
        if "amount_unit" not in unit_meta:
            unit_meta["amount_unit"] = facts.get("unit") or "亿元"
    sources = []
    for item in raw.get("sources") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("source_title") or item.get("title") or "").strip()
        if not title:
            continue
        sources.append(
            {
                "source_title": title,
                "source_type": str(item.get("source_type") or item.get("source_tool") or "").strip(),
                "house": str(item.get("house") or item.get("institution") or "").strip(),
                "summary": str(item.get("summary") or "").strip(),
                "as_of": str(item.get("as_of") or item.get("report_date") or item.get("date") or "")[:10],
                "horizon": str(item.get("horizon") or item.get("period") or "").strip(),
            }
        )
    return {
        "segment": name,
        "method": method,
        "预测方法": method,
        "ts": raw.get("ts") or facts.get("as_of") or "",
        "historical_data": hist_data,
        "final_forecast": fcst_data,
        "final_rationale": str(raw.get("final_rationale") or "").strip(),
        "unit_meta": unit_meta,
        "sources": sources,
        "open_gaps": [str(item).strip() for item in (raw.get("open_gaps") or []) if str(item).strip()],
        "historical_periods": hist,
        "forecast_periods": fcst,
        "revenue_unit": raw.get("revenue_unit") or facts.get("unit") or "亿元",
    }


def validate_forecast(
    payload: dict,
    facts: dict,
    notes_seg: dict,
) -> list[str]:
    errors: list[str] = []
    name = str(payload.get("segment") or "").strip()
    method = payload.get("method")
    hist = list(facts["hist_periods"])
    fcst = list(facts["forecast_periods"])
    parent = str(notes_seg.get("parent") or "")
    residual = is_residual_name(name, parent=parent)
    if name != str(notes_seg.get("name") or "").strip():
        errors.append(f"分部名必须是 {notes_seg.get('name')}，实际 {name}")
    if method not in METHODS:
        errors.append(f"预测方法不在白名单: {method}")
    if residual and method != FALLBACK_METHOD:
        errors.append(f"{name} 残差必须用 {FALLBACK_METHOD}")
    hist_need = HIST_FIELDS.get(method or "", ())
    fcst_need = FORECAST_FIELDS.get(method or "", ())
    hist_data = payload.get("historical_data") or {}
    fcst_data = payload.get("final_forecast") or {}
    unit_meta = payload.get("unit_meta") or {}
    for field in hist_need:
        series = hist_data.get(field) or {}
        for year in hist:
            if not _is_number(series.get(year)):
                errors.append(f"historical_data.{field}.{year} 缺数字")
        for year in series:
            if year in fcst:
                errors.append(f"historical_data.{field} 不得写入预测年 {year}")
    for field in fcst_need:
        series = fcst_data.get(field) or {}
        for year in fcst:
            if not _is_number(series.get(year)):
                errors.append(f"final_forecast.{field}.{year} 缺数字")
        for year in series:
            if year in hist:
                errors.append(f"final_forecast.{field} 不得写入历史年 {year}")
    notes_rev = notes_seg.get("historical_revenue") or {}
    ours = hist_data.get("分部收入") or {}
    for year in hist:
        if not _is_number(notes_rev.get(year)) or not _is_number(ours.get(year)):
            errors.append(f"分部收入 {year} 对不上 notes")
            continue
        if abs(float(ours[year]) - float(notes_rev[year])) > t_recon(float(notes_rev[year])):
            errors.append(f"分部收入 {year} 被改写，必须用 notes")
    errors.extend(identity_errors(method or "", hist_data, hist, notes_rev, unit_meta))
    if method in GROW_FROM_LAST and hist:
        last = hist[-1]
        last_rev = notes_rev.get(last)
        if _is_number(last_rev) and abs(float(last_rev)) <= 1e-12 and not residual:
            errors.append(f"{name} 最近历史年 {last} 为 0，不能用{method}外推")
    for field in RATE_FIELDS:
        series = fcst_data.get(field) or {}
        for year, value in series.items():
            if not _is_number(value):
                continue
            number = float(value)
            if field in SHARE_FIELDS and not 0 <= number <= 1:
                errors.append(f"{field}.{year} 须为 0–1 的小数，实际 {value}")
            elif field in GROWTH_FIELDS and abs(number) - GROWTH_ABS_CAP > 1e-12:
                errors.append(
                    f"{field}.{year} 须为小数且绝对值不超过 {GROWTH_ABS_CAP}，实际 {value}"
                )
    return errors


def _copy_series(raw: dict, fields: tuple[str, ...]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for field in fields:
        series = raw.get(field)
        if not isinstance(series, dict):
            continue
        copied = {
            str(year): float(value)
            for year, value in series.items()
            if _is_number(value)
        }
        if field in RATE_FIELDS:
            copied = {year: as_growth_rate(value) for year, value in copied.items()}
        out[field] = copied
    return out


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
