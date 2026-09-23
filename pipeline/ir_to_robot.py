"""IR -> Robot Framework compilation step.

Two-layer split, deliberately:

  - LLM layer: composes calls to the hand-written library.resource
    "locate/read" keywords, in the right order, wiring IR Variables into
    Robot ${variables}. This is real LLM participation (per the brief's
    §34 requirement) but scoped to composition, never to writing Robot
    syntax or business-logic comparisons by hand.

  - Deterministic layer: Decision/BusinessRule/Computation nodes are
    ALREADY fully structured data in the IR (operator, operands, function
    name) -- so the IF/ELSE branch, the arithmetic, and the final
    Update Invoice Status/Resolution call are compiled mechanically by
    this module, never by the LLM. This is what makes "LLM reverses a
    comparison operator" (Part 9 failure mode) structurally impossible
    at this stage: the operator was never handed to a generative step in
    the first place.

Both layers' output steps carry lineage back to specific IR node ids,
tagged with which layer produced them (LLM vs DETERMINISTIC_COMPILER).
"""
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# The only keywords the LLM is allowed to call. Deliberately excludes the
# Update Invoice Status/Resolution write keywords -- those are only ever
# emitted by the deterministic decision compiler below. Also deliberately
# excludes "Locate X By Y" -- every Read* keyword below already performs
# its own locate internally (see library.resource), so a separate locate
# step would just be redundant, not incorrect.
KEYWORD_CATALOG = {
    "Purchase Order Exists": {"args": ["po_number"], "returns": True},
    "Read Invoice Field": {"args": ["invoice_id", "field"], "returns": True},
    "Read Purchase Order Field": {"args": ["po_number", "field"], "returns": True},
    "Read Goods Receipt Field": {"args": ["po_number", "field"], "returns": True},
    "Read Processing Rule Value": {"args": ["rule_name"], "returns": True},
}

#: Every function the schema allows must appear here, or a schema-valid IR
#: compiles to a script with a hole in it. Binary functions take {a} and {b} in
#: the order the Computation lists its inputs.
FUNCTION_TEMPLATES = {
    "ABS_DIFFERENCE": "abs({a} - {b})",
    "ABS_PERCENT_DIFFERENCE": "abs({a} - {b}) / {b} * 100",
    "DIFFERENCE": "{a} - {b}",
    "PRODUCT": "{a} * {b}",
    "SUM": "{a} + {b}",
    "IDENTITY": "{a}",
}

#: Business field -> the keyword that writes it. Keys are normalised, so an IR
#: may name the field "Resolution", "resolution" or "Invoice Resolution".
WRITE_KEYWORD_BY_FIELD = {
    "status": "Update Invoice Status",
    "resolution": "Update Invoice Resolution",
}


def _normalise_field(name: str) -> str:
    """Casing and separators are presentation, not identity."""
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


SYSTEM_PROMPT_TEMPLATE = """You are the IR-to-Robot task-composer for a Finance invoice-review automation.

You will be given a canonical process IR (JSON). Your ONLY job: produce an
ordered list of keyword calls that realize the IR's `tasks` (NOT its
decisions/rules -- those are compiled separately and deterministically).

Return ONLY a JSON object of this exact shape, no markdown fences, no commentary:
{{
  "steps": [
    {{
      "keyword": "<one of the allowed keyword names>",
      "args": ["<arg1>", "<arg2>"],
      "assignTo": "<variable name, only if the keyword returns a value>",
      "generatedFrom": ["<IR node id>", "..."]
    }}
  ]
}}

ALLOWED KEYWORDS (call ONLY these, by these exact names):
{catalog}

RULES:
1. Cover every BusinessTask in the IR's `tasks` array, in an order that
   respects each task's `dependsOn`.
2. When an arg refers to an IR Variable's value, write it as a Robot
   variable reference using that Variable's `name` field, e.g. "${{invoiceId}}"
   -- never invent a new variable name, and never hardcode a literal
   invoice id or PO number anywhere.
3. When an arg is a literal field name (e.g. which column to read), use
   the exact `field` string from that task's matching implementationHints
   entry (operation READ_CELL) -- do not paraphrase it.
3a. When a task's LOOKUP_ROW hint carries a `key`, that literal identifies
   WHICH ROW the task reads, and it is the argument the keyword wants -- for
   example Read Processing Rule Value takes the rule's name, which is the
   `key`, never the name of the column or of the IR field.
4. Every step's "generatedFrom" must list the specific IR Task id (and,
   where relevant, Variable/Entity ids) that justify that call existing.
5. Only set "assignTo" for a keyword whose catalog entry says returns:
   true, and its value must be the exact `name` of the IR Variable this
   task produces (check the task's `outputs` and the matching Variable's
   `name`).
6. Do not add any step for a task's SELECT_SHEET/LOOKUP_ROW/WRITE_CELL
   implementationHints beyond what the matching Locate/Read keyword
   already covers -- one keyword call per meaningful read, not one per
   raw UI action.
7. Do not emit anything for Decisions, BusinessRules, or Computations --
   omit them entirely; they are handled elsewhere.
"""


