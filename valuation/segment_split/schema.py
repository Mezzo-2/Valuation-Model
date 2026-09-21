"""主营业务拆分 / 一致预期的规范表。

各阶段和人审只写这一套。workbook 通过 adapt 翻译后出表。
旧字段在 normalize_* 里升级，不另发明第三套。
"""

from __future__ import annotations

from statistics import median
from typing import Any

METHODS = frozenset(
    {
        "量价增速法",
        "量价绝对值法",
        "市场渗透法",
        "订单转化法",
        "门店坪效法",
        "用户单价法",
        "收入增速法",
    }
)
METHOD_ORDER = (
    "量价增速法",
    "量价绝对值法",
    "市场渗透法",
    "订单转化法",
    "门店坪效法",
    "用户单价法",
    "收入增速法",
)
METHOD_ALIASES = {"收入增速兜底": "收入增速法"}
QUALITY = frozenset({"直接披露", "有据可查", "行业推算", "推算", "倒推", "兜底"})
FILL_METHODS = frozenset({"官方抄录", "卖方抄录", "残差倒推", "结构推算"})
SPLIT_CALIBERS = frozenset({"official", "sellside", "spec_mix"})
EXPLAIN_KEYS = ("拆分逻辑", "历史数据说明", "其他业务说明", "主要来源")
FALLBACK_METHOD = "收入增速法"
REVENUE_SCOPE = "营业收入"
RECON_PCT = 0.005
RECON_ABS = 0.1
COVERAGE = frozenset({"complete", "partial", "unavailable"})
SOURCE_MODES = frozenset({"institution_median", "provider_aggregate", "none"})
METRIC_COVERAGE = frozenset({"robust", "thin", "unavailable"})


class SchemaError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


def t_recon(reported: float) -> float:
    return max(RECON_ABS, abs(reported) * RECON_PCT)


def canonical_method(method: str) -> str:
    name = str(method or "").strip()
    return METHOD_ALIASES.get(name, name)


def require_split(payload: dict, facts: dict, *, require_method: bool = False) -> dict:
    canonical = normalize_split(payload, facts)
    errors = validate_split(canonical, facts, require_method=require_method)
    if errors:
        raise SchemaError(errors)
    return canonical


def require_consensus(payload: dict, facts: dict) -> dict:
    canonical = normalize_consensus(payload, facts)
    errors = validate_consensus(canonical, facts)
    if errors:
        raise SchemaError(errors)
    return canonical


def normalize_split(raw: dict, facts: dict) -> dict:
    hist = list(facts["hist_periods"])
    segs_in = list(raw.get("final_segments") or [])
    if segs_in and _is_canonical_seg(segs_in[0]):
        segs = [_fill_canonical_seg(seg, hist, facts) for seg in segs_in]
        expl = dict(raw.get("split_explanation") or {})
    else:
        segs = [_upgrade_legacy_seg(seg, hist, facts) for seg in segs_in]
        expl = {
            "拆分逻辑": str(raw.get("split_logic") or ""),
            "历史数据说明": str(raw.get("hist_data_note") or ""),
            "其他业务说明": str(raw.get("other_note") or ""),
            "主要来源": _legacy_sources_text(raw.get("sources")),
        }
    sources = raw.get("sources")
    if not isinstance(sources, list):
        sources = [
            {
                "source_id": "S1",
                "source_tool": "fixture",
                "source_title": "历史拆分来源",
                "summary": expl.get("主要来源") or "未单列来源",
                "segments_identified": [seg["name"] for seg in segs],
            }
        ]
        for seg in segs:
            seg["source_refs"] = ["S1"]
    official_parents = [
        str(name).strip() for name in (raw.get("official_parents") or []) if str(name).strip()
    ]
    if not official_parents:
        seen: list[str] = []
        for seg in segs:
            parent = str(seg.get("parent") or "").strip()
            if parent and parent not in seen:
                seen.append(parent)
        official_parents = seen
    return {
        "company": raw.get("company") or facts.get("company"),
        "ticker": raw.get("ticker") or facts.get("ticker"),
        "ts": raw.get("ts") or facts.get("as_of") or "",
        "historical_periods": list(raw.get("historical_periods") or raw.get("hist_periods") or hist),
        "revenue_unit": raw.get("revenue_unit") or facts.get("unit") or "亿元",
        "revenue_scope": raw.get("revenue_scope") or REVENUE_SCOPE,
        "split_caliber": raw.get("split_caliber") or "official",
        "fill_method": raw.get("fill_method") or "官方抄录",
        "official_parents": official_parents,
        "final_segments": segs,
        "split_explanation": {key: str(expl.get(key) or "") for key in EXPLAIN_KEYS},
        "final_rationale": str(raw.get("final_rationale") or expl.get("拆分逻辑") or ""),
        "sources": sources,
    }


