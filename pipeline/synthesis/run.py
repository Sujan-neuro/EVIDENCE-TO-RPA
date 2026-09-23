"""Narration -> hypothesis -> seed IR -> extended IR.

    pass 1   every narration, no events      -> ProcessHypothesis
    pass 2   hypothesis + a seed batch       -> SynthesizedIR (draft)
    pass 3   + a fixed-size batch, repeating -> SynthesizedIR, until the
             until the corpus is exhausted      corpus is exhausted

The extension is a loop, not a single call. Handing the model everything that
is left works while a corpus is small and stops working the moment it is not:
the prompt grows without bound, and one rejected attempt throws away what
several recordings had already contributed. Batching means each round is
validated, replayed and regression-checked on its own, so the IR entering the
next round is one that already reproduces everything seen so far -- and the
cost of a bad round is one batch, not the whole corpus.

Each pass is a validate-and-correct loop, not a single call. The model's reply
is parsed, checked against the Pydantic contract WITH the recordings as
validation context, and -- for the IR passes -- replayed against the evidence.
Anything that fails comes back as a specific correction rather than a retry of
the same prompt, because re-asking an unchanged question gets an unchanged
answer.

Seed selection is deterministic and coverage-driven. Which recordings seed the
model matters enormously: seeding on `missing-po` alone yields a process model
with no goods-receipt entity, and everything downstream inherits the hole. The
greedy choice below maximises distinct sheets, outcomes, processing rules and
fields, so the seed is the widest view of the process that N recordings can
give -- and it is a function of the corpus, not of directory order.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ..llm_client import LLMClient
from . import prompts
from .emit import emit
from .models import ProcessHypothesis, SynthesizedIR
from .oracle import ReplayRow, format_failures, replay, summary
from .trace import EvidenceTrace


# ==========================================================================
# Seed selection
# ==========================================================================

def _features(trace: EvidenceTrace, slug: str) -> set[str]:
    """What one recording shows, in terms that need no knowledge of the domain:
    which sheets it visits, which columns it touches on each, which sheets it
    locates rows on, and what verdict it ends in."""
    one = trace.subset([slug])
    rec = one.recordings[0]
    feats = {f"sheet:{s}" for s in rec.sheets}
    feats |= {f"col:{sheet}.{col}" for sheet, cols in one.columns().items() for col in cols}
    feats |= {f"search:{sheet}" for sheet in rec.searched()}
    if rec.outcome:
        feats.add(f"outcome:{rec.outcome}")
    return feats


def select_seed(trace: EvidenceTrace, k: int = 3) -> list[str]:
    """The k recordings that together show the most of the process.

    Greedy, with an alphabetical tie-break, so the same corpus always produces
    the same seed. Reproducibility here is not a nicety: the seed decides the
    vocabulary every later pass inherits.
    """
    remaining = sorted(trace.slugs)
    chosen: list[str] = []
    covered: set[str] = set()
    while remaining and len(chosen) < k:
        # `remaining` is sorted and `max` keeps the first of equal elements, so
        # ties break alphabetically without needing a second key.
        best = max(remaining, key=lambda s: len(_features(trace, s) - covered))
        gain = len(_features(trace, best) - covered)
        if gain == 0 and chosen:
            break
        chosen.append(best)
        covered |= _features(trace, best)
        remaining.remove(best)
    return chosen


# ==========================================================================
# The validate-and-correct loop
# ==========================================================================

#: The two shapes an endpoint takes in a transport error: a full URL, and the
#: bare host that `requests`/`urllib3` quote in connection-pool messages.
_URL_RE = re.compile(r"https?://[^\s'\"),]+")
_HOST_RE = re.compile(r"(host=)'[^']*'")


def _redact_endpoint(message: str) -> str:
    """Strip endpoint URLs out of anything written to a run report.

    A transport error quotes the host it failed to reach, and run reports are
    committed. The host is infrastructure -- publishing which endpoint an
    organisation calls is exactly what keeping it out of source was for, and a
    failed run should not put it back.
    """
    return _HOST_RE.sub(r"\1'<endpoint>'", _URL_RE.sub("<endpoint>", message))


def _checkpoint(out_dir, result: "SynthesisResult") -> None:
    """Write what the run has produced so far.

    A pass can fail after several minutes of work, and until now that work went
    in the bin: the run directory was only written on success, so a timeout in
    the seed pass discarded a hypothesis that had already cost a model call.
    """
    if out_dir is None:
        return
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if result.hypothesis is not None:
        (out_dir / "hypothesis.json").write_text(result.hypothesis.model_dump_json(indent=2))
    if result.draft is not None:
        (out_dir / "ir.synthesis.draft.json").write_text(result.draft.model_dump_json(indent=2))
    (out_dir / "synthesis_report.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False))


def _extension_batches(trace: EvidenceTrace, seed_slugs, size: int) -> list[list[str]]:
    """The recordings outside the seed, cut into fixed-size rounds.

    Corpus order, so the schedule is reproducible; the remainder forms a
    shorter final batch rather than being dropped, because a recording that
    never reaches the model is a check the automation will never perform.
    """
    if size < 1:
        raise ValueError(f"extend_batch must be at least 1, got {size}")
    rest = [s for s in trace.slugs if s not in set(seed_slugs)]
    return [rest[i:i + size] for i in range(0, len(rest), size)]


@dataclass
class PassResult:
    name: str
    success: bool
    document: object | None
    attempts: list[dict] = field(default_factory=list)


def _ask(client: LLMClient, messages: list[dict], model_cls, *, name: str,
         context: dict | None = None, max_attempts: int = 3,
         extra_check=None) -> PassResult:
    attempts: list[dict] = []
    for n in range(1, max_attempts + 1):
        try:
            raw = client.complete(messages)
        except Exception as exc:  # transport, timeout, provider error
            # Reaching the model is not something a corrected prompt fixes, so
            # this is recorded and the pass gives up rather than spending the
            # remaining attempts on a request that will fail the same way. It
            # is recorded rather than raised because everything already earned
            # in this run -- the hypothesis, any earlier round -- is still
            # worth writing to disk.
            detail = _redact_endpoint(f"{type(exc).__name__}: {exc}")
            attempts.append({"attempt": n, "transportError": detail,
                             "errors": [{"loc": ["<transport>"], "msg": detail}]})
            return PassResult(name, False, None, attempts)
        record: dict = {"attempt": n, "rawReply": raw}
        attempts.append(record)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            record["parseError"] = str(exc)
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"That was not valid JSON ({exc}). Return ONLY the JSON."}]
            continue

        try:
            doc = model_cls.model_validate(payload, context=context or {})
        except ValidationError as exc:
            errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
            record["errors"] = errors
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": prompts.format_errors(errors)}]
            continue

        if extra_check is not None:
            problems = extra_check(doc, record)
            if problems:
                record["errors"] = problems
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": prompts.format_errors(problems)}]
                continue

        record["accepted"] = True
        return PassResult(name, True, doc, attempts)
    return PassResult(name, False, None, attempts)


# ==========================================================================
# Orchestration
# ==========================================================================

@dataclass
class SynthesisResult:
    success: bool
    hypothesis: ProcessHypothesis | None
    draft: SynthesizedIR | None
    ir: SynthesizedIR | None
    seed_slugs: list[str]
    passes: list[PassResult] = field(default_factory=list)
    replay_rows: list[ReplayRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: The same model rendered in the canonical `ir/schema.json` shape, so the
    #: existing validator and Robot code generator can consume it unchanged.
    canonical: dict | None = None
    validation: dict | None = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "seed": self.seed_slugs,
            "warnings": self.warnings,
            "passes": [{"name": p.name, "success": p.success,
                        "attempts": p.attempts} for p in self.passes],
            "hypothesis": (json.loads(self.hypothesis.model_dump_json())
                           if self.hypothesis else None),
            "draftIR": json.loads(self.draft.model_dump_json()) if self.draft else None,
            "ir": json.loads(self.ir.model_dump_json()) if self.ir else None,
            "validation": self.validation,
            "canonicalIR": self.canonical,
            "replay": [{"slug": r.slug, "expected": r.expected, "actual": r.actual,
                        "passed": r.passed, "via": r.via,
                        "rules": [{"ruleId": x.rule_id, "status": x.status,
                                   "detail": x.detail} for x in r.rules]}
                       for r in self.replay_rows],
        }


def synthesize(recordings_dir, client: LLMClient, *, seed_size: int = 3,
               extend_batch: int = 2, max_attempts: int = 3,
               out_dir: Path | None = None, validate=None,
               on_event=print) -> SynthesisResult:
    trace = EvidenceTrace.load_dir(recordings_dir)
    if not trace:
        raise ValueError(f"no recordings in {recordings_dir}")

    warnings: list[str] = []
    result = SynthesisResult(False, None, None, None, [], [], warnings)

    # -- pass 1: narrations only -----------------------------------------
    on_event(f"[1/3] Narrations -> hypothesis      ({len(trace)} narrations)")
    p1 = _ask(client, prompts.narration_messages(trace), ProcessHypothesis,
              name="hypothesis", max_attempts=max_attempts)
    result.passes.append(p1)
    if not p1.success:
        _checkpoint(out_dir, result)
        return result
    hypothesis: ProcessHypothesis = p1.document
    result.hypothesis = hypothesis
    _checkpoint(out_dir, result)
    on_event(f"      entities={len(hypothesis.entities)} rules={len(hypothesis.rules)} "
             f"tasks={len(hypothesis.tasks)} uncertainties={len(hypothesis.uncertainties)} "
             f"(attempts={len(p1.attempts)})")
    # -- pass 2: hypothesis + seed recordings -> IR ----------------------
    seed_slugs = select_seed(trace, seed_size)
    result.seed_slugs = seed_slugs
    seed = trace.subset(seed_slugs)
    on_event(f"[2/3] Hypothesis + {len(seed)} recordings -> draft IR")
    on_event(f"      seed: {', '.join(seed_slugs)}")

    def _seed_replay(doc: SynthesizedIR, record: dict):
        rows = replay(doc, seed)
        record["replay"] = [{"slug": r.slug, "passed": r.passed, "via": r.via} for r in rows]
        return format_failures(rows)

    p2 = _ask(client, prompts.seed_messages(hypothesis, seed), SynthesizedIR,
              name="seed", context={"trace": seed}, max_attempts=max_attempts,
              extra_check=_seed_replay)
    result.passes.append(p2)
    if not p2.success:
        _checkpoint(out_dir, result)
        return result
    draft: SynthesizedIR = p2.document
    result.draft = draft
    _checkpoint(out_dir, result)
    on_event(f"      entities={len(draft.entities)} rules={len(draft.rules)} "
             f"tasks={len(draft.tasks)} derivations={len(draft.derivations)} "
             f"(attempts={len(p2.attempts)})")

    # -- pass 3: extend, one batch at a time, until the corpus is exhausted --
    #
    # A single call carrying every remaining recording works while a corpus is
    # small and stops working the moment it is not: the prompt grows without
    # bound, and one rejected attempt discards what several recordings had
    # already contributed. Looping instead means each batch is validated,
    # replayed and regression-checked on its own, and the IR that enters the
    # next round is one that has already reproduced everything seen so far.
    batches = _extension_batches(trace, seed_slugs, extend_batch)
    current: SynthesizedIR = draft
    seen = list(seed_slugs)

    if batches:
        remaining = sum(len(b) for b in batches)
        on_event(f"[3/3] Extend  {remaining} recording(s) in "
                 f"{len(batches)} batch(es) of up to {extend_batch}")

        for n, batch in enumerate(batches, start=1):
            batch_trace = trace.subset(batch)
            # Everything the IR is meant to explain by the end of this round --
            # the batch just added plus everything already absorbed.
            covered = trace.subset(seen + batch)
            previous = current
            on_event(f"      [{n}/{len(batches)}] + {', '.join(batch)}")

            def _check(doc: SynthesizedIR, record: dict, _covered=covered,
                       _previous=previous):
                rows = replay(doc, _covered)
                record["replay"] = [{"slug": r.slug, "passed": r.passed, "via": r.via}
                                    for r in rows]
                return format_failures(rows) + _regressions(_previous, doc)

            # Validated against everything seen so far, not just the new batch:
            # its whole job is to stay true to evidence it can no longer see
            # while absorbing evidence it can.
            step = _ask(client,
                        prompts.extend_messages(previous, hypothesis, batch_trace),
                        SynthesizedIR, name=f"extend[{n}]",
                        context={"trace": covered}, max_attempts=max_attempts,
                        extra_check=_check)
            result.passes.append(step)
            if not step.success:
                _checkpoint(out_dir, result)
                return result

            current = step.document
            seen += batch
            on_event(f"            entities={len(current.entities)} "
                     f"rules={len(current.rules)} tasks={len(current.tasks)} "
                     f"derivations={len(current.derivations)} "
                     f"(attempts={len(step.attempts)})")

    result.ir = current
    result.replay_rows = replay(current, trace)
    result.success = True
    final = current

    # The synthesis shape is for the model to fill in; the canonical shape is
    # what the rest of the repo compiles. Rendering it here means this route
    # is what the Robot code generator consumes.
    result.canonical = emit(final, trace)
    if validate is not None:
        result.validation = validate(result.canonical)
        if not result.validation.get("valid"):
            errs = (result.validation.get("schemaErrors", [])
                    + result.validation.get("semanticErrors", [])
                    + result.validation.get("groundingErrors", []))
            on_event(f"      canonical IR FAILED validation ({len(errs)} error(s))")
            for e in errs[:8]:
                on_event(f"        {e.get('path', '')}: {e.get('message', '')}")

    if out_dir:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "hypothesis.json").write_text(hypothesis.model_dump_json(indent=2))
        (out_dir / "ir.synthesis.draft.json").write_text(draft.model_dump_json(indent=2))
        (out_dir / "ir.synthesis.json").write_text(final.model_dump_json(indent=2))
        (out_dir / "ir.json").write_text(
            json.dumps(result.canonical, indent=2, ensure_ascii=False))
        (out_dir / "synthesis_report.json").write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return result


def _regressions(draft: SynthesizedIR, extended: SynthesizedIR) -> list[dict]:
    """Things the draft had grounded that the extension dropped.

    The extension pass cannot see the seed recordings, so it cannot legitimately
    decide that something they demonstrated was wrong -- it can only forget it.
    Silent forgetting is the specific failure mode of any incremental build, and
    it is invisible in the output: a smaller IR looks like a tidier one.
    """
    problems: list[dict] = []

    lost_rules = ({r.rule_id for r in draft.rules} - {r.rule_id for r in extended.rules})
    if lost_rules:
        problems.append({"loc": ["rules"], "msg": (
            f"rule(s) {sorted(lost_rules)} were grounded in the seed recordings and have "
            f"disappeared. You cannot see those recordings, so you cannot have disproved "
            f"them. Restore them, or -- if a new recording genuinely contradicts one -- keep "
            f"it and record the contradiction in open_questions.")})

    lost_fields = ({f"{e.entity}.{f.field}" for e in draft.entities for f in e.fields}
                   - {f"{e.entity}.{f.field}" for e in extended.entities for f in e.fields})
    if lost_fields:
        problems.append({"loc": ["entities"], "msg": (
            f"field(s) {sorted(lost_fields)} were in the draft and are gone. Restore them.")})

    lost_demos = set(draft.covered_demonstrations) - set(extended.covered_demonstrations)
    if lost_demos:
        problems.append({"loc": ["covered_demonstrations"], "msg": (
            f"covered_demonstrations must still include the seed recordings "
            f"{sorted(lost_demos)}; this IR is built from them too.")})
    return problems
