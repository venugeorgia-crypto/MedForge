# MedForge Interactive Adaptive Tutor (P7, V8)

A **stateful teaching loop**, not a chatbot. Every step is persisted, every
learner response is graded, every adaptation is deterministic, and every piece
of learner evidence flows into the P6 recency-weighted learner model through
`record_learning_event`.

```
P6 learner state ──▶ target selection ──▶ P3 textbook + retrieval ──▶ P4 evidence
                                                                        │
                          ┌─────────────────────────────────────────────┘
                          ▼
   teach ─▶ ask ─▶ answer ─▶ grade ─▶ explain ─▶ adapt ─▶ next question
                          │                              │
                          └──▶ record_learning_event ────┴──▶ P6 updated
```

Engine: `medforge/tutor.py` (`p7-tutor-v1`). Schema: V8 migration
(`core/database/migrate_v8.py`). Constants: `medforge/types.py`. Audit +
execution record: [P7_MATRIX.md](P7_MATRIX.md).

---

## Session lifecycle

Every session is a row in `tutor_sessions` with an explicit stage machine:

| Stage | Meaning |
| --- | --- |
| `TEACH` | evidence-backed teaching content for the current concept |
| `ASK` | a question is prepared and shown |
| `WAITING_FOR_ANSWER` | the learner's answer is saved; grading has not completed |
| `EVALUATE` | grading in progress (model-assisted paths) |
| `EXPLAIN` | the error/answer explanation and the adaptation decision are recorded |
| `ADAPT` | the next question/difficulty/mode is chosen |
| `COMPLETE` | summary stored, one spaced-repetition review recorded |

Statuses: `active`, `waiting`, `blocked` (abstained — no evidence),
`completed`, `aborted`.

Nothing depends on in-memory Python state. A dashboard refresh, a new CLI
process or a machine restart resumes the same session: `resume_tutor_session()`
returns the newest unfinished session, and `get_tutor_state()` rebuilds the
whole view (stage, question, pending answer, evidence, assessment, learner
state, transcript, plan) from SQLite.

Goals map to interaction budgets: `quick` 3 · `10min` 5 · `20min` 8 · `30min`
12 (custom counts allowed up to `TUTOR_MAX_INTERACTIONS`). A session stops when
the budget is reached, the objective is achieved (mastery ≥ 0.80 and recent
performance ≥ 0.80), or a prerequisite-repair detour has run its course
(≥ 4 interactions).

## Target selection (deterministic, P6-driven)

`select_tutor_target()`:

1. **explicit topic** (resolved by title, slug or curriculum node id) always
   wins — the learner may override the recommendation;
2. otherwise P6 `study_priority(limit=50)` ranks what needs attention:
   weakness, recent failure/decline, overdue review, prerequisite risk — the
   reason and component scores are stored on the session
   (`target_source`, `target_reason`);
