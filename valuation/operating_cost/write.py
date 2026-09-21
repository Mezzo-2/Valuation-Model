from __future__ import annotations

from valuation.operating_cost.cost_agent import run_live_cost
from valuation.shared.cli import run_dir_arg
from valuation.shared.io import spec_dir
from valuation.shared.state import load_state


def main() -> int:
    run_dir, _ = run_dir_arg()
    load_state(run_dir)
    root = spec_dir(run_dir)
    card = run_live_cost(run_dir)
    print(f"operating_cost: wrote {root / 'cost_assumptions.json'}")
    gm = card.get("gross_margin") or {}
    print("  合并毛利率 " + " ".join(f"{year}={gm[year]:.2%}" for year in card.get("forecast_periods") or [] if year in gm))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
