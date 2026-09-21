"""按编表恒等式展开营收 / EPS / 目标价，只给分析师引用，不写回 Excel。"""

from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Any

from valuation.segment_research.methods import roll_segment
from valuation.shared.houses import (
    bibliography as build_bibliography,
    is_broker_source,
    is_synthetic_title,
    report_title,
    short_house_name,
)
from valuation.shared.io import dump_json, load_json, logs_dir, spec_dir
from valuation.workbook.load_json import load_spec

GAP_CUTOFF = 0.15
RATING_CUTOFF = 0.15


def build_snapshot(run_dir: Path) -> dict[str, Any]:
    spec = load_spec(run_dir, require_notes=False)
    snap = snapshot_from_spec(spec, run_dir=run_dir)
    dump_json(spec_dir(run_dir) / "snapshot.json", snap)
    return snap


def snapshot_from_spec(spec: dict, *, run_dir: Path | None = None) -> dict[str, Any]:
    facts = spec["facts"]
    split = spec["split"]
    forecasts = spec["forecasts"]
    cost = spec["cost"]
    comps = spec["comps"]
    hist = list(facts["hist_periods"])
    fcst = list(facts["forecast_periods"])
    periods = hist + fcst
    last_a = hist[-1]
    y1 = fcst[0]

    segments = [_segment_roll(seg, forecasts[seg["name"]], hist, fcst) for seg in split["final_segments"]]
    last_total = sum(float(item["revenue"][last_a]) for item in segments) or 1.0
    for item in segments:
        item["share_latest"] = float(item["revenue"][last_a]) / last_total
    revenue = {year: sum(item["revenue"][year] for item in segments) for year in periods}
    pnl = _pnl_roll(facts, cost, revenue, hist, fcst)
    consensus = _consensus_block(spec, run_dir, fcst, revenue, {year: pnl[year]["eps"] for year in fcst})
    valuation = _valuation_block(facts, comps, pnl[y1]["eps"], y1)
    core, tail = _core_and_tail(segments, last_a)
    material = [
        item
        for year in fcst
        for item in (
            _gap_item(year, "营收", consensus[year].get("rev_gap")),
            _gap_item(year, "EPS", consensus[year].get("eps_gap")),
        )
        if item
    ]
    attribution = None
    if run_dir is not None:
        attr_path = spec_dir(run_dir) / "consensus_attribution.json"
        if attr_path.exists():
            attribution = load_json(attr_path)
    snap = {
        "company": facts["company"],
        "ticker": facts.get("ticker"),
        "hist_periods": hist,
        "forecast_periods": fcst,
        "last_actual": last_a,
        "y1": y1,
        "price": valuation["price"],
        "shares_mn": valuation["shares_mn"],
        "segments": segments,
        "core_segments": [item["name"] for item in core],
        "tail_segments": [item["name"] for item in tail],
        "revenue": revenue,
        "pnl": pnl,
        "consensus": consensus,
        "material_gaps": material,
        "valuation": valuation,
        "display": _display(segments, revenue, pnl, consensus, valuation, hist, fcst, material),
        "split_logic": split.get("split_logic") or "",
        "cost_rationale": cost.get("rationale") or "",
        "comps_rationale": comps.get("rationale") or "",
        "attribution": attribution,
        "sources": _collect_sources(spec, run_dir),
    }
    snap["bibliography"] = _bibliography(snap, run_dir=run_dir)
    return snap


def load_snapshot(run_dir: Path) -> dict[str, Any]:
    path = spec_dir(run_dir) / "snapshot.json"
    if path.exists():
        return load_json(path)
    return build_snapshot(run_dir)


def _segment_roll(seg: dict, forecast: dict, hist: list[str], fcst: list[str]) -> dict[str, Any]:
    name = seg["name"]
    method = seg["method"]
    hist_rev = _hist_rev_map(seg, forecast, hist)
    revenue, drivers = roll_segment(method, forecast, hist, fcst, hist_rev)
    yoy = {}
    periods = hist + fcst
    for i, year in enumerate(periods):
        if i == 0:
            continue
        prev = periods[i - 1]
        if revenue[prev]:
            yoy[year] = revenue[year] / revenue[prev] - 1
    return {
        "name": name,
        "method": method,
        "note": seg.get("note") or "",
        "rationale": forecast.get("final_rationale") or "",
        "sources": forecast.get("sources") or [],
        "revenue": revenue,
        "revenue_yoy": yoy,
        "share_latest": None,
        "drivers": drivers,
    }


