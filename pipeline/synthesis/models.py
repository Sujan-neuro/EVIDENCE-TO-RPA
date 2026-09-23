"""The contract for direct IR synthesis: what the model may say, and what the
recordings must agree to before we believe it.

There is no domain vocabulary in this file. No list of entities, no sheet-to-
entity table, no outcome enum, no field-unit map. The model is given the
recordings and works out what exists; every name in the resulting IR is one it
chose. The alternative -- handing it a closed vocabulary -- makes a class of
error impossible, but only for the one application the table was written for,
and it means the pipeline cannot be pointed at a process nobody has hand-
modelled first.

What replaces the vocabulary is cross-examination. Every structural claim
carries the events that support it, and validation asks the trace whether those
events say what the claim says:

    claim     Invoice.unitPrice is column "Unit Price" on sheet "Invoices"
    evidence  THOROUGH_RELEASE:E003
    check     does that event exist, and did it read "Unit Price" on "Invoices"?

That is strictly weaker than a lookup table in one respect -- a consistent but
wrong binding passes -- and stronger in another: it catches a claim that
contradicts the evidence even when the domain is one nobody anticipated. The
provenance rule is the part worth stating plainly, because it is where this
pipeline has been burned before: a model asked for event ids will happily
produce well-formed ones. `E041` looks exactly as real as `E003`. Shape
validation cannot tell them apart -- only resolution against the corpus can,
which is why every `evidence` field here is checked with
`model_validate(..., context={"trace": ...})` and never on its own.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import (BaseModel, ConfigDict, Field, ValidationInfo,
                      field_validator, model_validator)

_REF_RE = re.compile(r"^[A-Z0-9_]+:E\d{3,}$")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]*$")


class Unit(str, Enum):
    """How a value is measured.

    Not domain vocabulary: these are kinds of quantity, the same in any
    business. They exist so that a comparison can be checked for coherence,
    because a spreadsheet shows a price and a percentage as the same thing --
    a number in a cell -- and nothing in the UI stops them being compared.
    """
    TEXT = "text"
    NUMBER = "number"
    CURRENCY = "currency"
    CURRENCY_PER_UNIT = "currency_per_unit"
    QUANTITY = "quantity"
    PERCENT = "percent"
    RATIO = "ratio"
    DATE = "date"
    BOOLEAN = "boolean"


UNITS = tuple(u.value for u in Unit)
#: Units that can be ordered. Comparing text with `<` is a category error, not
#: a preference.
ORDERED = {Unit.NUMBER, Unit.CURRENCY, Unit.CURRENCY_PER_UNIT, Unit.QUANTITY,
           Unit.PERCENT, Unit.RATIO, Unit.DATE}


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ==========================================================================
# 1. The narration pass -- a hypothesis, not yet a claim about the evidence
# ==========================================================================

class CandidateEntity(Base):
    name: str = Field(min_length=2, description="A thing the process reasons about.")
    why: str = Field(min_length=15, description="What the narrator uses it for.")
    likely_fields: list[str] = Field(
        default_factory=list,
        description="Attributes the narration implies, in business words.")


class CandidateOutcome(Base):
    name: str = Field(min_length=2,
                      description="What the narrator calls this verdict, normalised.")
    phrasing: str = Field(min_length=5,
                          description="How the narrator actually said it, verbatim where possible.")
    when: str = Field(min_length=15, description="The condition the narrator gives for it.")


class CandidateRule(Base):
    name: str = Field(min_length=3)
    statement: str = Field(min_length=20, description="The rule in one business sentence.")
    threshold_hint: str | None = Field(
        default=None,
        description="A number or limit the narration mentions. null if it only implies one.")
    #: Narrations describe outcomes far more reliably than they describe
    #: thresholds, so this is the part of a candidate rule worth trusting.
    failure_outcome: str | None = None


class CandidateTask(Base):
    name: str = Field(min_length=3)
    description: str = Field(min_length=20,
                             description="What this step achieves, in business terms -- not which "
                                         "button was pressed.")
    entity: str = Field(min_length=2)


class ProcessHypothesis(Base):
    """What the narrations, read together, say the process is."""
    process_intent: str = Field(min_length=40)
    entities: list[CandidateEntity] = Field(min_length=1)
    outcomes: list[CandidateOutcome] = Field(min_length=1)
    rules: list[CandidateRule] = Field(default_factory=list)
    tasks: list[CandidateTask] = Field(min_length=1)
    uncertainties: list[str] = Field(
        default_factory=list,
        description="What the narrations do NOT settle. Being explicit here is more useful "
                    "than a confident guess, because the next pass can go looking.")

    @model_validator(mode="after")
    def _distinct(self) -> "ProcessHypothesis":
        names = [e.name for e in self.entities]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate entity name(s): {dupes}")
        return self


# ==========================================================================
# 2. The IR itself -- every claim carries evidence
# ==========================================================================

class Evidenced(Base):
    """Anything that must be able to point at the events that support it."""
    evidence: list[str] = Field(
        min_length=1,
        description="Qualified event references, e.g. 'PRICE_EXCESS:E003'. At least one. "
                    "A bare 'E003' is not a reference: event ids restart in every recording.")

    @field_validator("evidence")
    @classmethod
    def _well_formed(cls, refs: list[str]) -> list[str]:
        bad = [r for r in refs if not _REF_RE.match(r)]
        if bad:
            raise ValueError(
                f"malformed event reference(s): {bad}. The form is SLUG:EVENT_ID, "
                f"e.g. 'QUANTITY_MISMATCH:E012'.")
        return refs


class IRField(Evidenced):
    field: str = Field(pattern=_NAME_RE.pattern,
                       description="Your name for this attribute, in business terms.")
    column: str = Field(min_length=1,
                        description="The column header exactly as the recordings show it.")
    unit: Literal[UNITS]


class IREntity(Evidenced):
    entity: str = Field(pattern=_NAME_RE.pattern,
                        description="Your name for this kind of thing.")
    sheet: str = Field(min_length=1, description="The sheet it lives on, exactly as recorded.")
    key_field: str = Field(min_length=1,
                           description="The field identifying one of these. It is the value "
                                       "searched for to find the row, so it usually has no "
                                       "read of its own -- declare it as a field anyway.")
    fields: list[IRField] = Field(min_length=1)

    @model_validator(mode="after")
    def _key_declared(self) -> "IREntity":
        names = [f.field for f in self.fields]
        dupes = sorted({n for n in names if names.count(n) > 1})
        problems = []
        if dupes:
            problems.append(f"duplicate field name(s): {dupes}")
        if self.key_field not in names:
            problems.append(
                f"key_field {self.key_field!r} is not among its fields {names}. Add it as a "
                f"field (citing the SEARCH that looks a row up by it) or name one of the "
                f"fields you already declared.")
        if problems:
            raise ValueError(f"entity {self.entity}: " + "; ".join(problems))
        return self


class Derivation(Evidenced):
    """A quantity the SME worked out rather than read.

    Separate from a field because the unit usually changes: a percentage
    variance derived from two prices is a percent, and comparing it against a
    price is the single most plausible wrong comparison a spreadsheet invites.
    """
    name: str = Field(pattern=_NAME_RE.pattern)
    function: Literal["PERCENT_DIFFERENCE", "ABSOLUTE_DIFFERENCE", "DIFFERENCE",
                      "PRODUCT", "SUM"]
    operands: list[str] = Field(min_length=1,
                                description="Field refs ('Invoice.unitPrice') or the names of "
                                            "other derivations.")
    unit: Literal[UNITS]
    baseline: str | None = Field(
        default=None,
        description="For PERCENT_DIFFERENCE: which operand is the 100% denominator. "
                    "Required for it -- 'a 2% difference' is meaningless without it.")

    @model_validator(mode="after")
    def _percent_needs_baseline(self) -> "Derivation":
        if self.function == "PERCENT_DIFFERENCE":
            if self.unit != Unit.PERCENT.value:
                raise ValueError(f"{self.name}: PERCENT_DIFFERENCE yields percent, not {self.unit}")
            if not self.baseline:
                raise ValueError(f"{self.name}: PERCENT_DIFFERENCE needs a baseline operand")
            if self.baseline not in self.operands:
                raise ValueError(f"{self.name}: baseline {self.baseline!r} is not one of "
                                 f"its operands {self.operands}")
        return self


class Operand(Base):
    kind: Literal["field", "derived", "observed_value", "constant"]
    ref: str = Field(min_length=1,
                     description="field -> 'Invoice.unitPrice'; derived -> a derivation name; "
                                 "observed_value -> what the thing read is called; "
                                 "constant -> the literal itself.")
    unit: Literal[UNITS]
    #: For `observed_value`: the value as it appeared on screen, and the events
    #: that read it. A limit somebody looked up is evidence, not a magic number,
    #: and citing the read is what keeps it that way.
    value: str | None = None
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _observed_is_cited(self) -> "Operand":
        if self.kind == "observed_value":
            if not self.value:
                raise ValueError(f"observed_value {self.ref!r} must carry the value that was read")
            if not self.evidence:
                raise ValueError(
                    f"observed_value {self.ref!r} must cite the event(s) that read it -- "
                    f"that citation is the whole difference between it and a constant")
        elif self.evidence or self.value:
            raise ValueError(f"only observed_value carries `value`/`evidence` "
                             f"(got kind={self.kind!r})")
        return self


class IROutcome(Evidenced):
    """One verdict the process can reach."""
    name: str = Field(min_length=2, description="Exactly as it is written into the sheet.")
    meaning: str = Field(min_length=15, description="What it means for the business.")


class IRRule(Evidenced):
    rule_id: str = Field(pattern=r"^RULE-[A-Z0-9-]+$")
    name: str = Field(min_length=3)
    statement: str = Field(min_length=20, description="The rule in business language.")
    left: Operand
    #: ALWAYS stated in the PASSING direction: true means satisfied, carry on to
    #: the next rule. Stating every rule the same way round removes the
    #: perennial ambiguity of which branch is the failure one.
    operator: Literal["<=", "<", ">", ">=", "==", "!=", "EXISTS"]
    right: Operand | None = Field(default=None, description="Omitted only for EXISTS.")
    #: For EXISTS only. `Invoice.poNumber EXISTS` is ambiguous on its face: it
    #: could mean the invoice has a PO number, or that the purchase order it
    #: names can be found. Those check different sheets and route differently,
    #: so the entity being looked up is stated rather than inferred from the
    #: operand -- a guess here silently validates against the wrong recording.
    subject_entity: str | None = Field(
        default=None,
        description="Required for EXISTS: the entity whose existence is being checked.")
    true_means: str = Field(min_length=15, description="What PASSING means.")
    failure_outcome: str = Field(min_length=2)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _comparable(self) -> "IRRule":
        if self.operator == "EXISTS":
            problems = []
            if self.right is not None:
                problems.append("EXISTS takes no right operand")
            if not self.subject_entity:
                problems.append("EXISTS needs subject_entity: which entity must be findable")
            if problems:
                raise ValueError(f"{self.rule_id}: " + "; ".join(problems))
            return self
        if self.subject_entity:
            raise ValueError(f"{self.rule_id}: subject_entity applies only to EXISTS")
        if self.right is None:
            raise ValueError(f"{self.rule_id}: operator {self.operator!r} needs a right operand")
        if self.left.unit != self.right.unit:
            raise ValueError(
                f"{self.rule_id}: cannot compare {self.left.unit} against {self.right.unit} "
                f"({self.left.ref} {self.operator} {self.right.ref}). If one side is derived, "
                f"declare the derivation and compare like with like.")
        if Unit(self.left.unit) not in ORDERED and self.operator not in ("==", "!="):
            raise ValueError(
                f"{self.rule_id}: {self.operator!r} is not meaningful on {self.left.unit} values")
        return self


class IRTask(Evidenced):
    """One step of the process, named for what it achieves."""
    task_id: str = Field(pattern=r"^TASK-[A-Z0-9-]+$")
    name: str = Field(min_length=3)
    intent: str = Field(min_length=20,
                        description="Business language only. 'Establish the ordered price for "
                                    "this invoice', not 'click the Purchase Orders tab'.")
    entity: str = Field(min_length=2)
    kind: Literal["LOOKUP", "READ", "EVALUATE", "WRITE"]
    inputs: list[str] = Field(default_factory=list,
                              description="Field or derivation refs this step needs.")
    outputs: list[str] = Field(default_factory=list,
                               description="Field or derivation refs this step establishes.")


class SynthesizedIR(Evidenced):
    """The process model, as the LLM composes it from hypothesis + recordings."""
    process_intent: str = Field(min_length=40)
    entities: list[IREntity] = Field(min_length=1)
    derivations: list[Derivation] = Field(default_factory=list)
    outcomes: list[IROutcome] = Field(min_length=2)
    tasks: list[IRTask] = Field(min_length=1)
    rules: list[IRRule] = Field(min_length=1)
    #: rule_ids, highest priority first.
    evaluation_order: list[str] = Field(min_length=1)
    success_outcome: str = Field(min_length=2,
                                 description="Where it goes when every rule passes.")
    #: Where an invoice goes when the model cannot decide. Never a silent success.
    default_outcome: str = Field(min_length=2)
    #: What to do when MORE THAN ONE rule fails at once. No recording shows it,
    #: so choosing a destination would be inventing a business rule and hiding
    #: it inside a cascade order. ABORT stops and reports; set
    #: ROUTE_BY_PRIORITY only once a recording demonstrates the case.
    multiple_failure_policy: Literal["ABORT", "ROUTE_BY_PRIORITY"] = "ABORT"
    covered_demonstrations: list[str] = Field(
        min_length=1, description="Recording slugs this IR was built from, e.g. 'PRICE_EXCESS'.")
    open_questions: list[str] = Field(
        default_factory=list,
        description="What a reviewer must confirm with the SME before trusting this.")

    # -- internal coherence, independent of the evidence -------------------
    @model_validator(mode="after")
    def _coherent(self) -> "SynthesizedIR":
        """Collect EVERY problem, not just the first.

        These constraints are coupled: renaming a rule to satisfy one breaks
        another. Raising on the first means each retry teaches the model a
        single rule and reveals the next, so a document three fixes away needs
        three round trips -- and with three attempts allowed, it never lands.
        """
        problems: list[str] = []

        ids = [r.rule_id for r in self.rules]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            problems.append(f"duplicate rule_id(s): {dupes}")
        tids = [t.task_id for t in self.tasks]
        tdupes = sorted({i for i in tids if tids.count(i) > 1})
        if tdupes:
            problems.append(f"duplicate task_id(s): {tdupes}")

        unknown = [r for r in self.evaluation_order if r not in ids]
        if unknown:
            hint = ""
            if any(u.startswith("TASK-") for u in unknown):
                hint = (" NOTE: those are task_ids. evaluation_order is the order the RULES are "
                        "checked in, not the order the steps are performed.")
            problems.append(f"evaluation_order names unknown rule_id(s): {unknown}. "
                            f"The rules you declared are: {ids}.{hint}")
        missing = [r for r in ids if r not in self.evaluation_order]
        if missing:
            problems.append(
                f"every rule must appear in evaluation_order; missing: {missing}. "
                f"An unordered rule has no defined place in the cascade.")

        entity_names = {e.entity for e in self.entities}
        known_fields = {f"{e.entity}.{f.field}" for e in self.entities for f in e.fields}
        known_derived = {d.name for d in self.derivations}
        outcome_names = {o.name for o in self.outcomes}

        for d in self.derivations:
            bad = [o for o in d.operands if o not in known_fields and o not in known_derived]
            if bad:
                problems.append(
                    f"derivation {d.name!r} uses undeclared operand(s): {bad}. "
                    f"Use exactly one of: {sorted(known_fields | (known_derived - {d.name}))}")

        for rule in self.rules:
            for side, op in (("left", rule.left), ("right", rule.right)):
                if op is None:
                    continue
                if op.kind == "field" and op.ref not in known_fields:
                    problems.append(f"{rule.rule_id}.{side}: {op.ref!r} is not a declared field "
                                    f"of any declared entity. Declared: {sorted(known_fields)}")
                if op.kind == "derived" and op.ref not in known_derived:
                    problems.append(f"{rule.rule_id}.{side}: {op.ref!r} is not a declared "
                                    f"derivation. Declared: {sorted(known_derived)}")
            if rule.subject_entity and rule.subject_entity not in entity_names:
                problems.append(f"{rule.rule_id}: subject_entity {rule.subject_entity!r} is not "
                                f"a declared entity ({sorted(entity_names)})")
            if rule.failure_outcome not in outcome_names:
                problems.append(f"{rule.rule_id}: failure_outcome {rule.failure_outcome!r} is "
                                f"not a declared outcome ({sorted(outcome_names)})")

        available = sorted(known_fields | known_derived)
        for task in self.tasks:
            bad = [r for r in (task.inputs + task.outputs)
                   if r not in known_fields and r not in known_derived]
            if bad:
                problems.append(
                    f"{task.task_id}: undeclared field/derivation ref(s): {bad}. "
                    f"Use exactly one of: {available}")
            if task.entity not in entity_names:
                problems.append(f"{task.task_id}: entity {task.entity!r} is not declared")

        for label, value in (("success_outcome", self.success_outcome),
                             ("default_outcome", self.default_outcome)):
            if value not in outcome_names:
                problems.append(f"{label} {value!r} is not a declared outcome "
                                f"({sorted(outcome_names)})")
        if self.success_outcome == self.default_outcome:
            problems.append(
                f"success_outcome and default_outcome are both {self.success_outcome!r}; "
                f"an invoice the process could not decide would be indistinguishable from "
                f"one it deliberately released.")

        if problems:
            raise ValueError("; ".join(problems))
        return self

    # -- grounding: does the evidence actually say this? -------------------
    @model_validator(mode="after")
    def _grounded(self, info: ValidationInfo) -> "SynthesizedIR":
        trace = (info.context or {}).get("trace")
        if trace is None:
            return self

        problems: list[str] = []

        def cited(label: str, node) -> list[dict]:
            """Resolve a node's references, reporting any that do not exist."""
            unknown = trace.unknown_refs(node.evidence)
            if unknown:
                problems.append(f"{label}: cites event(s) that do not exist in the supplied "
                                f"recordings: {unknown}")
            return [trace.event(r) for r in node.evidence if trace.has(r)]

        cited("<root>", self)

        sheets = trace.sheets
        columns = trace.columns()

        for entity in self.entities:
            label = f"entity {entity.entity}"
            cited(label, entity)
            if entity.sheet not in sheets:
                problems.append(f"{label}: no recording visits a sheet called {entity.sheet!r}. "
                                f"Sheets in the corpus: {sorted(sheets)}")
                continue
            for f in entity.fields:
                flabel = f"{entity.entity}.{f.field}"
                events = cited(flabel, f)
                if f.field == entity.key_field and not any(e.get("column") for e in events):
                    continue  # a key is searched for, not read out of a cell
                if f.column not in columns.get(entity.sheet, set()):
                    problems.append(
                        f"{flabel}: no recording reads a column called {f.column!r} on sheet "
                        f"{entity.sheet!r}. Columns observed there: "
                        f"{sorted(columns.get(entity.sheet, ()))}")
                    continue
                # The claim names a sheet and a column; the evidence must show
                # somebody touching THAT cell. This is what replaces a
                # hand-written column-to-field table.
                wrong = [e for e in events if e.get("column")
                         and (e.get("sheet") != entity.sheet or e.get("column") != f.column)]
                if wrong and len(wrong) == len(events):
                    seen = sorted({f"{e.get('sheet')}.{e.get('column')}" for e in wrong})
                    problems.append(
                        f"{flabel}: claimed as {entity.sheet!r}.{f.column!r}, but every event "
                        f"cited for it reads {seen}. Cite the events that read the column you "
                        f"are describing.")

        for d in self.derivations:
            cited(f"derivation {d.name}", d)
        for t in self.tasks:
            cited(t.task_id, t)
        for r in self.rules:
            cited(r.rule_id, r)

        # An outcome is a value the process writes. One that was demonstrated
        # must cite the write; one that was not is an inference and says so.
        written = set(trace.observed_outcomes().values())
        for outcome in self.outcomes:
            events = cited(f"outcome {outcome.name}", outcome)
            if outcome.name in written and not any(
                    str(e.get("value")) == outcome.name for e in events):
                problems.append(
                    f"outcome {outcome.name!r} was written in the recordings, but none of the "
                    f"events cited for it is that write. Cite the event that wrote it.")

        # A threshold read off the screen must be cited, not retyped.
        values_read = trace.values_read()
        for rule in self.rules:
            for side, op in (("left", rule.left), ("right", rule.right)):
                if op is None:
                    continue
                if op.kind == "observed_value":
                    unknown = trace.unknown_refs(op.evidence)
                    if unknown:
                        problems.append(f"{rule.rule_id}.{side}: cites event(s) that do not "
                                        f"exist: {unknown}")
                        continue
                    actual = [str(trace.event(r).get("value")) for r in op.evidence]
                    if not any(_same_value(op.value, a) for a in actual):
                        problems.append(
                            f"{rule.rule_id}.{side}: you say {op.ref!r} reads {op.value!r}, but "
                            f"the events you cite read {actual}. Cite the read of the value you "
                            f"are using.")
                elif op.kind == "constant":
                    match = next((v for v in values_read if _same_value(op.ref, v)), None)
                    if match is not None:
                        problems.append(
                            f"{rule.rule_id}.{side}: {op.ref!r} is a value the recordings "
                            f"actually read ({values_read[match][:2]}). Declare it as an "
                            f"observed_value citing that read, so the automation looks the "
                            f"limit up instead of freezing the one true on the day of the "
                            f"recording.")

        known_slugs = set(trace.slugs)
        stray = [s for s in self.covered_demonstrations if s not in known_slugs]
        if stray:
            problems.append(f"covered_demonstrations names recordings that were not supplied: "
                            f"{stray}. Available: {sorted(known_slugs)}")

        if problems:
            raise ValueError("; ".join(problems))
        return self


def _same_value(a, b) -> bool:
    """Equal as numbers where both look numeric, else as trimmed text."""
    sa, sb = str(a).strip(), str(b).strip()
    try:
        return float(sa.replace(",", "").rstrip("%")) == float(sb.replace(",", "").rstrip("%"))
    except ValueError:
        return sa.casefold() == sb.casefold()
