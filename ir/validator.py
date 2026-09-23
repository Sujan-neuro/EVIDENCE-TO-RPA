#!/usr/bin/env python3
"""Deterministic IR validation: schema + semantic (cross-reference) checks.

Two-stage gate, matching the brief's §28 pipeline:
  1. Schema validation (structure, enums, required fields) — via jsonschema.
  2. Semantic validation (cross-references, data-flow, referential
     integrity) — hand-written here, since JSON Schema alone cannot express
     "this ref must point to a node that actually exists elsewhere in the
     same document."

Only a document that passes BOTH stages is "Accepted IR" and may proceed
to IR->Robot compilation. Semantic errors are hard failures; warnings are
reported but do not block acceptance (e.g. a provenance sourceId that
doesn't look like a real eventId/narrationId — worth flagging for review,
not necessarily wrong).
"""
import json
import re
import sys
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).parent / "schema.json"

# A bare eventId (E001) or, when an IR draws on more than one demonstration
# and eventIds are not unique across source files, an alias-qualified one
# (e.g. "PRICE_EXCESS:E001") -- the alias disambiguates which recording the
# eventId belongs to. Same idea for narrationId.
EVENT_ID_RE = re.compile(r"^(?:[A-Za-z0-9_]+:)?E\d+$")
NARRATION_ID_RE = re.compile(r"^(?:[A-Za-z0-9_]+:)?NARRATION-")


class ValidationIssue:
    def __init__(self, code, message, path):
        self.code = code
        self.message = message
        self.path = path

    def to_dict(self):
        return {"code": self.code, "message": self.message, "path": self.path}

    def __repr__(self):
        return f"[{self.code}] {self.path}: {self.message}"


def validate_schema(doc):
    """Stage 1. Returns a list of ValidationIssue (empty if valid)."""
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    issues = []
    for err in validator.iter_errors(doc):
        path = "$" + "".join(
            f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path
        )
        issues.append(ValidationIssue("SCHEMA_VIOLATION", err.message, path or "$"))
    return issues


def _index_by_id(items):
    return {item["id"]: item for item in items}