def _hist_rev_map(seg: dict, forecast: dict, hist: list[str]) -> dict[str, float]:
    raw = seg.get("hist_revenue")
    if isinstance(raw, dict):
        out = {year: float(raw[year]) for year in hist if _is_number(raw.get(year))}
    else:
        rows = list(raw or [])
        out = {
            year: float(rows[i])
            for i, year in enumerate(hist)
            if i < len(rows) and _is_number(rows[i])
        }
    notes = (forecast.get("historical_data") or {}).get("分部收入") or {}
    for year in hist:
        if year not in out and _is_number(notes.get(year)):
            out[year] = float(notes[year])
    return out


def _pnl_roll(
    facts: dict,
    cost: dict,
    revenue: dict[str, float],
    hist: list[str],
    fcst: list[str],
) -> dict[str, dict[str, float | None]]:
    income = facts.get("income") or {}
    shares = float((facts.get("market") or {})["当前总股本_百万股"])
    out: dict[str, dict[str, float | None]] = {}
    periods = hist + fcst
    for i, year in enumerate(hist):
        rev = _income_at(income, "营业收入", i)
        gp = _income_at(income, "毛利", i)
        if gp is None and rev is not None:
            cogs = _income_at(income, "营业成本", i)
            if cogs is not None:
                gp = rev - cogs
        sales = _income_at(income, "销售费用", i)
        admin = _income_at(income, "管理费用", i)
        rd = _income_at(income, "研发费用", i)
        ebit = _income_at(income, "息税前利润", i)
        ni = _income_at(income, "归母净利润", i)
        eps = _income_at(income, "EPS", i)
        tax = _income_at(income, "所得税费用", i)
        pre = _income_at(income, "税前利润", i)
        row = {
            "revenue": rev,
            "gp": gp,
            "gm": None if not rev else (gp or 0) / rev,
            "sales": sales,
            "admin": admin,
            "rd": rd,
            "sales_ratio": None if not rev else (sales or 0) / rev,
            "admin_ratio": None if not rev else (admin or 0) / rev,
            "rd_ratio": None if not rev else (rd or 0) / rev,
            "ebit": ebit,
            "ebit_margin": None if not rev else (ebit or 0) / rev,
            "ni": ni,
            "ni_margin": None if not rev else (ni or 0) / rev,
            "eps": eps,
            "tax_rate": None if not pre else (tax or 0) / pre,
            "rev_yoy": None,
        }
        if i:
            prev = _income_at(income, "营业收入", i - 1)
            if prev:
                row["rev_yoy"] = rev / prev - 1
        out[year] = row
    for year in fcst:
        rev = revenue[year]
        gm = float(cost["gross_margin"][year])
        sr = float(cost["sales_ratio"][year])
        ar = float(cost["admin_ratio"][year])
        dr = float(cost["rd_ratio"][year])
        tax_rate = float(cost["tax_rate"][year])
        fin = float(cost["fin_exp"][year])
        nonop_inc = float(cost["nonop_inc"][year])
        nonop_exp = float(cost["nonop_exp"][year])
        minority = float((cost.get("minority") or {}).get(year) or 0.0)
        other_op = float((cost.get("other_op") or {}).get(year) or 0.0)
        gp = rev * gm
        sales = rev * sr
        admin = rev * ar
        rd = rev * dr
        ebit = gp - sales - admin - rd + other_op
        pre = ebit - fin + nonop_inc - nonop_exp
        tax = pre * tax_rate
        mino = (pre - tax) * minority
        ni = pre - tax - mino
        eps = ni * 100 / shares
        out[year] = {
            "revenue": rev,
            "gp": gp,
            "gm": gm,
            "sales": sales,
            "admin": admin,
            "rd": rd,
            "sales_ratio": sr,
            "admin_ratio": ar,
            "rd_ratio": dr,
            "ebit": ebit,
            "ebit_margin": ebit / rev if rev else None,
            "ni": ni,
            "ni_margin": ni / rev if rev else None,
            "eps": eps,
            "tax_rate": tax_rate,
            "fin": fin,
            "other_op": other_op,
            "rev_yoy": None,
        }
    for i, year in enumerate(periods):
        if i == 0:
            continue
        prev = periods[i - 1]
        cur = out[year]["revenue"]
        pri = out[prev]["revenue"]
        if cur is not None and pri:
            out[year]["rev_yoy"] = cur / pri - 1
    return out