def _build_system_prompt() -> str:
    catalog_lines = []
    for name, spec in KEYWORD_CATALOG.items():
        ret = " -> returns a value (needs assignTo)" if spec["returns"] else ""
        catalog_lines.append(f"- {name}({', '.join(spec['args'])}){ret}")
    return SYSTEM_PROMPT_TEMPLATE.format(catalog="\n".join(catalog_lines))


def build_initial_messages(ir: dict) -> list[dict]:
    return [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": json.dumps(ir, indent=2)},
    ]


def _index_by_id(items):
    return {item["id"]: item for item in items}


def validate_task_steps(steps_doc: dict, ir: dict) -> list[str]:
    """Deterministic validation of the LLM's structured step list, before
    any rendering happens. Returns a list of error strings (empty = valid).
    """
    errors = []
    if not isinstance(steps_doc, dict) or "steps" not in steps_doc:
        return ["Response must be a JSON object with a 'steps' array"]

    all_ids = set()
    for coll in ("entities", "entityReferences", "variables", "tasks", "computations", "rules", "decisions"):
        all_ids.update(item["id"] for item in ir.get(coll, []))

    variable_names = {v["name"] for v in ir.get("variables", [])}
    task_ids_covered = set()

    for i, step in enumerate(steps_doc["steps"]):
        path = f"steps[{i}]"
        keyword = step.get("keyword")
        if keyword not in KEYWORD_CATALOG:
            errors.append(f"{path}: keyword '{keyword}' is not in the allowed catalog")
            continue

        spec = KEYWORD_CATALOG[keyword]
        args = step.get("args", [])
        if len(args) != len(spec["args"]):
            errors.append(f"{path}: keyword '{keyword}' expects {len(spec['args'])} args, got {len(args)}")

        for arg in args:
            if isinstance(arg, str) and arg.startswith("${") and arg.endswith("}"):
                var_name = arg[2:-1]
                if var_name not in variable_names:
                    errors.append(f"{path}: arg '{arg}' references an unknown Variable name '{var_name}'")

        if spec["returns"] and not step.get("assignTo"):
            errors.append(f"{path}: keyword '{keyword}' returns a value but 'assignTo' is missing")
        if not spec["returns"] and step.get("assignTo"):
            errors.append(f"{path}: keyword '{keyword}' does not return a value but 'assignTo' was set")
        if step.get("assignTo") and step["assignTo"] not in variable_names:
            errors.append(f"{path}: assignTo '{step['assignTo']}' does not match any declared Variable name")

        generated_from = step.get("generatedFrom", [])
        if not generated_from:
            errors.append(f"{path}: 'generatedFrom' must not be empty")
        for ref_id in generated_from:
            if ref_id not in all_ids:
                errors.append(f"{path}: generatedFrom id '{ref_id}' does not match any IR node")
            if ref_id.startswith("TASK-"):
                task_ids_covered.add(ref_id)

    # A task that only WRITES is realized by the decision block, not by a step:
    # the prompt forbids the model from emitting anything for decisions, so the
    # write is rendered from each outcome's writeAction. Demanding a step for it
    # is unsatisfiable -- the model's only escape is to invent a keyword, which
    # is exactly what it tried. The task is covered; just not from here.
    def _is_write_only(task: dict) -> bool:
        ops = {h.get("operation") for h in task.get("implementationHints", [])}
        return "WRITE_CELL" in ops and not (ops & {"READ_CELL", "CHECK_EXISTS"})

    written_fields = {
        (o["writeAction"]["entity"], o["writeAction"]["field"])
        for d in ir.get("decisions", []) for o in d.get("outcomes", [])
        if o.get("writeAction")
    }

    for task in ir.get("tasks", []):
        if task["id"] in task_ids_covered:
            continue
        if _is_write_only(task):
            fields = {(task["entity"], h["field"])
                      for h in task.get("implementationHints", [])
                      if h.get("operation") == "WRITE_CELL" and h.get("field")}
            if fields <= written_fields:
                continue
            errors.append(
                f"IR BusinessTask '{task['id']}' writes {sorted(f for _, f in fields)} but no "
                f"decision outcome writes those fields, so nothing will ever perform it")
            continue
        errors.append(f"IR BusinessTask '{task['id']}' is not covered by any generated step")

    return errors