def validate_semantics(doc):
    """Stage 2. Cross-reference / referential-integrity checks.

    Returns (errors, warnings) — both lists of ValidationIssue.
    Assumes the document already passed schema validation (so required
    fields/shapes are present); this stage only checks that references
    between nodes actually resolve.
    """
    errors = []
    warnings = []

    entities = _index_by_id(doc.get("entities", []))
    variables = _index_by_id(doc.get("variables", []))
    tasks = _index_by_id(doc.get("tasks", []))
    computations = _index_by_id(doc.get("computations", []))
    rules = _index_by_id(doc.get("rules", []))
    decisions = _index_by_id(doc.get("decisions", []))
    entity_refs = doc.get("entityReferences", [])

    # --- Global id uniqueness across all node collections -----------------
    all_ids = []
    for coll in (doc.get("entities", []), doc.get("variables", []), doc.get("tasks", []),
                 doc.get("computations", []), doc.get("rules", []), doc.get("decisions", []),
                 entity_refs):
        all_ids.extend(item["id"] for item in coll)
    seen = set()
    for node_id in all_ids:
        if node_id in seen:
            errors.append(ValidationIssue(
                "DUPLICATE_ID", f"id '{node_id}' is used by more than one node", f"$..id={node_id}"
            ))
        seen.add(node_id)

    # --- EntityReference: fromEntity/toEntity must exist -------------------
    for ref in entity_refs:
        for side in ("fromEntity", "toEntity"):
            if ref[side] not in entities:
                errors.append(ValidationIssue(
                    "DANGLING_ENTITY_REF",
                    f"{side} '{ref[side]}' does not match any declared Entity",
                    f"$.entityReferences[id={ref['id']}].{side}",
                ))

    # --- Variable.producedBy must resolve to a Task, Computation, or PROCESS_INPUT
    for var in doc.get("variables", []):
        producer = var["producedBy"]
        if producer == "PROCESS_INPUT":
            continue
        if producer.startswith("TASK-") and producer not in tasks:
            errors.append(ValidationIssue(
                "DANGLING_PRODUCER", f"producedBy '{producer}' does not match any declared BusinessTask",
                f"$.variables[id={var['id']}].producedBy",
            ))
        elif producer.startswith("COMP-") and producer not in computations:
            errors.append(ValidationIssue(
                "DANGLING_PRODUCER", f"producedBy '{producer}' does not match any declared Computation",
                f"$.variables[id={var['id']}].producedBy",
            ))

    # --- BusinessTask: entity, inputs, outputs, dependsOn ------------------
    for task in doc.get("tasks", []):
        if task["entity"] not in entities:
            errors.append(ValidationIssue(
                "DANGLING_ENTITY", f"entity '{task['entity']}' does not match any declared Entity",
                f"$.tasks[id={task['id']}].entity",
            ))
        for var_id in task.get("inputs", []):
            if var_id.startswith("VAR-") and var_id not in variables:
                errors.append(ValidationIssue(
                    "DANGLING_VARIABLE", f"input '{var_id}' does not match any declared Variable",
                    f"$.tasks[id={task['id']}].inputs",
                ))
        for var_id in task.get("outputs", []):
            if var_id not in variables:
                errors.append(ValidationIssue(
                    "DANGLING_VARIABLE", f"output '{var_id}' does not match any declared Variable",
                    f"$.tasks[id={task['id']}].outputs",
                ))
            elif variables[var_id]["producedBy"] != task["id"]:
                errors.append(ValidationIssue(
                    "OUTPUT_PRODUCER_MISMATCH",
                    f"Variable '{var_id}' is listed as an output of Task '{task['id']}' "
                    f"but its own producedBy is '{variables[var_id]['producedBy']}'",
                    f"$.tasks[id={task['id']}].outputs",
                ))
        for dep_id in task.get("dependsOn", []):
            if dep_id not in tasks:
                errors.append(ValidationIssue(
                    "DANGLING_TASK", f"dependsOn '{dep_id}' does not match any declared BusinessTask",
                    f"$.tasks[id={task['id']}].dependsOn",
                ))

    # --- Computation: inputs/output must resolve to Variables, and the
    #     output Variable's producedBy must point back to this Computation
    #
    #: How many inputs each function needs. A binary function given one input
    #: is not a dangling reference -- every id it does carry resolves -- so
    #: nothing else here catches it, and it compiles to an arithmetic
    #: expression with an empty operand.
    ARITY = {"ABS_DIFFERENCE": 2, "ABS_PERCENT_DIFFERENCE": 2, "DIFFERENCE": 2,
             "PRODUCT": 2, "SUM": 2, "IDENTITY": 1}
    for comp in doc.get("computations", []):
        expected = ARITY.get(comp.get("function"))
        if expected is not None and len(comp["inputs"]) != expected:
            errors.append(ValidationIssue(
                "COMPUTATION_ARITY",
                f"{comp['function']} takes {expected} input(s), but "
                f"{len(comp['inputs'])} were given ({comp['inputs']}). An operand was "
                f"dropped somewhere between the process model and this document.",
                f"$.computations[id={comp['id']}].inputs",
            ))
        for var_id in comp["inputs"]:
            if var_id not in variables:
                errors.append(ValidationIssue(
                    "DANGLING_VARIABLE", f"input '{var_id}' does not match any declared Variable",
                    f"$.computations[id={comp['id']}].inputs",
                ))
        out_id = comp["output"]
        if out_id not in variables:
            errors.append(ValidationIssue(
                "DANGLING_VARIABLE", f"output '{out_id}' does not match any declared Variable",
                f"$.computations[id={comp['id']}].output",
            ))
        elif variables[out_id]["producedBy"] != comp["id"]:
            errors.append(ValidationIssue(
                "OUTPUT_PRODUCER_MISMATCH",
                f"Variable '{out_id}' is listed as the output of Computation '{comp['id']}' "
                f"but its own producedBy is '{variables[out_id]['producedBy']}'",
                f"$.computations[id={comp['id']}].output",
            ))

    # --- BusinessRule: condition operands must resolve ----------------------
    def _check_operand(operand, rule_id, side):
        if "ref" not in operand:
            return
        ref = operand["ref"]
        if ref.startswith("VAR-") and ref not in variables:
            errors.append(ValidationIssue(
                "DANGLING_VARIABLE", f"{side} operand '{ref}' does not match any declared Variable",
                f"$.rules[id={rule_id}].condition.{side}",
            ))
        elif ref.startswith("COMP-") and ref not in computations:
            errors.append(ValidationIssue(
                "DANGLING_COMPUTATION", f"{side} operand '{ref}' does not match any declared Computation",
                f"$.rules[id={rule_id}].condition.{side}",
            ))

    for rule in doc.get("rules", []):
        cond = rule["condition"]
        _check_operand(cond["left"], rule["id"], "left")
        _check_operand(cond["right"], rule["id"], "right")

    # --- Decision: outcomes' rule refs, entity/field, and coverage ---------
    for decision in doc.get("decisions", []):
        seen_conditions = set()
        for i, outcome in enumerate(decision["outcomes"]):
            path = f"$.decisions[id={decision['id']}].outcomes[{i}]"
            cond = outcome.get("condition")
            if cond is not None:
                rule_id = cond["ref"]
                if rule_id not in rules:
                    errors.append(ValidationIssue(
                        "DANGLING_RULE", f"condition.ref '{rule_id}' does not match any declared BusinessRule",
                        f"{path}.condition.ref",
                    ))
                key = (rule_id, cond["equals"])
                if key in seen_conditions:
                    errors.append(ValidationIssue(
                        "DUPLICATE_OUTCOME_CONDITION",
                        f"Decision '{decision['id']}' has more than one outcome for {rule_id} == {cond['equals']}",
                        path,
                    ))
                seen_conditions.add(key)

            write = outcome["writeAction"]
            entity = entities.get(write["entity"])
            if entity is None:
                errors.append(ValidationIssue(
                    "DANGLING_ENTITY", f"writeAction.entity '{write['entity']}' does not match any declared Entity",
                    f"{path}.writeAction.entity",
                ))
            else:
                field_names = {f["name"] for f in entity["fields"]}
                if write["field"] not in field_names:
                    errors.append(ValidationIssue(
                        "UNKNOWN_FIELD",
                        f"writeAction.field '{write['field']}' is not a declared field of Entity '{entity['id']}'",
                        f"{path}.writeAction.field",
                    ))

        # Coverage warning: a rule-driven decision should normally cover both
        # equals=true and equals=false -- UNLESS it is one link of a cascade.
        #
        # In a first-match cascade each check states only the branch that ends
        # the process (usually the failure), and the complementary case is not
        # missing: it falls through to the next decision, and the final decision
        # carries the terminal outcome. Warning there would fire on every link
        # of a perfectly complete process, which trains a reader to ignore the
        # warning entirely -- worse than not having it.
        decision_index = doc.get("decisions", []).index(decision)
        is_last_decision = decision_index == len(doc.get("decisions", [])) - 1
        in_cascade = (
            any(d.get("priorityOrder") or d.get("defaultOutcome")
                for d in doc.get("decisions", []))
            and not is_last_decision
        )

        rule_ids_used = {o["condition"]["ref"] for o in decision["outcomes"] if o.get("condition")}
        for rule_id in rule_ids_used:
            covered = {o["condition"]["equals"] for o in decision["outcomes"] if o.get("condition", {}).get("ref") == rule_id}
            if covered != {True, False} and not in_cascade:
                warnings.append(ValidationIssue(
                    "INCOMPLETE_DECISION_COVERAGE",
                    f"Decision '{decision['id']}' only covers {rule_id} == {sorted(covered)}, not both true/false",
                    f"$.decisions[id={decision['id']}]",
                ))

    # --- Provenance sourceId sanity (warnings only) -------------------------
    def _walk_provenance(node, path):
        for entry in node.get("provenance", []):
            st, sid = entry["sourceType"], entry["sourceId"]
            if st == "OBSERVED" and not (EVENT_ID_RE.match(sid) or sid.startswith("PROCESSING_RULES")):
                warnings.append(ValidationIssue(
                    "SUSPECT_PROVENANCE_ID",
                    f"OBSERVED provenance sourceId '{sid}' does not look like an eventId",
                    f"{path}.provenance",
                ))
            if st == "STATED" and not NARRATION_ID_RE.match(sid):
                warnings.append(ValidationIssue(
                    "SUSPECT_PROVENANCE_ID",
                    f"STATED provenance sourceId '{sid}' does not look like a narrationId",
                    f"{path}.provenance",
                ))

    for coll_name in ("entities", "entityReferences", "variables", "tasks", "computations", "rules", "decisions"):
        for node in doc.get(coll_name, []):
            _walk_provenance(node, f"$.{coll_name}[id={node['id']}]")
    _walk_provenance(doc["process"], "$.process")

    return errors, warnings