def _valuation_block(facts: dict, comps: dict, eps_y1: float, y1: str) -> dict[str, Any]:
    core = list(comps.get("core") or [])
    pes = [float(item["pe_y1"]) for item in core]
    pe_mean = sum(pes) / len(pes)
    pe_adjust = float(comps.get("pe_adjust") if comps.get("pe_adjust") is not None else 1.0)
    target_pe = pe_mean * pe_adjust
    target_price = target_pe * float(eps_y1)
    price = float((facts.get("market") or {})["当前股价"])
    upside = target_price / price - 1
    rating = "买入" if upside >= RATING_CUTOFF else "观察"
    return {
        "y1": y1,
        "core": [
            {
                "name": item["name"],
                "pe_y1": float(item["pe_y1"]),
                "pe_ttm": item.get("pe_ttm"),
                "mcap": item.get("mcap"),
                "currency": item.get("currency"),
                "note": item.get("note") or "",
            }
            for item in core
        ],
        "pe_mean": pe_mean,
        "pe_adjust": pe_adjust,
        "target_pe": target_pe,
        "eps_y1": float(eps_y1),
        "target_price": target_price,
        "price": price,
        "shares_mn": float((facts.get("market") or {})["当前总股本_百万股"]),
        "upside": upside,
        "rating": rating,
        "pe_adjust_is_one": abs(pe_adjust - 1.0) < 1e-9,
        "target_horizon": "当前合理价格",
        "implied_eps": price / target_pe if target_pe else None,
    }


def _consensus_block(
    spec: dict,
    run_dir: Path | None,
    fcst: list[str],
    revenue: dict[str, float],
    eps: dict[str, float],
) -> dict[str, dict[str, Any]]:
    canonical = _canonical_consensus(spec, run_dir)
    out: dict[str, dict[str, Any]] = {}
    for year in fcst:
        rev_cons = _consensus_metric(canonical, year, "revenue")
        eps_cons = _consensus_metric(canonical, year, "eps")
        rev_gap = None if not rev_cons else revenue[year] / rev_cons - 1
        eps_gap = None if not eps_cons else eps[year] / eps_cons - 1
        out[year] = {
            "revenue": rev_cons,
            "eps": eps_cons,
            "rev_gap": rev_gap,
            "eps_gap": eps_gap,
            "houses": _house_year(canonical, year),
        }
    return out


def _canonical_consensus(spec: dict, run_dir: Path | None) -> dict:
    if spec.get("consensus_canonical"):
        return spec["consensus_canonical"]
    if run_dir is not None:
        path = spec_dir(run_dir) / "consensus_estimates.json"
        if path.exists():
            return load_json(path)
    return spec.get("consensus") or {}


def _consensus_metric(canonical: dict, year: str, metric: str) -> float | None:
    block = ((canonical.get("consensus") or {}).get(year) or {}).get(metric) or {}
    if _is_number(block.get("value")):
        return float(block["value"])
    values: list[float] = []
    for item in canonical.get("institution_estimates") or canonical.get("institutions") or []:
        if "estimates" in item:
            raw = ((item.get("estimates") or {}).get(year) or {}).get(metric)
        else:
            series = item.get(metric) or item.get("revenue") or []
            idx = (canonical.get("forecast_periods") or []).index(year) if year in (canonical.get("forecast_periods") or []) else -1
            raw = series[idx] if 0 <= idx < len(series) else None
        if _is_number(raw):
            values.append(float(raw))
    return float(median(values)) if values else None


def _house_year(canonical: dict, year: str) -> list[dict[str, Any]]:
    rows = []
    for item in canonical.get("institution_estimates") or []:
        est = (item.get("estimates") or {}).get(year) or {}
        rows.append(
            {
                "name": short_house_name(item.get("institution")),
                "published": item.get("report_date"),
                "title": item.get("source_title"),
                "revenue": est.get("revenue"),
                "eps": est.get("eps"),
            }
        )
    if rows:
        return rows
    for item in canonical.get("institutions") or []:
        rows.append({"name": short_house_name(item.get("name")), "published": item.get("published"), "revenue": None, "eps": None})
    return rows