def _render_step_line(step: dict) -> str:
    if step.get("assignTo"):
        prefix = f"    ${{{step['assignTo']}}}=    "
    else:
        prefix = "    "
    arg_str = "    ".join(step["args"])
    return f"{prefix}{step['keyword']}    {arg_str}".rstrip()


def _steps_by_task_id(task_steps: list[dict]) -> dict:
    """Groups the LLM's composed steps by the BusinessTask each one realizes."""
    mapping = {}
    for step in task_steps:
        for ref in step.get("generatedFrom", []):
            if ref.startswith("TASK-"):
                mapping.setdefault(ref, []).append(step)
    return mapping


class _ProcessRenderer:
    """Renders tasks, computations and decisions into correctly nested Robot
    control flow.

    Two problems this solves that a flat "render every decision as its own
    IF block" approach cannot:

    1. **Early exit.** Once a decision writes an outcome, later decisions
       must not run and overwrite it. Each non-exhaustive decision (one
       that only declares an outcome for one side of its rule) therefore
       nests everything that follows inside its ELSE branch.

    2. **Gated tasks.** A task whose `dependsOn` chain leads back to a task
       that an earlier decision guards must not run before that guard
       passes. Concretely: if the purchase order does not exist, the tasks
       that look up rows keyed by that PO number must never execute --
       they would fail on a row that isn't there. Tasks are therefore
       scheduled by readiness (all `dependsOn` already rendered) at each
       nesting level, rather than all emitted up front.

    Both behaviours are derived from the IR's own dependency graph, not
    from anything invoice-specific.
    """

    def __init__(self, ir: dict, task_steps: list[dict]):
        self.ir = ir
        self.variables = _index_by_id(ir.get("variables", []))
        self.computations = ir.get("computations", [])
        self.computations_by_id = _index_by_id(self.computations)
        self.rules = _index_by_id(ir.get("rules", []))
        self.tasks = ir.get("tasks", [])
        self.tasks_by_id = _index_by_id(self.tasks)
        self.steps_by_task = _steps_by_task_id(task_steps)
        self.invoice_id_var = next(
            v["name"] for v in ir["variables"] if v["producedBy"] == "PROCESS_INPUT"
        )
        self.rendered_tasks = set()
        self.rendered_comps = set()
        self.lineage = []

    # --- dependency resolution -------------------------------------------

    def _required_tasks_for_ref(self, ref: str) -> set:
        """Which BusinessTasks must have run before this VAR-/COMP- id holds a value."""
        if ref.startswith("COMP-"):
            out = set()
            for inp in self.computations_by_id[ref]["inputs"]:
                out |= self._required_tasks_for_ref(inp)
            return out
        producer = self.variables[ref]["producedBy"]
        if producer.startswith("TASK-"):
            return {producer}
        if producer.startswith("COMP-"):
            return self._required_tasks_for_ref(producer)
        return set()  # PROCESS_INPUT

    def _transitive_deps(self, task_id: str, seen=None) -> set:
        """All tasks this one depends on, directly or indirectly."""
        if seen is None:
            seen = set()
        for dep in self.tasks_by_id.get(task_id, {}).get("dependsOn", []):
            if dep not in seen:
                seen.add(dep)
                self._transitive_deps(dep, seen)
        return seen

    def _decision_required_tasks(self, decision: dict) -> set:
        """Tasks producing the values this decision's rule(s) compare."""
        required = set()
        for outcome in decision["outcomes"]:
            cond = outcome.get("condition")
            if not cond:
                continue
            rule_cond = self.rules[cond["ref"]]["condition"]
            for side in ("left", "right"):
                if "ref" in rule_cond[side]:
                    required |= self._required_tasks_for_ref(rule_cond[side]["ref"])
        return required

    def _flow_order(self) -> list[str] | None:
        """Task ids in the order the IR DECLARES, or None if it declares none.

        When the IR carries a flow block, that order was observed across the
        recordings and merged; there is nothing left to infer. Preferring it
        removes the whole `_is_gated_by` heuristic from the picture, and with it
        a class of failure where two decisions descending from a common ancestor
        each appear to be waiting on the other.
        """
        flow = self.ir.get("flow")
        if not flow or not flow.get("nodes"):
            return None
        known = {t["id"] for t in self.tasks}
        return [n["ref"] for n in flow["nodes"] if n.get("ref") in known]

    def _is_gated_by(self, task: dict, pending_decisions: list) -> bool:
        """True if this task must wait behind a decision that hasn't run yet.

        `dependsOn` alone only says "needs that task's output". It does not
        say "must not run unless that task's check passed". The distinction
        matters: a task keyed on a purchase-order number depends on the task
        that looked the PO up, AND must not run at all if that lookup found
        nothing. That guard relationship is derived here -- if a pending
        decision tests a value produced by something this task depends on,
        the task belongs inside that decision's continuation branch.
        """
        deps = self._transitive_deps(task["id"])
        for decision in pending_decisions:
            guard_tasks = self._decision_required_tasks(decision)
            # A task that the decision itself needs is not gated by it.
            if task["id"] in guard_tasks:
                continue
            # The task must depend on EVERYTHING the decision needs, not merely
            # share one dependency with it. Sharing a dependency is ordinary --
            # nearly every task descends from the invoice read -- and treating
            # that as a guard relationship deadlocks any IR where two decisions
            # both descend from a common ancestor: each task looks gated behind
            # the other decision, so neither can ever be scheduled.
            if guard_tasks and guard_tasks <= deps:
                return True
        return False

    def _comp_is_ready(self, comp: dict) -> bool:
        for inp in comp["inputs"]:
            if inp.startswith("COMP-") and inp not in self.rendered_comps:
                return False
            if not self._required_tasks_for_ref(inp) <= self.rendered_tasks:
                return False
        return True

    # --- expression helpers ----------------------------------------------

    def _operand_expr(self, operand: dict) -> str:
        if "ref" in operand:
            return f"${{{self.variables[operand['ref']]['name']}}}"
        value = operand["value"]
        # Robot's IF evaluates a Python expression, so booleans must be
        # rendered Python-style (True/False), not JSON-style (true/false).
        if isinstance(value, bool):
            return "True" if value else "False"
        return json.dumps(value)

    def _condition_expr(self, rule_id: str, equals: bool) -> str:
        rule = self.rules[rule_id]
        cond = rule["condition"]
        expr = f"{self._operand_expr(cond['left'])} {cond['operator']} {self._operand_expr(cond['right'])}"
        return expr if equals else f"not ({expr})"

    def _write_keyword_for(self, decision: dict, index: int, write: dict) -> str:
        # Matched on the normalised name. Field names in the IR are chosen by
        # whoever built it -- a pipeline that discovers its own vocabulary will
        # quite reasonably call the verdict column "Resolution" rather than
        # "resolution", and refusing that would make the keyword layer usable
        # only by an IR that happened to guess this table's capitalisation.
        field = write["field"]
        keyword = WRITE_KEYWORD_BY_FIELD.get(_normalise_field(field))
        if keyword is None:
            raise ValueError(
                f"Decision '{decision['id']}' outcome {index} writes field '{field}', "
                f"but no Layer 2 keyword is registered for that field "
                f"(known: {list(WRITE_KEYWORD_BY_FIELD)}). Add one to library.resource "
                f"and WRITE_KEYWORD_BY_FIELD before compiling this IR."
            )
        return keyword

    # --- rendering --------------------------------------------------------

    def _render_ready_items(self, lines: list, indent: str, pending_decisions: list) -> None:
        """Emits every task/computation whose dependencies are now satisfied
        AND which is not gated behind a decision that hasn't run yet. Loops
        to a fixed point since rendering one item can unlock another."""
        declared = self._flow_order()
        if declared is not None:
            # Flow-driven: emit every declared task, in the declared order,
            # before the decisions that consume them. The recordings show the
            # operator gathering what they need and then deciding, and the one
            # early exit -- a purchase order that does not exist -- is carried
            # by the flow node's onNotFound rather than derived from
            # dependencies.
            by_id = {t["id"]: t for t in self.tasks}
            for task_id in declared:
                task = by_id.get(task_id)
                if task is None or task_id in self.rendered_tasks:
                    continue
                for step in self.steps_by_task.get(task_id, []):
                    lines.append(indent + _render_step_line(step).lstrip())
                    self.lineage.append({
                        "keyword": step["keyword"], "args": step["args"],
                        "generatedFrom": [f"IR:{ref}" for ref in step["generatedFrom"]],
                        "lineageSource": "LLM",
                    })
                self.rendered_tasks.add(task_id)
                self._render_ready_comps(lines, indent)
            return

        progress = True
        while progress:
            progress = False
            for task in self.tasks:
                if task["id"] in self.rendered_tasks:
                    continue
                if not all(d in self.rendered_tasks for d in task.get("dependsOn", [])):
                    continue
                if self._is_gated_by(task, pending_decisions):
                    continue
                for step in self.steps_by_task.get(task["id"], []):
                    lines.append(indent + _render_step_line(step).lstrip())
                    self.lineage.append({
                        "keyword": step["keyword"],
                        "args": step["args"],
                        "generatedFrom": [f"IR:{ref}" for ref in step["generatedFrom"]],
                        "lineageSource": "LLM",
                    })
                self.rendered_tasks.add(task["id"])
                progress = True

            if self._render_ready_comps(lines, indent):
                progress = True

    def _render_ready_comps(self, lines: list, indent: str) -> bool:
        """Emit every computation whose inputs are now available. Returns True
        if anything was emitted, so the caller's fixed-point loop can continue."""
        emitted = False
        while True:
            round_emitted = False
            for comp in self.computations:
                if comp["id"] in self.rendered_comps or not self._comp_is_ready(comp):
                    continue
                template = FUNCTION_TEMPLATES[comp["function"]]
                input_names = [self.variables[v]["name"] for v in comp["inputs"]]
                subs = {"a": f"${{{input_names[0]}}}"}
                if len(input_names) > 1:
                    subs["b"] = f"${{{input_names[1]}}}"
                expr = template.format(**subs)
                out_name = self.variables[comp["output"]]["name"]
                lines.append(f"{indent}${{{out_name}}}=    Evaluate    {expr}")
                self.lineage.append({
                    "keyword": "Evaluate",
                    "args": [expr],
                    "generatedFrom": [f"IR:{comp['id']}"]
                    + [f"IR:{v}" for v in comp["inputs"]]
                    + [f"IR:{comp['output']}"],
                    "lineageSource": "DETERMINISTIC_COMPILER",
                })
                self.rendered_comps.add(comp["id"])
                round_emitted = emitted = True
            if not round_emitted:
                return emitted

    def _emit_write(self, lines, indent, decision, index, outcome, extra_refs=()):
        write = outcome["writeAction"]
        keyword = self._write_keyword_for(decision, index, write)
        lines.append(f"{indent}{keyword}    ${{{self.invoice_id_var}}}    {write['value']}")
        self.lineage.append({
            "keyword": keyword,
            "args": [f"${{{self.invoice_id_var}}}", write["value"]],
            "generatedFrom": [f"IR:{decision['id']}"] + [f"IR:{r}" for r in extra_refs],
            "lineageSource": "DETERMINISTIC_COMPILER",
        })

    def render(self, lines: list, indent: str, pending_decisions: list) -> None:
        self._render_ready_items(lines, indent, pending_decisions)

        if not pending_decisions:
            return

        decision, rest = pending_decisions[0], pending_decisions[1:]
        outcomes = decision["outcomes"]

        conditioned = [o for o in outcomes if o.get("condition")]
        has_fallback = any(o.get("condition") is None for o in outcomes)
        rule_refs = {o["condition"]["ref"] for o in conditioned}
        exhaustive = (
            len(rule_refs) == 1
            and {o["condition"]["equals"] for o in conditioned} == {True, False}
        )

        required = self._decision_required_tasks(decision)
        if not required <= self.rendered_tasks:
            missing = sorted(required - self.rendered_tasks)
            raise ValueError(
                f"Decision '{decision['id']}' needs values from task(s) {missing}, but they "
                f"are not reachable at this point in the process. Check the IR's dependsOn "
                f"graph and the order of the decisions array."
            )

        if exhaustive or has_fallback:
            # Terminal decision: every path writes an outcome, so nothing
            # after it can run. Rendered as a flat IF/ELSE chain.
            for i, outcome in enumerate(outcomes):
                cond = outcome.get("condition")
                if i == 0:
                    header = (
                        f"{indent}IF    {self._condition_expr(cond['ref'], cond['equals'])}"
                        if cond else f"{indent}IF    True"
                    )
                    lines.append(header)
                elif cond and i < len(outcomes) - 1:
                    lines.append(f"{indent}ELSE IF    {self._condition_expr(cond['ref'], cond['equals'])}")
                else:
                    lines.append(f"{indent}ELSE")
                self._emit_write(
                    lines, indent + "    ", decision, i, outcome,
                    extra_refs=[cond["ref"]] if cond else [],
                )
            lines.append(f"{indent}END")
            return

        # Non-exhaustive decision: it only writes on one side of its rule.
        # The other side is the "keep going" path -- everything that follows
        # nests inside it, which is what gives early-exit semantics and
        # keeps gated tasks from running when the guard fails.
        outcome = conditioned[0]
        cond = outcome["condition"]
        lines.append(f"{indent}IF    {self._condition_expr(cond['ref'], cond['equals'])}")
        self._emit_write(lines, indent + "    ", decision, 0, outcome, extra_refs=[cond["ref"]])
        lines.append(f"{indent}ELSE")
        self.render(lines, indent + "    ", rest)
        lines.append(f"{indent}END")


