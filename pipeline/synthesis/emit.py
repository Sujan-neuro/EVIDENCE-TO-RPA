"""SynthesizedIR -> the canonical IR document.

The synthesis models are shaped for a language model to fill in: flat, named
by business concept, every node carrying its own evidence. The canonical IR
in `ir/schema.json` is shaped for a compiler: everything referenced by id,
variables separated from the tasks that produce them, provenance normalised.

Keeping them apart is deliberate. Asking a model to emit the compiler's shape
directly means asking it to invent `VAR-INVOICE-UNIT-PRICE` consistently in
nine places, and a single typo becomes a dangling reference rather than a
wrong sentence. Every id below is DERIVED here, by code, from names the model
chose, so ids cannot drift.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import IRRule, SynthesizedIR
from .trace import EvidenceTrace

@dataclass(frozen=True)
class _ThresholdRead:
    """One limit, read from the row that names it."""
    name: str
    entity: object
    sheet: str
    column: str


@dataclass(frozen=True)
class _RowRead:
    """One reference-table read, pinned to the row it came from."""
    task: object
    entity: object
    field: object
    row_key: str | None


IR_VERSION = "1.0.0"
#: Which of the synthesis units are numbers, for the canonical IR's dataType.
#: Not domain knowledge -- a statement about arithmetic.
NUMERIC_UNITS = {"number", "currency", "currency_per_unit", "quantity", "percent", "ratio"}
FUNCTION_MAP = {"PERCENT_DIFFERENCE": "ABS_PERCENT_DIFFERENCE",
                "ABSOLUTE_DIFFERENCE": "ABS_DIFFERENCE",
                "DIFFERENCE": "DIFFERENCE", "PRODUCT": "PRODUCT", "SUM": "SUM"}


def _slug(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", text.upper()).strip("-")


def _var_id(entity: str, field: str) -> str:
    return f"VAR-{_slug(entity)}-{_slug(field)}"


def _var_name(*parts: str) -> str:
    """A Robot-safe ${name} built from names the model chose.

    Those names are business words -- "Invoice ID", "Purchase Order" -- and a
    Robot variable carrying a space in it renders, then fails to resolve. The
    words are kept; only the spacing is discarded.
    """
    words: list[str] = []
    for part in parts:
        words += re.findall(r"[A-Za-z0-9]+", str(part))
    if not words:
        return "value"
    head, *rest = words
    return head[0].lower() + head[1:] + "".join(w[0].upper() + w[1:] for w in rest)


def _prov(evidence: list[str], kind: str = "OBSERVED", rationale: str | None = None,
          confidence: float | None = None) -> list[dict]:
    """Provenance from the event references the model supplied.

    Those references were resolved against the recordings during validation,
    so by the time they reach here they are known to name real events. That is
    the only reason it is safe to stamp them OBSERVED: a reference the corpus
    could not confirm never got this far.
    """
    entries = []
    for ref in evidence:
        entry: dict[str, Any] = {"sourceType": kind, "sourceId": ref}
        if kind != "OBSERVED":
            entry["inferredBy"] = "ir-synthesis"
            entry["rationale"] = rationale or "Concluded from the recordings as a whole."
            entry["confidence"] = confidence if confidence is not None else 0.9
        entries.append(entry)
    return entries


def _evidence_relative(source_file: str) -> str:
    """A recording's path as the IR must declare it: relative to `evidence/`.

    The validator resolves `demonstrations[].file` against the evidence root,
    so an absolute path here makes every declared demonstration unreadable --
    and with no readable corpus, provenance grounding degrades to a warning
    and stops being a check at all.
    """
    parts = Path(source_file).parts
    if "evidence" in parts:
        return str(Path(*parts[parts.index("evidence") + 1:]))
    return str(Path(source_file).name)


def emit(ir: SynthesizedIR, trace: EvidenceTrace) -> dict:
    """Render the synthesised model as a canonical IR document."""
    by_slug = {r.slug: r for r in trace.recordings}
    doc: dict[str, Any] = {
        "irVersion": IR_VERSION,
        "sourceDemonstrations": [_evidence_relative(by_slug[s].source_file)
                                 for s in ir.covered_demonstrations if s in by_slug],
        "demonstrations": [{"prefix": s, "file": _evidence_relative(by_slug[s].source_file)}
                           for s in ir.covered_demonstrations if s in by_slug],
        "process": {
            "id": "PROC-INVOICE-REVIEW", "type": "Process", "intent": ir.process_intent,
            "provenance": _prov(ir.evidence, "INFERRED",
                                "Process intent synthesised across all demonstrations; each "
                                "recording shows only one path through it."),
        },
        "entities": [], "entityReferences": [], "variables": [],
        "tasks": [], "computations": [], "rules": [], "decisions": [],
        # The outcome vocabulary the model discovered is carried by the
        # decisions rather than declared separately: `ir/schema.json` has no
        # slot for it, and enumerating outcomes there would be a second place
        # for the same fact to live.
        "openQuestions": list(ir.open_questions),
    }

    # --- entities and their variables -------------------------------------
    for entity in ir.entities:
        name = entity.entity
        doc_fields = [{"name": f.field,
                        "dataType": "number" if f.unit in NUMERIC_UNITS else "string"}
                       for f in entity.fields]
        doc["entities"].append({
            "id": f"ENTITY-{_slug(name)}", "type": "Entity", "name": name,
            "keyField": entity.key_field, "fields": doc_fields,
            "implementationHints": [{"application": "SpreadsheetApp",
                                     "operation": "LOOKUP_ROW",
                                     "sheet": entity.sheet}],
            "provenance": _prov(entity.evidence)})

    # Filled in after the tasks are emitted: splitting a reference-table read
    # changes which task produces a variable, and that is not known until the
    # sequence is built.
    produced_by: dict[str, object] = {}
    for entity in ir.entities:
        for f in entity.fields:
            ref = f"{entity.entity}.{f.field}"
            task = produced_by.get(ref)
            doc["variables"].append({
                "id": _var_id(entity.entity, f.field), "type": "Variable",
                "name": _var_name(entity.entity, f.field),
                "dataType": "number" if f.unit in NUMERIC_UNITS else "string",
                "producedBy": task.task_id if task else "PROCESS_INPUT",
                "provenance": _prov(f.evidence)})

    # --- derivations -> computations + their variables ---------------------
    # Ids for ALL of them first. A derivation may take another derivation as an
    # operand -- a total overcharge is a per-unit difference times a quantity --
    # so resolving operands while still assigning ids drops any reference to one
    # declared later. Dropping an operand does not fail: it yields a binary
    # computation with a single input, which compiles to an expression with a
    # hole in it.
    derived_var = {d.name: f"VAR-{_slug(d.name)}" for d in ir.derivations}

    def _operand_var(ref: str) -> str:
        if ref in derived_var:
            return derived_var[ref]
        return _var_id(*ref.split("."))

    for d in ir.derivations:
        vid, comp_id = derived_var[d.name], f"COMP-{_slug(d.name)}"
        doc["variables"].append({
            "id": vid, "type": "Variable", "name": _var_name(d.name), "dataType": "number",
            "producedBy": comp_id, "provenance": _prov(d.evidence)})
        doc["computations"].append({
            "id": comp_id, "type": "Computation",
            "function": FUNCTION_MAP.get(d.function, d.function),
            "inputs": [_operand_var(o) for o in d.operands],
            "output": vid, "provenance": _prov(d.evidence)})

    # --- tasks -------------------------------------------------------------
    def _ref(token: str) -> str:
        if token in derived_var:
            return derived_var[token]
        return _var_id(*token.split(".")) if "." in token else token

    #: Where each canonical field lives on screen, so a READ_CELL hint can name
    #: the sheet and column the compiler must actually touch.
    placement = {f"{e.entity}.{f.field}": (e.sheet, f.column)
                 for e in ir.entities for f in e.fields}

    sheet_of = {e.entity: e.sheet for e in ir.entities}

    def _hints(task) -> list[dict]:
        sheet = sheet_of.get(task.entity, "")
        if task.kind == "LOOKUP":
            return [{"application": "SpreadsheetApp", "operation": "CHECK_EXISTS",
                     "sheet": sheet}]
        operation = "READ_CELL" if task.kind == "READ" else "WRITE_CELL"
        touched = [r for r in task.outputs if r in placement]
        hints: list[dict] = [{"application": "SpreadsheetApp", "operation": "LOOKUP_ROW",
                              "sheet": sheet}]
        for ref in touched:
            field_sheet, column = placement[ref]
            hint = {"application": "SpreadsheetApp", "operation": operation,
                    "sheet": field_sheet, "field": column}
            if operation == "WRITE_CELL":
                hint["value"] = _ref(ref)
            hints.append(hint)
        return hints

    # --- reference tables: which ROW did a value come from? ------------------
    #
    # A sheet like Processing Rules holds one row per rule, and a field bound to
    # its "Value" column is meaningless without saying which row. The IR has no
    # per-row notion, so the row is recovered from the evidence: the key column
    # read most recently before the value read IS that row's key. Without this
    # the automation searches for a field name instead of a rule name and finds
    # nothing.
    key_column = {e.sheet: next((f.column for f in e.fields if f.field == e.key_field), None)
                  for e in ir.entities}

    def _is_reference_table(entity) -> bool:
        """True when rows are identified by a key READ on the sheet itself,
        rather than by the business key the process carries in from outside."""
        column = key_column.get(entity.sheet)
        if not column:
            return False
        return any(ev.get("action") == "READ_CELL" and ev.get("sheet") == entity.sheet
                   and ev.get("column") == column
                   for rec in trace.recordings for ev in rec.events)

    def _row_key_for(entity, field) -> str | None:
        """The row this field's value was read from, if the evidence agrees."""
        column = key_column.get(entity.sheet)
        if not column:
            return None
        found = set()
        for ref in field.evidence:
            slug, _, event_id = ref.partition(":")
            rec = next((r for r in trace.recordings if r.slug == slug), None)
            if rec is None:
                continue
            key = None
            for ev in rec.events:
                if ev.get("sheet") != entity.sheet:
                    continue
                if ev.get("action") == "READ_CELL" and ev.get("column") == column:
                    key = str(ev.get("value"))
                if ev["eventId"] == event_id:
                    break
            if key:
                found.add(key)
        return found.pop() if len(found) == 1 else None

    # An EVALUATE step is arithmetic, not an interaction: it touches no sheet
    # and presses nothing. The canonical IR already represents it as a
    # Computation, and emitting it as a BusinessTask too would force a UI hint
    # that describes an operation the process never performs.
    ui_tasks = [t for t in ir.tasks if t.kind != "EVALUATE"]

    #: Entities whose existence a rule tests before anything is read from them.
    guarded = {r.subject_entity: r for r in ir.rules if r.operator == "EXISTS"}

    # An existence check has to be REACHED before the decision that tests it,
    # and it belongs immediately before the step that reads the entity it
    # guards -- reading a row before knowing it exists is the thing the check
    # prevents. Emitting it after the other tasks, with no place in the chain,
    # leaves the compiler unable to reach it and the whole script unbuildable.
    entity_by_name = {e.entity: e for e in ir.entities}

    #: Every distinct threshold a rule compares against, keyed by the row it
    #: names. One read each, whatever shape the model gave the reference sheet:
    #: one run declared a field per threshold, another declared a single
    #: `value` field for both, and only the second collapses two rules onto one
    #: variable. The operand knows which row it means; the entity may not.
    thresholds: dict[str, object] = {}
    for rule in ir.rules:
        for op in (rule.left, rule.right):
            if op is not None and op.kind == "observed_value":
                thresholds.setdefault(op.ref, op)

    def _threshold_site(op) -> tuple[str | None, str | None]:
        """The sheet and column this threshold was read from, per its evidence."""
        for ref in op.evidence:
            event = trace.event(ref)
            if event and event.get("column"):
                return event.get("sheet"), event.get("column")
        return None, None

    #: Value columns of a reference table. The model's own read tasks must stop
    #: claiming these: a single task cannot read two rows, and the dedicated
    #: reads above already cover every value a rule actually uses.
    reference_values = {f"{e.entity}.{f.field}"
                        for e in ir.entities if _is_reference_table(e)
                        for f in e.fields if f.field != e.key_field}

    sequence: list[tuple[str, object]] = []
    checked: set[str] = set()
    for task in ui_tasks:
        if task.entity in guarded and task.entity not in checked:
            checked.add(task.entity)
            sequence.append((f"TASK-CHECK-{_slug(task.entity)}", guarded[task.entity]))

        entity = entity_by_name.get(task.entity)
        if task.kind == "READ" and entity is not None and _is_reference_table(entity):
            # Superseded by the per-threshold reads appended below, which name
            # their row. Nothing else this task offers is used.
            continue
        sequence.append((task.task_id, task))

    # The threshold reads go last among the reads, so every value a decision
    # needs exists before the cascade runs.
    write_at = next((i for i, (_id, node) in enumerate(sequence)
                     if getattr(node, "kind", None) == "WRITE"), len(sequence))
    for offset, (name, op) in enumerate(thresholds.items()):
        sheet, column = _threshold_site(op)
        if sheet is None:
            continue
        owner = next((e for e in ir.entities if e.sheet == sheet), None)
        if owner is None:
            continue
        sequence.insert(write_at + offset,
                        (f"TASK-READ-{_slug(name)}", _ThresholdRead(name, owner, sheet, column)))
    # A guarded entity nobody reads still needs its check, or the decision that
    # tests it has nothing to stand on.
    for entity, rule in guarded.items():
        if entity not in checked:
            sequence.insert(0, (f"TASK-CHECK-{_slug(entity)}", rule))

    order = [task_id for task_id, _ in sequence]
    for n, (task_id, node) in enumerate(sequence):
        depends = {"dependsOn": [order[n - 1]]} if n else {}
        if isinstance(node, _ThresholdRead):
            doc["tasks"].append({
                "id": task_id, "type": "BusinessTask",
                "intent": f"Read the {node.name} limit currently in force",
                "entity": f"ENTITY-{_slug(node.entity.entity)}",
                "inputs": [], "outputs": [f"VAR-{_slug(node.name)}"],
                **depends,
                "implementationHints": [
                    {"application": "SpreadsheetApp", "operation": "LOOKUP_ROW",
                     "sheet": node.sheet, "key": node.name},
                    {"application": "SpreadsheetApp", "operation": "READ_CELL",
                     "sheet": node.sheet, "field": node.column}],
                "provenance": _prov(thresholds[node.name].evidence)})
            continue
        if isinstance(node, _RowRead):
            hints = [{"application": "SpreadsheetApp", "operation": "LOOKUP_ROW",
                      "sheet": node.entity.sheet}]
            if node.row_key:
                hints[0]["key"] = node.row_key
            hints.append({"application": "SpreadsheetApp", "operation": "READ_CELL",
                          "sheet": node.entity.sheet, "field": node.field.column})
            doc["tasks"].append({
                "id": task_id, "type": "BusinessTask",
                "intent": f"Read {node.row_key or node.field.field} "
                          f"from {node.entity.entity}",
                "entity": f"ENTITY-{_slug(node.entity.entity)}",
                "inputs": [], "outputs": [_var_id(node.entity.entity, node.field.field)],
                **depends,
                "implementationHints": hints,
                "provenance": _prov(node.field.evidence)})
            continue
        if isinstance(node, IRRule):
            entity = node.subject_entity
            doc["tasks"].append({
                "id": task_id, "type": "BusinessTask",
                "intent": f"Check whether a matching {entity} can be found",
                "entity": f"ENTITY-{_slug(entity)}",
                "inputs": [_ref(node.left.ref)] if node.left.kind == "field" else [],
                "outputs": [f"VAR-{_slug(entity)}-EXISTS"],
                **depends,
                "implementationHints": [{"application": "SpreadsheetApp",
                                         "operation": "CHECK_EXISTS",
                                         "sheet": sheet_of.get(entity, "")}],
                "provenance": _prov(node.evidence)})
            continue
        doc["tasks"].append({
            "id": task_id, "type": "BusinessTask", "intent": node.intent,
            "entity": f"ENTITY-{_slug(node.entity)}",
            "inputs": [_ref(i) for i in node.inputs],
            "outputs": [_ref(o) for o in node.outputs],
            **depends,
            "implementationHints": _hints(node),
            "provenance": _prov(node.evidence)})

    #: `Entity.field` for every declared field, keyed by the events that read
    #: it -- so an operand citing the same read resolves to the same variable.
    field_by_event: dict[str, str] = {}
    for entity in ir.entities:
        for f in entity.fields:
            for ref in f.evidence:
                field_by_event.setdefault(ref, _var_id(entity.entity, f.field))

    def _observed_var(op) -> str:
        """The variable holding this threshold: one per named limit.

        Deliberately NOT matched to a declared field. A reference sheet holds
        one row per limit, and a model that declares a single `value` field for
        the whole sheet would make both price rules resolve to the same
        variable -- so the second rule would test whichever limit was read
        last.
        """
        return f"VAR-{_slug(op.ref)}"

    # --- rules -------------------------------------------------------------
    def operand(op) -> dict:
        if op is None:
            return {}
        if op.kind == "constant":
            try:
                return {"value": float(op.ref)}
            except ValueError:
                return {"value": op.ref}
        if op.kind == "observed_value":
            return {"ref": _observed_var(op)}
        return {"ref": _ref(op.ref)}

    for rule in ir.rules:
        if rule.operator == "EXISTS":
            # Stated as "the entity was found", the same way round as every
            # other rule: true means satisfied, carry on. The decision below
            # routes on `equals: False`, so writing the failing direction here
            # would negate twice and send found invoices to manual review.
            condition = {"operator": "==",
                         "left": {"ref": f"VAR-{_slug(rule.subject_entity)}-EXISTS"},
                         "right": {"value": True}}
        else:
            condition = {"operator": rule.operator,
                         "left": operand(rule.left), "right": operand(rule.right)}
        doc["rules"].append({
            "id": rule.rule_id, "type": "BusinessRule", "name": rule.name,
            "statement": rule.statement, "trueMeans": rule.true_means,
            "condition": condition, "provenance": _prov(rule.evidence)})

    # --- a threshold is a value somebody read, so it gets a variable -------
    # Produced by the read emitted for it above, which names the row it wants.
    for rule in ir.rules:
        for op in (rule.left, rule.right):
            if op is None or op.kind != "observed_value":
                continue
            vid = _observed_var(op)
            if any(v["id"] == vid for v in doc["variables"]):
                continue
            doc["variables"].append({
                "id": vid, "type": "Variable", "name": _var_name(op.ref),
                "dataType": "number", "producedBy": f"TASK-READ-{_slug(op.ref)}",
                "provenance": _prov(op.evidence)})

    # --- existence: a conclusion drawn from a search returning nothing -----
    # No cell reports that a row is absent, so there is no read to point at.
    # The task that establishes this is emitted in sequence above; the variable
    # it produces is declared here so an EXISTS rule has something to refer to.
    for rule in ir.rules:
        if rule.operator != "EXISTS":
            continue
        entity = rule.subject_entity
        vid = f"VAR-{_slug(entity)}-EXISTS"
        if any(v["id"] == vid for v in doc["variables"]):
            continue
        doc["variables"].append({
            "id": vid, "type": "Variable",
            "name": _var_name(entity, "exists"), "dataType": "boolean",
            "producedBy": f"TASK-CHECK-{_slug(entity)}",
            "provenance": _prov(rule.evidence, "INFERRED",
                                "Concluded from a search for the entity returning no row; no "
                                "event reports existence directly.", 0.95)})

    # A variable is produced by whichever task emits it. Resolved here rather
    # than guessed earlier, because splitting a reference-table read moves an
    # output from one task to another.
    by_var = {v["id"]: v for v in doc["variables"]}
    for task in doc["tasks"]:
        for out in task.get("outputs", []):
            if out in by_var:
                by_var[out]["producedBy"] = task["id"]

    # --- where the verdict is written -------------------------------------
    # Derived from the IR, not hardcoded. The model discovered which entity and
    # field carry the outcome by watching it be written; naming them here would
    # smuggle back the fixed vocabulary this pipeline exists without, and would
    # mis-address any process whose verdict lives somewhere else.
    write_task = next((t for t in ir.tasks if t.kind == "WRITE" and t.outputs), None)
    if write_task is not None:
        verdict_entity, _, verdict_field = write_task.outputs[0].partition(".")
    else:
        verdict_entity = ir.entities[0].entity
        verdict_field = ir.entities[0].fields[-1].field
    write_action = {"entity": f"ENTITY-{_slug(verdict_entity)}", "field": verdict_field}

    # --- decisions: one node per rule, in evaluation order -----------------
    # A single decision carrying every branch cannot be compiled: the renderer
    # nests each Decision inside the previous one's ELSE, so one node leaves an
    # empty ELSE and Robot rejects the file. One node per rule also reads more
    # honestly -- each really is its own decision point in the cascade.
    by_id = {r.rule_id: r for r in ir.rules}
    last = len(ir.evaluation_order)
    for position, rule_id in enumerate(ir.evaluation_order, start=1):
        rule = by_id[rule_id]
        outcomes = [{"condition": {"ref": rule_id, "equals": False},
                     "result": rule.failure_outcome,
                     "writeAction": {**write_action, "value": rule.failure_outcome}}]
        if position == last:
            # Every check has passed by the time control reaches here, so this
            # branch is the process succeeding. Without it the innermost ELSE
            # is empty -- which Robot rejects outright, and which would anyway
            # leave the success outcome never written.
            outcomes.append({"result": ir.success_outcome,
                             "writeAction": {**write_action, "value": ir.success_outcome}})
        doc["decisions"].append({
            "id": f"DEC-{position:02d}-{_slug(rule_id.replace('RULE-', ''))}",
            "type": "Decision",
            "intent": f"Route to {rule.failure_outcome} when the {rule.name} check fails"
                      + (f", or to {ir.success_outcome} when every check has passed"
                         if position == last else ""),
            "evaluationOrder": "FIRST_MATCH",
            "outcomes": outcomes,
            **({"priorityOrder": list(ir.evaluation_order),
                "defaultOutcome": ir.default_outcome} if position == 1 else {}),
            "provenance": _prov(rule.evidence)})

    # --- flow ----------------------------------------------------------------
    # Deliberately NOT emitted. The order is already carried by each task's
    # `dependsOn`, and the compiler treats a declared flow as permission to run
    # every task up front -- which is wrong here, because reading a purchase
    # order that does not exist is exactly what the existence check is there to
    # prevent. Leaving it out lets the compiler derive the guard from the
    # dependency graph, so PO-dependent reads stay inside the branch where the
    # PO was found.
    return doc
