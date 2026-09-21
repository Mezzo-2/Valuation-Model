"""把规范拆分 / 一致预期翻译成当前 Kernel 能读的薄结构。"""

from __future__ import annotations

from typing import Any

from valuation.shared.houses import short_house_name


def adapt_split(canonical: dict, facts: dict, forecasts: dict | None = None) -> dict[str, Any]:
    hist = list(facts["hist_periods"])
    cards = forecasts or {}
    segs = []
    for seg in canonical["final_segments"]:
        revenue = seg["historical_revenue"]
        card = cards.get(str(seg.get("name") or "").strip()) or {}
        method = str(card.get("method") or card.get("预测方法") or "").strip()
        segs.append(
            {
                "name": seg["name"],
                "parent": str(seg.get("parent") or "").strip(),
                "method": method,
                "hist_revenue": [revenue[year] for year in hist],
                "unit": canonical.get("revenue_unit") or "亿元",
                "note": seg.get("note") or "",
            }
        )
    expl = canonical["split_explanation"]
    return {
        "company": canonical.get("company"),
        "ticker": canonical.get("ticker"),
        "hist_periods": hist,
        "official_parents": [
            str(name).strip() for name in (canonical.get("official_parents") or []) if str(name).strip()
        ],
        "final_segments": segs,
        "split_logic": expl["拆分逻辑"],
        "hist_data_note": expl["历史数据说明"],
        "other_note": expl["其他业务说明"],
        "sources": expl["主要来源"],
    }


def adapt_consensus(canonical: dict, facts: dict) -> dict[str, Any]:
    fcst = list(facts["forecast_periods"])
    houses = []
    for item in canonical.get("institution_estimates") or []:
        estimates = item.get("estimates") or {}
        houses.append(
            {
                "name": short_house_name(item.get("institution")),
                "published": item.get("report_date"),
                "revenue": [
                    (estimates.get(year) or {}).get("revenue") for year in fcst
                ],
            }
        )
    if not houses:
        for item in canonical.get("provider_aggregates") or []:
            estimates = item.get("estimates") or item.get("values") or {}
            houses.append(
                {
                    "name": short_house_name(item.get("provider") or "提供方聚合"),
                    "published": item.get("as_of_date") or item.get("report_date"),
                    "revenue": [
                        (estimates.get(year) or {}).get("revenue") for year in fcst
                    ],
                }
            )
    return {
        "company": canonical.get("company"),
        "ticker": canonical.get("ticker"),
        "forecast_periods": fcst,
        "as_of_date": canonical.get("as_of_date"),
        "institutions": houses,
        "coverage": canonical.get("coverage_status"),
        "revenue_scope": canonical.get("revenue_scope"),
    }