def render_robot_script(ir: dict, task_steps: list[dict], resource_path: str = "../library.resource") -> tuple[str, str, list[dict]]:
    """Deterministically renders the generated process as TWO files, plus
    a full script-level lineage list (LLM-composed task steps + the
    deterministically-compiled decision block):

      - a .resource file (Settings + Keywords only) -- reusable, and
        importable from other suites, e.g. a batch runner or the
        execution-correctness evaluation harness.
      - a .robot file (imports that resource + one runnable *** Tasks ***
        entry point) -- a Robot file with a Tasks section cannot itself
        be imported as a Resource, hence the split.

    Returns (resource_text, robot_text, lineage).
    """
    invoice_id_var = next(v["name"] for v in ir["variables"] if v["producedBy"] == "PROCESS_INPUT")

    resource_lines = [
        "*** Settings ***",
        f"Resource    {resource_path}",
        "",
        "*** Keywords ***",
        "Process Invoice Exception Review",
        f"    [Arguments]    ${{{invoice_id_var}}}",
    ]

    # Tasks, computations and decisions are emitted together by the
    # renderer, because where a task belongs depends on which decision (if
    # any) gates it -- they cannot be laid out as two independent blocks.
    renderer = _ProcessRenderer(ir, task_steps)
    renderer.render(resource_lines, "    ", ir.get("decisions", []))
    lineage = renderer.lineage

    robot_lines = [
        "*** Settings ***",
        "Resource    invoice_review.resource",
        "Suite Setup    Open Sheet Application",
        "Suite Teardown    Close Sheet Application",
        "",
        "*** Tasks ***",
        "Process Invoice Exception Review Task",
        "    [Documentation]    Run with: robot --variable INVOICE_ID:<id> <this file>",
        f"    Process Invoice Exception Review    ${{INVOICE_ID}}",
    ]

    return "\n".join(resource_lines) + "\n", "\n".join(robot_lines) + "\n", lineage


