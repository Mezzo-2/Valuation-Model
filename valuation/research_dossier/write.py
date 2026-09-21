from __future__ import annotations

from valuation.research_dossier.analyst_agent import AnalystDeps, compile_dossier, format_pack
from valuation.research_dossier.snapshot import build_snapshot
from valuation.shared.cli import run_dir_arg
from valuation.shared.io import dossier_path, dump_json, spec_dir
from valuation.shared.state import load_state
from valuation.workbook.load_json import load_spec


def main() -> int:
    run_dir, _ = run_dir_arg()
    load_state(run_dir)
    spec = load_spec(run_dir, require_notes=False)
    snapshot = build_snapshot(run_dir)
    facts = spec["facts"]
    deps = AnalystDeps(
        company=str(facts.get("company") or ""),
        ticker=str(facts.get("ticker") or ""),
        snapshot=snapshot,
        spec=spec,
        pack_text=format_pack(snapshot, spec),
        run_dir=run_dir,
    )
    markdown, notes = compile_dossier(deps)
    print(f"research_dossier: {facts.get('company')}")

    out = dossier_path(run_dir, facts["company"])
    out.write_text(markdown, encoding="utf-8")
    dump_json(spec_dir(run_dir) / "summary_notes.json", notes)
    print(f"research_dossier: {out}")
    print(f"summary_notes: {spec_dir(run_dir) / 'summary_notes.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
