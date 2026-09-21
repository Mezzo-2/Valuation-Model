"""全部分部预测卡齐后，由代码收成一份总卡。不重搜、不改数。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from valuation.segment_research.schema import SchemaError, require_forecast
from valuation.shared.io import dump_json, load_json, spec_dir


def gather_forecasts(run_dir: Path) -> dict:
    root = spec_dir(run_dir)
    facts = load_json(root / "facts.json")
    notes = load_json(root / "segment_split_notes.json")
    cards: list[dict] = []
    missing: list[str] = []
    errors: list[str] = []
    for seg in notes.get("final_segments") or []:
        name = str(seg.get("name") or "").strip()
        if not name:
            continue
        path = root / f"forecast_notes_{name}.json"
        if not path.is_file():
            missing.append(name)
            continue
        try:
            card = require_forecast(
                load_json(path),
                facts,
                notes_seg=seg,
            )
        except SchemaError as exc:
            errors.extend(f"{name}: {item}" for item in exc.errors)
            continue
        cards.append(card)
    if missing:
        raise SystemExit("缺少分部预测卡：" + "、".join(missing))
    if errors:
        raise SystemExit("汇总预测卡未通过规范表:\n  " + "\n  ".join(errors))
    pack = {
        "company": facts.get("company") or notes.get("company"),
        "ticker": facts.get("ticker") or notes.get("ticker"),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "segments": cards,
    }
    dump_json(root / "forecast_notes.json", pack)
    print(f"gather: {len(cards)} 个分部 → {root / 'forecast_notes.json'}")
    return pack
