"""定稿前把共识差拆成时效、口径和经营观点。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from valuation.research_dossier.snapshot import snapshot_from_spec
from valuation.shared.io import dump_json, load_json, spec_dir
from valuation.workbook.load_json import load_spec

STALE_DAYS = 180


def write_attribution(run_dir: Path) -> Path:
    spec = load_spec(run_dir, require_notes=False)
    payload = build_attribution(spec, run_dir)
    path = spec_dir(run_dir) / "consensus_attribution.json"
    dump_json(path, payload)
    return path


def build_attribution(spec: dict, run_dir: Path | None = None) -> dict[str, Any]:
    facts = spec.get("facts") or {}
    as_of = _parse_date(facts.get("as_of") or facts.get("as_of_date") or "")
    snap = snapshot_from_spec(spec, run_dir=run_dir)
    briefs = _load_briefs(run_dir) if run_dir is not None else []
    rows: list[dict[str, Any]] = []
    for year in snap.get("forecast_periods") or []:
        cons = (snap.get("consensus") or {}).get(year) or {}
        stale, fresh = _split_houses(cons.get("houses") or [], as_of)
        brief_stale, brief_fresh = _split_brief_rows(briefs, year, as_of)
        causes: list[str] = []
        if stale or brief_stale:
            causes.append("资料时效")
        if _maybe_caliber(cons, snap, year):
            causes.append("口径")
        if (cons.get("rev_gap") is not None or cons.get("eps_gap") is not None) and (fresh or brief_fresh):
            causes.append("经营观点")
        if not causes and (cons.get("rev_gap") is not None or cons.get("eps_gap") is not None):
            causes.append("经营观点")
        rows.append(
            {
                "year": year,
                "metric_gaps": {
                    "rev_gap": cons.get("rev_gap"),
                    "eps_gap": cons.get("eps_gap"),
                },
                "stale_houses": stale + brief_stale,
                "fresh_houses": fresh + brief_fresh,
                "likely_causes": causes,
            }
        )
    return {
        "as_of": facts.get("as_of") or facts.get("as_of_date") or "",
        "stale_days": STALE_DAYS,
        "rows": rows,
        "instruction": "先写时效和口径，再写经营观点。不要直接写成我们更谨慎。",
    }


def _load_briefs(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(spec_dir(run_dir).glob("research_brief_*.json")):
        brief = load_json(path)
        segment = str(brief.get("segment") or path.stem.replace("research_brief_", ""))
        for item in brief.get("sellside") or brief.get("卖方假设") or []:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "segment": segment,
                    "house": item.get("house") or item.get("institution"),
                    "as_of": item.get("as_of") or item.get("report_date"),
                    "horizon": item.get("horizon") or item.get("period"),
                    "metric": item.get("metric"),
                    "value": item.get("value"),
                }
            )
    return rows


def _split_houses(houses: list[dict[str, Any]], as_of: datetime | None) -> tuple[list[str], list[str]]:
    stale: list[str] = []
    fresh: list[str] = []
    for item in houses:
        name = str(item.get("name") or item.get("house") or "").strip()
        if not name:
            continue
        date = _parse_date(item.get("published") or item.get("report_date") or item.get("as_of"))
        label = f"{name} {date.date() if date else ''}".strip()
        if _is_stale(date, as_of):
            stale.append(label)
        else:
            fresh.append(label)
    return stale, fresh


def _split_brief_rows(
    briefs: list[dict[str, Any]],
    year: str,
    as_of: datetime | None,
) -> tuple[list[str], list[str]]:
    stale: list[str] = []
    fresh: list[str] = []
    for item in briefs:
        horizon = str(item.get("horizon") or "")
        if horizon and year[:4] not in horizon:
            continue
        name = str(item.get("house") or "").strip()
        if not name:
            continue
        date = _parse_date(item.get("as_of"))
        label = f"{name} {date.date() if date else ''} {item.get('segment') or ''}".strip()
        if _is_stale(date, as_of):
            stale.append(label)
        else:
            fresh.append(label)
    return stale, fresh


def _maybe_caliber(cons: dict, snap: dict, year: str) -> bool:
    del cons, snap, year
    return False


def _is_stale(date: datetime | None, as_of: datetime | None) -> bool:
    if date is None or as_of is None:
        return False
    return date < as_of - timedelta(days=STALE_DAYS)


def _parse_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return None
