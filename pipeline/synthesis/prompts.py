"""What each synthesis pass is told.

Three prompts, and the difference between them is the point:

  1. NARRATION  -- narrations only, no events. Asked for a hypothesis.
  2. SEED       -- hypothesis + a few complete recordings. Asked for an IR.
  3. EXTEND     -- that IR + the remaining recordings. Asked what must change.

The evidence rendering is shared, and it deliberately drops the `element`
block from every event. Selectors, xpaths and accessibility paths are how this
particular workbook is drawn; none of them belong in a business process model,
and including them invites the model to write UI mechanics into `intent`
fields. They are also the bulk of the bytes.
"""
from __future__ import annotations

import json

from .models import ProcessHypothesis, SynthesizedIR
from .trace import EvidenceTrace, Recording

_UI_ONLY = ("element",)


def render_events(rec: Recording) -> str:
    lines = []
    for ev in rec.events:
        row = {k: v for k, v in ev.items() if k not in _UI_ONLY}
        row["eventId"] = f"{rec.slug}:{ev['eventId']}"
        row.pop("timestamp", None)
        lines.append(json.dumps(row, ensure_ascii=False))
    return "\n".join(lines)


def render_recording(rec: Recording) -> str:
    return (f"### {rec.slug}  ({rec.demonstration_id})\n"
            f"NARRATION: {rec.narration}\n"
            f"EVENTS:\n{render_events(rec)}")


def render_corpus(trace: EvidenceTrace) -> str:
    return "\n\n".join(render_recording(r) for r in trace.recordings)


def _vocabulary(trace: EvidenceTrace) -> str:
    """What the corpus contains -- derived from it, never from a fixed table.

    The model is told which sheets and columns exist because those are facts
    about the recordings it is being shown. It is NOT told what any of them
    mean: no entity list, no outcome enum, no column-to-field mapping. Working
    that out is the task.
    """
    return trace.describe()


def _schema_block(model) -> str:
    return json.dumps(model.model_json_schema(), indent=1, ensure_ascii=False)


# ==========================================================================
# Pass 1 -- narrations only
# ==========================================================================

NARRATION_SYSTEM = """\
You are reading what a subject-matter expert SAID while working, and nothing else.

You have no events, no clicks and no screen. That is deliberate. Your job is to
say what the process appears to be from the narration alone, so that the next
pass can check it against what was actually done. A hypothesis that later turns
out to be half wrong is useful; a confident guess dressed as fact is not.

Rules:
  * Say what the narrator says. Do not supply thresholds, orderings or
    exception handling they never mention.
  * When narrations disagree or go quiet on something that obviously matters,
    put it in `uncertainties` rather than resolving it.
  * Name each verdict the way it would be recorded -- a short, stable label --
    and keep the narrator's own wording in `phrasing`.

Return ONLY a JSON object matching the schema. No prose, no code fence."""


def narration_messages(trace: EvidenceTrace) -> list[dict]:
    blocks = "\n\n".join(
        f"### {r.slug}\n{r.narration}" for r in trace.recordings if r.narration.strip())
    user = (f"{_vocabulary(trace)}\n\n"
            f"NARRATIONS ({len(trace)} demonstrations)\n\n{blocks}\n\n"
            f"SCHEMA\n{_schema_block(ProcessHypothesis)}\n\n"
            f"Return the ProcessHypothesis JSON.")
    return [{"role": "system", "content": NARRATION_SYSTEM},
            {"role": "user", "content": user}]


# ==========================================================================
# Pass 2 -- hypothesis + seed recordings -> IR
# ==========================================================================

SEED_SYSTEM = """\
You are turning demonstrations of a business process into a canonical process
model (an IR) that automation will later be generated from.

You are given a HYPOTHESIS drawn from the narrations, and the FULL recordings
of a few demonstrations. The hypothesis is a reading, not a fact. Where the
events contradict it, the events win; say so in `open_questions`.

The one rule that matters most:

  EVERY structural claim must cite the events that support it, by qualified
  reference (`SLUG:EVENT_ID`, e.g. `PRICE_EXCESS:E003`). Those references are
  resolved against the actual recordings. An id that does not exist is not a
  small error -- it is a fabricated justification, and it will be rejected.
  Bare ids like `E003` are meaningless: ids restart in every recording.

Further:
  * Work out the domain yourself. Nobody has told you what these sheets mean,
    which entities exist, what the columns represent or what the verdicts are
    called -- that is the task. Name entities and fields in business terms
    (`Invoice.unitPrice`), not in screen terms ("row 3 of the price column"),
    and bind each one to the sheet and column the recordings show it on.
  * Separate business intent from screen mechanics. `intent` says what a step
    ACHIEVES ("establish the ordered price for this invoice"), never which tab
    was clicked.
  * State every rule in the PASSING direction: `operator` true means satisfied,
    carry on. `failure_outcome` is where the invoice goes when it is not.
  * Compare like with like. A percentage variance is not a price. If the SME
    worked a number out rather than read it, declare it in `derivations`. A
    derivation may be built on another derivation.
  * Any limit the recordings READ must be an `observed_value` citing that
    read -- never a constant with the same number. A constant keeps enforcing
    today's limit after the business changes it. Use `constant` only for a
    number that appears nowhere on screen.
  * A value that reads as a sentence rather than a number is not a threshold.
    Express what it means as a comparison between fields instead.
  * The verdict is a value WRITTEN into a cell. Declare every verdict you see
    written in `outcomes`, citing the write. There is no separate "disposition"
    value to invent, and `inputs`/`outputs` may only name fields or derivations
    you declared.
  * An entity's `key_field` is how one of them is identified. It is established
    by SEARCHING for it, so it has no read of its own -- declare it as a field
    anyway, and cite the search.
  * For an EXISTS rule, say which entity must be findable in `subject_entity`.
    "Invoice.poNumber EXISTS" on its own is ambiguous: it could mean the
    invoice carries a PO number, or that the purchase order it names can be
    found. Those check different sheets and route differently.
  * You are seeing only part of the corpus. Prefer a model that is honestly
    incomplete, with the gaps in `open_questions`, over one that is complete
    because you filled the gaps yourself.

Return ONLY a JSON object matching the schema. No prose, no code fence."""


