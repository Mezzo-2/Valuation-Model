from __future__ import annotations

from pathlib import Path

from valuation.research_dossier.schema import SchemaError as CoverSchemaError, require_summary_notes
from valuation.segment_split.schema import SchemaError, require_consensus, require_split
from valuation.shared.io import load_json, spec_dir
from valuation.workbook.adapt import adapt_consensus, adapt_split


def load_spec(run_dir: Path, *, require_notes: bool = False) -> dict:
    root = spec_dir(run_dir)
    facts = load_json(root / "facts.json")
    try:
        split_notes = require_split(
            load_json(root / "segment_split_notes.json"), facts, require_method=False
        )
        consensus = require_consensus(load_json(root / "consensus_estimates.json"), facts)
    except SchemaError as exc:
        raise SystemExit("spec 未通过规范表校验:\n  " + "\n  ".join(exc.errors)) from exc
    forecasts = {}
    missing: list[str] = []
    empty_method: list[str] = []
    for seg in split_notes.get("final_segments") or []:
        name = str(seg.get("name") or "").strip()
        if not name:
            continue
        path = root / f"forecast_notes_{name}.json"
        if not path.is_file():
            missing.append(name)
            continue
        card = load_json(path)
        forecasts[name] = card
        if not str(card.get("method") or card.get("预测方法") or "").strip():
            empty_method.append(name)
    if missing:
        raise SystemExit("缺少分部预测卡：" + "、".join(missing))
    if empty_method:
        raise SystemExit("预测卡没有方法：" + "、".join(empty_method))
    split = adapt_split(split_notes, facts, forecasts)
    cost = load_json(root / "cost_assumptions.json")
    comps = load_json(root / "comps_spec.json")
    info_path = root / "company_info.json"
    spec = {
        "facts": facts,
        "company_info": load_json(info_path) if info_path.is_file() else {},
        "split": split,
        "split_notes": split_notes,
        "consensus": adapt_consensus(consensus, facts),
        "consensus_canonical": consensus,
        "cost": cost,
        "comps": comps,
        "forecasts": forecasts,
    }
    if require_notes:
        notes_path = root / "summary_notes.json"
        if not notes_path.exists():
            raise FileNotFoundError(
                f"缺 summary_notes.json：先跑 research_dossier，或改封面后再 --rebuild-only。"
                f"路径 {notes_path}"
            )
        try:
            spec["summary_notes"] = require_summary_notes(load_json(notes_path))
        except CoverSchemaError as exc:
            raise SystemExit("summary_notes 未通过校验:\n  " + "\n  ".join(exc.errors)) from exc
    return spec
