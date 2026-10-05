# P8 — Question-Level Assessment Engine (V9)

Status: **implementation complete, verification recorded at the end of this file.**
Baseline entering P8: P3 `d405bcd`, P4 `f916733`+`9fe0e3c`, P6 `3d1b668`,
P7 `c29f339`, migration `8.0.0`, 163/163 tests green, learner model
`p6-rwm-v1`, tutor `p7-tutor-v1`.

## 1. Audit — what exists and what P8 must reuse

| Area | Existing owner | P8 rule |
| --- | --- | --- |
| Curriculum hierarchy (Subject→Week→Seminar→Topic) | `medforge/curriculum.py` (P2/V4) | read-only: `find_node`, `get_children`, `topic_path`, `add_prerequisite` |
| Textbook provenance (document→edition→chapter→page→chunk) | `medforge/textbook.py` (P3/V5) | read-only: `textbook_evidence_for_topic` |
| Evidence graph (claim→evidence→source, verification) | `medforge/evidence.py` (P4/V6) | read-only: `store_evidence`, `aggregate_verdicts`, `_sanitize_delimited` |
| Recency-weighted learner model + learning events | `medforge/learner_model.py` (P6/V7) | **single learner write path**: `record_learning_event`; reads: `get_mastery`, `get_weaknesses`, `get_prerequisite_risks`, `study_priority` |
| Spaced repetition | `medforge/learner.py` | `review_card` for the post-assessment review opportunity |
| Tutor engine (sessions, evidence retrieval, verification floor, question construction, grading, adaptation, review) | `medforge/tutor.py` (P7/V8) | reused, never replaced: `gather_evidence`, `extract_key_points`, `assess_evidence`, `verification_policy`, `evaluate_answer` (→ `grade_mcq`/`grade_free_text`/`parse_grader_json`/`grade_key_points`), `injection_suspected`, `public_view` |

### P7 public interfaces P8 depends on (contract-tested in `tests/test_assessment.py`)

1. `select_tutor_target` — topic resolution to `(topic, mastery_key, node_id)`; P8 uses it for item provenance.
2. `gather_evidence` — bounded, deduped evidence persisted through P4; P8 uses it for item generation/validation.
3. `extract_key_points` — evidence sentences covering a concept; P8 rubrics use the same extraction so a rubric can never be invented.
4. `assess_evidence` — deterministic verification floor (≥2 distinct records ⇒ SUPPORTED, else PARTIALLY_SUPPORTED; empty ⇒ INSUFFICIENT_EVIDENCE).
5. `verification_policy` — teach/qualify/abstain decision; P8 maps it to the item evidence gate.
6. `evaluate_answer` — grading dispatch; the **only** grading entry point P8 calls.
7. `grade_mcq` / `grade_free_text` / `grade_key_points` / `parse_grader_json` — P7 grading primitives behind that dispatch.
8. `record_tutor_learning_event` — proves the P6 `record_learning_event` write path P8 mirrors for question items.
9. `start_tutor_session` / `get_tutor_state` — used read-only to prove P8's sessions are separate and P7 state is untouched.
10. `injection_suspected` — shared injection flagger (evidence, stems, choices, answers).
11. `public_view` — shared redaction for private/exportable output.
12. `ensure_tutor_tables` — guarantees the V8 seam exists before V9 runs.

### Audit findings (no silent P7 redesign needed)

- **Sufficient as-is:** evidence retrieval, evidence verification floor, model-assisted free-text grading with `retryable` failure semantics, strict grader JSON parsing, injection flagging, redaction.
- **Two smallest backward-compatible extensions required, both additive and documented here:**
  1. **Multi-select grading.** P7's `grade_mcq` is single-answer by construction (`correct_option`). P8 needs `MCQ_MULTI`, so it adds one deterministic scorer, `assessment.grade_multi_select`, governed by an item-level `scoring_policy` (`exact` default; `partial` only when configured). It is not a second grading engine: it is one more deterministic strategy next to `grade_mcq`, and every free-text/clinical item still goes through `evaluate_answer`.
  2. **`TRUE_FALSE` items** are graded through P7's `grade_mcq` by presenting `["True","False"]` as the option set with a deterministic `correct_option` — no new grader.
