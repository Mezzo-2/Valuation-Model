from __future__ import annotations

from valuation.shared.io import dump_json, spec_dir
from valuation.shared.state import load_state
from valuation.shared.cli import run_dir_arg
from valuation.historical_financials.fetch import fetch_facts


def main() -> int:
    run_dir, _ = run_dir_arg()
    state = load_state(run_dir)
    ticker = str(state.get("ticker") or "")
    company = str(state.get("company") or "")
    payload = fetch_facts(run_dir, ticker=ticker, company=company)
    company_info = payload.pop("company_info", None)
    dump_json(spec_dir(run_dir) / "facts.json", payload)
    if company_info:
        dump_json(spec_dir(run_dir) / "company_info.json", company_info)
    cov = payload.get("coverage") or {}
    extra = ""
    if cov:
        extra = (
            f"  IS={cov.get('income_rows')} BS={cov.get('balance_rows')} "
            f"gaps={len(cov.get('optional_missing') or [])}"
        )
    info_note = "  company_info=1" if company_info else ""
    print(
        f"historical_financials: wrote {payload['company']} {payload['ticker']} "
        f"source={payload.get('source', 'live')}{extra}{info_note}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
