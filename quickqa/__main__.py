"""CLI: python -m quickqa "<task>" [--app PKG] [--device NAME] ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .agent import QARunner
from .device import Device

EXIT = {"passed": 0, "passed_weak": 0, "passed_with_crash_log": 0, "unverified": 3, "failed": 1, "max_steps": 2}


def default_out_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "quickqa" / "runs") if base else (Path.home() / ".cache" / "quickqa" / "runs")


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="quickqa", description="Fast Android QA executor (Claude Haiku 5.5 planner + TypeSafe Jev selector)")
    ap.add_argument("task", help="what to do / check, in natural language")
    ap.add_argument("--app", help="package (or package/activity) to launch first")
    ap.add_argument("--device", default=os.environ.get("QUICKQA_DEVICE"),
                    help="adb serial or host name (default: $QUICKQA_DEVICE, else the only connected device)")
    ap.add_argument("--start", choices=["restart", "resume"], default="restart",
                    help="with --app: restart = force-stop and launch fresh (default); resume = bring the app "
                         "forward in whatever state the previous run left it")
    ap.add_argument("--restart", action="store_true", help=argparse.SUPPRESS)  # old flag; restart is now the default
    ap.add_argument("--no-safe", action="store_true",
                    help="disable safe mode (by default, taps that look state-changing - delete, settings, "
                         "view/sort/filter modes, toggles - are blocked unless the task needs them)")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--vision", choices=["auto", "always", "never"], default="auto",
                    help="send screenshots to the planner (auto: only when the UI tree is nearly empty)")
    ap.add_argument("--image-side", type=int, default=512,
                    help="longest side in px of screenshots sent to the planner (default 512)")
    ap.add_argument("--report-lang", default=os.environ.get("QUICKQA_REPORT_LANG", "English"),
                    help="language of the report image texts, e.g. English or Korean "
                         "(default: $QUICKQA_REPORT_LANG, else English)")
    ap.add_argument("--no-report-image", action="store_true", help="skip rendering report.png")
    ap.add_argument("--effort", choices=["none", "low", "medium", "high"], default="medium",
                    help="planner effort (default medium: low failed the long Play Store scenario); none = thinking off")
    ap.add_argument("--planner", choices=["claude", "cerebras"], default="claude")
    ap.add_argument("--planner-model", help="override the planner model id (default claude-haiku-5-5)")
    ap.add_argument("--max-input-tokens", type=int, default=100_000,
                    help="per-request planner input ceiling; prompts are shrunk to stay under it (default 100000)")
    ap.add_argument("--out", default=os.environ.get("QUICKQA_OUT", str(default_out_dir())),
                    help="run artifacts directory (default: outside any repo, $QUICKQA_OUT or "
                         "%%LOCALAPPDATA%%/quickqa/runs or ~/.cache/quickqa/runs)")
    ap.add_argument("--save-screens", action="store_true", help="save a screenshot for every step")
    ap.add_argument("--json", action="store_true", help="print the full report JSON instead of a summary")
    ap.add_argument("-q", "--quiet", action="store_true", help="no per-step progress lines")
    a = ap.parse_args(argv)

    try:
        device = Device(a.device)
    except (RuntimeError, FileNotFoundError) as ex:  # no adb, no device, or an ambiguous choice
        print(f"quickqa: {ex}", file=sys.stderr)
        return 4
    run_dir = Path(a.out) / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    runner = QARunner(device, a.task, app=a.app, max_steps=a.max_steps, vision=a.vision,
                      reasoning_effort=a.effort, restart_app=a.start == "restart" or a.restart, safe=not a.no_safe,
                      planner=a.planner, planner_model=a.planner_model, max_input_tokens=a.max_input_tokens, image_side=a.image_side,
                      report_lang=a.report_lang, report_image=not a.no_report_image, run_dir=run_dir,
                      save_screens=a.save_screens, verbose=not a.quiet)
    try:
        report = runner.run()
    finally:
        device.close()

    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"\nSTATUS: {report['status']}  (verifier p_done={report['verifier_p_done']}, "
              f"{len(report['steps'])} steps, {report['elapsed_s']}s)")
        print(f"SUMMARY: {report['summary']}")
        pu = report.get("planner_usage", {})
        if "input_tokens" in pu:
            print(f"PLANNER: {pu['model']} {pu['calls']} calls, in {pu['input_tokens']} / out {pu['output_tokens']} "
                  f"tokens (peak prompt {pu['peak_input_tokens']}), ~${pu['est_cost_usd']}")
        if report.get("failure_kind"):
            print(f"FAILURE KIND: {report['failure_kind']}")
        if report.get("safe_mode_blocked"):
            print("SAFE MODE BLOCKED: " + "; ".join(report["safe_mode_blocked"]))
        if report.get("app_state_changes") is not None:
            ch = report["app_state_changes"]
            print("APP STATE CHANGES: " + ("; ".join(ch[:15]) if ch else "none"))
        for o in report["observations"]:
            print(f"OBS: {o}")
        if report["crash_log"]:
            print("CRASH LOG:\n" + report["crash_log"][-1500:])
        for img in report.get("report_images", []):
            print(f"REPORT IMAGE: {img}")
        print(f"REPORT: {report.get('report_path')}")
        if report.get("final_screenshot"):
            print(f"FINAL SCREENSHOT: {report['final_screenshot']}")
    return EXIT.get(report["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
