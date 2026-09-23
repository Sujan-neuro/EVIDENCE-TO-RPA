import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

from ir_to_robot import (  # noqa: E402
    KEYWORD_CATALOG,
    compile_ir_to_robot,
    render_robot_script,
    validate_task_steps,
)
from llm_client import MockLLMClient  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
REAL_LIBRARY_RESOURCE = str(Path(__file__).parent.parent / "robot" / "library.resource")


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def demo_a_steps():
    return {
        "steps": [
            {"keyword": "Read Invoice Field", "args": ["${invoiceId}", "poNumber"], "assignTo": "poNumber", "generatedFrom": ["TASK-01"]},
            {"keyword": "Read Invoice Field", "args": ["${invoiceId}", "unitPrice"], "assignTo": "invoicePrice", "generatedFrom": ["TASK-01"]},
            {"keyword": "Read Purchase Order Field", "args": ["${poNumber}", "unitPrice"], "assignTo": "purchaseOrderUnitPrice", "generatedFrom": ["TASK-02"]},
            {"keyword": "Read Processing Rule Value", "args": ["Maximum Price Variance"], "assignTo": "maxPriceVariancePercent", "generatedFrom": ["TASK-03"]},
        ]
    }


def demo_b_steps():
    return {
        "steps": [
            {"keyword": "Read Invoice Field", "args": ["${invoiceId}", "poNumber"], "assignTo": "poNumber", "generatedFrom": ["TASK-01"]},
            {"keyword": "Read Invoice Field", "args": ["${invoiceId}", "invoiceQty"], "assignTo": "invoiceQuantity", "generatedFrom": ["TASK-01"]},
            {"keyword": "Read Goods Receipt Field", "args": ["${poNumber}", "receivedQty"], "assignTo": "receivedQuantity", "generatedFrom": ["TASK-02"]},
        ]
    }


# --- validate_task_steps ----------------------------------------------------

def test_valid_step_lists_pass_for_both_demos():
    assert validate_task_steps(demo_a_steps(), load_fixture("demo-a-price-difference.ir.json")) == []
    assert validate_task_steps(demo_b_steps(), load_fixture("demo-b-quantity-mismatch.ir.json")) == []


def test_rejects_keyword_outside_the_catalog():
    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    steps = demo_b_steps()
    steps["steps"][0]["keyword"] = "Click"
    errors = validate_task_steps(steps, ir)
    assert any("not in the allowed catalog" in e for e in errors)


def test_update_invoice_status_is_not_in_the_llm_catalog():
    # The LLM must never be able to emit the write keyword itself --
    # decisions are compiled deterministically, not composed by the LLM.
    assert "Update Invoice Status" not in KEYWORD_CATALOG
    assert "Update Invoice Resolution" not in KEYWORD_CATALOG


def test_rejects_wrong_arg_count():
    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    steps = demo_b_steps()
    steps["steps"][0]["args"] = ["${invoiceId}"]
    errors = validate_task_steps(steps, ir)
    assert any("expects 2 args" in e for e in errors)


def test_rejects_unknown_variable_reference():
    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    steps = demo_b_steps()
    steps["steps"][0]["args"] = ["${madeUpVar}", "poNumber"]
    errors = validate_task_steps(steps, ir)
    assert any("madeUpVar" in e for e in errors)


def test_rejects_missing_assign_to_for_a_returning_keyword():
    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    steps = demo_b_steps()
    del steps["steps"][0]["assignTo"]
    errors = validate_task_steps(steps, ir)
    assert any("assignTo" in e and "missing" in e for e in errors)


def test_rejects_uncovered_task():
    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    steps = demo_b_steps()
    steps["steps"] = steps["steps"][:1]  # drop coverage of TASK-02
    errors = validate_task_steps(steps, ir)
    assert any("TASK-02" in e and "not covered" in e for e in errors)


def test_rejects_dangling_generated_from_id():
    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    steps = demo_b_steps()
    steps["steps"][0]["generatedFrom"] = ["TASK-GHOST"]
    errors = validate_task_steps(steps, ir)
    assert any("TASK-GHOST" in e for e in errors)


