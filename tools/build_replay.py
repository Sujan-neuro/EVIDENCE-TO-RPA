#!/usr/bin/env python3
"""Turn a recorded synthesis run into a self-contained page you can film.

    python3 tools/build_replay.py runs/run1 -o docs/replay.html   # any run directory

A live run is the wrong thing to point a camera at: eight model calls, twelve
minutes of wall clock, and perhaps forty seconds where anything is on screen.
It is also non-deterministic, so a re-take is a different film.

Every run already writes down the whole story -- each pass, each rejected
attempt and why it was rejected, and the IR as it stood afterwards. This
replays that record at whatever pace reads well, driven by the arrow keys.

The data is inlined rather than fetched, so the page is one file that opens
from disk with no server and no CORS argument in the middle of a recording.
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.synthesis.trace import EvidenceTrace  # noqa: E402


def _ir_after(pass_record: dict) -> dict | None:
    """The IR a pass ended with: whatever its accepted attempt returned.

    Taken from the attempts rather than the report's `ir`/`draftIR` fields so
    that a run with several extension rounds yields one IR per round, instead
    of jumping from the draft straight to the final answer.
    """
    for attempt in pass_record.get("attempts", []):
        if attempt.get("accepted"):
            try:
                return json.loads(attempt["rawReply"])
            except (json.JSONDecodeError, KeyError):
                return None
    return None


def _evidence_map(ir: dict) -> tuple[list[dict], dict]:
    """Every node that carries evidence, and the reverse index.

    This is what makes the provenance click-through possible: the claim that
    each part of the model traces to specific recorded moments is the one a
    viewer would otherwise have to take on trust.
    """
    nodes: list[dict] = []

    def add(kind: str, label: str, detail: str, refs) -> None:
        if refs:
            nodes.append({"id": f"{kind}:{label}", "kind": kind, "label": label,
                          "detail": detail, "refs": list(refs)})

    for e in ir.get("entities", []):
        add("entity", e["entity"], f'sheet "{e["sheet"]}" · key {e["key_field"]}', e.get("evidence"))
        for f in e.get("fields", []):
            add("field", f'{e["entity"]}.{f["field"]}',
                f'column "{f["column"]}" · {f["unit"]}', f.get("evidence"))
    for d in ir.get("derivations", []):
        add("derivation", d["name"],
            f'{d["function"]}({", ".join(d["operands"])}) → {d["unit"]}', d.get("evidence"))
    for o in ir.get("outcomes", []):
        add("outcome", o["name"], o.get("meaning", ""), o.get("evidence"))
    for t in ir.get("tasks", []):
        add("task", t["task_id"], t.get("intent", ""), t.get("evidence"))
    for r in ir.get("rules", []):
        left = r.get("left", {}).get("ref", "")
        right = (r.get("right") or {}).get("ref", "")
        op = r.get("operator", "")
        add("rule", r["rule_id"], f"{left} {op} {right}".strip(), r.get("evidence"))

    reverse: dict[str, list[str]] = {}
    for node in nodes:
        for ref in node["refs"]:
            reverse.setdefault(ref, []).append(node["id"])
    return nodes, reverse


def build(run_dir: Path, recordings_dir: Path) -> dict:
    report = json.loads((run_dir / "synthesis_report.json").read_text())
    trace = EvidenceTrace.load_dir(recordings_dir)

    recordings = [{
        "slug": r.slug,
        "narration": r.narration,
        "outcome": r.outcome,
        # The element block is how the page is drawn, not what the process
        # means -- dropping it keeps the file small and the screen readable.
        "events": [{k: v for k, v in ev.items() if k != "element"} for ev in r.events],
    } for r in trace.recordings]

    seed = report.get("seed") or []
    steps: list[dict] = []
    for record in report.get("passes", []):
        name = record["name"]
        ir = _ir_after(record)
        # The hypothesis is a different document -- candidate names drawn from
        # narration, with no evidence to map. Only the IR passes have claims
        # that point at recorded moments.
        is_hypothesis = name == "hypothesis"
        nodes, reverse = ([], {}) if (is_hypothesis or not ir) else _evidence_map(ir)
        if is_hypothesis:
            title, given = "Narrations → Hypothesis", "every narration — no events at all"
        elif name == "seed":
            title, given = "Hypothesis + seed recordings → Draft IR", ", ".join(seed)
        else:
            covered = (ir or {}).get("covered_demonstrations", [])
            added = [c for c in covered if c not in seed] or ["the next batch"]
            # `extend[2]` names round two; a bare `extend` is a single round.
            round_no = name[len("extend"):].strip("[]") or "1"
            title, given = f"Extend — round {round_no}", ", ".join(added)
        steps.append({
            "name": name, "title": title, "given": given,
            "attempts": [{
                "n": a["attempt"],
                "accepted": bool(a.get("accepted")),
                "errors": [{"loc": ".".join(str(x) for x in e.get("loc", [])) or "—",
                            "msg": e.get("msg", "")} for e in (a.get("errors") or [])],
            } for a in record.get("attempts", [])],
            "hypothesis": ir if is_hypothesis else None,
            "ir": None if is_hypothesis else ir,
            "nodes": nodes, "reverse": reverse,
        })

    return {"runId": run_dir.name, "recordings": recordings, "steps": steps,
            "replay": report.get("replay", []), "seed": seed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", help="A run directory, e.g. runs/run1")
    ap.add_argument("--recordings", default="evidence/recorded")
    ap.add_argument("-o", "--out", default="docs/replay.html")
    args = ap.parse_args()

    data = build(Path(args.run_dir), Path(args.recordings))
    template = (Path(__file__).parent / "replay_template.html").read_text()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(template.replace(
        "/*__DATA__*/null", json.dumps(data, ensure_ascii=False)))

    size = out.stat().st_size / 1024
    print(f"{out}  ({size:.0f} KB)")
    print(f"  run       : {data['runId']}")
    print(f"  steps     : {len(data['steps'])}  ({', '.join(s['name'] for s in data['steps'])})")
    print(f"  recordings: {len(data['recordings'])}")
    print(f"  evidenced nodes in the final IR: {len(data['steps'][-1]['nodes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
