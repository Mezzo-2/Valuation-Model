from __future__ import annotations

from valuation.segment_split.hist_from_plan import build_hist_notes
from valuation.shared.cli import run_dir_arg
from valuation.shared.io import dump_json, spec_dir
from valuation.shared.state import load_state


def main() -> int:
    run_dir, _ = run_dir_arg()
    load_state(run_dir)
    root = spec_dir(run_dir)
    notes = build_hist_notes(run_dir)
    dump_json(root / "segment_split_notes.json", notes)
    names = [item["name"] for item in notes.get("final_segments") or []]
    print(
        f"historical_fill: {notes.get('fill_method')} "
        f"segments={','.join(names)} "
        f"notes={root / 'segment_split_notes.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
