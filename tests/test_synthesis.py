"""Tests for the three-pass IR synthesis.

The pipeline is handed no domain vocabulary at all: no entity list, no
sheet-to-entity table, no outcome enum, no column-to-field mapping. The model
reads the recordings and works out what exists. Nothing but the evidence stands
between a wrong answer and a shipped process model, so what makes the result
trustworthy is not that it produces a good IR on a good day, but that a bad one
cannot get through.

Hence the tests that matter here are the negative ones. Each is a specific way
an IR can be wrong while still looking perfectly well formed -- and, with the
lookup tables gone, the only thing standing between each of them and a shipped
process model is the recordings themselves.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.synthesis.models import SynthesizedIR  # noqa: E402
from pipeline.synthesis.oracle import entity_found, evaluate, replay  # noqa: E402
from pipeline.synthesis.run import _regressions, select_seed  # noqa: E402
from pipeline.synthesis.trace import EvidenceTrace, slug_for  # noqa: E402

RECORDINGS = REPO_ROOT / "evidence" / "recorded"


@pytest.fixture(scope="module")
def trace() -> EvidenceTrace:
    return EvidenceTrace.load_dir(RECORDINGS)


@pytest.fixture(scope="module")
def seed_trace(trace) -> EvidenceTrace:
    return trace.subset(["PRICE_EXCESS"])


# ==========================================================================
# The trace: what the corpus shows, with no prior knowledge
# ==========================================================================

def test_event_references_are_qualified(trace):
    """A bare event id names one event per recording, which is no event at all."""
    assert trace.has("PRICE_EXCESS:E003")
    assert not trace.has("E003")


def test_unknown_refs_reports_every_bad_one_at_once(trace):
    bad = trace.unknown_refs(["PRICE_EXCESS:E003", "PRICE_EXCESS:E999", "NOPE:E001"])
    assert bad == ["PRICE_EXCESS:E999", "NOPE:E001"]


def test_outcome_is_read_from_the_trace_not_declared(trace):
    """Which column carries the verdict is not assumed -- the SME reads many
    cells and writes exactly one, and that write is the decision."""
    outcomes = trace.observed_outcomes()
    assert outcomes["MISSING_PO"] == "MANUAL_REVIEW"
    assert outcomes["QUANTITY_MISMATCH"] == "WAREHOUSE_REVIEW"
    assert outcomes["ABSOLUTE_DIFFERENCE_EXCEEDED"] == "PROCUREMENT_REVIEW"


def test_the_corpus_describes_itself_without_interpreting_itself(trace):
    """The prompt is told which sheets and columns exist, because those are
    facts about the recordings. It is told nothing about what they MEAN."""
    described = trace.describe()
    assert '"Unit Price"' in described and 'sheet "Purchase Orders"' in described
    for domain_word in ("Invoice.", "entity", "PurchaseOrder", "business key"):
        assert domain_word not in described


def test_a_search_is_how_a_key_enters_the_process(trace):
    """Nobody reads back the identifier they just searched for, so a search is
    the only place a business key ever appears."""
    missing_po = next(r for r in trace.recordings if r.slug == "MISSING_PO")
    assert missing_po.searched()["Purchase Orders"] == ["PO-9999999"]
    assert missing_po.subject == "INV-95700"


def test_values_read_traces_a_threshold_back_to_the_moment_it_was_looked_up(trace):
    """Without this, nothing can tell a limit somebody read from a magic number
    -- and no table here says which sheet holds limits."""
    assert "PRICE_EXCESS:E012" in trace.values_read()["5000"]
    assert "PRICE_EXCESS:E010" in trace.values_read()["2%"]


def test_slug_is_stable():
    assert slug_for("evidence/recorded/price-within-tolerance.json") == "PRICE_WITHIN_TOLERANCE"


# ==========================================================================
# Seed selection
# ==========================================================================

def test_seed_is_deterministic_and_independent_of_file_order(trace):
    first = select_seed(trace, 3)
    assert first == select_seed(trace, 3)
    shuffled = EvidenceTrace(list(reversed(trace.recordings)))
    assert select_seed(shuffled, 3) == first


def test_seed_prefers_breadth(trace):
    """A three-recording seed should reach every sheet, or the IR it produces
    is missing an entity that no later pass is asked to invent."""
    seed = trace.subset(select_seed(trace, 3))
    sheets = {s for r in seed.recordings for s in r.sheets}
    assert sheets == trace.sheets


# ==========================================================================
# The extension schedule
# ==========================================================================

def test_extension_runs_in_fixed_size_batches_until_the_corpus_is_exhausted(trace):
    """Every recording outside the seed must be absorbed, in batches of the
    configured size, with the remainder forming a final shorter batch. A
    recording silently skipped is a check the automation will never perform."""
    from pipeline.synthesis.run import _extension_batches  # noqa: PLC0415

    seed = select_seed(trace, 3)
    batches = _extension_batches(trace, seed, 2)
    assert [len(b) for b in batches] == [2, 2]
    absorbed = [slug for batch in batches for slug in batch]
    assert sorted(absorbed + seed) == sorted(trace.slugs)
    assert not set(absorbed) & set(seed)


def test_a_remainder_becomes_a_shorter_final_batch(trace):
    from pipeline.synthesis.run import _extension_batches  # noqa: PLC0415

    seed = select_seed(trace, 2)
    batches = _extension_batches(trace, seed, 2)
    assert [len(b) for b in batches] == [2, 2, 1]


def test_nothing_to_extend_is_not_an_error(trace):
    """A corpus entirely consumed by the seed leaves the draft as the answer."""
    from pipeline.synthesis.run import _extension_batches  # noqa: PLC0415

    assert _extension_batches(trace, trace.slugs, 2) == []


# ==========================================================================
# The contract: what an IR may claim
# ==========================================================================

def _ir(**overrides) -> dict:
    """A small but complete IR over price-excess, in names a model chose."""
    doc = {
        "evidence": ["PRICE_EXCESS:E001"],
        "process_intent": "Review a blocked supplier invoice and decide where it should go next.",
        "entities": [
            {"evidence": ["PRICE_EXCESS:E001"], "entity": "Invoice", "sheet": "Invoices",
             "key_field": "invoiceId", "fields": [
                 {"evidence": ["PRICE_EXCESS:E001"], "field": "invoiceId",
                  "column": "Invoice ID", "unit": "text"},
                 {"evidence": ["PRICE_EXCESS:E003"], "field": "unitPrice",
                  "column": "Unit Price", "unit": "currency_per_unit"}]},
            {"evidence": ["PRICE_EXCESS:E006"], "entity": "PurchaseOrder",
             "sheet": "Purchase Orders", "key_field": "poNumber", "fields": [
                 {"evidence": ["PRICE_EXCESS:E006"], "field": "poNumber",
                  "column": "PO Number", "unit": "text"},
                 {"evidence": ["PRICE_EXCESS:E007"], "field": "unitPrice",
                  "column": "Unit Price", "unit": "currency_per_unit"}]},
        ],
        "derivations": [
            {"evidence": ["PRICE_EXCESS:E003", "PRICE_EXCESS:E007"],
             "name": "priceVariancePercent", "function": "PERCENT_DIFFERENCE",
             "operands": ["Invoice.unitPrice", "PurchaseOrder.unitPrice"],
             "unit": "percent", "baseline": "PurchaseOrder.unitPrice"}],
        "outcomes": [
            {"evidence": ["PRICE_EXCESS:E016"], "name": "PROCUREMENT_REVIEW",
             "meaning": "Procurement must review the price discrepancy."},
            {"evidence": ["PRICE_EXCESS:E015"], "name": "RELEASED",
             "meaning": "The invoice is cleared for payment."},
            {"evidence": ["PRICE_EXCESS:E015"], "name": "MANUAL_REVIEW",
             "meaning": "Nobody could decide; a human must look at it."},
        ],
        "tasks": [
            {"evidence": ["PRICE_EXCESS:E003"], "task_id": "TASK-READ-PRICE",
             "name": "Read the invoiced price", "kind": "READ", "entity": "Invoice",
             "intent": "Establish what the supplier has charged per unit.",
             "inputs": [], "outputs": ["Invoice.unitPrice"]}],
        "rules": [
            {"evidence": ["PRICE_EXCESS:E010"], "rule_id": "RULE-PRICE-TOLERANCE",
             "name": "Maximum price variance", "confidence": 0.9,
             "statement": "The invoiced price must be within the allowed variance of the order.",
             "left": {"kind": "derived", "ref": "priceVariancePercent", "unit": "percent"},
             "operator": "<=",
             "right": {"kind": "observed_value", "ref": "Maximum Price Variance",
                       "unit": "percent", "value": "2%",
                       "evidence": ["PRICE_EXCESS:E010"]},
             "true_means": "The price difference is acceptable.",
             "failure_outcome": "PROCUREMENT_REVIEW"}],
        "evaluation_order": ["RULE-PRICE-TOLERANCE"],
        "success_outcome": "RELEASED",
        "default_outcome": "MANUAL_REVIEW",
        "covered_demonstrations": ["PRICE_EXCESS"],
    }
    doc.update(overrides)
    return doc


def test_a_well_formed_ir_validates(seed_trace):
    SynthesizedIR.model_validate(_ir(), context={"trace": seed_trace})


# -- provenance ------------------------------------------------------------

def test_a_fabricated_event_reference_is_rejected(seed_trace):
    """The failure this whole design exists to prevent: E999 is as well formed
    as E003, and only the corpus can tell them apart."""
    doc = _ir()
    doc["rules"][0]["evidence"] = ["PRICE_EXCESS:E999"]
    with pytest.raises(ValidationError, match="do not exist"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_an_event_from_another_recording_is_rejected(seed_trace):
    """A real event id, cited against a recording that was never supplied."""
    doc = _ir()
    doc["rules"][0]["evidence"] = ["QUANTITY_MISMATCH:E026"]
    with pytest.raises(ValidationError, match="do not exist"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_unqualified_reference_is_rejected_before_lookup(seed_trace):
    doc = _ir()
    doc["rules"][0]["evidence"] = ["E010"]
    with pytest.raises(ValidationError, match="malformed event reference"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


# -- the binding, cross-examined against the evidence ----------------------

def test_a_sheet_no_recording_visits_is_rejected(seed_trace):
    doc = _ir()
    doc["entities"][1]["sheet"] = "Vendor Master"
    with pytest.raises(ValidationError, match="no recording visits a sheet"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_a_column_that_is_not_on_that_sheet_is_rejected(seed_trace):
    """"Invoice Qty" is a real column -- on Invoices, not on Purchase Orders."""
    doc = _ir()
    doc["entities"][1]["fields"][1]["column"] = "Invoice Qty"
    with pytest.raises(ValidationError, match="no recording reads a column"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_a_field_whose_evidence_reads_a_different_cell_is_rejected(seed_trace):
    """This is what replaces the hand-written column-to-field table: the claim
    names a sheet and column, and the cited events must show somebody touching
    THAT cell. E003 reads the invoice's price, not the order's."""
    doc = _ir()
    doc["entities"][1]["fields"][1]["evidence"] = ["PRICE_EXCESS:E003"]
    with pytest.raises(ValidationError, match="every event cited for it reads"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_the_key_field_must_be_one_of_the_entity_s_fields(seed_trace):
    doc = _ir()
    doc["entities"][0]["key_field"] = "documentNumber"
    with pytest.raises(ValidationError, match="is not among its fields"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


# -- thresholds ------------------------------------------------------------

def test_an_observed_value_must_cite_the_read_of_that_value(seed_trace):
    """E012 reads 5000, not 2%. Citing the wrong read makes a threshold look
    grounded while the number came from somewhere else entirely."""
    doc = _ir()
    doc["rules"][0]["right"]["evidence"] = ["PRICE_EXCESS:E012"]
    with pytest.raises(ValidationError, match="but the events you cite read"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_a_threshold_that_was_read_cannot_be_frozen_as_a_constant(seed_trace):
    """`<= 2` and `<= the limit on screen` behave identically today and diverge
    the moment the business changes the limit -- at which point the literal
    keeps enforcing the old one, silently and indefinitely."""
    doc = _ir()
    doc["rules"][0]["right"] = {"kind": "constant", "ref": "2", "unit": "percent"}
    with pytest.raises(ValidationError, match="is a value the recordings actually read"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_an_observed_value_without_a_citation_is_just_a_constant(seed_trace):
    doc = _ir()
    doc["rules"][0]["right"]["evidence"] = []
    with pytest.raises(ValidationError, match="must cite the event"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


# -- comparisons -----------------------------------------------------------

def test_price_cannot_be_compared_against_a_percentage(seed_trace):
    """Both are just numbers on screen. Nothing in the UI stops this."""
    doc = _ir()
    doc["rules"][0]["left"] = {"kind": "field", "ref": "Invoice.unitPrice",
                               "unit": "currency_per_unit"}
    with pytest.raises(ValidationError, match="cannot compare"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_text_cannot_be_ordered(seed_trace):
    doc = _ir()
    doc["rules"][0]["left"] = {"kind": "field", "ref": "Invoice.invoiceId", "unit": "text"}
    doc["rules"][0]["right"] = {"kind": "field", "ref": "PurchaseOrder.poNumber",
                                "unit": "text"}
    with pytest.raises(ValidationError, match="not meaningful on text"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_percent_difference_requires_a_baseline(seed_trace):
    doc = _ir()
    doc["derivations"][0]["baseline"] = None
    with pytest.raises(ValidationError, match="needs a baseline"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


# -- outcomes and the cascade ---------------------------------------------

def test_a_rule_cannot_route_to_an_undeclared_outcome(seed_trace):
    doc = _ir()
    doc["rules"][0]["failure_outcome"] = "LEGAL_REVIEW"
    with pytest.raises(ValidationError, match="not a declared outcome"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_a_demonstrated_outcome_must_cite_the_write_that_produced_it(seed_trace):
    """PROCUREMENT_REVIEW was written in this recording. Citing some other
    event for it makes a demonstrated fact look inferred."""
    doc = _ir()
    doc["outcomes"][0]["evidence"] = ["PRICE_EXCESS:E003"]
    with pytest.raises(ValidationError, match="none of the events cited for it is that write"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_every_rule_must_have_a_place_in_the_cascade(seed_trace):
    doc = _ir(evaluation_order=["RULE-PRICE-TOLERANCE", "RULE-GHOST"])
    with pytest.raises(ValidationError, match="unknown rule_id"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_undecidable_must_be_distinguishable_from_success(seed_trace):
    doc = _ir(success_outcome="RELEASED", default_outcome="RELEASED")
    with pytest.raises(ValidationError, match="indistinguishable"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


def test_exists_must_say_which_entity_must_be_findable(seed_trace):
    doc = _ir()
    doc["rules"][0]["operator"] = "EXISTS"
    doc["rules"][0].pop("right")
    with pytest.raises(ValidationError, match="needs subject_entity"):
        SynthesizedIR.model_validate(doc, context={"trace": seed_trace})


# ==========================================================================
# The extension pass must not quietly forget
# ==========================================================================

def test_dropping_a_grounded_rule_is_a_regression(seed_trace):
    draft = SynthesizedIR.model_validate(_ir(), context={"trace": seed_trace})
    thinner = draft.model_copy(deep=True)
    thinner.rules[0].rule_id = "RULE-SOMETHING-ELSE"
    problems = _regressions(draft, thinner)
    assert any("RULE-PRICE-TOLERANCE" in p["msg"] for p in problems)


def test_keeping_everything_is_not_a_regression(seed_trace):
    draft = SynthesizedIR.model_validate(_ir(), context={"trace": seed_trace})
    assert _regressions(draft, draft.model_copy(deep=True)) == []


# ==========================================================================
# The replay oracle
# ==========================================================================

def test_a_failed_lookup_leaves_no_event_of_its_own(trace, seed_trace):
    """A search with no reads after it is what "not found" looks like in a
    trace -- there is no "row absent" event to observe."""
    ir = SynthesizedIR.model_validate(_ir(), context={"trace": seed_trace})
    missing = next(r for r in trace.recordings if r.slug == "MISSING_PO")
    found = next(r for r in trace.recordings if r.slug == "PRICE_EXCESS")
    assert not entity_found(ir, missing, "PurchaseOrder")
    assert entity_found(ir, found, "PurchaseOrder")


def test_replay_catches_an_ir_that_contradicts_its_own_evidence(seed_trace):
    """Invert the operator: the document stays valid, the process becomes wrong."""
    good = SynthesizedIR.model_validate(_ir(), context={"trace": seed_trace})
    assert evaluate(good, seed_trace.recordings[0]).passed

    doc = _ir()
    doc["rules"][0]["operator"] = ">"
    inverted = SynthesizedIR.model_validate(doc, context={"trace": seed_trace})
    row = evaluate(inverted, seed_trace.recordings[0])
    assert not row.passed
    assert row.expected == "PROCUREMENT_REVIEW" and row.actual == "RELEASED"


def test_an_ir_bound_to_the_wrong_column_fails_to_reproduce_the_evidence(trace, seed_trace):
    """The oracle learns the sheet/column binding from the IR under test, so a
    mis-bound field is not a special case -- it simply stops resolving."""
    doc = _ir()
    doc["entities"][1]["fields"][1]["column"] = "Ordered Qty"
    doc["entities"][1]["fields"][1]["evidence"] = ["QUANTITY_MISMATCH:E021"]
    doc["covered_demonstrations"] = ["PRICE_EXCESS", "QUANTITY_MISMATCH"]
    sub = trace.subset(["PRICE_EXCESS", "QUANTITY_MISMATCH"])
    ir = SynthesizedIR.model_validate(doc, context={"trace": sub})
    row = evaluate(ir, seed_trace.recordings[0])
    assert not row.passed
    assert row.rules[0].status == "unevaluated"


def test_a_rule_a_recording_never_exercises_is_not_a_failure(trace, seed_trace):
    """price-within-tolerance reads no goods receipt. A quantity rule is
    unevaluated there, which must not be scored as passing OR failing."""
    ir = SynthesizedIR.model_validate(_ir(), context={"trace": seed_trace})
    rows = {r.slug: r for r in replay(ir, trace.subset(
        ["PRICE_EXCESS", "PRICE_WITHIN_TOLERANCE"]))}
    assert rows["PRICE_EXCESS"].actual == "PROCUREMENT_REVIEW"
    assert rows["PRICE_WITHIN_TOLERANCE"].actual == "RELEASED"


# ==========================================================================
# Emission into the canonical IR shape
# ==========================================================================

def test_emitted_ir_validates_against_the_canonical_schema(trace, seed_trace):
    """The synthesis shape is for the model; the canonical shape is what the
    rest of the repo compiles. If the bridge between them does not produce a
    valid document, this route ends in a file nothing can consume."""
    sys.path.insert(0, str(REPO_ROOT / "ir"))
    from validator import validate_ir_document  # noqa: PLC0415

    from pipeline.synthesis.emit import emit  # noqa: PLC0415

    ir = SynthesizedIR.model_validate(_ir(), context={"trace": seed_trace})
    doc = emit(ir, trace)
    report = validate_ir_document(doc, evidence_dir=str(REPO_ROOT / "evidence"))
    assert report["schemaErrors"] == [], report["schemaErrors"]
    assert report.get("groundingErrors", []) == [], report["groundingErrors"]


def test_emission_derives_every_id_rather_than_trusting_one(trace, seed_trace):
    """Ids the model never wrote cannot be ids the model got wrong."""
    from pipeline.synthesis.emit import emit  # noqa: PLC0415

    ir = SynthesizedIR.model_validate(_ir(), context={"trace": seed_trace})
    doc = emit(ir, trace)
    var_ids = {v["id"] for v in doc["variables"]}
    assert {"VAR-INVOICE-UNITPRICE", "VAR-PURCHASEORDER-UNITPRICE",
            "VAR-PRICEVARIANCEPERCENT"} <= var_ids
    computation = doc["computations"][0]
    assert computation["inputs"] == ["VAR-INVOICE-UNITPRICE", "VAR-PURCHASEORDER-UNITPRICE"]
    assert computation["function"] == "ABS_PERCENT_DIFFERENCE"


def test_every_emitted_provenance_id_resolves(trace, seed_trace):
    """The failure that started all of this: provenance that looks right."""
    from pipeline.synthesis.emit import emit  # noqa: PLC0415

    ir = SynthesizedIR.model_validate(_ir(), context={"trace": seed_trace})
    doc = emit(ir, trace)
    refs: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "provenance":
                    refs.extend(e["sourceId"] for e in value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    assert refs
    assert trace.unknown_refs(refs) == []


# ==========================================================================
# What a failed run is allowed to write down
# ==========================================================================

def test_a_transport_error_does_not_record_the_endpoint():
    """Run reports are committed, so a failure must not put back the endpoint
    that was deliberately kept out of source."""
    from pipeline.synthesis.run import _redact_endpoint  # noqa: PLC0415

    real = ("ReadTimeout: HTTPSConnectionPool(host='ws-abc123def.ap-southeast-1."
            "maas.example.com', port=443): Read timed out. (read timeout=900.0)")
    redacted = _redact_endpoint(real)
    assert "ws-abc123def" not in redacted
    assert "example.com" not in redacted
    assert "Read timed out" in redacted, "the useful part must survive"

    url = "LLM request failed: https://ws-abc.example.com/compatible-mode/v1 refused"
    assert "example.com" not in _redact_endpoint(url)
    assert "refused" in _redact_endpoint(url)


def test_two_thresholds_never_collapse_onto_one_variable(trace):
    """A reference sheet holds one row per limit, and how the model models that
    sheet must not change the automation.

    One run declared a field per threshold; another declared a single `value`
    field for the whole sheet. Under the second shape both price rules resolved
    to the same variable, so the absolute rule tested whichever limit had been
    read last -- an IR that replayed 7/7 and compiled to a script that could
    not run.
    """
    from pipeline.synthesis.emit import emit  # noqa: PLC0415

    doc = _ir()
    # The shape that used to break: one generic value field for the sheet.
    doc["entities"].append({
        "evidence": ["PRICE_EXCESS:E009"], "entity": "ProcessingRules",
        "sheet": "Processing Rules", "key_field": "rule", "fields": [
            {"evidence": ["PRICE_EXCESS:E009"], "field": "rule",
             "column": "Rule", "unit": "text"},
            {"evidence": ["PRICE_EXCESS:E010"], "field": "value",
             "column": "Value", "unit": "percent"}]})
    doc["rules"].append({
        "evidence": ["PRICE_EXCESS:E012"], "rule_id": "RULE-ABS-CAP",
        "name": "Maximum absolute difference", "confidence": 0.9,
        "statement": "The total overcharge must stay within the absolute cap.",
        "left": {"kind": "derived", "ref": "priceVariancePercent", "unit": "percent"},
        "operator": "<=",
        "right": {"kind": "observed_value", "ref": "Maximum Absolute Difference",
                  "unit": "percent", "value": "5000",
                  "evidence": ["PRICE_EXCESS:E012"]},
        "true_means": "The total overcharge is acceptable.",
        "failure_outcome": "PROCUREMENT_REVIEW"})
    doc["evaluation_order"].append("RULE-ABS-CAP")

    sub = trace.subset(["PRICE_EXCESS"])
    ir = SynthesizedIR.model_validate(doc, context={"trace": sub})
    emitted = emit(ir, trace)

    refs = {r["id"]: r["condition"]["right"]["ref"] for r in emitted["rules"]
            if r["condition"].get("right", {}).get("ref")}
    assert refs["RULE-PRICE-TOLERANCE"] != refs["RULE-ABS-CAP"], \
        "two limits must not share a variable"

    # Each limit is read from the row that names it.
    keyed = {h["key"]: t["id"] for t in emitted["tasks"]
             for h in t["implementationHints"] if h.get("key")}
    assert "Maximum Price Variance" in keyed
    assert "Maximum Absolute Difference" in keyed