3. otherwise the first curriculum topic with no learner history ("new
   curriculum topic");
4. with no curriculum at all, the tutor raises instead of inventing a topic.

The tutor never recalculates mastery. It asks P6 what the learner needs and
records the answer; the math stays in `learner_model.py`.

## Evidence-first teaching

Before any substantive medical statement, `gather_evidence()` retrieves and
persists evidence through the P4 graph:

1. **curriculum-linked textbook chunks** (P3 provenance: document → edition →
   chapter → page → chunk) — highest priority;
2. bounded hybrid retrieval (`retrieval.py`) as a supplement.

Every source is stored with `evidence.store_evidence`, so locators, page
numbers and edition identity survive. Two guards keep teaching on-topic:

- **exact-duplicate sources are stored once** (the same chunk reaching the tutor
  twice via the textbook link and retrieval is one piece of evidence, not two);
- **evidence rows must be about the concept** — chapter/section headings are
  stripped (they are metadata, kept in `chapter_title`/`locator`), and a row
  whose body covers less than half of the concept's terms cannot contribute key
  points or rubric items.

### Verification policy (no self-verification)

The model's own text can never create or upgrade an evidence status. The tutor
maps the P4 status to behaviour:

| Status | Behaviour |
| --- | --- |
| `SUPPORTED` | teach normally (`teach: true`) |
| `PARTIALLY_SUPPORTED` | teach, and qualify the explanation (`qualify: true`) |
| `UNSUPPORTED` | do not present the claim as established fact (`abstain: true`) |
| `CONTRADICTED` | surface the disagreement instead of choosing a side |
| `INSUFFICIENT_EVIDENCE` | abstain: a `blocked` session is stored with the reason and the learner is pointed at better-evidenced topics |

Assessment is a deterministic floor (`assess_evidence`): no evidence rows or no
key points → `INSUFFICIENT_EVIDENCE`; concept coverage < 0.40 →
`INSUFFICIENT_EVIDENCE`; a stored P4 `UNSUPPORTED`/`CONTRADICTED` claim wins;
≥ 2 distinct evidence records → `SUPPORTED`, otherwise
`PARTIALLY_SUPPORTED`. An injected P4 verifier may refine a verdict but can
never upgrade it, and verifier errors are recorded, not hidden.

## Teaching modes

| Mode | Intent |
| --- | --- |
| `explain` | teach the concept clearly (default for an explicit topic) |
| `socratic` | guide with one focused question; the model is asked for a guiding question, not the full answer; questions prefer short answers |
| `drill` | repeated practice around a weak concept; prefers quiz items |
| `correct` | repair a specific mistake after an incorrect/partial answer |
| `case` | clinical-style reasoning prompts |
| `review` | reinforce previously learned knowledge with unaided recall |
| `prerequisite_repair` | temporarily step back to a weak prerequisite |

A mode shapes the first question type (`socratic` → short answer, `case` →
clinical reasoning, `drill` → MCQ, `review` → recall), and adaptation switches
mode as the learner's performance requires. If a requested question type cannot
be grounded — an MCQ needs at least two genuine distractors from the sources —
the tutor falls back to a recall question rather than inventing options.

## Adaptive decision logic

`adapt_tutor()` is a deterministic table over the graded answer, the stored
streak counters and the freshly re-read P6 state:

| Situation | Action (mode, difficulty) |
| --- | --- |
| correct + confident | `progress`, keep difficulty, raise after 2 consecutive successes |
| correct + low confidence | `reinforce_confidence`, explain why the reasoning was right |
| incorrect + high confidence | `explain_error` with the `overconfident` flag, mode `correct`, difficulty −1 |
| incorrect + low confidence | `explain_error`, mode `correct`/`drill`, difficulty −1, stronger scaffolding |
| partial | `reinforce_partial`, mode `correct` (conceptual) or `drill`, difficulty −1 |
| repeated failure (2 in a row) | `prerequisite_repair` — re-route to the weakest evidenced prerequisite (impact-ordered); if the prerequisite has no evidence to teach, the decision degrades to `simplify` and says so |
| grading `retryable`/`insufficient_evidence`/`ungraded` | `hold`: no adaptation, no learning event, no fabricated grade |

The same question is never asked twice in a session: question types are cycled,
used item ids are skipped, and when every formulation is exhausted a
deterministic round marker is appended.

## Questions

Stored in `tutor_questions`, content-addressed (`item_id = sha1(...)[:16]`), so
the same concept/type/prompt always resolves to the same item:

- `recall`, `mcq`, `short_answer`, `clinical_reasoning`;
- difficulty 1–5, derived from P6 mastery (1 + round(4 × mastery), else 2);
- prompt, expected answer, rubric (evidence-backed key points with evidence id +
  locator), options (`mcq` only, with real wrong options drawn from other
  source sentences), evidence refs, verification status, `question_version`.

P8 owns the full assessment framework; P7 implements only what interactive
teaching needs and reuses these items.

## Grading

| Path | Behaviour |
| --- | --- |
| MCQ | fully deterministic: index, letter or option text; no match → incorrect with `error_type: unknown` |
| free text, no model | deterministic rubric coverage (≥ 0.50 term coverage per key point); score → correct/partial/incorrect; no overlap at all is `unknown`, not a conceptual error |
| free text, model | strict JSON contract (`score`, `correctness`, `error_type`, missing points, explanation); the prompt is delimited and declares the data untrusted; malformed output falls back to deterministic grading (`deterministic-fallback`) |

Thresholds: `TUTOR_PASS_SCORE` 0.70, `TUTOR_PARTIAL_SCORE` 0.40.

## Failure states and recovery

- **Model failure while grading** → `grading_status: retryable`. The answer was
  persisted *before* grading, so it is preserved verbatim; the session returns
  to `WAITING_FOR_ANSWER` with `model_error` recorded; **no learning event, no
  grade**. A later submit retries the same turn (one event, never two).
- **Grading with no evidence-backed rubric** → `insufficient_evidence`, session
  `blocked` and completed with the reason.
- **Teaching failure** → evidence-quoted fallback (the retrieved key points,
  verbatim). The tutor can never invent content to fill a failed model call.
- **Process/machine restart** → everything needed is in SQLite; `tutor status`
  in a new process shows the same stage, question and pending answer.
- **Stale UI re-submitting a graded question** → `already_graded`, no second
  event, no re-grade.
- **Model unavailable entirely** → the tutor runs deterministically
  (evidence-quoted teaching, rubric grading). `MEDFORGE_TUTOR_AUTO_MODEL=0`
  disables automatic model use; offline mode disables it too.

## Privacy

Learner answers, confidence and notes are internal. `public_view()` recursively
redacts `pending_answer`, `pending_confidence`, `learner_answer`,
`learner_confidence`, `model_error`, `misconception_id` and free-text summaries
from any view that leaves the learner's own screen (session lists, logs, shared
output). Source excerpts are private verification data and are never written
into distributable artifacts (`docs/EVIDENCE.md`).