def validate_split(payload: dict, facts: dict, *, require_method: bool = False) -> list[str]:
    errors: list[str] = []
    hist = list(facts["hist_periods"])
    if list(payload.get("historical_periods") or []) != hist:
        errors.append(f"historical_periods {payload.get('historical_periods')} != facts {hist}")
    if payload.get("revenue_scope") != REVENUE_SCOPE:
        errors.append(f"revenue_scope 必须是 {REVENUE_SCOPE}，实际 {payload.get('revenue_scope')}")
    if payload.get("revenue_unit") not in {None, "亿元", facts.get("unit")}:
        errors.append(f"revenue_unit 必须与事实单位一致，实际 {payload.get('revenue_unit')}")
    if payload.get("split_caliber") not in SPLIT_CALIBERS:
        errors.append(f"split_caliber 非法: {payload.get('split_caliber')}")
    if payload.get("fill_method") not in FILL_METHODS:
        errors.append(f"fill_method 非法: {payload.get('fill_method')}（这是历史填数方法，不是预测方法）")
    segs = payload.get("final_segments") or []
    if len(segs) < 2:
        errors.append("final_segments 至少两个分部")
    source_ids = {str(item.get("source_id") or "") for item in payload.get("sources") or [] if isinstance(item, dict)}
    names = [str(seg.get("name") or "").strip() for seg in segs]
    if names and len(names) != len(set(names)):
        errors.append("分部名称重复")
    revenue = facts["income"][REVENUE_SCOPE]
    for seg in segs:
        name = str(seg.get("name") or "").strip()
        method = canonical_method(seg.get("预测方法") or "")
        if require_method:
            if method not in METHODS:
                errors.append(f"{name or '?'} 预测方法不在白名单: {method}")
        elif method and method not in METHODS:
            errors.append(f"{name or '?'} 预测方法不在白名单: {method}")
    for i, year in enumerate(hist):
        total = 0.0
        for seg in segs:
            name = str(seg.get("name") or "").strip()
            hist_rev = seg.get("historical_revenue") or {}
            if year not in hist_rev or not _is_number(hist_rev[year]):
                errors.append(f"{name} 缺少 {year} historical_revenue")
                continue
            amount = float(hist_rev[year])
            total += amount
            quality = (seg.get("data_quality") or {}).get(year)
            if quality not in QUALITY:
                errors.append(f"{name} {year} data_quality 非法: {quality}")
            for ref in seg.get("source_refs") or []:
                if str(ref) not in source_ids:
                    errors.append(f"{name} source_refs 无法回指 {ref}")
            note = str(seg.get("note") or "")
            if not note.startswith("["):
                errors.append(f"{name} note 须以质量标签开头")
        if _is_number(revenue[i]):
            reported = float(revenue[i])
            if abs(total - reported) > t_recon(reported):
                errors.append(
                    f"{year} 分部加总 {total} 对不上 {REVENUE_SCOPE} {reported}（容差 {t_recon(reported)}）"
                )
    expl = payload.get("split_explanation") or {}
    for key in EXPLAIN_KEYS:
        if not str(expl.get(key) or "").strip():
            errors.append(f"split_explanation 缺 {key}")
    if not str(payload.get("final_rationale") or "").strip():
        errors.append("缺 final_rationale")
    if not source_ids:
        errors.append("sources 不能为空")
    return errors