- **Deliberately not reused:** `tutor.build_question`/`_persist_question`, because those write into `tutor_questions` and would couple the P8 item bank to the P7 transcript. P8 constructs items from the same public evidence primitives and stores them in its own versioned bank.
- **P6 enum constraint:** `learning_attempts.source` has `CHECK(source IN ('session','review','manual','backfill'))`. P8 records assessment attempts with `item_type='question'`, `source='session'` and `content_version='p8-assessment-v1'`; no P6 table is altered.

## 2. Requirements → design

### 2.1 Item bank and versioning

Two tables give immutable, reproducible item identity:

- `assessment_items` — identity + lifecycle (`status`, `current_version`, provenance links, quality flags).
- `assessment_item_versions` — immutable content per version (`stem`, `choices`, `correct_choices`, `rubric`, `correct_answer`, `explanation`, `scoring_policy`, `evidence_refs`, `claim_refs`, `evidence_state`, `content_hash`, `validation_report`, `duplicate_of`, `item_model_version`).

`(item_id, item_version)` is the historical key. Material changes (stem,
answer, rubric, evidence basis) create a **new version** via `revise_item`;
old versions are never mutated, so old assessments replay exactly.

### 2.2 Item types and grading method

| P8 type | Grader | Deterministic? |
| --- | --- | --- |
| `MCQ_SINGLE` | P7 `grade_mcq` (index/letter/text) | yes |
| `MCQ_MULTI` | `grade_multi_select` + item `scoring_policy` | yes |
| `TRUE_FALSE` | P7 `grade_mcq` with a two-option set | yes |
| `RECALL` | P7 `evaluate_answer` → free-text path | deterministic fallback always available |
| `SHORT_ANSWER` | P7 `evaluate_answer` → free-text path | deterministic fallback always available |
| `CLINICAL_REASONING` | P7 `evaluate_answer` → free-text path | deterministic fallback always available |

### 2.3 Provenance and evidence gate

`evidence_refs` = `[{evidence_id, claim_id, source_id, locator, page_number, edition_id, chapter_title, section_title}]`
captured from P3/P4 rows, plus `claim_refs`. Every item stores the P4
`evidence_state` computed by `tutor.assess_evidence` (never by generated text).
Approval gate: `SUPPORTED` ⇒ ACTIVE; `PARTIALLY_SUPPORTED` ⇒ REVIEW_REQUIRED
unless an explicit reviewed approval (`allow_partially_supported=True`) records
the qualification; `UNSUPPORTED`/`CONTRADICTED`/`INSUFFICIENT_EVIDENCE` ⇒
approval refused and validation rejects the item. There is no throughput bypass.

### 2.4 Generation pipeline

`generate_items(topic, count, item_types)` → black-box pipeline:
`gather_evidence` → `extract_key_points` → `assess_evidence` → deterministic
item construction (MCQ distractors are real evidence sentences that do **not**
mention the concept; if fewer than 2 exist the type falls back rather than
fabricating options) → `create_item(status="DRAFT")`. Generation **never**
publishes: lifecycle is `DRAFT → VALIDATION → REVIEW_REQUIRED → APPROVED → ACTIVE → RETIRED`
(with `REJECTED` as the terminal invalid state). All steps are explicit public
calls (`validate_item`, `approve_item`, `retire_item`).

### 2.5 Validation rules (deterministic, reject not fix)

Evidence availability; answer determinacy; single-answer uniqueness; ≥2 real
distractors for MCQ_SINGLE; ≥2 correct keys for MCQ_MULTI; TRUE_FALSE exactly
one of two; rubric/reference answer for free text; partial credit only with a
multi-point rubric; no empty/duplicate choice text; curriculum node exists;
stem non-trivial; duplicate content reported; injection markers reported.
Hard failures ⇒ `REJECTED`; soft findings ⇒ `REVIEW_REQUIRED`.

### 2.6 Blueprints