## Model usage

Model calls are optional and bounded:

- teaching text (≤ 120 words, evidence key points only, untrusted data
  delimited), Socratic guidance (≤ 60 words);
- free-text grading (one JSON-only call per answer);
- misconception wording (bounded, conceptual errors only).

All untrusted data is passed inside `<<<...>>>` blocks with a "data, not
instructions" notice, embedded delimiters are neutralized so data cannot escape
its block, and instruction-like text is flagged (`injection_suspected`) on the
turn and counted in the session summary. Injected instructions are never
executed: the MCQ path is deterministic, and free-text grading obeys the rubric,
not the answer.

## P6 integration

- `record_learning_event(mastery_key, item_type="question", source="session",
  content_version="p7-tutor-v1", item_id=question id, score, correctness,
  confidence, answered_at)` — one event per graded interaction, linked back to
  the turn as `attempt_id`.
- Misconceptions are recorded only for conceptual errors (`error_type ==
  "conceptual"` with an incorrect/partial answer) via `learner.record_weakness`,
  then tagged `origin='tutor'` with the curriculum node id. Minor/unknown errors
  never become misconceptions.
- On completion, one review opportunity is synced through the *existing*
  interfaces: an item `"<mastery_key>:tutor-review"` is inserted if missing and
  graded with `learner.review_card(...)` using the session's mean score
  (`TUTOR_REVIEW_GRADES`). SM-2/FSRS math is untouched.

## Session summary

`get_tutor_summary()` returns topic, objective, mode, goal, interactions,
attempt counts, mean score, concepts covered, prerequisites visited, confidence
pattern, misconceptions, learner-model change (before/now/delta/recent/
uncertainty/evidence count), open vs recovered weaknesses, prerequisite risks,
verification statuses, abstentions, injection flags, model calls, stop reason
and the recommended next review.

## CLI

```bash
medforge tutor targets                       # what P6 says to study next
medforge tutor "start|<topic>|<mode>|<goal>"  # or: tutor start <topic>
medforge tutor status [id]                   # full state (no id: resume newest)
medforge tutor next <id>                     # advance/re-show the next step
medforge tutor "answer|<id>|<answer>|<confidence>"
medforge tutor end <id>
medforge tutor summary <id>
medforge tutor sessions
```

## Dashboard

**TUTOR** tab: topic (blank = recommended), mode, goal, session start/resume,
objective + interaction counters, the teaching text, the current question with
an answer box or option radio, a confidence slider, grading feedback, an
evidence expander (private excerpts), and a recalculation button. Session state
is stored, so a refresh resumes the same session.

## P8 boundary

Stable interfaces for the assessment engine:

`start_tutor_session`, `get_tutor_state`, `select_tutor_target`,
`generate_teaching_step`, `generate_question`, `submit_answer`,
`evaluate_answer`, `adapt_tutor`, `record_tutor_learning_event`,
`complete_tutor_session`, `get_tutor_summary`, `resume_tutor_session`.

P8 should extend item banking, item statistics, exam-mode blueprints and
psychometrics *on top of* these interfaces — not duplicate the session loop,
evidence assessment, grading dispatch or learner-event recording.

## Explicit non-goals (P7)

- No second learner/mastery system: all learner evidence goes through P6.
- No replacement of P4 verification: the tutor communicates evidence status, it
  never decides it.
- No full assessment framework (P8), no video production (P12).
- No new spaced-repetition scheduler: the existing SM-2/FSRS interfaces are
  reused as-is.