def normalize_consensus(raw: dict, facts: dict) -> dict:
    fcst = list(facts["forecast_periods"])
    if raw.get("institution_estimates") is not None or isinstance(raw.get("consensus"), dict):
        houses = list(raw.get("institution_estimates") or [])
        aggregates = list(raw.get("provider_aggregates") or [])
        consensus = raw.get("consensus") or _aggregate_consensus(houses, aggregates, fcst)
        status = raw.get("coverage_status") or _top_coverage(consensus, fcst)
        return {
            "company": raw.get("company") or facts.get("company"),
            "ticker": raw.get("ticker") or facts.get("ticker"),
            "ts": raw.get("ts") or facts.get("as_of") or "",
            "as_of_date": raw.get("as_of_date") or "",
            "forecast_periods": list(raw.get("forecast_periods") or fcst),
            "revenue_unit": raw.get("revenue_unit") or "亿元",
            "revenue_scope": raw.get("revenue_scope") or REVENUE_SCOPE,
            "eps_unit": raw.get("eps_unit") or "元/股",
            "eps_scope": raw.get("eps_scope") or "EPS",
            "aggregation_policy": raw.get("aggregation_policy") or _default_policy(),
            "institution_estimates": houses,
            "provider_aggregates": aggregates,
            "consensus": consensus,
            "coverage_status": status,
            "open_gaps": list(raw.get("open_gaps") or []),
        }
    houses = []
    for i, item in enumerate(raw.get("institutions") or [], 1):
        estimates = {}
        for j, year in enumerate(fcst):
            rev = (item.get("revenue") or [None] * len(fcst))
            eps = (item.get("eps") or [None] * len(fcst))
            estimates[year] = {
                "revenue": _num_or_none(rev[j] if j < len(rev) else None),
                "eps": _num_or_none(eps[j] if j < len(eps) else None),
            }
        houses.append(
            {
                "institution": item.get("name") or item.get("institution"),
                "report_date": item.get("published") or item.get("report_date"),
                "source_id": item.get("source_id") or f"C{i}",
                "source_tool": item.get("source_tool") or "fixture",
                "source_title": item.get("source_title") or str(item.get("name") or "机构预测"),
                "estimates": estimates,
            }
        )
    consensus = _aggregate_consensus(houses, [], fcst)
    gaps = []
    if any(
        (consensus[year]["eps"]["value"] is None) for year in fcst
    ):
        gaps.append("旧口径一致预期未提供 EPS，已按 unavailable 登记，未编造。")
    return {
        "company": raw.get("company") or facts.get("company"),
        "ticker": raw.get("ticker") or facts.get("ticker"),
        "ts": raw.get("ts") or "",
        "as_of_date": raw.get("as_of_date") or "",
        "forecast_periods": fcst,
        "revenue_unit": "亿元",
        "revenue_scope": REVENUE_SCOPE,
        "eps_unit": "元/股",
        "eps_scope": "EPS",
        "aggregation_policy": _default_policy(),
        "institution_estimates": houses,
        "provider_aggregates": [],
        "consensus": consensus,
        "coverage_status": _top_coverage(consensus, fcst),
        "open_gaps": gaps,
    }


def validate_consensus(payload: dict, facts: dict) -> list[str]:
    errors: list[str] = []
    fcst = list(facts["forecast_periods"])
    if list(payload.get("forecast_periods") or []) != fcst:
        errors.append(f"forecast_periods {payload.get('forecast_periods')} != facts {fcst}")
    if payload.get("revenue_scope") != REVENUE_SCOPE:
        errors.append(f"一致预期 revenue_scope 必须是 {REVENUE_SCOPE}")
    if payload.get("coverage_status") not in COVERAGE:
        errors.append(f"coverage_status 非法: {payload.get('coverage_status')}")
    houses = payload.get("institution_estimates") or []
    aggregates = payload.get("provider_aggregates") or []
    if houses and aggregates:
        errors.append("机构明细与提供方聚合不得写进同一份并混算")
    if not houses and not aggregates:
        if payload.get("coverage_status") != "unavailable":
            errors.append("无机构也无聚合时 coverage_status 须为 unavailable")
    consensus = payload.get("consensus") or {}
    if set(consensus) != set(fcst):
        errors.append(f"consensus 年度键必须恰好为 {fcst}")
    for year in fcst:
        block = consensus.get(year) or {}
        for metric in ("revenue", "eps"):
            cell = block.get(metric) or {}
            if cell.get("source_mode") not in SOURCE_MODES:
                errors.append(f"{year} {metric} source_mode 非法: {cell.get('source_mode')}")
            if cell.get("coverage_status") not in METRIC_COVERAGE | COVERAGE:
                errors.append(f"{year} {metric} coverage_status 非法: {cell.get('coverage_status')}")
            if cell.get("value") is not None and not _is_number(cell.get("value")):
                errors.append(f"{year} {metric}.value 不是数字")
            if int(cell.get("sample_count") or 0) < 0:
                errors.append(f"{year} {metric} sample_count 非法")
    for item in houses:
        if not item.get("institution") or not item.get("report_date"):
            errors.append("institution_estimates 缺 institution / report_date")
        if not item.get("source_id"):
            errors.append(f"{item.get('institution')} 缺 source_id")
    return errors


def _is_canonical_seg(seg: dict) -> bool:
    return isinstance(seg.get("historical_revenue"), dict)