# --- render_robot_script (deterministic decision compiler) -----------------

def test_render_produces_correct_operator_and_branches_for_demo_a():
    ir = load_fixture("demo-a-price-difference.ir.json")
    resource_text, robot_text, lineage = render_robot_script(ir, demo_a_steps()["steps"])

    assert "IF    ${priceVariancePercent} <= ${maxPriceVariancePercent}" in resource_text
    assert "Update Invoice Resolution    ${invoiceId}    RELEASED" in resource_text
    assert "Update Invoice Resolution    ${invoiceId}    PROCUREMENT_REVIEW" in resource_text
    # The rule's operator must never be re-derived or paraphrased -- it's
    # copied verbatim from the IR's structured condition.
    assert ir["rules"][0]["condition"]["operator"] == "<="

    # invoice id is a real Robot variable, never a literal id anywhere.
    assert "INV-" not in resource_text
    assert "${INVOICE_ID}" in robot_text


def test_render_produces_correct_branches_for_demo_b_with_no_computation():
    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    resource_text, robot_text, lineage = render_robot_script(ir, demo_b_steps()["steps"])

    assert "IF    ${invoiceQuantity} <= ${receivedQuantity}" in resource_text
    assert "Update Invoice Resolution    ${invoiceId}    RELEASED" in resource_text
    assert "Update Invoice Resolution    ${invoiceId}    WAREHOUSE_REVIEW" in resource_text
    assert "Evaluate" not in resource_text  # no Computation nodes in this IR


def test_every_lineage_entry_traces_back_to_a_real_ir_node():
    ir = load_fixture("demo-a-price-difference.ir.json")
    _, _, lineage = render_robot_script(ir, demo_a_steps()["steps"])

    all_ids = set()
    for coll in ("entities", "variables", "tasks", "computations", "rules", "decisions"):
        all_ids.update(item["id"] for item in ir.get(coll, []))

    for entry in lineage:
        assert entry["lineageSource"] in ("LLM", "DETERMINISTIC_COMPILER")
        assert entry["generatedFrom"], f"{entry['keyword']} has empty lineage"
        for ref in entry["generatedFrom"]:
            node_id = ref.removeprefix("IR:")
            assert node_id in all_ids, f"lineage ref '{ref}' does not match any IR node"


def test_decision_writeaction_to_unsupported_field_raises_a_clear_error():
    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    ir["decisions"][0]["outcomes"][0]["writeAction"]["field"] = "resolution"
    # 'resolution' IS supported -- switch to something genuinely unsupported
    ir["decisions"][0]["outcomes"][0]["writeAction"]["field"] = "supplier"
    try:
        render_robot_script(ir, demo_b_steps()["steps"])
        assert False, "expected a ValueError for an unsupported write field"
    except ValueError as exc:
        assert "supplier" in str(exc)


# --- compile_ir_to_robot end-to-end (mocked LLM, real robot --dryrun) ------

def test_compile_end_to_end_succeeds_with_a_valid_step_list(tmp_path):
    ir = load_fixture("demo-a-price-difference.ir.json")
    client = MockLLMClient([json.dumps(demo_a_steps())])

    result = compile_ir_to_robot(ir, client, out_dir=tmp_path / "robot" / "generated", resource_path=REAL_LIBRARY_RESOURCE)

    assert result["success"] is True
    assert len(result["attempts"]) == 1
    assert Path(result["robotScript"]).exists()
    assert (tmp_path / "robot" / "generated" / "invoice_review.resource").exists()
    assert (tmp_path / "robot" / "generated" / "lineage.json").exists()


def test_compile_retries_after_an_invalid_step_list(tmp_path):
    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    bad_steps = demo_b_steps()
    bad_steps["steps"] = bad_steps["steps"][:1]  # missing TASK-02 coverage

    client = MockLLMClient([json.dumps(bad_steps), json.dumps(demo_b_steps())])
    result = compile_ir_to_robot(ir, client, out_dir=tmp_path / "robot" / "generated", resource_path=REAL_LIBRARY_RESOURCE)

    assert result["success"] is True
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["errors"]


