from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from valuation.shared.io import OUTPUT_DIR, artifacts_dir, dossier_path, dump_json, spec_dir
from valuation.shared.state import load_state, new_state, save_state
from valuation.workbook.build import build_workbook
from valuation.workbook.certify import certify_workbook

ROOT = Path(__file__).resolve().parents[1]

STAGES = [
    "historical_financials",
    "segment_split",
    "review_split",
    "historical_fill",
    "segment_research",
    "review_forecast",
    "operating_cost",
    "peer_valuation",
    "review_comps",
    "review_consensus",
    "research_dossier",
    "build",
    "certify",
]

STAGE_MODULES = {
    "historical_financials": "valuation.historical_financials.write",
    "segment_split": "valuation.segment_split.write",
    "historical_fill": "valuation.segment_split.write_hist",
    "segment_research": "valuation.segment_research.write",
    "operating_cost": "valuation.operating_cost.write",
    "peer_valuation": "valuation.peer_valuation.write",
    "research_dossier": "valuation.research_dossier.write",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="估值模型：各阶段写 JSON，再一次编译 Excel")
    parser.add_argument("--ticker", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--auto-approve", action="store_true")
    parser.add_argument("--resume", default="")
    parser.add_argument("--rebuild-only", action="store_true")
    args = parser.parse_args(argv)

    if args.resume:
        run_dir = _find_run(args.resume)
        state = load_state(run_dir)
    else:
        if not (args.ticker and args.name):
            parser.error("需要同时提供 --ticker 与 --name")
        auto = args.auto_approve
        ticker = args.ticker
        company = args.name
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + ticker
        run_dir = OUTPUT_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)
        state = new_state(run_id, ticker=ticker, company=company, auto_approve=auto)
        save_state(run_dir, state)
        print(f"run_id={run_id}")
        print(f"run_dir={run_dir}")

    if args.rebuild_only:
        return _build_and_certify(run_dir, state)

    return _advance(run_dir, state)


def _find_run(token: str) -> Path:
    direct = OUTPUT_DIR / token
    if (direct / "run_state.json").exists():
        return direct
    matches = sorted(
        path.parent
        for path in OUTPUT_DIR.rglob("run_state.json")
        if token in path.parent.name
    )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"找不到建模目录: {token}")
    names = ", ".join(path.name for path in matches)
    raise SystemExit(f"建模目录不唯一: {token} -> {names}")


def _advance(run_dir: Path, state: dict) -> int:
    completed = set(state.get("completed") or [])
    auto = bool(state.get("auto_approve"))
    if state.get("waiting_for"):
        completed.add(state["waiting_for"])
        state["waiting_for"] = None
        print("resume: 已确认上次审核点，继续往后跑")

    def do(stage: str) -> None:
        if stage in completed:
            return
        print(f"\n== {stage} ==")
        if stage == "segment_research":
            from valuation.segment_research.gather import gather_forecasts

            for name in _segment_names(run_dir):
                card_path = spec_dir(run_dir) / f"forecast_notes_{name}.json"
                if card_path.is_file():
                    print(f"segment_research: {name} 已有预测卡，跳过")
                    continue
                extra = ["--segment", name]
                brief_path = spec_dir(run_dir) / f"research_brief_{name}.json"
                if brief_path.is_file():
                    extra.append("--reuse-search")
                _spawn("segment_research", run_dir, extra=extra)
            gather_forecasts(run_dir)
        elif stage == "build":
            path = build_workbook(run_dir)
            state["workbook"] = str(path)
            print(f"workbook: {path}")
        elif stage == "certify":
            report = certify_workbook(run_dir, Path(state["workbook"]))
            dump_json(artifacts_dir(run_dir) / "certify.json", report)
            calc_fails = report.get("calc_fails") or []
            if report.get("format_fails"):
                print("certify format:")
                for item in report["format_fails"]:
                    print("  -", item)
            state["gates"]["certify"] = "PASS" if not calc_fails else "FAIL"
            if calc_fails:
                print("certify FAIL:")
                for item in calc_fails:
                    print("  -", item)
                raise SystemExit(2)
            print("certify PASS")
        elif stage.startswith("review_"):
            if stage == "review_consensus":
                from valuation.research_dossier.attribution import write_attribution

                path = write_attribution(run_dir)
                print(f"review_consensus: {path}")
            if auto:
                print(f"{stage}: auto-approve")
            else:
                state["waiting_for"] = stage
                state["current_stage"] = stage
                save_state(run_dir, state)
                print(f"已暂停于 {stage}。检查 {spec_dir(run_dir)} 后执行:")
                print(f"  python -m valuation --resume {state['run_id']}")
                raise SystemExit(10)
        else:
            _spawn(stage, run_dir)
        completed.add(stage)
        state["completed"] = sorted(completed)
        state["current_stage"] = stage
        state["waiting_for"] = None
        save_state(run_dir, state)

    for stage in STAGES:
        do(stage)

    state["status"] = "done"
    save_state(run_dir, state)
    print("\n完成。")
    print(f"Excel: {state.get('workbook')}")
    print(f"底稿: {dossier_path(run_dir, state.get('company'))}")
    return 0


def _build_and_certify(run_dir: Path, state: dict) -> int:
    print("== rebuild-only ==")
    path = build_workbook(run_dir)
    state["workbook"] = str(path)
    report = certify_workbook(run_dir, path)
    dump_json(artifacts_dir(run_dir) / "certify.json", report)
    calc_fails = report.get("calc_fails") or []
    if report.get("format_fails"):
        print("certify format:")
        for item in report["format_fails"]:
            print("  -", item)
    state["gates"]["certify"] = "PASS" if not calc_fails else "FAIL"
    save_state(run_dir, state)
    if calc_fails:
        print("certify FAIL:")
        for item in calc_fails:
            print("  -", item)
        return 2
    print(f"certify PASS  {path}")
    return 0


def _spawn(stage: str, run_dir: Path, extra: list[str] | None = None) -> None:
    cmd = [sys.executable, "-m", STAGE_MODULES[stage], "--run-dir", str(run_dir)]
    if extra:
        cmd.extend(extra)
    proc = subprocess.run(cmd, cwd=str(ROOT), check=False)
    if proc.returncode != 0:
        raise SystemExit(f"{stage} failed with {proc.returncode}")


def _segment_names(run_dir: Path) -> list[str]:
    root = spec_dir(run_dir)
    plan_path = root / "split_plan.json"
    if plan_path.exists():
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        return [item["name"] for item in data.get("segments") or []]
    data = json.loads((root / "segment_split_notes.json").read_text(encoding="utf-8"))
    return [item["name"] for item in data["final_segments"]]


if __name__ == "__main__":
    raise SystemExit(main())
