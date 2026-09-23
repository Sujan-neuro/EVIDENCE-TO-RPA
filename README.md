# evidence-to-rpa

**From human process evidence to executable robot automation.**

Turns recorded demonstrations of a business process into automation that runs in
a browser, by way of a canonical, evidence-backed process model.

```
  a person works, and talks           →  evidence/recorded/*.json
  narrations + events                 →  the process model (IR)
  the process model                   →  a Robot Framework script
  the script                          →  a real browser, against the real app
```

The model is given **no domain vocabulary**. No entity list, no sheet mapping,
no outcome enum. It is shown what the recordings contain and works out the
domain itself, and every claim it makes is resolved back against the recorded
events before the model is accepted.

---

## 1. Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/rfbrowser init          # downloads Playwright's browsers, one-time
```

### LLM configuration

The pipeline speaks the OpenAI-compatible chat-completions API over plain
HTTPS. There is no vendor SDK, so any endpoint offering that shape works.

```bash
cp .env.example .env
```

| Variable | Required | Notes |
|---|---|---|
| `LLM_BASE_URL` | yes | the OpenAI-compatible endpoint, ending at the version segment |
| `API_KEY` | yes | bearer token for that endpoint |
| `LLM_MODEL` | no | defaults to `qwen3.6-flash` |

No endpoint is hard-coded. An endpoint is infrastructure rather than
configuration: shipping one would publish where a particular workspace lives,
and would make pointing this at a different provider an edit to source.

`.env` is gitignored. You can also export the variables in your shell instead.

Check it works:

```bash
.venv/bin/python -c "from pipeline.llm_client import WorkspaceClient; print(WorkspaceClient().model)"
```

### The application

Everything else needs the sheet app running:

```bash
.venv/bin/python app/serve.py 8811
```

Then open <http://localhost:8811>. The Robot keyword library expects port
**8811** (`${BASE_URL}` in `robot/library.resource`).

---

## 2. The whole chain, one command

```bash
.venv/bin/python pipeline/run_pipeline.py evidence/recorded \
    --model qwen3.8-max \
    --scenario INV-95700:MANUAL_REVIEW \
    --scenario INV-94117:WAREHOUSE_REVIEW \
    --scenario INV-95600:PROCUREMENT_REVIEW
```

Recordings → IR → Robot script → dry run → live execution → summary.

Everything one invocation produces lands in **`runs/runN/`**, numbered in the
order runs happened:

```
runs/run2/
  hypothesis.json              what the narrations alone suggested
  ir.synthesis.json            the model's own output, evidence on every claim
  ir.synthesis.draft.json      the draft, before the extension rounds
  ir.json                      the canonical IR
  synthesis_report.json        every attempt, every rejection and its reason
  robot_compile_report.json    the composer's attempts
  generated/                   the .robot script, its keywords, and the lineage
  execution/<invoice>/         browser logs per scenario
  summary.json                 replay, validity, and business-outcome accuracy
```

The stages can also be run on their own, which is what the next two sections
describe — useful when iterating on one of them.

## 3. Evidence → IR

```bash
.venv/bin/python pipeline/synthesize_ir.py evidence/recorded \
    --seed-size 3 --extend-batch 2
```

Three passes, each a validate-and-correct loop rather than a single call:

| Pass | Given | Produces |
|---|---|---|
| **1 · Hypothesis** | every narration, **no events** | candidate entities, outcomes, rules — and what the narrations do *not* settle |
| **2 · Seed** | the hypothesis + a seed batch of full recordings | a draft IR, every claim citing recorded events |
| **3 · Extend** | the current IR + the next batch, **repeating** | the complete IR, once every recording is absorbed |

Options: `--seed-size` (recordings that seed the draft), `--extend-batch`
(recordings absorbed per extension round), `--max-attempts` (correction rounds
per pass), `--model`, `--out-dir`.

Everything lands in `runs/runN/`:

| File | What |
|---|---|
| `ir.json` | **the canonical IR** — what the next stage compiles |
| `ir.synthesis.json` | the same model in the evidence-carrying form |
| `ir.synthesis.draft.json` | the draft, before the extension rounds |
| `hypothesis.json` | what the narrations alone suggested |
| `synthesis_report.json` | every attempt, every rejection and its reason, the replay |

### What has to hold before an IR is accepted

- **Grounded** — every claim cites events as `RECORDING:EVENT_ID`, resolved
  against the actual recordings. A fabricated id is as well-formed as a real
  one; only resolution tells them apart.
- **Bound to the right cell** — claiming a field is a given column on a given
  sheet requires cited events that read *that* cell.
- **Coherent** — a percentage cannot be compared against a price, and a limit
  that was read on screen must cite the read rather than be retyped as a
  literal.
- **Not forgetful** — a later extension round cannot silently drop what an
  earlier one established.
- **Replayed** — the decision cascade is run against every recording absorbed so
  far, using only values that recording contains, and must reproduce the
  outcome the expert actually wrote down.

Failures come back to the model as specific corrections, never as a bare retry.

---

## 4. IR → Robot script

```bash
.venv/bin/python pipeline/compile_robot.py runs/runN/ir.json
```

The IR is validated first; an invalid one stops here. Then:

- **One model call** composes the ordered *read* steps, choosing only from a
  fixed catalogue of hand-written keywords. The write keywords are deliberately
  withheld from that catalogue, so it cannot express a decision.
- **Code compiles everything that decides anything** — every comparison, every
  branch, every value written back — straight from the IR. This is what makes a
  reversed operator impossible rather than merely unlikely.
- **`robot --dryrun` gates the result.** Nothing reaches a browser until the
  script parses and every keyword resolves. A failure returns the real parser
  error and the steps are composed again.

Written to `runs/runN/generated/`:

```
invoice_review.resource   the steps, as keywords
invoice_review.robot      the runnable entry point
verify_outcome.robot      runs it for one invoice and checks the resulting cell
lineage.json              every generated line → the IR node behind it
```

---

## 5. Live execution

With the app running on 8811:

```bash
.venv/bin/python -m robot \
    --variable INVOICE_ID:INV-96000 \
    --variable EXPECTED_STATUS:PROCUREMENT_REVIEW \
    --outputdir runs/runN/generated/run \
    runs/runN/generated/verify_outcome.robot
