#!/usr/bin/env python3
"""Build the canonical IR by direct synthesis, in three LLM passes.

    python pipeline/synthesize_ir.py evidence/recorded --seed-size 3

    narrations           -> a hypothesis of what the process is
    hypothesis + N recs  -> a draft IR, every claim carrying event references
    + a batch, and again -> until every recording has been absorbed

Each extension round is validated, replayed and regression-checked on its own,
so the IR entering the next round is one that already reproduces everything
seen so far.

The model is given no domain vocabulary: no entity list, no sheet mapping, no
outcome enum. It is shown what the recordings contain and works the domain out
itself, and every claim it makes is resolved back against the events before the
IR is accepted.
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

sys.path.insert(0, str(REPO_ROOT / "ir"))

from pipeline.llm_client import WorkspaceClient  # noqa: E402
from pipeline.synthesis.oracle import summary  # noqa: E402
from pipeline.runs import next_run_dir  # noqa: E402
from pipeline.synthesis.run import synthesize  # noqa: E402
from validator import validate_ir_document  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recordings", help="Directory of recordings, e.g. evidence/recorded")
    ap.add_argument("--seed-size", type=int, default=3,
                    help="How many recordings seed the draft IR (default 3).")
    ap.add_argument("--extend-batch", type=int, default=2,
                    help="How many further recordings each extension round absorbs "
                         "(default 2). The extension loops until the corpus is exhausted.")
    ap.add_argument("--max-attempts", type=int, default=5,
                    help="Correction rounds allowed per pass (default 5). The seed pass asks "
                         "for a whole process model at once, and a document three fixes away "
                         "needs three round trips -- three attempts is not enough headroom.")
    ap.add_argument("--model", default=None)
    ap.add_argument("--out-dir", default="runs",
                    help="Where run directories are created (default: runs/).")
    ap.add_argument("--run-dir", action="store_true",
                    help="Treat --out-dir as the run directory itself, instead of "
                         "creating the next runN inside it.")
    args = ap.parse_args()

    out_dir = Path(args.out_dir) if args.run_dir else next_run_dir(args.out_dir)
    if args.run_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    run_id = out_dir.name
    client = WorkspaceClient(model=args.model) if args.model else WorkspaceClient()
    print(f"runId={run_id}  model={client.model}")

    result = synthesize(
        args.recordings, client, seed_size=args.seed_size,
        extend_batch=args.extend_batch,
        max_attempts=args.max_attempts, out_dir=out_dir,
        validate=lambda d: validate_ir_document(
            d, evidence_dir=str(REPO_ROOT / "evidence")))

    if not result.success:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "synthesis_report.json").write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        failed = next((p for p in result.passes if not p.success), None)
        print(f"\nFAILED at pass {failed.name!r} after {len(failed.attempts)} attempt(s)")
        for err in (failed.attempts[-1].get("errors") or [])[:10]:
            print(f"  - {'.'.join(str(x) for x in err.get('loc', []))}: {err.get('msg', '')}")
        print(f"see {out_dir / 'synthesis_report.json'}")
        return 1

    ir = result.ir
    print()
    print(summary(result.replay_rows))
    print()
    verdict = result.validation and result.validation.get("valid")
    print(f"canonical IR -> {out_dir / 'ir.json'}   "
          f"schema+semantics+grounding: {'VALID' if verdict else 'INVALID'}")
    print(f"  entities={len(ir.entities)} derivations={len(ir.derivations)} "
          f"tasks={len(ir.tasks)} rules={len(ir.rules)}")
    print(f"  cascade : {' -> '.join(ir.evaluation_order)}")
    print(f"  passes all -> {ir.success_outcome}   undecidable -> {ir.default_outcome}   "
          f"multi-failure -> {ir.multiple_failure_policy}")
    for rule_id in ir.evaluation_order:
        rule = next(r for r in ir.rules if r.rule_id == rule_id)
        right = f" {rule.right.ref}" if rule.right else ""
        print(f"    {rule.rule_id:<24} {rule.left.ref} {rule.operator}{right}"
              f"  fails -> {rule.failure_outcome}  conf={rule.confidence}")
    for warning in result.warnings:
        print(f"  ! {warning}")
    for question in ir.open_questions:
        print(f"    ? {question}")
    return 0 if all(r.passed for r in result.replay_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