def _core_and_tail(segments: list[dict], last_a: str) -> tuple[list[dict], list[dict]]:
    ordered = sorted(segments, key=lambda item: -float(item["revenue"][last_a]))
    total = sum(float(item["revenue"][last_a]) for item in ordered) or 1.0
    core: list[dict] = []
    acc = 0.0
    for item in ordered:
        if not core:
            core.append(item)
            acc += float(item["revenue"][last_a])
            continue
        if len(core) < 2 and acc / total < 0.80:
            core.append(item)
            acc += float(item["revenue"][last_a])
        else:
            break
    names = {item["name"] for item in core}
    tail = [item for item in ordered if item["name"] not in names]
    return core, tail


def _gap_item(year: str, metric: str, gap: float | None) -> dict[str, Any] | None:
    if gap is None or abs(gap) <= GAP_CUTOFF:
        return None
    direction = "更高" if gap > 0 else "更低"
    return {"year": year, "metric": metric, "gap": gap, "direction": direction}


def _display(
    segments: list[dict],
    revenue: dict[str, float],
    pnl: dict,
    consensus: dict,
    valuation: dict,
    hist: list[str],
    fcst: list[str],
    material: list[dict],
) -> dict[str, Any]:
    periods = hist + fcst
    return {
        "target_price": _r2(valuation["target_price"]),
        "target_pe": _r1(valuation["target_pe"]),
        "pe_mean": _r1(valuation["pe_mean"]),
        "eps_y1": _r2(valuation["eps_y1"]),
        "price": _r2(valuation["price"]),
        "upside_pct": _r1(valuation["upside"] * 100),
        "rating": valuation["rating"],
        "revenue": {year: _r1(revenue[year]) for year in periods},
        "eps": {year: _r2(pnl[year]["eps"]) for year in periods if pnl[year].get("eps") is not None},
        "gm_pct": {year: _r1(pnl[year]["gm"] * 100) for year in periods if pnl[year].get("gm") is not None},
        "rev_yoy_pct": {
            year: _r1(pnl[year]["rev_yoy"] * 100) for year in periods if pnl[year].get("rev_yoy") is not None
        },
        "consensus_revenue": {year: _r1(consensus[year]["revenue"]) for year in fcst if consensus[year].get("revenue") is not None},
        "consensus_eps": {year: _r2(consensus[year]["eps"]) for year in fcst if consensus[year].get("eps") is not None},
        "rev_gap_pct": {year: _r1(consensus[year]["rev_gap"] * 100) for year in fcst if consensus[year].get("rev_gap") is not None},
        "eps_gap_pct": {year: _r1(consensus[year]["eps_gap"] * 100) for year in fcst if consensus[year].get("eps_gap") is not None},
        "segment_revenue": {
            item["name"]: {year: _r1(item["revenue"][year]) for year in periods} for item in segments
        },
        "material_gaps": [
            {
                "year": item["year"],
                "metric": item["metric"],
                "gap_pct": _r1(item["gap"] * 100),
                "direction": item["direction"],
            }
            for item in material
        ],
        "core_pe": [
            {
                "name": item["name"],
                "pe_y1": _r1(item["pe_y1"]),
                "currency": item.get("currency"),
            }
            for item in valuation["core"]
        ],
        "target_horizon": valuation.get("target_horizon"),
        "implied_eps": _r2(valuation["implied_eps"]) if valuation.get("implied_eps") is not None else None,
    }