def test_compile_fails_after_exhausting_attempts_on_persistently_invalid_steps(tmp_path):
    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    bad_steps = {"steps": []}

    client = MockLLMClient([json.dumps(bad_steps)] * 3)
    result = compile_ir_to_robot(ir, client, out_dir=tmp_path / "robot" / "generated", max_attempts=3, resource_path=REAL_LIBRARY_RESOURCE)

    assert result["success"] is False
    assert result["robotScript"] is None
    assert len(result["attempts"]) == 3


# --- multi-decision IRs: early exit + gated task placement -----------------

def unified_steps():
    return {
        "steps": [
            {"keyword": "Read Invoice Field", "args": ["${invoiceId}", "blockReason"], "assignTo": "blockReason", "generatedFrom": ["TASK-01"]},
            {"keyword": "Read Invoice Field", "args": ["${invoiceId}", "poNumber"], "assignTo": "poNumber", "generatedFrom": ["TASK-01"]},
            {"keyword": "Purchase Order Exists", "args": ["${poNumber}"], "assignTo": "purchaseOrderExists", "generatedFrom": ["TASK-02"]},
            {"keyword": "Read Invoice Field", "args": ["${invoiceId}", "invoiceQty"], "assignTo": "invoiceQuantity", "generatedFrom": ["TASK-03"]},
            {"keyword": "Read Invoice Field", "args": ["${invoiceId}", "unitPrice"], "assignTo": "invoicePrice", "generatedFrom": ["TASK-03"]},
            {"keyword": "Read Purchase Order Field", "args": ["${poNumber}", "orderedQty"], "assignTo": "purchaseOrderOrderedQty", "generatedFrom": ["TASK-04"]},
            {"keyword": "Read Purchase Order Field", "args": ["${poNumber}", "unitPrice"], "assignTo": "purchaseOrderUnitPrice", "generatedFrom": ["TASK-04"]},
            {"keyword": "Read Goods Receipt Field", "args": ["${poNumber}", "receivedQty"], "assignTo": "receivedQuantity", "generatedFrom": ["TASK-05"]},
            {"keyword": "Read Processing Rule Value", "args": ["Maximum Price Variance"], "assignTo": "maxPriceVariancePercent", "generatedFrom": ["TASK-06"]},
            {"keyword": "Read Processing Rule Value", "args": ["Maximum Absolute Difference"], "assignTo": "maxAbsolutePriceDifference", "generatedFrom": ["TASK-06"]},
        ]
    }


def _line_index(text, needle):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not found in:\n{text}")


def test_unified_ir_compiles_and_covers_all_four_outcomes():
    ir = load_fixture("unified-invoice-review.ir.json")
    resource_text, _, _ = render_robot_script(ir, unified_steps()["steps"])

    for outcome in ("MANUAL_REVIEW", "WAREHOUSE_REVIEW", "PROCUREMENT_REVIEW", "RELEASED"):
        assert f"Update Invoice Resolution    ${{invoiceId}}    {outcome}" in resource_text


def test_po_dependent_tasks_are_gated_behind_the_existence_check():
    """The core safety property: tasks that look up rows keyed by the PO
    number must not run when the PO was not found -- they would fail on a
    row that isn't there."""
    ir = load_fixture("unified-invoice-review.ir.json")
    resource_text, _, _ = render_robot_script(ir, unified_steps()["steps"])

    guard_line = _line_index(resource_text, "IF    ${purchaseOrderExists}")
    for gated in (
        "Read Purchase Order Field    ${poNumber}    orderedQty",
        "Read Purchase Order Field    ${poNumber}    unitPrice",
        "Read Goods Receipt Field    ${poNumber}    receivedQty",
    ):
        assert _line_index(resource_text, gated) > guard_line, (
            f"{gated!r} must be rendered after the PO-existence guard"
        )

    # The existence check itself must come before its own guard.
    assert _line_index(resource_text, "Purchase Order Exists") < guard_line


