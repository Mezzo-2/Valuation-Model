from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_NAME = "run_state.json"


def load_state(run_dir: Path) -> dict[str, Any]:
    path = run_dir / STATE_NAME
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(run_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    (run_dir / STATE_NAME).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def new_state(
    run_id: str,
    *,
    ticker: str,
    company: str,
    auto_approve: bool,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "ticker": ticker,
        "company": company,
        "auto_approve": auto_approve,
        "current_stage": "init",
        "status": "running",
        "waiting_for": None,
        "completed": [],
        "gates": {},
        "workbook": None,
        "dossier": None,
    }
