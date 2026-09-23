import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ir"))
from validator import validate_ir_document  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"


def load(name):
    return json.loads((FIXTURES / name).read_text())


#: The one reference IR that is grounded in the recorded corpus. The compiler
#: fixtures beside it are deliberately NOT used here: they describe the brief's
#: illustrative demonstrations, which this repository does not carry, so they
#: can be compiled but not grounded.
GROUNDED_FIXTURE = "unified-invoice-review.ir.json"


def test_reference_ir_is_valid_with_no_warnings():
    # Grounded against the real evidence corpus: without --evidence the validator
    # only checks that provenance ids have the right SHAPE, and deliberately warns
    # that it did so, which is not an acceptance this test should accept.
    report = validate_ir_document(load(GROUNDED_FIXTURE), evidence_dir=str(EVIDENCE_DIR))
    assert report["valid"] is True
    assert report["schemaErrors"] == []
    assert report["semanticErrors"] == []
    assert report["groundingErrors"] == []


def test_provenance_is_not_grounded_without_an_evidence_dir():
    report = validate_ir_document(load(GROUNDED_FIXTURE))
    codes = {w["code"] for w in report["warnings"]}
    assert "PROVENANCE_UNGROUNDED" in codes


def test_fabricated_event_reference_is_rejected():
    """The failure mode this stage exists for: a provenance id that is perfectly
    well-formed, passes the shape regex, and refers to an event that does not
    exist in the recording it is cited against."""
    doc = load(GROUNDED_FIXTURE)
    doc["tasks"][0]["provenance"].append(
        {"sourceType": "OBSERVED", "sourceId": "MISSING_PO:E999"})
    report = validate_ir_document(doc, evidence_dir=str(EVIDENCE_DIR))
    assert report["valid"] is False
    assert any(e["code"] == "DANGLING_PROVENANCE" for e in report["groundingErrors"]), report


def test_event_cited_against_the_wrong_recording_is_rejected():
    """E025 is a real event -- in quantity-mismatch.json, not in missing-po.json
    (which ends at E010). Catching this requires the prefix->file map: without
    it, the id exists somewhere in the corpus and looks perfectly grounded."""
    doc = {
        "process": {"provenance": []},
        "demonstrations": [
            {"prefix": "MISSING_PO", "file": "recorded/missing-po.json"},
            {"prefix": "QTY_MISMATCH", "file": "recorded/quantity-mismatch.json"},
        ],
        "tasks": [{"id": "TASK-X", "provenance": [
            {"sourceType": "OBSERVED", "sourceId": "MISSING_PO:E025"}]}],
    }
    from validator import validate_provenance_grounding
    errors, _ = validate_provenance_grounding(doc, str(EVIDENCE_DIR))
    assert len(errors) == 1
    assert "not in recorded/missing-po.json" in errors[0].message

    doc["tasks"][0]["provenance"][0]["sourceId"] = "QTY_MISMATCH:E025"
    errors, _ = validate_provenance_grounding(doc, str(EVIDENCE_DIR))
    assert errors == []


def test_grounding_degrades_without_a_prefix_map():
    """An IR 1.0 document cannot be strictly grounded: with no prefix->file map
    a qualified id can only be checked for existence somewhere in the corpus."""
    # Demonstration IDS, not file paths: the 1.0 style that carries no way to
    # tell which recording a prefix belongs to.
    doc = {
        "process": {"provenance": []},
        "sourceDemonstrations": ["DEMO-1", "DEMO-2"],
        "tasks": [{"id": "TASK-X", "provenance": [
            {"sourceType": "OBSERVED", "sourceId": "MISSING_PO:E005"}]}],
    }
    from validator import validate_provenance_grounding
    errors, warnings = validate_provenance_grounding(doc, str(EVIDENCE_DIR))
    assert errors == []
    assert any(w.code == "PROVENANCE_GROUNDING_DEGRADED" for w in warnings)


def test_rejects_bad_enum_value():
    doc = load("demo-a-price-difference.ir.json")
    doc["decisions"][0]["outcomes"][0]["result"] = "APPROVED_XYZ"
    report = validate_ir_document(doc)
    assert report["valid"] is False
    assert any(e["code"] == "SCHEMA_VIOLATION" for e in report["schemaErrors"])


def test_rejects_incomplete_inferred_provenance():
    doc = load("demo-a-price-difference.ir.json")
    doc["entities"][2]["provenance"] = [{"sourceType": "INFERRED", "sourceId": "NARRATION-A"}]
    report = validate_ir_document(doc)
    assert report["valid"] is False
    assert any(e["code"] == "SCHEMA_VIOLATION" for e in report["schemaErrors"])


def test_rejects_dangling_entity_reference():
    doc = load("demo-b-quantity-mismatch.ir.json")
    doc["tasks"][0]["entity"] = "ENTITY-DOES-NOT-EXIST"
    report = validate_ir_document(doc)
    assert report["valid"] is False
    assert any(e["code"] == "DANGLING_ENTITY" for e in report["semanticErrors"])


def test_rejects_dangling_rule_reference_in_decision():
    doc = load("demo-b-quantity-mismatch.ir.json")
    doc["decisions"][0]["outcomes"][0]["condition"]["ref"] = "RULE-GHOST"
    report = validate_ir_document(doc)
    assert report["valid"] is False
    assert any(e["code"] == "DANGLING_RULE" for e in report["semanticErrors"])


def test_rejects_unknown_write_action_field():
    doc = load("demo-b-quantity-mismatch.ir.json")
    doc["decisions"][0]["outcomes"][0]["writeAction"]["field"] = "statuz"
    report = validate_ir_document(doc)
    assert report["valid"] is False
    assert any(e["code"] == "UNKNOWN_FIELD" for e in report["semanticErrors"])


def test_rejects_output_producer_mismatch():
    doc = load("demo-b-quantity-mismatch.ir.json")
    doc["variables"][2]["producedBy"] = "TASK-99"
    report = validate_ir_document(doc)
    assert report["valid"] is False
    codes = [e["code"] for e in report["semanticErrors"]]
    assert "OUTPUT_PRODUCER_MISMATCH" in codes
    assert "DANGLING_PRODUCER" in codes


def test_rejects_duplicate_id_across_same_prefix():
    doc = load("demo-b-quantity-mismatch.ir.json")
    doc["variables"][1]["id"] = doc["variables"][0]["id"]
    report = validate_ir_document(doc)
    assert report["valid"] is False
    assert any(e["code"] == "DUPLICATE_ID" for e in report["semanticErrors"])


def test_incomplete_decision_coverage_is_a_warning_not_an_error():
    doc = load("demo-b-quantity-mismatch.ir.json")
    doc["decisions"][0]["outcomes"] = [doc["decisions"][0]["outcomes"][0]]
    report = validate_ir_document(doc)
    assert report["valid"] is True
    assert any(w["code"] == "INCOMPLETE_DECISION_COVERAGE" for w in report["warnings"])


def test_semantic_validator_cannot_catch_wrong_but_structurally_valid_field():
    """Documents a known limitation: writing to a real-but-wrong field
    (e.g. invoiceQty instead of status) is structurally valid and must be
    caught by the evaluation harness / golden-reference comparison, not by
    this deterministic validator."""
    doc = load("demo-b-quantity-mismatch.ir.json")
    doc["decisions"][0]["outcomes"][0]["writeAction"]["field"] = "invoiceQty"
    doc["decisions"][0]["outcomes"][0]["writeAction"]["entity"] = "ENTITY-INVOICE"
    report = validate_ir_document(doc)
    assert report["valid"] is True