# --- Stage 3: evidence grounding (V1) ---------------------------------------
#
# Stages 1 and 2 can both pass on an IR whose provenance is entirely invented:
# `_walk_provenance` above only checks that a sourceId has the SHAPE of an
# eventId. A fabricated-but-well-formed reference such as "QTY_MISMATCH:E041"
# matches that regex perfectly while pointing at an event that does not exist
# in any recording. That is the brief's §39 failure mode "a generated step
# cannot be traced back to evidence", and format checking cannot catch it.
#
# This stage opens the actual demonstration files and resolves every OBSERVED
# and STATED sourceId against them. Dangling references are hard ERRORS: a node
# justified by evidence that does not exist is unfalsifiable, and admitting it
# would silently corrupt every lineage query built on top of the IR.


class EvidenceIndex:
    """Resolvable eventIds and narrationIds, keyed by demonstration prefix."""

    def __init__(self):
        self.by_prefix = {}
        self.load_errors = []
        self.duplicate_demo_ids = {}
        self.corpus = {}
        self.has_prefix_map = True
        self.ambiguous_prefixes = {}

    @property
    def is_empty(self):
        return not self.by_prefix

    def resolve(self, source_id):
        """Return (ok, reason). `reason` is None when ok."""
        if ":" not in source_id:
            hits = {d["file"]: p for p, d in self.by_prefix.items()
                    if source_id in d["events"] or source_id in d["narrations"]}
            if not hits:
                return False, "unqualified id not found in any demonstration"
            if len(hits) > 1:
                return False, (f"unqualified id is ambiguous across {sorted(hits.values())}; "
                               "qualify it as PREFIX:ID")
            return True, None

        prefix, local = source_id.split(":", 1)
        demo = self.by_prefix.get(prefix)
        if demo is None:
            if not self.has_prefix_map:
                # No prefix -> file map was declared, so the prefix cannot be
                # tied to a recording. Fall back to asking only whether the id
                # exists ANYWHERE in the corpus. This is deliberately weak: it
                # cannot catch an id that is real in one recording but cited
                # against another, which is precisely the mistake a prefix map
                # exists to prevent.
                anywhere = any(local in m["events"] or local in m["narrations"]
                               for m in self.corpus.values())
                if anywhere:
                    return True, "DEGRADED"
                return False, f"'{local}' does not exist in any indexed recording"
            return False, f"unknown demonstration prefix '{prefix}'"
        if local.startswith("NARRATION"):
            if local in demo["narrations"]:
                return True, None
            return False, (f"narration '{local}' not in {demo['file']} "
                           f"(it declares {sorted(demo['narrations'])})")
        if local in demo["events"]:
            return True, None
        lo, hi = demo["eventRange"]
        return False, (f"event '{local}' not in {demo['file']} "
                       f"(that recording spans {lo}..{hi})")


