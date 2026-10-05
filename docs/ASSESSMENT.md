# MedForge Question-Level Assessment Engine (P8, V9)

A **persistent, reusable assessment system** on top of the provenance,
evidence, learner and tutor phases. Items are versioned, every medical item
carries a P4 evidence basis, blueprints and selection are deterministic, and
every graded answer flows into the P6 learner model through
`record_learning_event` — the same write path the tutor uses.

```
P4 verified evidence ──▶ item generation/validation ──▶ item bank (versioned)
                                                                  │
P6 learner state ──▶ blueprint ──▶ deterministic selection ──▶ assessment session
                                                                  │
                    answer ─▶ grade (P7 dispatch) ─▶ score/timing/stats
                                            │
                                            └──▶ record_learning_event ──▶ P6 updated
```

Engine: `medforge/assessment.py` (`p8-assessment-v1`). Schema: V9 migration
(`core/database/migrate_v9.py`). Constants: `medforge/types.py`. Audit +
execution record: [P8_MATRIX.md](P8_MATRIX.md).

---

## Item bank and versioning

Two tables give immutable, reproducible item identity:

- `assessment_items` — identity + lifecycle (`status`, `current_version`,
  curriculum node, mastery key, quality flags).
- `assessment_item_versions` — immutable content per version: stem, choices,
  correct choices, rubric, reference answer, explanation, scoring policy,
  evidence refs, claim refs, evidence state, `content_hash`,
  `item_model_version`.

`(item_id, item_version)` is the historical key. Material changes (wording,
answer, rubric, evidence basis) create a **new version** through
`revise_item()`; old versions are never mutated, so any historical assessment
replays exactly. `retire_item()` removes an item from selection without
rewriting history.

## Item lifecycle

```
DRAFT → VALIDATION → REVIEW_REQUIRED → APPROVED → ACTIVE → RETIRED
                    ↘ REJECTED (terminal invalid state)
```

- `validate_item()` checks structure, determinacy, curriculum alignment,
  duplicates and injection markers. It **rejects, never fixes**.
- `approve_item()` refuses items whose evidence is `UNSUPPORTED`,
  `CONTRADICTED` or `INSUFFICIENT_EVIDENCE`; `PARTIALLY_SUPPORTED` requires an
  explicit `allow_partially_supported=True` review that is recorded on the
  approval.
- Only `APPROVED → ACTIVE` items are selectable.

## Item types and grading

| P8 type | Grader | Deterministic? |
| --- | --- | --- |
| `MCQ_SINGLE` | P7 `grade_mcq` (key/letter/text) | yes |
| `TRUE_FALSE` | P7 `grade_mcq` with a two-option set | yes |
| `MCQ_MULTI` | `grade_multi_select` + item scoring policy | yes |
| `SHORT_ANSWER` | P7 `evaluate_answer` free-text path | model + deterministic fallback |
| `CLINICAL_REASONING` | P7 `evaluate_answer` free-text path | model + deterministic fallback |
| `RECALL` | P7 `evaluate_answer` free-text path | model + deterministic fallback |

P7's `evaluate_answer` is the **only** grading entry point. The single
addition is one deterministic scorer (`grade_multi_select`) beside `grade_mcq`
— not a second grading engine; all free-text/clinical items still go through
the P7 dispatch (model-assisted with the local chat model, deterministic
rubric fallback otherwise).

## Provenance and the evidence gate

Every medically substantive item stores `evidence_refs` captured from P3/P4
rows (`evidence_id`, `claim_id`, `source_id`, locator, page number, edition,
chapter) plus `claim_refs`, and the P4 `evidence_state` computed by
`tutor.assess_evidence` (≥ 2 distinct records ⇒ SUPPORTED; empty ⇒
INSUFFICIENT_EVIDENCE). Approval refuses unsupported facts — there is no
throughput bypass. Generated distractors are real evidence sentences that do
not mention the concept; if fewer than 2 exist the generator falls back to
another item type instead of inventing unsafe options.

## Blueprints and deterministic selection

`create_blueprint()` stores scope (`topic | seminar | week | subject |
custom`), item count, type/difficulty/topic distributions (fractions or
integer counts, normalized deterministically), time limit, pass threshold and
seed. `validate_blueprint()` checks arithmetic, scope resolution and bank
availability — reporting shortfalls instead of inventing items.

