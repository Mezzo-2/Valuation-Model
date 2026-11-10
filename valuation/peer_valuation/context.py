"""同业估值前的经营摘要，不依赖 comps 或研究快照。"""

from __future__ import annotations

from pathlib import Path

from valuation.shared.io import load_json, safe_segment_filename, spec_dir
from valuation.shared.operating import operating_from_spec
from valuation.workbook.adapt import adapt_split


def operating_context(facts: dict, split: dict, forecasts: dict, cost: dict) -> dict:
    hist = list(facts["hist_periods"])
    fcst = list(facts["forecast_periods"])
    periods = hist + fcst
    segments = []
    for seg in split["final_segments"]:
        card = forecasts[seg["name"]]
        segments.append({
            "name": seg["name"], "parent": seg.get("parent"),
            "historical_revenue": dict(zip(hist, seg["hist_revenue"])),
            "method": seg["method"], "forecast": card.get("final_forecast"),
            "rationale": card.get("final_rationale"), "sources": card.get("sources") or [],
        })
    _, pnl = operating_from_spec({
        "facts": facts, "split": split, "forecasts": forecasts, "cost": cost,
    })
    for i, year in enumerate(periods):
        previous = pnl[periods[i - 1]]["eps"] if i else None
        eps = pnl[year]["eps"]
        comparable = previous is not None and previous > 0 and eps is not None and eps > 0
        pnl[year]["eps_yoy"] = eps / previous - 1 if comparable else None
    return {"as_of": facts.get("as_of") or facts.get("as_of_date"),
            "hist_periods": hist, "forecast_periods": fcst,
            "currency": facts.get("currency") or "CNY", "unit": facts.get("unit") or "亿元",
            "eps_scope": facts.get("eps_scope") or "EPS", "segments": segments,
            "pnl": pnl, "cost_rationale": cost.get("rationale"),
            "cost_sources": cost.get("sources") or []}


def load_operating_context(run_dir: Path, facts: dict) -> dict:
    root = spec_dir(run_dir)
    notes = load_json(root / "segment_split_notes.json")
    forecasts = {}
    for seg in notes["final_segments"]:
        name = seg["name"]
        path = root / f"forecast_notes_{safe_segment_filename(name)}.json"
        if not path.is_file():
            path = root / f"forecast_notes_{name}.json"
        forecasts[name] = load_json(path)
    context = operating_context(
        facts, adapt_split(notes, facts, forecasts), forecasts,
        load_json(root / "cost_assumptions.json"),
    )
    info_path = root / "company_info.json"
    if info_path.is_file():
        context["company_profile"] = load_json(info_path).get("profile") or {}
    return context
