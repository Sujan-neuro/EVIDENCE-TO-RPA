"""Does this IR actually reproduce the demonstrations it claims to model?

Schema validity and evidence grounding both answer "is this well formed and
honestly sourced". Neither answers "is it right". A process model can cite
every event correctly, use coherent units, and still route an invoice to the
wrong department -- and that failure looks like success to every other check in
this pipeline, because the document is immaculate.

So: run the cascade against each recording using only values that recording
actually contains, and compare the verdict to the one the SME wrote down.

The oracle knows no domain vocabulary. It learns which sheet and column carry
which field from the IR under test, which is the honest arrangement: the IR is
the hypothesis, and a hypothesis that names the wrong column simply fails to
reproduce the evidence.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Operand, SynthesizedIR
from .trace import READ, SEARCH, WRITE, EvidenceTrace, Recording

_OPS = {
    "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


class Unresolved(Exception):
    """This recording does not contain what the rule needs. Not a failure of
    the rule -- a rule the demonstration never exercised."""


def _number(value) -> float:
    return float(str(value).strip().replace(",", "").rstrip("%"))


def placement(ir: SynthesizedIR) -> dict[tuple[str, str], str]:
    """(sheet, column) -> `Entity.field`, as the IR under test declares it."""
    return {(e.sheet, f.column): f"{e.entity}.{f.field}"
            for e in ir.entities for f in e.fields}


def key_by_sheet(ir: SynthesizedIR) -> dict[str, str]:
    """sheet -> the `Entity.field` a search on that sheet establishes."""
    return {e.sheet: f"{e.entity}.{e.key_field}" for e in ir.entities}


def field_values(ir: SynthesizedIR, rec: Recording) -> dict[str, object]:
    """`Entity.field` -> the last value this recording read or wrote for it.

    A SEARCH counts: it establishes the business key of whatever sheet it was
    typed into, and that key is never read out of a cell afterwards -- nobody
    looks up the identifier they just used to find the row.
    """
    cells = placement(ir)
    keys = key_by_sheet(ir)
    out: dict[str, object] = {}
    for ev in rec.events:
        action, sheet = ev.get("action"), ev.get("sheet")
        if action == SEARCH and sheet in keys:
            out[keys[sheet]] = ev.get("value")
        elif action in (READ, WRITE):
            ref = cells.get((sheet, ev.get("column")))
            if ref:
                out[ref] = ev.get("value")
    return out


def entity_found(ir: SynthesizedIR, rec: Recording, entity: str) -> bool:
    """Was a lookup for this entity successful in this recording?

    A search on the entity's sheet followed by no read of that sheet is a
    search that found nothing -- which is exactly what a missing row looks like
    in a trace, and the only way it can look, since "no match" produces no
    event of its own.
    """
    sheets = {e.sheet for e in ir.entities if e.entity == entity}
    searched = False
    for ev in rec.events:
        if ev.get("sheet") not in sheets:
            continue
        if ev.get("action") == SEARCH:
            searched = True
        elif ev.get("action") == READ and searched:
            return True
    return not searched  # never looked it up -> not this recording's concern


def _derive(name: str, ir: SynthesizedIR, values: dict,
            seen: frozenset[str] = frozenset()) -> float:
    """Evaluate a derived quantity, including ones built on other derivations.

    A derivation whose operands are themselves derived is not exotic -- the
    total overcharge on an order is the per-unit price difference times the
    quantity, and the per-unit difference is itself derived. An evaluator that
    only resolves raw fields silently reports such a rule as "not exercised by
    this recording", which reads like an honest gap and is in fact a blind
    spot: the rule never fires, and the replay passes for the wrong reason.
    """
    if name in seen:
        raise Unresolved(f"{name} is defined in terms of itself")
    derivation = next((d for d in ir.derivations if d.name == name), None)
    if derivation is None:
        raise Unresolved(f"no derivation named {name!r}")

    def value_of(ref: str) -> float:
        if ref in values:
            try:
                return _number(values[ref])
            except ValueError as exc:
                raise Unresolved(f"{ref} read as {values[ref]!r}, which is not a number") from exc
        if any(d.name == ref for d in ir.derivations):
            return _derive(ref, ir, values, seen | {name})
        raise Unresolved(f"{ref} (needed by {name}) was not read in this recording")

    operands = [value_of(ref) for ref in derivation.operands]
    fn = derivation.function
    if fn == "PERCENT_DIFFERENCE":
        base = value_of(derivation.baseline)
        if base == 0:
            raise Unresolved(f"{name}: baseline is zero")
        return abs(operands[0] - operands[1]) / abs(base) * 100.0
    if fn == "ABSOLUTE_DIFFERENCE":
        return abs(operands[0] - operands[1])
    if fn == "DIFFERENCE":
        return operands[0] - operands[1]
    if fn == "PRODUCT":
        result = 1.0
        for v in operands:
            result *= v
        return result
    if fn == "SUM":
        return float(sum(operands))
    raise Unresolved(f"unknown function {fn}")


def _resolve(op: Operand, ir: SynthesizedIR, values: dict) -> float | str:
    if op.kind in ("constant", "observed_value"):
        raw = op.value if op.kind == "observed_value" else op.ref
        try:
            return _number(raw)
        except ValueError:
            return str(raw)
    if op.kind == "field":
        if op.ref not in values:
            raise Unresolved(f"{op.ref} was not read in this recording")
        try:
            return _number(values[op.ref])
        except ValueError:
            return str(values[op.ref])
    return _derive(op.ref, ir, values)


@dataclass
class RuleOutcome:
    rule_id: str
    status: str  # "pass" | "fail" | "unevaluated"
    detail: str


@dataclass
class ReplayRow:
    slug: str
    expected: str | None
    actual: str
    passed: bool
    via: str
    rules: list[RuleOutcome]


def evaluate(ir: SynthesizedIR, rec: Recording) -> ReplayRow:
    values = field_values(ir, rec)
    by_id = {r.rule_id: r for r in ir.rules}
    results: list[RuleOutcome] = []
    failures: list[str] = []

    for rule_id in ir.evaluation_order:
        rule = by_id[rule_id]
        if rule.operator == "EXISTS":
            # Declared, not inferred. `Invoice.poNumber EXISTS` means "the
            # purchase order this invoice names can be found", and reading the
            # entity off the left operand would check the invoice's own sheet
            # -- which always succeeds, so the rule could never fire.
            ok = entity_found(ir, rec, rule.subject_entity)
            results.append(RuleOutcome(rule_id, "pass" if ok else "fail",
                                       f"{rule.subject_entity} "
                                       f"{'found' if ok else 'not found'}"))
            if not ok:
                failures.append(rule_id)
            continue
        try:
            left = _resolve(rule.left, ir, values)
            right = _resolve(rule.right, ir, values)
        except Unresolved as exc:
            results.append(RuleOutcome(rule_id, "unevaluated", str(exc)))
            continue
        if isinstance(left, str) != isinstance(right, str):
            results.append(RuleOutcome(rule_id, "unevaluated",
                                       f"cannot compare {left!r} with {right!r}"))
            continue
        ok = _OPS[rule.operator](left, right)
        results.append(RuleOutcome(rule_id, "pass" if ok else "fail",
                                   f"{left} {rule.operator} {right}"))
        if not ok:
            failures.append(rule_id)

    if len(failures) > 1 and ir.multiple_failure_policy == "ABORT":
        actual, via = "ABORT", f"{len(failures)} rules failed: {failures}"
    elif failures:
        actual, via = by_id[failures[0]].failure_outcome, failures[0]
    else:
        actual, via = ir.success_outcome, "<all rules passed>"

    expected = rec.outcome
    return ReplayRow(rec.slug, expected, actual, expected == actual, via, results)


def replay(ir: SynthesizedIR, trace: EvidenceTrace) -> list[ReplayRow]:
    return [evaluate(ir, rec) for rec in trace.recordings]


def summary(rows: list[ReplayRow]) -> str:
    ok = sum(1 for r in rows if r.passed)
    lines = [f"REPLAY AGAINST THE RECORDINGS: {ok}/{len(rows)} reproduce the observed outcome"]
    for row in rows:
        mark = "ok  " if row.passed else "FAIL"
        lines.append(f"  {mark} {row.slug:<32} expected={row.expected or '?':<20} "
                     f"got={row.actual:<20} via {row.via}")
        for r in row.rules:
            if r.status == "unevaluated":
                lines.append(f"       - {r.rule_id} not exercised here ({r.detail})")
    return "\n".join(lines)


def format_failures(rows: list[ReplayRow]) -> list[dict]:
    """Replay failures, phrased as corrections the model can act on."""
    out = []
    for row in rows:
        if row.passed:
            continue
        detail = "; ".join(f"{r.rule_id} {r.status} ({r.detail})" for r in row.rules)
        out.append({
            "loc": ["replay", row.slug],
            "msg": (f"the SME recorded {row.expected!r} for this demonstration, but your "
                    f"cascade produces {row.actual!r} via {row.via}. Rule results were: "
                    f"{detail}. Fix the rule, its operator, its operands, the sheet/column "
                    f"binding it relies on, or the evaluation order, so the model reproduces "
                    f"what was actually done."),
        })
    return out
