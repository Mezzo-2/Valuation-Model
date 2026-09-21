"""确认 split_plan 后回填历史分部收入。活数据交给 hist agent。"""

from __future__ import annotations

from pathlib import Path

from valuation.segment_split.hist_agent import run_live_hist_fill


def build_hist_notes(run_dir: Path) -> dict:
    return run_live_hist_fill(run_dir)
