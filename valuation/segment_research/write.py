from __future__ import annotations

import argparse
from pathlib import Path

from valuation.segment_research.forecast_agent import run_live_research
from valuation.shared.state import load_state
from valuation.shared.io import spec_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--segment", default="")
    parser.add_argument(
        "--reuse-search",
        action="store_true",
        help="把上次检索放进上下文当已读材料，仍可再搜",
    )
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    segment = args.segment
    if not segment:
        raise SystemExit("segment_research 需要 --segment")
    load_state(run_dir)
    root = spec_dir(run_dir)
    card = run_live_research(run_dir, segment, reuse_search=args.reuse_search)
    print(
        f"segment_research: {segment} method={card.get('method')} "
        f"notes={root / f'forecast_notes_{segment}.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
