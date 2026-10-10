# P16_MATRIX.md — Medical QA + Evaluation + Regression Framework

Status: **DONE** (`p16-evaluation-v1`, migration 16.0.0) — see §4 execution
results; gate PASS.

## 1. Audit (entry state)

- No evaluation code anywhere: `grep -ri "evaluation|golden"` over the release
  found zero matches before this phase.
- The system already *produces* the signals worth measuring:
  - P4 verification statuses per claim (SUPPORTED / PARTIALLY_SUPPORTED /
    UNSUPPORTED / CONTRADICTED / INSUFFICIENT_EVIDENCE) with verification runs;
  - deterministic abstention when no candidates exist (`verify_claim` →
    `INSUFFICIENT_EVIDENCE`, zero model calls, method `deterministic-gate`);
  - P9 canonical content where every fact carries `[S#]` refs, plus
    `content_consistency_report()`;
  - P9 rendered artifacts with checksums;
  - P10 approval gates that re-check citations/consistency/risk.
- Missing: a repeatable, deterministic way to *measure* those behaviours, store
  the measurements, and detect regressions between runs. Nothing in the loop
  compares "yesterday's quality" with "today's".

## 2. Design

### `medforge/evaluation.py` (evaluator + golden set + regression compare)

A **golden-set evaluator**: cases are plain JSON data (never executed as code),
each declaring its expectations. Four case kinds, all deterministic and offline:

| Kind | What it exercises (real engines) | Expectations |
| --- | --- | --- |
| `claim_status` | P4 `extract_claims` → `store_claims` → `store_evidence` → `verify_claim` with a *stub verdict* (`verifier_fn`) | expected status (SUPPORTED / PARTIALLY_SUPPORTED / CONTRADICTED), relationship count, model-call bound |
| `abstention` | P4 `verify_claim(candidates=[])` | `INSUFFICIENT_EVIDENCE`, zero model calls, latest run method `deterministic-gate` |
| `canonical` | P9 `generate_canonical_content(..., allow_model=False)` (deterministic fallback) | every fact cited, refs ⊆ known labels, minimum fact count, generation mode |
| `product` | P9 `render_study_products` + `content_consistency_report` | artifacts exist with matching checksums, no unknown citation labels, consistency true |

Metrics are **contribution-based**: a case contributes a value only for the
metrics it actually measures (`abstention_accuracy`, `grounding_rate`,
`support_rate`, `contradiction_recall`, `citation_continuity_rate`,
`consistency_rate`); the run reports the mean over contributing cases plus
per-kind counts. No metric is invented when no case measured it.

Run status: `failed` when any executed case fails, any threshold is violated, or
a **regression** is detected against the previous run of the same suite
(baseline case passed → now fails). Case statuses: `passed` / `failed` /
`skipped` (explicit `"enabled": false`).

Persistence (V16): `evaluation_runs` (one row per run: pass/fail counts,
metrics, thresholds, golden digest, baseline + regression count) and
`evaluation_cases` (per-case status, metrics, failures). Read-only
`snapshot_metrics()` measures the live home (claims by status, grounded claims,
canonical content/artifact integrity) without writing anything.

Honesty rules, carried from the global brief:
- evaluation never calls a model by default; `allow_model=False` everywhere;
- the OpenAI path stays unexercised (no key) and is never simulated;
- cases are untrusted data (invariant 7): parsed, validated, never executed;
- `builtin_golden_set()` ships a small hermetic suite; `write_sample_golden_set()`
  exports it for users to extend; malformed cases are rejected at load time.

### CLI

`medforge evaluate run [file] | status | list | show <run_id> |
compare <run_id>[|<baseline>] | snapshot | sample <path>`

### No schema change concern

V16 is additive: two new tables, chained from V15, verified backup, integrity +
foreign-key checks, idempotent — exactly the P13–P15 runner pattern.

## 3. Acceptance criteria (to be proved by tests)

1. V16 schema applies fresh and idempotently; backup + integrity + FK clean.
2. Builtin golden set passes end-to-end on a fixture home (all four kinds).
3. Stub verdicts drive P4 to the expected aggregate statuses; contradictory
   evidence is never silently reported as supported.
4. Abstention cases assert `INSUFFICIENT_EVIDENCE`, zero model calls and the
   `deterministic-gate` run method.
5. Missing verifier + present evidence degrades to abstention (conservative).
6. Canonical/product cases produce cited, checksum-consistent artifacts.
7. A failing case (wrong expectation) yields `failed` with a concrete reason;
   thresholds are enforced and reported.
8. Regression detection: baseline pass → new fail is flagged and fails the run.
9. `snapshot_metrics()` works on empty and populated homes, read-only.
10. Malformed golden files are rejected with a precise error.
11. CLI `evaluate` runs/status/list/show/compare/snapshot/sample.