def dry_run_robot_file(robot_path: Path) -> tuple[bool, str]:
    """Runs `robot --dryrun` against the rendered file. Returns (ok, output)."""
    result = subprocess.run(
        [sys.executable, "-m", "robot", "--dryrun", "--outputdir", str(robot_path.parent / "_dryrun"), str(robot_path)],
        capture_output=True, text=True,
    )
    ok = result.returncode == 0
    return ok, result.stdout + result.stderr


def _dryrun_signature(output: str) -> str:
    """A dry-run failure stripped of what changes between runs.

    Paths, timings and run ids differ every time; the complaint does not. Only
    the complaint distinguishes "the model wrote something different and it
    still failed the same way" from "the model fixed it".
    """
    lines = [ln.strip() for ln in output.splitlines()
             if ln.strip() and not ln.startswith(("Output:", "Log:", "Report:", "="))
             and "/" not in ln.split(":")[0]]
    return "\n".join(sorted(set(lines)))


def compile_ir_to_robot(ir: dict, llm_client, out_dir: Path, max_attempts: int = 3,
                         resource_path: str = "../library.resource") -> dict:
    compilation_id = f"ROBOTGEN-{uuid.uuid4().hex[:12]}"
    messages = build_initial_messages(ir)
    attempts = []
    previous_dryrun_signature = None

    for attempt_num in range(1, max_attempts + 1):
        raw = llm_client.complete(messages)
        try:
            steps_doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            attempts.append({"attempt": attempt_num, "rawResponse": raw, "errors": [f"Invalid JSON: {exc}"]})
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"Your response was not valid JSON: {exc}. Return ONLY the JSON object."})
            continue

        errors = validate_task_steps(steps_doc, ir)
        if errors:
            attempts.append({"attempt": attempt_num, "rawResponse": raw, "errors": errors})
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "Fix these issues and return the corrected, complete JSON object again:\n" + "\n".join(f"- {e}" for e in errors)})
            continue

        try:
            resource_text, robot_text, lineage = render_robot_script(ir, steps_doc["steps"], resource_path=resource_path)
        except ValueError as exc:
            attempts.append({"attempt": attempt_num, "rawResponse": raw, "errors": [str(exc)]})
            return {"compilationId": compilation_id, "success": False, "attempts": attempts, "robotScript": None, "lineage": None}

        out_dir.mkdir(parents=True, exist_ok=True)
        resource_out_path = out_dir / "invoice_review.resource"
        robot_path = out_dir / "invoice_review.robot"
        resource_out_path.write_text(resource_text)
        robot_path.write_text(robot_text)

        dryrun_ok, dryrun_output = dry_run_robot_file(robot_path)
        attempts.append({
            "attempt": attempt_num,
            "rawResponse": raw,
            "errors": [] if dryrun_ok else [f"robot --dryrun failed:\n{dryrun_output}"],
            "dryRunOutput": dryrun_output,
        })

        if not dryrun_ok:
            # A dry-run failure has two possible causes, and they need opposite
            # responses. If the step list is wrong -- a misspelled keyword, the
            # wrong argument count -- re-prompting fixes it. If the RENDERER
            # produced something Robot rejects, the model did not write that
            # part and cannot influence it: re-prompting burns the remaining
            # attempts and then reports "the model failed", when the fault was
            # ours. An identical failure twice running is the signature of the
            # second case, because the model did change its answer and nothing
            # downstream changed with it.
            signature = _dryrun_signature(dryrun_output)
            if signature == previous_dryrun_signature:
                attempts[-1]["compilerFault"] = True
                attempts[-1]["errors"] = [
                    "The rendered script failed --dryrun identically twice, on either side "
                    "of a changed step list. That points at the renderer, not the model:\n"
                    + dryrun_output]
                return {"compilationId": compilation_id, "success": False,
                        "attempts": attempts, "robotScript": None, "lineage": None}
            previous_dryrun_signature = signature
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"The rendered Robot script failed --dryrun:\n{dryrun_output}\nReconsider your steps and return corrected JSON."})
            continue

        (out_dir / "lineage.json").write_text(json.dumps(lineage, indent=2))
        return {
            "compilationId": compilation_id,
            "success": True,
            "attempts": attempts,
            "robotScript": str(robot_path),
            "lineage": lineage,
        }

    return {"compilationId": compilation_id, "success": False, "attempts": attempts, "robotScript": None, "lineage": None}
