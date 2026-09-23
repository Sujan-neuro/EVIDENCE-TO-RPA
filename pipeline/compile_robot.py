#!/usr/bin/env python3
"""Canonical IR -> a runnable Robot Framework script.

    python3 pipeline/compile_robot.py runs/runN/ir.json

One model call composes the ordered *read* steps, choosing only from a fixed
catalogue of hand-written keywords. Everything that decides anything -- every
comparison, every branch, every value written back -- is compiled from the IR
by code and never passes through the model. The rendered script must then pass
`robot --dryrun` before it is accepted; if it does not, the real parser error
goes back and the steps are composed again.

Writes into the output directory:

    invoice_review.resource   the steps, as keywords
    invoice_review.robot      the runnable entry point
    verify_outcome.robot      runs the above for one invoice and checks the
                              business outcome that lands in the sheet
    lineage.json              every generated line -> the IR node behind it
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ir"))

from pipeline.ir_to_robot import compile_ir_to_robot  # noqa: E402
from pipeline.llm_client import WorkspaceClient  # noqa: E402
from validator import validate_ir_document  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ir", help="A canonical IR document, e.g. runs/runN/ir.json")
    ap.add_argument("-o", "--out-dir", default=None,
                    help="Where to write the script (default: <ir dir>/generated).")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--evidence", default="evidence",
                    help="Evidence directory, used to ground provenance before compiling.")
    args = ap.parse_args()

    ir_path = Path(args.ir)
    ir = json.loads(ir_path.read_text())
    out_dir = Path(args.out_dir) if args.out_dir else ir_path.parent / "generated"

    # An IR that does not validate cannot produce automation worth running, and
    # failing here says so plainly rather than surfacing as a strange script.
    report = validate_ir_document(ir, evidence_dir=args.evidence)
    if not report["valid"]:
        errs = (report.get("schemaErrors", []) + report.get("semanticErrors", [])
                + report.get("groundingErrors", []))
        print(f"{ir_path} is not a valid IR ({len(errs)} error(s)):")
        for e in errs[:10]:
            print(f"  {e.get('path', '')}: {e.get('message', '')}")
        return 2
    print(f"IR valid  ->  {ir_path}")

    client = WorkspaceClient(model=args.model) if args.model else WorkspaceClient()
    print(f"model={client.model}")

    # Relative, not absolute: an absolute path bakes one machine's home
    # directory into a committed artefact, and the script stops working the
    # moment the repository is cloned anywhere else.
    library = os.path.relpath((REPO_ROOT / "robot" / "library.resource").resolve(),
                              out_dir.resolve())
    result = compile_ir_to_robot(
        ir, client, out_dir=out_dir, max_attempts=args.max_attempts,
        resource_path=library)

    for attempt in result["attempts"]:
        mark = "ok  " if not attempt["errors"] else "FAIL"
        print(f"  attempt {attempt['attempt']}  {mark}")
        for err in attempt["errors"][:4]:
            print(f"      {err.splitlines()[0]}")

    if not result["success"]:
        print(f"\nFAILED after {len(result['attempts'])} attempt(s) -- see {out_dir}")
        return 1

    # The execution harness lives beside the generated script so that a single
    # `robot` invocation can run the process and then read the cell back.
    (out_dir / "verify_outcome.robot").write_text(
        (REPO_ROOT / "robot" / "verify_outcome.robot").read_text())

    print(f"\npassed dry run  ->  {result['robotScript']}")
    print(f"  lineage: {len(result['lineage'])} generated lines, each traced to an IR node")
    print(f"  run it:  robot --variable INVOICE_ID:INV-96000 "
          f"--variable EXPECTED_STATUS:PROCUREMENT_REVIEW \\\n"
          f"             --outputdir {out_dir}/run {out_dir}/verify_outcome.robot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
