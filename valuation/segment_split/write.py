from __future__ import annotations

from valuation.shared.io import spec_dir
from valuation.shared.state import load_state
from valuation.shared.cli import run_dir_arg
from valuation.segment_split.collect import run_live_split


def main() -> int:
    run_dir, _ = run_dir_arg()
    load_state(run_dir)
    root = spec_dir(run_dir)
    pack = run_live_split(run_dir)
    plan = pack.get("split_plan") or {}
    names = [item.get("name") for item in plan.get("segments") or []]
    print(
        f"segment_split: caliber={plan.get('caliber')} "
        f"segments={','.join(str(name) for name in names)} "
        f"plan={root / 'split_plan.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