```

This drives the browser, then reads the resulting cell back and compares it.
The pass criterion is the **business outcome**, not a green test.

To watch it happen rather than run headless:

```bash
.venv/bin/python -m robot \
    --variable INVOICE_ID:INV-94117 --variable EXPECTED_STATUS:WAREHOUSE_REVIEW \
    --variable HEADLESS:False --variable SLOWMO:0.4s \
    --outputdir runs/runN/generated/run \
    runs/runN/generated/verify_outcome.robot
```

Invoices worth trying, one per outcome:

| Invoice | Expected | Why |
|---|---|---|
| `INV-95700` | `MANUAL_REVIEW` | its purchase order cannot be found |
| `INV-94117` | `WAREHOUSE_REVIEW` | invoiced more than was received |
| `INV-95600` | `PROCUREMENT_REVIEW` | price variance beyond tolerance |
| `INV-96000` | `PROCUREMENT_REVIEW` | within tolerance per unit, but over the cap in total |
| `INV-93821` | `RELEASED` | price difference inside tolerance |
| `INV-95500` | `RELEASED` | everything matches |

The hand-written keyword layer can also be exercised on its own, independently
of anything generated:

```bash
.venv/bin/python -m robot --outputdir /tmp/smoke robot/smoke_test.robot
```

---

## 6. Recording a demonstration

1. Start the app and open <http://localhost:8811>.
2. Press **Start Recording**. Every search, cell read, sheet change and cell
   write is captured, each stamped with the sheet it happened on.
3. Work the invoice as you normally would. Select a cell and press **Ctrl/Cmd+C**
   to record reading it; double-click to edit.
4. Write the verdict into the **Resolution** column — that write is the outcome
   the replay oracle will hold the generated rules to.
5. Press **Stop**, write the narration — say *why*, not what you clicked — and
   **Export**.
6. Save the file into `evidence/recorded/`.

A good corpus covers every outcome and exercises every processing rule. The
narration matters: it is the only input to pass 1, and it is where the reasoning
behind a decision lives.

### Replaying a run for a video

```bash
.venv/bin/python tools/build_replay.py runs/runN -o docs/replay.html
```

One self-contained page — data inlined, no server. Arrow keys step through the
passes; clicking any part of the IR highlights the recorded moments that
justify it. Filming a replay is better than filming a live run: a run is mostly
dead time waiting on the model, and it is not reproducible for a second take.

---

## 7. Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Mostly negative tests: each is a specific way an IR can be wrong while still
looking perfectly well formed. The IR→Robot tests include a real
`robot --dryrun`; no API key is needed, as model replies are canned.

---

## Repository

| Path | What |
|---|---|
| `app/` | the instrumented sheet application, and its evidence logger |
| `evidence/recorded/` | the demonstrations |
| `ir/schema.json` | the canonical IR — JSON Schema, draft 2020-12 |
| `ir/validator.py` | schema, then semantics, then provenance grounding |
| `pipeline/synthesis/` | evidence → IR: the corpus index, the contract, the replay oracle, the canonical emitter |
| `pipeline/run_pipeline.py` | **entry point for the whole chain**, one run directory per invocation |
| `pipeline/synthesize_ir.py` | entry point for evidence → IR on its own |
| `pipeline/ir_to_robot.py` | IR → Robot: the step composer and the deterministic compiler |
| `pipeline/compile_robot.py` | entry point for IR → Robot |
| `pipeline/llm_client.py` | the only place a model is called from |
| `robot/library.resource` | hand-written keywords over Browser/Playwright |
| `robot/smoke_test.robot` | proves that library works, independently of anything generated |
| `tools/build_replay.py` | turns a run into a page you can film |
| `pipeline/runs.py` | where a run's artefacts live, and how run directories are numbered |
| `runs/` | run output — gitignored, since every run is regenerable from the evidence |
| `docs/` | architecture diagrams |
| `archive/` | superseded artefacts, kept for reference — see `archive/README.md` |

## Known limits

- **The canonical schema is closed-world.** `ir/schema.json` enumerates the four
  business outcomes and the four sheet names. The pipeline discovers outcomes
  freely, but naming one differently would fail schema validation.
- **`price-within-tolerance` never reads the invoice's unit price**, so the
  percentage rule cannot be evaluated from it. It replays correctly, but for
  the wrong reason — the rule it is named after is not actually exercised by it.
- **No evaluation stage.** What runs today is verification — the replay oracle,
  three-stage IR validation, and business-outcome accuracy against named
  scenarios. Scoring a candidate IR against a reference is not implemented;
  `summary.json` is the place to attach it.
