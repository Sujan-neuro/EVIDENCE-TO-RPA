# Archive

Kept for reference. Nothing here is part of the working pipeline.

## `ir.v2.json`

An earlier canonical IR, superseded by what the pipeline emits today.

It was rendered from a model output this repository no longer carries (run
output is gitignored), so regenerate a run to compare against it. This one was rendered before four defects in the emitter were fixed,
and **it does not validate or compile**:

| | `ir.v2.json` | the working IR |
|---|---|---|
| passes `ir/validator.py` | no — a decision carries no `provenance` | yes |
| the purchase-order rule compares against | `False` | `True` |
| reference-table reads pinned to their row | no | yes |
| declares a `flow` block | yes, naming a task it does not define | no, deliberately |

Two of those change behaviour rather than merely failing a check. The rule is
written in the failing direction while the decision routes on failure, so the
two negations cancel and an invoice whose purchase order **is** found would be
sent to manual review. And the unsplit reference-table read asks the Processing
Rules sheet for a field name rather than a rule name, so it finds nothing.

Do not use it as an input. To see the IR this repository actually produces:

```bash
.venv/bin/python pipeline/run_pipeline.py evidence/recorded --no-execute
.venv/bin/python ir/validator.py runs/run1/ir.json --evidence evidence
```
