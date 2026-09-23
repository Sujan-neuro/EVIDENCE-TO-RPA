#!/usr/bin/env python3
"""The whole chain, in one command, into one run directory.

    python3 pipeline/run_pipeline.py evidence/recorded \\
        --scenario INV-95700:MANUAL_REVIEW \\
        --scenario INV-94117:WAREHOUSE_REVIEW

    recordings -> IR -> Robot script -> dry run -> live execution -> summary

Everything one invocation produces lands under `runs/runN/`, so a single
directory recovers the whole chain: what the model was asked, what it replied,
what was rejected and why, the accepted IR, the generated script, the browser
logs, and whether the business outcome that landed in the sheet was the right
one.

Stages can also be run individually -- `synthesize_ir.py` and
`compile_robot.py` -- when you want to iterate on one of them.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ir"))

from pipeline.ir_to_robot import compile_ir_to_robot  # noqa: E402
from pipeline.llm_client import WorkspaceClient  # noqa: E402
from pipeline.runs import next_run_dir  # noqa: E402
from pipeline.synthesis.oracle import summary as replay_summary  # noqa: E402
from pipeline.synthesis.run import synthesize  # noqa: E402
from validator import validate_ir_document  # noqa: E402


def _app_is_up(url: str = "http://localhost:8811/", timeout: float = 2.0) -> bool:
    """Is the sheet application actually serving?

    Worth asking before driving a browser at it: a dead port surfaces as a
    locator timeout deep inside Robot's log, which reads like the automation
    is wrong rather than absent.
    """
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _execute(generated: Path, invoice_id: str, expected: str, run_dir: Path,
             headed: bool, slowmo: str) -> dict:
    """Run the generated automation for one invoice and read the verdict back."""
    outdir = run_dir / "execution" / invoice_id
    started = time.time()
    result = subprocess.run(
        [sys.executable, "-m", "robot",
         "--variable", f"INVOICE_ID:{invoice_id}",
         "--variable", f"EXPECTED_STATUS:{expected}",
         "--variable", f"HEADLESS:{not headed}",
         "--variable", f"SLOWMO:{slowmo}",
         "--outputdir", str(outdir),
         str(generated / "verify_outcome.robot")],
        capture_output=True, text=True, cwd=REPO_ROOT)
    actual = None
    for line in result.stdout.splitlines():
        if "RESULT:" in line:
            actual = line.split("->")[-1].strip()
    return {"invoiceId": invoice_id, "expected": expected, "actual": actual,
            "passed": actual == expected, "seconds": round(time.time() - started, 1),
            "outputDir": str(outdir)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recordings", nargs="?", default="evidence/recorded")
    ap.add_argument("--scenario", action="append", default=[], metavar="INVOICE:EXPECTED",
                    help="An invoice and the outcome it should reach. Repeatable.")
    ap.add_argument("--model", default=None)
    ap.add_argument("--seed-size", type=int, default=3)
    ap.add_argument("--extend-batch", type=int, default=2)
    ap.add_argument("--max-attempts", type=int, default=5)
    ap.add_argument("--headed", action="store_true", help="Watch the browser work.")
    ap.add_argument("--slowmo", default="0s", help="Pace each interaction, e.g. 0.4s.")
    ap.add_argument("--no-execute", action="store_true",
                    help="Stop after the dry-run gate; do not drive a browser.")
    ap.add_argument("--out-dir", default="runs")
    args = ap.parse_args()

    run_dir = next_run_dir(args.out_dir)
    client = WorkspaceClient(model=args.model) if args.model else WorkspaceClient()
    started = time.time()
    print(f"run={run_dir.name}  model={client.model}\n")

    # --- 1. recordings -> IR ------------------------------------------------
    print("[1/3] Evidence -> IR")
    result = synthesize(
        args.recordings, client, seed_size=args.seed_size,
        extend_batch=args.extend_batch, max_attempts=args.max_attempts,
        out_dir=run_dir,
        validate=lambda d: validate_ir_document(d, evidence_dir=str(REPO_ROOT / "evidence")),
        on_event=lambda line: print("     " + line.strip()))
    if not result.success:
        failed = next((p for p in result.passes if not p.success), None)
        transport = failed.attempts[-1].get("transportError") if failed.attempts else None
        if transport:
            print(f"\nFAILED at pass {failed.name!r}: could not reach the model")
            print(f"  {transport[:200]}")
            print("  This is not a prompt problem. Check LLM_BASE_URL and the network, "
                  "or try a\n  faster model -- a reasoning model can exceed the request "
                  "timeout on the seed pass.")
        else:
            print(f"\nFAILED at pass {failed.name!r} after {len(failed.attempts)} attempt(s)")
            for err in (failed.attempts[-1].get("errors") or [])[:8]:
                print(f"  {'.'.join(str(x) for x in err.get('loc', []))}: "
                      f"{err.get('msg', '')[:200]}")
        print(f"\nwhat the run did produce is in {run_dir}/")
        return 1

    replayed = sum(1 for r in result.replay_rows if r.passed)
    print(f"      replay {replayed}/{len(result.replay_rows)}   "
          f"IR {'valid' if (result.validation or {}).get('valid') else 'INVALID'}")

    # --- 2. IR -> Robot -----------------------------------------------------
    print("[2/3] IR -> Robot script")
    generated = run_dir / "generated"
    import os
    robot = compile_ir_to_robot(
        result.canonical, client, out_dir=generated, max_attempts=3,
        resource_path=os.path.relpath(
            (REPO_ROOT / "robot" / "library.resource").resolve(), generated.resolve()))
    (run_dir / "robot_compile_report.json").write_text(json.dumps(
        {"attempts": [{"attempt": a["attempt"], "errors": a["errors"]}
                      for a in robot["attempts"]]}, indent=2))
    if not robot["success"]:
        print(f"      FAILED after {len(robot['attempts'])} attempt(s)")
        for err in robot["attempts"][-1]["errors"][:3]:
            print(f"        {err.splitlines()[0]}")
        return 1
    (generated / "verify_outcome.robot").write_text(
        (REPO_ROOT / "robot" / "verify_outcome.robot").read_text())
    print(f"      passed dry run in {len(robot['attempts'])} attempt(s), "
          f"{len(robot['lineage'])} lines traced to IR nodes")

    # --- 3. execution against the live application ---------------------------
    scenarios = []
    if args.no_execute:
        print("[3/3] Execution        skipped (--no-execute)")
    elif not args.scenario:
        print("[3/3] Execution        skipped (no --scenario given)")
    elif not _app_is_up():
        print("[3/3] Execution        SKIPPED -- nothing is serving on "
              "http://localhost:8811/")
        print("      Start it first:  python3 app/serve.py 8811")
        print("      The IR and the generated script above are unaffected.")
    else:
        print(f"[3/3] Execution        {len(args.scenario)} scenario(s)")
        for spec in args.scenario:
            invoice_id, _, expected = spec.partition(":")
            outcome = _execute(generated, invoice_id, expected, run_dir,
                               args.headed, args.slowmo)
            scenarios.append(outcome)
            mark = "ok  " if outcome["passed"] else "FAIL"
            print(f"      {mark} {invoice_id}  expected {expected}, "
                  f"got {outcome['actual']}  ({outcome['seconds']}s)")

    passed = sum(1 for s in scenarios if s["passed"])
    summary = {
        "run": run_dir.name,
        "model": client.model,
        "seconds": round(time.time() - started, 1),
        "recordings": len(result.replay_rows),
        "replay": f"{replayed}/{len(result.replay_rows)}",
        "irValid": (result.validation or {}).get("valid"),
        "robotAttempts": len(robot["attempts"]),
        "scenarios": scenarios,
        "businessOutcomeAccuracy": f"{passed}/{len(scenarios)}" if scenarios else None,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    print(f"\n{replay_summary(result.replay_rows).splitlines()[0]}")
    if scenarios:
        print(f"BUSINESS OUTCOME ACCURACY: {passed}/{len(scenarios)}")
    print(f"\neverything for this run is in {run_dir}/")
    return 0 if (not scenarios or passed == len(scenarios)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