def _scan_evidence_dir(root):
    """Index every demonstration file under `root`, by relative path."""
    corpus = {}
    for path in sorted(Path(root).rglob("*.json")):
        try:
            demo = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if "events" not in demo:
            continue
        events = {e["eventId"] for e in demo["events"] if "eventId" in e}
        narr = demo.get("narration") or {}
        corpus[str(path.relative_to(root))] = {
            "demoId": demo.get("demonstrationId"),
            "events": events,
            "narrations": {narr["narrationId"]} if "narrationId" in narr else set(),
            "eventRange": (min(events), max(events)) if events else ("-", "-"),
        }
    return corpus


def build_index_from_docs(evidence_docs):
    """Index the evidence documents that were actually handed to the model.

    Preferred over `build_evidence_index` wherever the caller still has them:
    it needs no path or demonstrationId resolution, so it is immune to a
    corpus whose demonstrationIds collide.
    """
    index = EvidenceIndex()
    index.has_prefix_map = True
    for demo in evidence_docs:
        events = {e["eventId"] for e in demo.get("events", []) if "eventId" in e}
        narr = demo.get("narration") or {}
        entry = {
            "file": demo.get("demonstrationId", "<in-memory>"),
            "events": events,
            "narrations": {narr["narrationId"]} if "narrationId" in narr else set(),
            "eventRange": (min(events), max(events)) if events else ("-", "-"),
        }
        # ONE prefix key per document: registering the same entry twice makes
        # every unqualified id look ambiguous against itself.
        index.by_prefix[demo.get("demonstrationId") or "<given>"] = entry
        index.corpus[entry["file"]] = entry
    return index