`assessment_blueprints` stores scope (`topic|seminar|week|subject|custom`),
`item_count`, type/difficulty/topic distributions, prerequisite coverage, time
limit, pass threshold, seed and version. Distributions accept fractions
(sum ≈ 1) or integer counts (sum = item_count) and are normalized
deterministically. `validate_blueprint` checks arithmetic, scope resolution and
**bank availability** (reports shortfalls instead of inventing items).

### 2.7 Selection

`select_items(blueprint, assessment_id, exclude_item_ids)` scores ACTIVE bank
items deterministically: blueprint bucket need, scope match, difficulty
distance, exposure penalty (recent presentations and answers from
`assessment_attempts`), P6 mastery boost for the item's mastery key, then a
seeded `random.Random(sha1(blueprint|version|assessment|seed))` stable sort
with `item_id` tie-break. Items presented in the most recent assessments are
excluded while enough alternatives remain (`prefer_unseen`); shortfalls are
reported, never fabricated.

### 2.8 Sessions, attempts, exposure

`assessment_sessions` (separate from `tutor_sessions`) holds the fixed
`item_order`, mode, status, server timestamps, time limit, scores/summary and
remediation. `assessment_attempts` persists one row per presented question per
assessment (`UNIQUE(assessment_id, question_order)`) with the presented item
snapshot (`stem`, `choices`, `correct_choices`, `rubric`, `correct_answer`,
`scoring_policy`, `evidence_refs`), so later item edits cannot change a
historical attempt. Graded answers are never overwritten; an ungraded/retryable
answer is re-graded in place with `grading_history` appended. Presentation
timestamps on the same rows are the exposure ledger (times presented, times
answered, last exposure, recent performance).

### 2.9 Modes and timing

One engine, mode flags: `PRACTICE` (immediate feedback), `REVIEW` (immediate
feedback + teaching/evidence), `EXAM` (feedback withheld until completion;
review available afterwards). Timing is server-side: `started_at`/`expires_at`
from stored timestamps, elapsed computed from them; client-supplied elapsed is
ignored. On expiry, answers stop being accepted, completed attempts are
preserved and the session finalizes as `expired` with a full result.

### 2.10 Scoring, statistics, remediation

Raw score, max score, percentage, per-domain, per-type, per-difficulty-band,
confidence calibration (over/under-confidence mismatches) and timing, all
computed in Python. Statistics are transparent and always carry `sample_size`;
below `ASSESSMENT_MIN_ITEM_STAT_SAMPLE` the value is labelled
`insufficient sample`, and discrimination requires
`ASSESSMENT_MIN_DISCRIMINATION_SAMPLE` plus non-empty upper/lower groups. No
claim of validated psychometrics. Quality flags (`too_easy`, `too_difficult`,
`possible_ambiguity`, `poor_discriminator`, `frequently_skipped`,
`inconsistent_grading`) are recorded for review; nothing is deleted.

### 2.11 P6/P7 integration and remediation

Every graded attempt calls `learner_model.record_learning_event(...)` exactly
once (`learning_attempt_id` guard) with node, mastery key, item id+version,
score, correctness, confidence and response time. Weakness/prerequisite/review
signals come from P6 (`get_weaknesses`, `get_prerequisite_risks`,
`study_priority`) — P8 implements no weakness algorithm. Completion records one
SM-2 review opportunity (`<mastery_key>:assessment-review`) through
`learner.review_card`. `get_assessment_remediation` returns a structured
payload (weak topics, prerequisite gaps, review priorities, tutor remediation
proposal, spaced-repetition opportunities) with `launch: false` — no automatic
tutor session.

### 2.12 Privacy, injection, performance

Assessment responses are private: `public=True` views redact learner answers,
confidence, key points and model errors (via P7 `public_view`), and exports
carry no long evidence excerpts (locators only). Stems, choices, evidence text
and learner answers are data, never instructions: items/answers are flagged
through `tutor.injection_suspected`, model grading uses P7's `<<<>>>`
delimited prompt with the "data, not instructions" system message, and no
deterministic path consults model output. SQLite-first, bounded queries
(no full bank loads), no model calls for arithmetic/timing/selection/stats.

## 3. Verification (recorded after execution)

Filled in by the P8 run; the full execution record follows.

---

