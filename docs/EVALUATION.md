# EVALUATION.md — Medical-QA evaluation + regression framework (P16)

**Version:** `p16-evaluation-v1` | **Migration:** `16.0.0` (`evaluation_runs`,
`evaluation_cases`) | **Module:** `medforge/evaluation.py` | **Tests:**
`tests/test_evaluation.py` (29)

## Why

MedForge already *produces* the signals worth measuring — P4 verification
verdicts per claim, deterministic abstention when no evidence exists, P9
canonical content where every fact carries `[S#]` refs, rendered artifacts with
checksums. What was missing was a repeatable, deterministic way to **measure**
those behaviours, store the measurements, and notice when they get worse.

This is **not** a claim of medical accuracy. It measures MedForge's own
conservatism and grounding on a small synthetic golden set, offline and with no
model calls.

## What it measures

| Case kind | Exercised engine | Assertions |
| --- | --- | --- |
| `claim_status` | P4 `extract_claims` → `store_claims` → `store_evidence` → `verify_claim` with an explicit **stub verdict** | expected aggregate status (`SUPPORTED` / `PARTIALLY_SUPPORTED` / `CONTRADICTED`), relationship count, model-call bound |
| `abstention` | P4 `verify_claim(candidates=[])` | `INSUFFICIENT_EVIDENCE`, **zero** model calls, latest run method `deterministic-gate` |
| `canonical` | P9 `generate_canonical_content(..., allow_model=False)` | every fact cited, refs ⊆ known labels, minimum fact count, generation mode |
| `product` | P9 `render_study_products` + `content_consistency_report` | artifacts exist with matching checksums, no unknown citation labels, consistency true |

Metrics are **contribution-based**: a case contributes only to the metrics it
actually measures (`abstention_accuracy`, `support_rate`, `contradiction_recall`,
`citation_continuity_rate`, `grounding_rate`, `consistency_rate`), and a metric
is only reported when at least one case measured it. No number is invented.

## How a run is judged

A run is `failed` when **any** executed case fails, **any** configured threshold
is violated, or a **regression** is detected against the previous run of the
same suite (a case that passed in the baseline and fails now). Case statuses are
`passed` / `failed` / `skipped` (a case may declare `"enabled": false`).

Thresholds default to `1.0` for `pass_rate` and for every metric a run
actually measured.

## Safety properties

- **Sandboxed**: cases execute in a throwaway home, so fixture claims and
  artifacts can never enter the learner's real evidence graph.
- **No model calls**: `allow_model=False` everywhere; claim cases use explicit
  stub verdicts. The OpenAI/cloud path is never exercised or simulated.
- **Data, not code**: golden sets are JSON that is parsed and validated, never
  executed; malformed cases are rejected at load time.
- **Read-only live measurement**: `snapshot_metrics()` is the only function that
  touches the live home, and it never writes.

## Usage

```bash
medforge evaluate                             # run the builtin golden set
medforge evaluate run my-golden.json          # run your own suite
medforge evaluate status                      # thresholds + latest run
medforge evaluate list [n]                    # recent runs
medforge evaluate show <run_id>               # run + per-case rows
medforge evaluate compare <run_id>            # diff vs. previous run
medforge evaluate compare <run_id>|<baseline> # diff vs. an explicit run
medforge evaluate snapshot                    # read-only live-home metrics
medforge evaluate sample out.json             # export the suite to extend
```

Dashboard: the **EVALUATION** tab shows stored-run counts, the last status and
pass rate, the metric row, threshold failures, a *Run the builtin golden set*
button with per-case results, recent runs, and the read-only live-home snapshot.

Python:

```python
from medforge import evaluation as EV
EV.run_evaluation()            # persist + compare against the previous run
EV.evaluate_case(case)         # run one case, no persistence
EV.builtin_golden_set()        # the shipped suite
EV.compare_evaluations(run_id) # case-level diff
EV.snapshot_metrics()          # read-only live-home metrics
```

## When to run it

After any change to the evidence verifier (P4), the canonical content model or
renderer (P9), the assessment engine (P8), or the model router (P12) — in
particular after a provider or model change — and before publishing anything
that depends on those engines. A failing threshold or a regression should block
the change rather than be re-baselined.

## Honest limitations

- Small synthetic suite (7 cases, 4 kinds); extend it with
  `medforge evaluate sample`.
- **Not measured yet**: question quality, distractor quality, grading
  consistency, hallucination rate on fixtures, curriculum alignment as its own
  metric. Assessment behaviour is only touched indirectly.
- Deterministic/offline only, so model-assisted generation quality is out of
  scope; the cloud path is unexercised.
- Regression detection needs a prior run of the same suite; a first run cannot
  regress, and an already-failing case is reported as failing, not as a
  regression.
- Runs are not tagged with the model/provider that produced the content they
  measure.

Full execution evidence, the defect found during E2E and the complete
limitations list are in `docs/P16_MATRIX.md`.