def build_evidence_index(doc, evidence_dir):
    """Index the demonstrations an IR declares.

    Two declaration styles are supported:

    * `demonstrations: [{prefix, file}]` (IR >= 1.1) gives an explicit
      prefix -> file map, which is what makes a qualified sourceId such as
      "QTY_MISMATCH:E024" resolvable to one specific recording.
    * `sourceDemonstrations: [str]` (IR 1.0) is a bare list of paths or
      demonstration ids with no prefixes. Where the IR nonetheless uses
      qualified sourceIds, the mapping is guesswork, so the index records
      that it is degraded and `validate_provenance_grounding` downgrades
      those references to a warning rather than claiming they are grounded.
    """
    index = EvidenceIndex()
    root = Path(evidence_dir)
    corpus = _scan_evidence_dir(root)
    index.corpus = corpus

    by_demo_id = {}
    for rel, meta in corpus.items():
        if meta["demoId"]:
            by_demo_id.setdefault(meta["demoId"], []).append(rel)
    index.duplicate_demo_ids = {k: v for k, v in by_demo_id.items() if len(v) > 1}

    declared = doc.get("demonstrations")
    if declared is not None:
        index.has_prefix_map = True
        for entry in declared:
            prefix, rel = entry["prefix"], entry["file"]
            meta = corpus.get(rel)
            if meta is None:
                index.load_errors.append((prefix, str(root / rel), "file not found"))
                continue
            index.by_prefix[prefix] = dict(meta, file=rel)
        return index

    # --- degraded path: IR 1.0 ------------------------------------------------
    index.has_prefix_map = False
    for token in doc.get("sourceDemonstrations", []):
        if token in corpus:
            index.by_prefix[Path(token).stem.upper().replace("-", "_")] = dict(
                corpus[token], file=token)
        elif token in by_demo_id:
            rels = by_demo_id[token]
            if len(rels) == 1:
                index.by_prefix[token] = dict(corpus[rels[0]], file=rels[0])
            else:
                # Several recordings claim this demonstrationId (a defect in the
                # evidence corpus). We cannot tell which one the IR meant, so
                # merge their ids and mark the index degraded -- an ambiguous
                # mapping is a reason to weaken the check, never a reason to
                # reject an IR whose provenance may be perfectly correct.
                merged_events, merged_narr = set(), set()
                for rel in rels:
                    merged_events |= corpus[rel]["events"]
                    merged_narr |= corpus[rel]["narrations"]
                index.by_prefix[token] = {
                    "file": " | ".join(rels), "events": merged_events,
                    "narrations": merged_narr,
                    "eventRange": (min(merged_events), max(merged_events)) if merged_events else ("-", "-"),
                }
                index.ambiguous_prefixes[token] = rels
        else:
            index.load_errors.append(
                (token, token, "not resolvable to a file under the evidence directory"))
    return index