def _fill_canonical_seg(seg: dict, hist: list[str], facts: dict) -> dict:
    revenue = dict(seg.get("historical_revenue") or {})
    quality = dict(seg.get("data_quality") or {})
    last = hist[-1]
    reported = facts["income"][REVENUE_SCOPE]
    last_rev = revenue.get(last)
    last_total = reported[-1] if reported else None
    share = None
    if _is_number(last_rev) and _is_number(last_total) and float(last_total) != 0:
        share = float(last_rev) / float(last_total)
    raw_method = str(seg.get("预测方法") or seg.get("method") or "").strip()
    return {
        "name": seg["name"],
        "parent": str(seg.get("parent") or "").strip(),
        "预测方法": canonical_method(raw_method) if raw_method else "",
        "historical_revenue": revenue,
        "data_quality": quality,
        "source_refs": list(seg.get("source_refs") or []),
        "revenue_share_latest": seg.get("revenue_share_latest", share),
        "note": seg.get("note") or "",
    }


def _upgrade_legacy_seg(seg: dict, hist: list[str], facts: dict) -> dict:
    values = list(seg.get("hist_revenue") or [])
    revenue = {year: values[i] for i, year in enumerate(hist) if i < len(values)}
    tag = _strip_tag(seg.get("quality") or seg.get("note") or "")
    quality = {year: tag or "有据可查" for year in hist}
    reported = facts["income"][REVENUE_SCOPE]
    last_rev = revenue.get(hist[-1]) if hist else None
    share = None
    if _is_number(last_rev) and reported and _is_number(reported[-1]) and float(reported[-1]) != 0:
        share = float(last_rev) / float(reported[-1])
    note = str(seg.get("note") or "")
    if note and not note.startswith("["):
        note = f"[{tag or '有据可查'}] {note}"
    raw_method = str(seg.get("预测方法") or seg.get("method") or "").strip()
    return {
        "name": seg["name"],
        "parent": str(seg.get("parent") or "").strip(),
        "预测方法": canonical_method(raw_method) if raw_method else "",
        "historical_revenue": revenue,
        "data_quality": quality,
        "source_refs": list(seg.get("source_refs") or ["S1"]),
        "revenue_share_latest": share,
        "note": note,
    }


def _legacy_sources_text(sources: Any) -> str:
    if isinstance(sources, str):
        return sources
    if isinstance(sources, list):
        parts = []
        for item in sources:
            if isinstance(item, dict):
                parts.append(str(item.get("source_title") or item.get("summary") or ""))
            else:
                parts.append(str(item))
        return "；".join(p for p in parts if p)
    return ""


def _strip_tag(text: str) -> str:
    text = str(text).strip()
    if text.startswith("[") and "]" in text:
        return text[1 : text.index("]")]
    return text


def _default_policy() -> dict[str, Any]:
    return {
        "institution_dedup": "latest_report_per_metric_per_period",
        "institution_method": "median",
        "min_robust_sample": 3,
        "aggregate_handling": "do_not_mix_with_institution_detail",
    }


def _aggregate_consensus(houses: list[dict], aggregates: list[dict], fcst: list[str]) -> dict:
    out: dict[str, Any] = {}
    use_houses = bool(houses) and not aggregates
    for year in fcst:
        out[year] = {
            "revenue": _metric_cell(houses if use_houses else [], aggregates, year, "revenue"),
            "eps": _metric_cell(houses if use_houses else [], aggregates, year, "eps"),
        }
    return out


def _metric_cell(houses: list[dict], aggregates: list[dict], year: str, metric: str) -> dict:
    values: list[float] = []
    if houses:
        for item in houses:
            cell = ((item.get("estimates") or {}).get(year) or {}).get(metric)
            if _is_number(cell):
                values.append(float(cell))
        mode = "institution_median" if values else "none"
    elif aggregates:
        for item in aggregates:
            cell = ((item.get("estimates") or {}).get(year) or {}).get(metric)
            if cell is None:
                cell = ((item.get("values") or {}).get(year) or {}).get(metric)
            if _is_number(cell):
                values.append(float(cell))
        mode = "provider_aggregate" if values else "none"
    else:
        mode = "none"
    n = len(values)
    if n == 0:
        status = "unavailable"
        value = None
    elif n < 3:
        status = "thin"
        value = float(median(values))
    else:
        status = "robust"
        value = float(median(values))
    return {
        "value": value,
        "sample_count": n,
        "source_mode": mode,
        "coverage_status": status,
    }


def _top_coverage(consensus: dict, fcst: list[str]) -> str:
    flags = []
    for year in fcst:
        for metric in ("revenue", "eps"):
            flags.append((consensus.get(year) or {}).get(metric, {}).get("value") is not None)
    if flags and all(flags):
        return "complete"
    if any(flags):
        return "partial"
    return "unavailable"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _num_or_none(value: Any) -> float | None:
    return float(value) if _is_number(value) else None