def seed_messages(hypothesis: ProcessHypothesis, seed: EvidenceTrace) -> list[dict]:
    user = (f"{_vocabulary(seed)}\n\n"
            f"HYPOTHESIS FROM THE NARRATIONS\n"
            f"{hypothesis.model_dump_json(indent=1)}\n\n"
            f"RECORDINGS ({len(seed)} of them: {', '.join(seed.slugs)})\n\n"
            f"{render_corpus(seed)}\n\n"
            f"SCHEMA\n{_schema_block(SynthesizedIR)}\n\n"
            f"Return the SynthesizedIR JSON. `covered_demonstrations` must be exactly "
            f"{seed.slugs}.")
    return [{"role": "system", "content": SEED_SYSTEM},
            {"role": "user", "content": user}]


# ==========================================================================
# Pass 3 -- extend the IR with the rest of the corpus
# ==========================================================================

EXTEND_SYSTEM = """\
You are extending an existing process model with demonstrations it has not seen.

You are given the current IR, the hypothesis the narrations produced, and a
FURTHER BATCH of recordings it has not seen. This is one round of several: more
recordings may follow, so a model that is honestly incomplete is fine and a
model that guesses ahead is not. Return the COMPLETE updated IR -- not a patch.

How to treat what is already there:
  * The existing IR is grounded in recordings you can no longer see. Keep its
    entities, fields, derivations, tasks and rules, and keep their evidence
    references exactly as they are. You cannot verify them, and you must not
    invent replacements for them.
  * Add what this batch demonstrates: new fields, new rules, new tasks, new
    evidence references on things that already exist.
  * If a new recording CONTRADICTS an existing rule, do not quietly rewrite it.
    Change it, and state plainly in `open_questions` what contradicted what.
  * Extend `evaluation_order` and `covered_demonstrations` to match.

Every new reference you add must be a real event in the recordings you were
just given, in `SLUG:EVENT_ID` form. Old references stay as they are.

Return ONLY a JSON object matching the schema. No prose, no code fence."""


def extend_messages(draft: SynthesizedIR, hypothesis: ProcessHypothesis,
                    rest: EvidenceTrace) -> list[dict]:
    user = (f"{_vocabulary(rest)}\n\n"
            f"CURRENT IR (built from {draft.covered_demonstrations})\n"
            f"{draft.model_dump_json(indent=1)}\n\n"
            f"HYPOTHESIS FROM THE NARRATIONS\n"
            f"{hypothesis.model_dump_json(indent=1)}\n\n"
            f"NEW RECORDINGS IN THIS BATCH ({len(rest)}: {', '.join(rest.slugs)})\n\n"
            f"{render_corpus(rest)}\n\n"
            f"SCHEMA\n{_schema_block(SynthesizedIR)}\n\n"
            f"Return the complete updated SynthesizedIR JSON. `covered_demonstrations` "
            f"must list all of {sorted(set(draft.covered_demonstrations) | set(rest.slugs))}.")
    return [{"role": "system", "content": EXTEND_SYSTEM},
            {"role": "user", "content": user}]


# ==========================================================================
# Retry feedback
# ==========================================================================

def format_errors(errors: list[dict]) -> str:
    """Turn validation failures into a correction the model can act on."""
    lines = ["Your JSON was rejected. Fix every point below and return the "
             "COMPLETE corrected JSON document -- not a patch, not an apology.", ""]
    for err in errors:
        loc = ".".join(str(p) for p in err.get("loc", [])) or "<root>"
        lines.append(f"  - {loc}: {err.get('msg', '')}")
    lines += ["", "Return ONLY the JSON."]
    return "\n".join(lines)