def _collect_sources(spec: dict, run_dir: Path | None) -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    company = str((spec.get("facts") or {}).get("company") or "")

    def add(name: str | None, date: str | None = None, title: str | None = None) -> None:
        if not is_broker_source(name):
            return
        short = short_house_name(name)
        if not short:
            return
        row = seen.setdefault(short, {"name": short})
        if date and not row.get("date"):
            row["date"] = str(date)[:10]
        incoming = str(title or "").strip()
        if incoming and not report_title(incoming, name, company, date):
            incoming = ""
        if incoming and (not row.get("title") or is_synthetic_title(row.get("title"), short, company, row.get("date"))):
            row["title"] = incoming

    canonical = _canonical_consensus(spec, run_dir)
    for item in canonical.get("institution_estimates") or []:
        add(item.get("institution"), item.get("report_date"), item.get("source_title"))
    for forecast in (spec.get("forecasts") or {}).values():
        for item in forecast.get("sources") or []:
            add(
                item.get("house") or item.get("institution"),
                item.get("as_of") or item.get("date"),
                item.get("title") or item.get("source_title"),
            )
    for item in (spec.get("cost") or {}).get("sources") or []:
        add(
            item.get("house") or item.get("institution"),
            item.get("as_of") or item.get("date"),
            item.get("title") or item.get("source_title"),
        )
    raw_split = spec.get("split_notes")
    if raw_split is None and run_dir is not None:
        split_path = spec_dir(run_dir) / "segment_split_notes.json"
        if split_path.exists():
            raw_split = load_json(split_path)
    for item in (raw_split or {}).get("sources") or []:
        add(item.get("institution"), item.get("date") or item.get("as_of"), item.get("source_title"))
    catalog = _hit_catalog(run_dir)
    for row in seen.values():
        hit = _best_hit(row, catalog, company)
        if not hit:
            continue
        row["title"] = hit["title"]
        if hit.get("date"):
            row["date"] = hit["date"]
    return list(seen.values())


def _hit_catalog(run_dir: Path | None) -> list[dict[str, str]]:
    if run_dir is None:
        return []
    rows: list[dict[str, str]] = []
    root = logs_dir(run_dir) / "mcp"
    if not root.is_dir():
        return []
    for path in sorted(root.glob("research_hits_*.json")):
        try:
            payload = load_json(path)
        except Exception:
            continue
        hits = payload.get("hits") if isinstance(payload, dict) else payload
        if not isinstance(hits, list):
            continue
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            title = str(hit.get("title") or "").strip()
            name = str(hit.get("institution") or hit.get("institutionName") or "").strip()
            if not title or not name:
                continue
            rows.append(
                {
                    "name": name,
                    "date": str(hit.get("date") or "")[:10],
                    "title": title,
                    "doc_type": str(hit.get("doc_type") or ""),
                    "content_type": str(hit.get("content_type") or ""),
                }
            )
    return rows


def _best_hit(row: dict, catalog: list[dict[str, str]], company: str) -> dict[str, str] | None:
    short = short_house_name(row.get("name"))
    want = str(row.get("date") or "")[:10]
    best: dict[str, str] | None = None
    best_score = -1
    for hit in catalog:
        if short_house_name(hit.get("name")) != short:
            continue
        title = report_title(hit.get("title"), hit.get("name"), company, hit.get("date"))
        if not title or (company and company not in title):
            continue
        if " | " in title and not title.startswith(company):
            continue
        score = 5
        if want and hit.get("date") == want:
            score += 10
        if str(hit.get("doc_type") or "").upper() == "REPORT":
            score += 4
        if str(hit.get("content_type") or "") in {"minutes", "foreign_report"}:
            score -= 3
        if any(token in title for token in ("点评", "深度", "跟踪", "首次覆盖", "点评报告")):
            score += 2
        if score > best_score or (
            score == best_score and str(hit.get("date") or "") > str((best or {}).get("date") or "")
        ):
            best_score = score
            best = {"name": short, "date": str(hit.get("date") or want), "title": title}
    return best


def _bibliography(snap: dict, *, run_dir: Path | None = None) -> list[dict[str, str]]:
    return build_bibliography(snap.get("sources") or [], company=snap.get("company"))


def _income_at(income: dict, name: str, index: int) -> float | None:
    series = income.get(name)
    if not isinstance(series, list) or index >= len(series) or series[index] is None:
        return None
    return float(series[index])


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _r1(value: float) -> float:
    return round(float(value), 1)


def _r2(value: float) -> float:
    return round(float(value), 2)


if __name__ == "__main__":
    from valuation.shared.cli import run_dir_arg

    run_dir, _ = run_dir_arg()
    snap = build_snapshot(run_dir)
    val = snap["display"]
    print(f"{snap['company']} {val['rating']} 目标价 {val['target_price']} 上行 {val['upside_pct']}%")
