from __future__ import annotations

from valuation.peer_valuation.peer_agent import run_live_peers
from valuation.shared.cli import run_dir_arg
from valuation.shared.state import load_state


def main() -> int:
    run_dir, _ = run_dir_arg()
    load_state(run_dir)
    cons, card = run_live_peers(run_dir)
    print(
        f"peer_valuation: consensus={cons.get('coverage_status')} "
        f"core={len(card.get('core') or [])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
