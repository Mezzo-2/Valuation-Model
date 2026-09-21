from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "output"


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def spec_dir(run_dir: Path) -> Path:
    return run_dir


def artifacts_dir(run_dir: Path) -> Path:
    return run_dir


def logs_dir(run_dir: Path) -> Path:
    return run_dir / "logs"


def workbook_filename(company: str | None, ticker: str | None = None) -> str:
    name = _safe_filename(company) or _safe_filename(ticker) or "未命名"
    return f"{name}_估值模型.xlsx"


def workbook_path(run_dir: Path, company: str | None = None, ticker: str | None = None) -> Path:
    return artifacts_dir(run_dir) / workbook_filename(company, ticker)


def _safe_filename(text: str | None) -> str:
    cleaned = "".join(ch for ch in str(text or "").strip() if ch not in '\\/:*?"<>|')
    return cleaned.strip(" .")


def dossier_path(run_dir: Path, company: str | None = None) -> Path:
    return artifacts_dir(run_dir) / "research_dossier.md"
