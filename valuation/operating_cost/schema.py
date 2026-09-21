"""运营成本规范表。历史轨迹从 facts 算，预测年才是我们拍的。"""

from __future__ import annotations

from typing import Any

from valuation.segment_split.schema import SchemaError

RATIO_KEYS = (
    "gross_margin",
    "sales_ratio",
    "admin_ratio",
    "rd_ratio",
    "tax_rate",
    "minority",
)
AMOUNT_KEYS = ("fin_exp", "nonop_inc", "nonop_exp", "other_op")
FORECAST_KEYS = RATIO_KEYS + AMOUNT_KEYS

_RATIO_FROM = {
    "gross_margin": ("毛利", "营业收入"),
    "sales_ratio": ("销售费用", "营业收入"),
    "admin_ratio": ("管理费用", "营业收入"),
    "rd_ratio": ("研发费用", "营业收入"),
    "tax_rate": ("所得税费用", "税前利润"),
}
_AMOUNT_FROM = {
    "fin_exp": "财务费用",
    "nonop_inc": "营业外收入",
    "nonop_exp": "营业外支出",
}


def hist_cost_track(facts: dict) -> dict:
    hist = [str(year) for year in (facts.get("hist_periods") or [])]
    income = facts.get("income") or {}
    notes: list[str] = []
    track: dict[str, Any] = {key: {} for key in FORECAST_KEYS}
    track["hist_periods"] = hist
    for i, year in enumerate(hist):
        for key, (num_name, den_name) in _RATIO_FROM.items():
            num = _income_at(income, num_name, i)
            if key == "gross_margin" and num is None:
                rev = _income_at(income, "营业收入", i)
                cost = _income_at(income, "营业成本", i)
                if rev is not None and cost is not None:
                    num = rev - cost
            den = _income_at(income, den_name, i)
            if den is None or den == 0:
                notes.append(f"{key} {year} 分母为 0 或缺失")
                continue
            if num is None:
                notes.append(f"{key} {year} 分子缺失")
                continue
            track[key][year] = num / den
        parent = _income_at(income, "归母净利润", i)
        minority = _income_at(income, "少数股东损益", i)
        den = None if parent is None or minority is None else parent + minority
        if den is None or den == 0:
            notes.append(f"minority {year} 分母为 0 或缺失")
        elif minority is not None:
            track["minority"][year] = minority / den
        for key, name in _AMOUNT_FROM.items():
            value = _income_at(income, name, i)
            if value is None:
                notes.append(f"{key} {year} 缺失")
                continue
            track[key][year] = value
        other = _list_at(facts.get("other_op_income"), i)
        if other is None:
            notes.append(f"other_op {year} 缺失")
        else:
            track["other_op"][year] = other
    track["notes"] = notes
    return track


def format_hist_track(track: dict) -> str:
    years = [str(year) for year in (track.get("hist_periods") or [])]
    lines = []
    for year in years:
        lines.append(
            f"{year}：毛利率 {_pct(track.get('gross_margin'), year)}，"
            f"销售/管理/研发 {_pct(track.get('sales_ratio'), year)}/"
            f"{_pct(track.get('admin_ratio'), year)}/{_pct(track.get('rd_ratio'), year)}，"
            f"税率 {_pct(track.get('tax_rate'), year)}，"
            f"财务费用 {_amt(track.get('fin_exp'), year)}亿元，"
            f"少数股东比率 {_pct(track.get('minority'), year)}，"
            f"营业外收入 {_amt(track.get('nonop_inc'), year)}、"
            f"支出 {_amt(track.get('nonop_exp'), year)}，"
            f"其他经营净收益 {_amt(track.get('other_op'), year)}"
        )
    for note in track.get("notes") or []:
        lines.append(str(note))
    return "\n".join(lines) if lines else "（无历史轨迹）"


def require_cost(payload: dict, facts: dict) -> dict:
    canonical = normalize_cost(payload, facts)
    errors = validate_cost(canonical, facts)
    if errors:
        raise SchemaError(errors)
    return canonical


def normalize_cost(raw: dict, facts: dict) -> dict:
    fcst = [str(year) for year in (facts.get("forecast_periods") or [])]
    out = {
        "ts": raw.get("ts") or facts.get("as_of") or "",
        "rationale": str(raw.get("rationale") or raw.get("final_rationale") or "").strip(),
        "open_gaps": [
            str(item).strip()
            for item in (raw.get("open_gaps") or [])
            if str(item).strip()
        ],
        "historical_track": hist_cost_track(facts),
        "forecast_periods": fcst,
        "sources": _copy_sources(raw.get("sources") or []),
    }
    for key in FORECAST_KEYS:
        series = raw.get(key)
        if not isinstance(series, dict):
            out[key] = {}
            continue
        copied: dict[str, float] = {}
        for year, value in series.items():
            if not _is_number(value):
                continue
            copied[str(year)] = float(value)
        out[key] = copied
    return out


def validate_cost(payload: dict, facts: dict) -> list[str]:
    errors: list[str] = []
    hist = {str(year) for year in (facts.get("hist_periods") or [])}
    fcst = [str(year) for year in (facts.get("forecast_periods") or [])]
    for key in FORECAST_KEYS:
        series = payload.get(key) or {}
        if not isinstance(series, dict):
            errors.append(f"{key} 须按年写数字")
            continue
        for year in fcst:
            if not _is_number(series.get(year)):
                errors.append(f"{key}.{year} 缺数字")
                continue
            value = float(series[year])
            if key in {"tax_rate", "minority"} and not 0 <= value <= 1:
                errors.append(f"{key}.{year} 须为 0–1 的小数，实际 {value}")
            elif key == "gross_margin" and not -0.5 <= value <= 1:
                errors.append(f"{key}.{year} 须为 -0.5–1 的小数，实际 {value}")
            elif key in {"sales_ratio", "admin_ratio", "rd_ratio"} and not 0 <= value <= 1:
                errors.append(f"{key}.{year} 须为 0–1 的小数，实际 {value}")
        for year in series:
            if year in hist:
                errors.append(f"{key} 不得写入历史年 {year}")
    rationale = str(payload.get("rationale") or "")
    if not rationale:
        errors.append("rationale 为空")
    elif not any(ch.isdigit() for ch in rationale):
        errors.append("rationale 须包含我们的预测数字")
    return errors


def _copy_sources(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("source_title") or item.get("title") or "").strip()
        if not title:
            continue
        out.append(
            {
                "source_title": title,
                "source_type": str(item.get("source_type") or item.get("source_tool") or "").strip(),
                "house": str(item.get("house") or item.get("institution") or "").strip(),
                "summary": str(item.get("summary") or "").strip(),
                "as_of": str(item.get("as_of") or item.get("report_date") or item.get("date") or "")[:10],
                "horizon": str(item.get("horizon") or item.get("period") or "").strip(),
            }
        )
    return out


def _income_at(income: dict, name: str, index: int) -> float | None:
    return _list_at(income.get(name), index)


def _list_at(values: Any, index: int) -> float | None:
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    if not _is_number(value):
        return None
    return float(value)


def _pct(series: Any, year: str) -> str:
    if not isinstance(series, dict) or not _is_number(series.get(year)):
        return "—"
    return f"{float(series[year]) * 100:.2f}%"


def _amt(series: Any, year: str) -> str:
    if not isinstance(series, dict) or not _is_number(series.get(year)):
        return "—"
    return f"{float(series[year]):.4g}"


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