## 4. Execution results (2026-10-05)

### Migration 9.0.0 — live data integrity

- `database/medforge.sqlite3` migrated `8.0.0 → 9.0.0` (`v9_assessment_engine`),
  applied by `core/database/migrate_v9.py::ensure_assessment_v9()` (idempotent,
  runs V8 first, verified backup first).
- **Verified backup:** `backups/medforge_pre_v9_backup.db` — pre-V9 snapshot,
  `integrity_check ok`, migrations end at `8.0.0`, contains **0** assessment
  tables.
- **Live counts before == after** for all pre-existing tables: chunks 47,
  claims 5, evidence 18, claim_evidence 10, verification_runs 10,
  learner_model_state 1, spaced_repetition_queue 119. The six V9 tables exist
  empty (0 rows) in the live DB — all P8 demonstrations ran in an isolated
  `MEDFORGE_HOME=/tmp/mf-p8-demo`. `PRAGMA integrity_check ok`,
  `foreign_key_check` clean, migration chain now `3.0.0 … 9.0.0`.
- Live `learning_attempts` changed 8 → 14 during the session window, which is
  the user's own dashboard (port 8501, default home) recording tutor activity;
  P8 code paths wrote nothing there.

### Tests

- `tests/test_assessment.py`: **47 tests** (Phase-0 seam contracts, item
  lifecycle/versioning, evidence gate, blueprint validation, deterministic
  selection incl. the relaxation fix, modes/timing/expiry, multi-select
  round-trip, stats + insufficient-sample floors, P6 event guard, privacy
  redaction, duplicate naming clash guard).
- **Full regression: 210/210 passed** (163 baseline + 47) in 25.7 s.
- `py_compile` clean on all touched Python files; `medforge` exports 267 names,
  no duplicates.

### Demonstrations (isolated demo home, real syllabus + handouts + Ollama)

1. **Item bank + 20-question PRACTICE** (`/tmp/p8_setup.py`, log
   `/tmp/p8_setup.log`): syllabus "Endocrine Physiology Module" (2 weeks, 4
   topics) → 2-page handout per topic ingested through P3 provenance → 32
   DRAFT items across all 6 types → validation/approval → **32 ACTIVE, 0
   refused, 0 duplicate groups** → blueprint `bp-b4f59fd8151b` (20 items,
   type+band distributions, 45 min, threshold 0.6, seed `p8-demo`) valid with
   shortfalls=[] → **deterministic selection ran twice, identical, 20/20,
   relaxations=0** → assessment answered with a scripted plan (4 deliberate
   wrong @0.9 confidence, 2 partial). Result: **14.5/19 = 76.3% passed** with
   one retryable pending (model hit its output limit on Q10 — the answer was
   preserved, exactly as designed); per-topic domains, per-type and per-band
   percentages, confidence calibration caught all 4 overconfident wrongs
   (mismatch_count=4), server-side timing, 1 flag, SM-2 review opportunity
   (`<mastery_key>:assessment-review`, grade 3, due +1 day), P6-sourced
   remediation (weak_topics=[growth-hormone-physiology], priorities with
   weakness/uncertainty/recent_failure reasons, `launch:false`).
2. **Retryable grading / recovery** (`/tmp/p8_recovery.py`, log
   `/tmp/p8_recovery.log`): model-path retry re-graded in place
   (grading_history appended, **no P6 event while retryable**), then the
   documented offline path (`ASSESSMENT_AUTO_MODEL=0`) graded it
   **deterministically (0.667)** recording **exactly one** P6 event
   (`learning_attempt_id=21`); results recomputed to 15.17/20 = 75.8%,
   pending=[]. A second session answered 4 questions + 1 flag in process 1, a
   **separate Python process** resumed via `resume_assessment()` with the same
   id, item order, current index, answers, flags and attempt rows; finishing
   it produced exactly 20 attempt rows (`UNIQUE(assessment_id,
   question_order)` holds; rows are created lazily per presented item).