## 4. Execution results

- **Tests**: 29 new in `tests/test_evaluation.py` (26 evaluator cases + 3 CLI
  regression cases) — all passing.
- **Full regression**: **422 passed, 1 skipped** (393 + 29; the skip is the
  pre-existing live-Ollama integration marker).
- **V16 migration** on the fresh isolated home `/tmp/mf-p16-demo`: chain applied
  `4.0.0 → … → 15.0.0 → 16.0.0`, `integrity_check: ok`, `foreign_key_check: 0`
  errors, 54 → **56 tables**, `evaluation_runs` + `evaluation_cases` present.
  Re-running `migrate_v16.py` twice more (with and without `--no-backup`) was a
  clean no-op; verified snapshot written to
  `backups/medforge_pre_v16_backup_20261010T112931.db`.
- **CLI E2E** (real entry point, `MEDFORGE_HOME=/tmp/mf-p16-demo`):
  - `evaluate sample /tmp/mf-p16-demo/golden.json` → writes the 7-case suite;
  - `evaluate run` on a bare home → `7/7 passed`, digest `87497bc1635331e0`;
  - `evaluate run /tmp/mf-p16-demo/golden.json` → `7/7 passed`, `regressions []`;
  - `evaluate list` / `show <run_id>` → run + per-case rows;
  - `compare <run_id>` → baseline `eval-c91051e8cde4`, `regressions []`,
    `pass_rate_delta 0.0`, all metric deltas `0.0`;
  - `evaluate snapshot` → live-home metrics (read-only);
  - `evaluate show eval-doesnotexist` → exit `1`, `Unknown evaluation run`.
- **Dashboard**: the new **EVALUATION** tab (14 tabs total) renders the stored-run
  metrics, runs the builtin suite from the button (`7/7 passed · status PASSED ·
  digest 87497bc1635331e0`, all seven case lines listed, recent-runs list grew
  2 → 3) and opens the read-only live-home snapshot; no console errors.
- **Defect found by the E2E and fixed**: `medforge_core.py`'s `video probe`
  branch (P11) re-imported `from pathlib import Path` **inside** `main()`, which
  made `Path` function-local for the entire function — so every branch that
  touched `Path` earlier in the body raised
  `UnboundLocalError: cannot access local variable 'Path'`. `evaluate sample
  <path>` and `evaluate run <file>` therefore died immediately (exit 1). The
  redundant local import was removed (the module-level import covers it), and
  three CLI regression tests now drive the real entry point in a subprocess with
  an isolated home so the class of defect cannot return silently.
- **P16 gate**: **PASS** — the evaluation framework exists, measures the real
  P4/P9 behaviour deterministically offline, persists runs, detects regressions
  and is exercised through CLI and dashboard.

## 5. Honest limitations

- **This is not a claim of clinical or 100% medical accuracy.** The suite
  measures MedForge's *behaviour* — abstention, support/contradiction handling,
  grounding, citation continuity and artifact consistency — on a small synthetic
  golden set. No clinician adjudication, no external benchmark, no medical
  ground truth beyond the fixtures.
- **Metrics from the brief are only partially covered by `p16-evaluation-v1`.**
  `unsupported claim rate`, `abstention behaviour`, `contradiction handling`,
  `evidence support`, `citation continuity` and `curriculum alignment` (via the
  claim/canonical/product cases) are measured. **Question quality, distractor
  quality, grading consistency and hallucination rate on test fixtures are NOT
  measured** by this suite version; assessment behaviour is only touched
  indirectly through canonical/product cases. Adding those case kinds is the
  natural next version bump of the evaluator.
- **Deterministic/offline only.** Every case forces `allow_model=False` or feeds
  explicit stub verdicts, so model-assisted generation quality is out of scope
  by design. The OpenAI/cloud path stays unexercised (no key) and is never
  simulated.
- **The golden set is tiny (7 cases, 4 kinds)** and ships as data users may
  extend; metric means are computed over contributing cases only, so a metric
  with one contributing case is a one-sample number, not a population estimate.
- **Thresholds default to 1.0** and are enforced only for metrics a run actually
  measured; there is no calibration study behind them.
- **Regression detection needs a prior run of the same suite** (baseline pass →
  now fail). A first run cannot regress, and a case that was already failing is
  reported as failing, not as a regression.
- **Snapshot metrics on an empty home are 0.0 by absence**, not by failure; they
  describe the live home, which genuinely holds no claims or content until a
  learner imports sources and generates products.
- The evaluation suite does not (yet) namespace runs by model/provider version,
  so comparing runs produced under different providers relies on the stored
  `evaluation_version` and suite digest rather than a per-run provider tag.