`select_items()` is deterministic: a most-constrained-slot-first scheduler
built from real bank capacity fills the blueprint exactly when feasible
(needed relaxations are reported, never silent); exposure is an ordering
preference (recent presentations/answers lower priority, never a hard filter);
P6 mastery feeds a small boost. Ties break by seeded
`random.Random(sha1(blueprint|version|assessment|seed))` then `item_id`, so
the same seed reproduces the same selection. Items are presented interleaved
round-robin over a canonical type order.

## Sessions, attempts, modes, timing

`assessment_sessions` is separate from `tutor_sessions`. Each attempt row in
`assessment_attempts` (`UNIQUE(assessment_id, question_order)`) persists the
presented item snapshot — stem, choices, correct choices, rubric, scoring
policy, evidence refs — so later item edits cannot change a historical
attempt. Graded answers are never overwritten; ungraded/retryable answers are
re-graded **in place** with `grading_history` appended.

One engine, mode flags:

| Mode | Feedback | Notes |
| --- | --- | --- |
| `PRACTICE` | immediate | default |
| `REVIEW` | immediate + evidence | teaching references |
| `EXAM` | withheld until submission | review available afterwards |

Timing is server-side: `started_at`/`expires_at` are stored timestamps and
elapsed time is computed from them; client-supplied elapsed is ignored. On
expiry answers stop being accepted, completed attempts are preserved and the
session finalizes as `expired` with a full result.

## Scoring, statistics, quality flags

`get_assessment_result()` reports raw score, maximum, percentage, per-domain
(topic), per-type and per-difficulty-band breakdowns, confidence calibration
(over/under-confidence mismatches) and timing. Item statistics
(`get_item_statistics`, `compute_item_quality`) are transparent and always
carry `sample_size`:

- below `ASSESSMENT_MIN_ITEM_STAT_SAMPLE` (5) graded answers the value is
  labelled **insufficient sample**;
- discrimination (upper/lower thirds) needs
  `ASSESSMENT_MIN_DISCRIMINATION_SAMPLE` (8) plus non-empty groups.

Quality flags (`too_easy`, `too_difficult`, `possible_ambiguity`,
`poor_discriminator`, `frequently_skipped`, `inconsistent_grading`) are
recorded for review; nothing is deleted. This is lightweight reporting, **not
validated psychometrics**.

## P6/P7 integration and remediation

Every graded attempt calls `learner_model.record_learning_event()` exactly
once (`item_type='question'`, `source='session'`, `content_version=p8-assessment-v1`)
with node, mastery key, item id+version, score, correctness, confidence and
response time. P6 remains the only learner-math component: weakness,
prerequisite and priority signals come from `get_weaknesses`,
`get_prerequisite_risks` and `study_priority`. Completion records one SM-2
review opportunity (`<mastery_key>:assessment-review`) through
`learner.review_card`.

`get_assessment_remediation()` returns a structured payload — weak topics,
prerequisite gaps, review priorities, a tutor remediation proposal, spaced-
repetition opportunities — with `launch: false`. Starting a tutor session is
always an explicit learner action.

## Privacy, injection, performance

- Learner answers, confidence and key points are private: `public=True` views
  redact them via P7 `public_view`, and exports carry locators only, never
  long evidence excerpts.
- Stems, choices, evidence text and learner answers are data, never
  instructions: `tutor.injection_suspected` flags them and model grading uses
  P7's delimited "data, not instructions" prompt.
- SQLite-first; no model calls for selection, scoring, timing or statistics.

## CLI

`medforge assessment` follows the tutor's pipe/space dual parsing:

```
medforge assessment list                     # sessions
medforge assessment blueprints
medforge assessment create|start|status|next|previous|answer|flag|submit
medforge assessment result|review|remediate <id>
medforge assessment items|item-info|stats    # bank inspection + item stats
medforge assessment blueprint-new            # create a blueprint
medforge assessment item-new                 # author an item manually
```

## Dashboard

**ASSESSMENTS** tab: create an assessment from a blueprint, navigate items
(next/previous/flag), answer with option radios or free text plus a confidence
slider, submit, view the result (score, domains, types, bands, calibration),
open the post-submission review and the remediation payload. All state is
stored — a refresh resumes the same session.

## Explicit non-goals (P8)

- No second grading engine, no second learner-event pipeline, no P6 math.
- No replacement of P4 verification: items carry the P4 verdict, never decide
  it.
- No validated psychometrics (IRT/adaptive testing); flagged quality only.
- No automatic tutor-session launching from results (P9 territory: unified
  study workflow).