3. **EXAM mode** (`/tmp/p8_exam.py`, log `/tmp/p8_exam.log`): week-2 blueprint,
   5 deterministic items, feedback withheld during the exam (`available:False`
   on result and review), answers + response times persisted per question,
   server-side clock (`remaining 1800s → 1781s`), after submission
   **4.0/5.0 = 80% passed**, review available with answers, correct answers and
   evidence locators (`p. 1`), late submission returns `already_complete`.
4. **P6 learner-state change** (`/tmp/p8_p6_analytics.py`, log
   `/tmp/p8_p6_analytics.log`): **95 question-level events** recorded with
   `content_version='p8-assessment-v1'` (20 setup + 20 recovery + 5 exam + 50
   longitudinal), all inside the demo topics; P6 mastery after: GH 67.5%,
   GP 86.0%, DI 86.8%, TH 84.1% with per-topic evidence counts and recent
   performance; `study_priority` returns ranked rows with reasons; remediation
   `spaced_repetition.launch=false` — P8 never auto-launches a tutor session.
5. **Item analytics** (same script): a 3-sample item reports
   **"insufficient sample"** with `discrimination=None`; after 5 deterministic
   assessments (relaxations=0 each) items carry **sample=11** with proportion
   correct, response-time mean, and **discrimination computed** (upper/lower
   thirds note included); `compute_item_quality` persisted 2 rows to
   `assessment_item_quality`, merged `['too_easy','poor_discriminator']` onto
   the items' `quality_flags`, action "flagged for review; nothing is
   auto-deleted".
6. **CLI** (`medforge assessment …`, dual pipe/space parsing): `list`,
   `items`, `blueprints`, `status`, `result`, `stats`, `review`, `remediate`,
   `item-info` verified returning structured JSON against the demo home.
7. **Dashboard**: 13 tabs with **ASSESSMENTS at index 5**; the tab renders the
   creation form (scope, blueprint, mode `PRACTICE`, items, minutes) and a
   live "Recent assessments" table. **One real defect found live and fixed:**
   the tab referenced `mf.ASSESSMENT_MODES` but `medforge_core.py` had not
   re-exported it (`module 'medforge_core' has no attribute
   'ASSESSMENT_MODES'` alert in the browser) — fixed by adding it to the
   medforge_core import block; re-verified in the browser (screenshot
   captured); verification server on port 8502 stopped afterwards (user's own
   8501 left untouched).

### Defects found during live validation (all fixed with regression tests)

1. `_tutor_question`/`grade_multi_select` crashed on JSON-string columns →
   `_coerce_item` normalisation.
2. `_compute_results` strongest/weakest crashed on list values → precomputed
   domain/type/band score dicts.
3. Multi-select list answers were stored as a Python repr → stored as the
   stable `"A,C"` joined string.
4. Export name clash with `curriculum.detect_duplicates` → P8 detector named
   `detect_duplicate_items` (asserted in tests).
5. `partial_credit` default now requires a ≥2-point rubric.
6. P6 read used `mastery_score` instead of `mastery` → fixed.
7. `study_priority` rows keyed by `topic` (not `items`) → fixed read path.
8. Selection over-eagerly relaxed constraints and starved feasible
   blueprints → most-constrained-slot-first scheduler with exposure as an
   ordering preference only (re-verified: relaxations=0 on feasible
   blueprints, demo re-run green).
9. Dashboard `ASSESSMENT_MODES` export missing from `medforge_core` (found in
   browser verification) → import added.

### Limitations (honest list)

- Statistics are lightweight and transparent, **not validated psychometrics**
  (no IRT/adaptive testing); discrimination is a simple upper/lower-third
  contrast and needs ≥8 graded samples.
- Free-text grading quality depends on the local model; a failed/over-long
  generation keeps the answer retryable instead of guessing, and the
  deterministic rubric grader is lexical.
- Single local learner (`learner_key='local'`); no multi-tenant isolation is
  claimed.
- Demo handouts are synthetic (2 pages/topic), so evidence pools are small
  (SUPPORTED floor = ≥2 distinct records).
- Duplicate detection is exact/normalised-text based; near-duplicates need
  human review.

### Next phase (do not start automatically)

**P9 — Study intelligence / product factory integration**: unify curriculum +
evidence + learner + tutor + assessment into one adaptive study workflow.