def validate_provenance_grounding(doc, evidence_dir=None, evidence_docs=None):
    """Stage 3. Resolve every evidence-backed provenance ref against the corpus.

    `evidence_docs` (the documents actually fed to the model) takes precedence
    over `evidence_dir`, because it identifies the source demonstrations exactly
    rather than inferring them from declared ids.
    """
    errors, warnings = [], []
    index = (build_index_from_docs(evidence_docs) if evidence_docs
             else build_evidence_index(doc, evidence_dir))

    for prefix, path, why in index.load_errors:
        errors.append(ValidationIssue(
            "EVIDENCE_UNREADABLE",
            f"Demonstration '{prefix}' declared but {why}: {path}",
            "$.demonstrations",
        ))

    for demo_id, files in index.duplicate_demo_ids.items():
        warnings.append(ValidationIssue(
            "DUPLICATE_DEMONSTRATION_ID",
            f"demonstrationId '{demo_id}' is claimed by {len(files)} recordings ({', '.join(files)}). "
            "Prefixes keep this IR unambiguous, but any tooling keyed on demonstrationId will conflate them.",
            "$.demonstrations",
        ))

    for prefix, rels in getattr(index, "ambiguous_prefixes", {}).items():
        warnings.append(ValidationIssue(
            "AMBIGUOUS_DEMONSTRATION_PREFIX",
            f"'{prefix}' maps to {len(rels)} recordings ({', '.join(rels)}); their eventIds were "
            "merged, so an id cited against the wrong one cannot be detected.",
            "$.sourceDemonstrations",
        ))

    if index.is_empty:
        warnings.append(ValidationIssue(
            "PROVENANCE_UNGROUNDED",
            "No demonstration files could be indexed; provenance was NOT grounded against evidence.",
            "$.demonstrations",
        ))
        return errors, warnings

    degraded = []

    def _check(node, path):
        for entry in node.get("provenance", []):
            st, sid = entry.get("sourceType"), entry.get("sourceId", "")
            if st not in ("OBSERVED", "STATED"):
                continue  # INFERRED/GENERATED are justified by rationale, not by an event
            ok, reason = index.resolve(sid)
            if ok and reason == "DEGRADED":
                degraded.append(sid)
            elif not ok:
                errors.append(ValidationIssue(
                    "DANGLING_PROVENANCE",
                    f"{st} provenance '{sid}' does not resolve: {reason}",
                    f"{path}.provenance",
                ))

    for coll in ("entities", "entityReferences", "variables", "tasks", "computations",
                 "rules", "decisions", "exceptions", "inputs", "outputs"):
        for node in doc.get(coll, []):
            _check(node, f"$.{coll}[id={node.get('id')}]")
    for name in ("Outcome", "Status"):
        node = doc.get("enums", {}).get(name)
        if node:
            _check(node, f"$.enums.{name}")
    _check(doc["process"], "$.process")

    if degraded:
        warnings.append(ValidationIssue(
            "PROVENANCE_GROUNDING_DEGRADED",
            f"{len(degraded)} qualified sourceId(s) were checked for existence anywhere in the "
            "corpus rather than against a specific recording, because this IR declares no "
            "prefix->file map (`demonstrations`). An id cited against the wrong recording "
            "cannot be detected in this mode.",
            "$.sourceDemonstrations",
        ))
    return errors, warnings


def validate_ir_document(doc, evidence_dir=None, evidence_docs=None):
    """Full two-stage gate. Returns a report dict:
    { valid: bool, schemaErrors: [...], semanticErrors: [...], warnings: [...] }
    Semantic validation only runs if schema validation passed, since it
    assumes required fields/shapes are already present.
    """
    schema_errors = validate_schema(doc)
    if schema_errors:
        return {
            "valid": False,
            "schemaErrors": [e.to_dict() for e in schema_errors],
            "semanticErrors": [],
            "groundingErrors": [],
            "warnings": [],
        }

    semantic_errors, warnings = validate_semantics(doc)

    grounding_errors = []
    if evidence_dir is None and not evidence_docs:
        warnings.append(ValidationIssue(
            "PROVENANCE_UNGROUNDED",
            "No evidence directory supplied; provenance was checked for FORMAT only, "
            "not for existence. Pass --evidence to ground it.",
            "$",
        ))
    else:
        grounding_errors, grounding_warnings = validate_provenance_grounding(
            doc, evidence_dir=evidence_dir, evidence_docs=evidence_docs)
        warnings.extend(grounding_warnings)

    return {
        "valid": len(semantic_errors) == 0 and len(grounding_errors) == 0,
        "schemaErrors": [],
        "semanticErrors": [e.to_dict() for e in semantic_errors],
        "groundingErrors": [e.to_dict() for e in grounding_errors],
        "warnings": [w.to_dict() for w in warnings],
    }


def main():
    args = sys.argv[1:]
    evidence_dir = None
    if "--evidence" in args:
        i = args.index("--evidence")
        try:
            evidence_dir = args[i + 1]
        except IndexError:
            print("usage: validator.py <ir-document.json> [--evidence <dir>]", file=sys.stderr)
            sys.exit(2)
        del args[i:i + 2]
    if len(args) != 1:
        print("usage: validator.py <ir-document.json> [--evidence <dir>]", file=sys.stderr)
        sys.exit(2)
    doc = json.loads(Path(args[0]).read_text())
    report = validate_ir_document(doc, evidence_dir=evidence_dir)
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