def test_later_decisions_nest_inside_earlier_ones_for_early_exit():
    """Once a decision writes an outcome, later decisions must not run and
    overwrite it -- enforced structurally by nesting, not a runtime flag."""
    ir = load_fixture("unified-invoice-review.ir.json")
    resource_text, _, _ = render_robot_script(ir, unified_steps()["steps"])

    manual = _line_index(resource_text, "MANUAL_REVIEW")
    warehouse = _line_index(resource_text, "WAREHOUSE_REVIEW")
    procurement = _line_index(resource_text, "PROCUREMENT_REVIEW")

    def indent_of(i):
        line = resource_text.splitlines()[i]
        return len(line) - len(line.lstrip())

    # Each successive outcome sits strictly deeper than the previous one.
    assert indent_of(manual) < indent_of(warehouse) < indent_of(procurement)


def test_boolean_literals_render_python_style_not_json_style():
    """Robot's IF evaluates a Python expression -- JSON's lowercase `false`
    would be a NameError at runtime."""
    ir = load_fixture("unified-invoice-review.ir.json")
    resource_text, _, _ = render_robot_script(ir, unified_steps()["steps"])

    assert "== False" in resource_text
    assert "== false" not in resource_text


def test_decision_needing_unreachable_task_raises_a_clear_error():
    ir = load_fixture("unified-invoice-review.ir.json")
    # Break the graph: the quantity decision's inputs can no longer be produced.
    ir["tasks"] = [t for t in ir["tasks"] if t["id"] != "TASK-05"]
    steps = {"steps": [s for s in unified_steps()["steps"] if "TASK-05" not in s["generatedFrom"]]}

    try:
        render_robot_script(ir, steps["steps"])
        assert False, "expected a ValueError for an unreachable decision dependency"
    except ValueError as exc:
        assert "TASK-05" in str(exc)


def test_a_renderer_fault_is_not_blamed_on_the_model(tmp_path, monkeypatch):
    """A dry-run failure the model cannot influence must stop the loop.

    If the renderer emits something Robot rejects, re-prompting changes the
    step list and nothing else -- so the same failure returns, the attempts run
    out, and the report says the model failed. It did not.
    """
    import pipeline.ir_to_robot as mod

    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    monkeypatch.setattr(mod, "dry_run_robot_file",
                        lambda path: (False, "ELSE branch cannot be empty."))

    client = MockLLMClient([json.dumps(demo_b_steps())] * 3)
    result = mod.compile_ir_to_robot(
        ir, client, out_dir=tmp_path / "generated", max_attempts=3,
        resource_path=REAL_LIBRARY_RESOURCE)

    assert result["success"] is False
    # Stopped on the second identical failure rather than burning the third.
    assert len(result["attempts"]) == 2
    assert result["attempts"][-1]["compilerFault"] is True
    assert "renderer, not the model" in result["attempts"][-1]["errors"][0]


def test_a_failure_the_model_can_fix_still_retries(tmp_path, monkeypatch):
    """The short circuit must not fire on a changing error -- that is the case
    where re-prompting is exactly the right response."""
    import pipeline.ir_to_robot as mod

    ir = load_fixture("demo-b-quantity-mismatch.ir.json")
    outputs = iter([(False, "No keyword with name 'Reed Invoice Field' found."),
                    (False, "Keyword 'Read Invoice Field' expected 2 arguments, got 1."),
                    (True, "")])
    monkeypatch.setattr(mod, "dry_run_robot_file", lambda path: next(outputs))

    client = MockLLMClient([json.dumps(demo_b_steps())] * 3)
    result = mod.compile_ir_to_robot(
        ir, client, out_dir=tmp_path / "generated", max_attempts=3,
        resource_path=REAL_LIBRARY_RESOURCE)

    assert result["success"] is True
    assert len(result["attempts"]) == 3
