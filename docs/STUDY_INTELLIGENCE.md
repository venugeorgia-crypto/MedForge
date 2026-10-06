# Study Intelligence (P9, V10)

`medforge.study` is the **orchestrator** that answers one question:

> What should I study now, why should I study it, what evidence should teach it,
> how should it be taught, what should be generated, and when should I be
> reassessed?

It owns no domain logic. Mastery comes from P6, evidence from P3/P4, teaching
from P7, grading from P8, scheduling from spaced repetition, rendering from the
product factory. P9 decides *what to do next* and *wires the engines together*.

```
                       CURRICULUM (P2)
                             |
                             v
                    +-------------------+
                    |   STUDY ENGINE    |   <- medforge.study (P9)
                    |      P9           |
                    +---------+---------+
                              |
        +---------------------+---------------------+
        v                     v                     v
   LEARNER (P6)         EVIDENCE (P4)         PRIORITY (P6)
        |                     |                     |
        +---------------------+---------------------+
                              v
                       STUDY STRATEGY
                              |
        +---------------------+---------------------+
        v                     v                     v
   TUTOR (P7)          ASSESSMENT (P8)      REVIEW / SR (learner)
        |                     |                     |
        +---------------------+---------------------+
                              v
                     PRODUCT FACTORY (content.py)
                              |
                              v
                        STUDY RECORD (V10)
```

Versions: `STUDY_VERSION = "p9-study-v1"`, `PLANNER_VERSION = "p9-planner-v1"`.

## 1. Public interface

| Function | Purpose |
| --- | --- |
| `ensure_study_tables()` | Apply/verify the V10 schema (candidate-walk import of `core.database.migrate_v10`). |
| `gather_learner_context(topic=None)` | One bounded read of curriculum + P6 + SR + P8 + evidence coverage. |
| `get_study_status()` | Curriculum tree annotated with mastery, uncertainty, evidence state and derived topic state. |
| `recommend_next_action(topic=None, goal=None)` | Deterministic target selection with machine-readable reasons. |
| `get_knowledge_gaps()` | Topics with insufficient/contradictory evidence or unstudied coverage. |
| `get_readiness(topic)` | Honest estimate of whether a topic can be assessed/sat. |
| `adaptation_profile(topic)` | `weak \| developing \| strong \| underconfident \| overconfident` from P6. |
| `build_study_plan(...)` / `get_plan` / `list_plans` / `get_today_plan` | Persistent, versioned, reproducible plans + daily fit. |
| `start_study_mission(topic=None, plan_id=None, ...)` | Materialize a mission and open the real engines. |
| `get_mission_state` / `list_missions` / `get_study_history` | Inspect state and the append-only action log. |
| `complete_mission_action` / `complete_mission` | Record-and-advance step completion; finish the mission. |
| `harvest_mission_results` | Bounded engine aggregates (tutor scores, assessment percentage). |
| `launch_mission_engines` | Idempotent re-attach to P7/P8 after a restart. |

`get_study_status()`, `recommend_next_action()`, `build_study_plan()`,
`get_today_plan()`, `start_study_mission()`, `get_mission_state()`,
`complete_mission_action()`, `get_knowledge_gaps()`, `get_readiness()` and
`get_study_history()` are the stable P9 contract for later phases.

## 2. Topic state classification

Derived in P9 from P6 + P4 + P8; never a second mastery algorithm.

| State | Rule |
| --- | --- |
| `NOT_STARTED` | No attempts and no evidence-linked study. |
| `IN_PROGRESS` | Attempts exist, mastery below `STUDIED_PCT` (40%). |
| `STUDIED` | Mastery ≥ `STUDIED_PCT`. |
| `ASSESSING` | Studied but assessment evidence is thin or failing. |
| `MASTERED_ESTIMATE` | Mastery ≥ `MASTERED_PCT` (85%). |
| `REVIEW_DUE` | SR cards due within `REVIEW_DUE_CUTOFF_DAYS` (7). |

Constants live in `study.py` (`MASTERED_PCT`, `STUDIED_PCT`,
`REVIEW_DUE_CUTOFF_DAYS`, `DECLINING_TREND_THRESHOLD = -0.15`) so the thresholds
are auditable instead of scattered magic numbers. A single successful mission
never promotes a topic to mastered — only P6 evidence does.

## 3. Target selection ("why this topic")

`recommend_next_action()` computes, per candidate topic, a weighted sum of
reasons from P6 state and P9 context:

| Reason code | Signal |
| --- | --- |
| `weakness` | Low mastery (`1 - mastery`, weighted). |
| `uncertainty` | Wide credible interval from few attempts. |
| `overdue` | Reviews past due. |
| `recent_failure` | Low recency-weighted recent performance. |
| `prerequisite_impact` | Weak prerequisite blocking downstream topics. |
| `not_started` | Evidence exists but the topic was never studied. |
| `assessment_gap` | Studied but never assessed. |
| `review_due` | SR due soon (positive weight for `SPACED_REVIEW`). |

Every ranked candidate carries `reasons: [{code, detail, weight}]` — the
machine-readable form — and the response also carries a rendered sentence:

```
"Growth Hormone Physiology selected for TUTOR because: mastery 55.2%; recent performance 0.56"
```

Weights are explicit in `REASON_WEIGHTS`. Declining topics use P6's own
`get_recent_performance(key)["trend"]` (`<= -0.15`), so "declining" means the
recency-weighted model says so — not a re-derived difference of our own.

## 4. Action selection

| Learner state | Action | Engine |
| --- | --- | --- |
| New topic, no prior evidence | `NEW_TEACHING` | P7 tutor (`explain`) |
| Weak + repeated failure | `TUTOR` / `DRILL` | P7 tutor |
| Prerequisite weak | `PREREQUISITE_REPAIR` | P7 tutor (prerequisite mode) |
| Strong + overdue | `SPACED_REVIEW` / `REVIEW` | SR queue |
| Studied but poorly assessed | `ASSESS` | P8 assessment |
| Recall-only need | `RECALL` | P7 tutor (drill) |
| Open remediation | `REMEDIATION` | P8 remediation |

Action names match the V10 `STUDY_ACTION_TYPES` CHECK constraint. The
module-local tuple in `study.py` mirrors that constraint deliberately, so the
orchestrator can validate before writing.

## 5. Plans and the daily plan

A plan row (`study_plans`) stores plan_id, learner, scope, objective, start
date, daily minutes, generated priorities (JSON), planner version, status and
timestamps. `build_study_plan()` is deterministic given
(curriculum, learner, review, assessment, config): the same inputs produce the
same plan. Re-planning writes a **new version** and retires the previous one —
existing plans are never mutated.

`get_today_plan(plan_id)` fits actions to the budget greedily in priority order
and reports the remainder:

```json
{"budget_minutes": 20, "planned_minutes": 15,
 "actions": [{"action": "TUTOR", "estimated_minutes": 15, "fits": true, "reason": "..."}],
 "did_not_fit": [{"topic": "Growth Plate Physiology", "action": "REVIEW", "estimated_minutes": 15}]}
```

Two honesty rules:

1. **Real durations.** Each action type has a real per-type duration; missions
   are not all fifteen minutes.
2. **Fit before drop.** An action is shortened to fit the remaining budget when
   that still leaves it meaningful; otherwise it lands in `did_not_fit`.
   Nothing is padded and nothing is silently discarded. This was a bug found by
   the E2E run: a 10-minute budget used to produce an empty day.

## 6. Mission lifecycle

```
created --> active --> completed
                 \--> abandoned
```

A mission row records: target topic + `curriculum_node_id`, objective,
`action_type`, `adaptation_profile`, a JSON step sequence
(`[{step, engine, mode, minutes}]`), `estimated_minutes`, evidence refs,
expected outcome, completion criteria (JSON), status, `tutor_session_id`,
`assessment_id`, `current_step`, `results` (JSON), timestamps and
`study_version`.

`start_study_mission()` opens the real engines:

- TUTOR / DRILL / PREREQUISITE_REPAIR / RECALL → `start_tutor_session(topic,
  mode=…, goal="10min"|"20min")`; the session id is persisted.
- ASSESS / REMEDIATION → `create_assessment(scope_type="topic",
  scope_node_id=…, item_count=5, mode="PRACTICE")`; the assessment id is
  persisted.

If an engine refuses (for example no eligible items), the mission records an
**honest abstention** (`{"launched": false, "reason": …}`) and is still created.
Mission creation never depends on an engine succeeding.

`complete_mission_action()` records one step in `results` and advances
`current_step`; harvesting is bounded to private aggregates (tutor
`questions_attempted/correct/incorrect/mean_score`, assessment
`percentage/passed/graded_items/pending_items`). Full transcripts stay inside
the engines. `complete_mission()` marks every remaining step done and closes the
linked `study_actions` row.

### Restart safety

Mission state, engine ids and step results are all persisted. A new process
calls `get_mission_state()` and `launch_mission_engines()` (idempotent) and
continues. The recovery E2E finished a mission in a second process with
`study_actions` count unchanged at 1 — step records live in `results`, so
resuming cannot duplicate events.

## 7. Knowledge gaps and readiness

`get_knowledge_gaps()` reports topics whose evidence is missing, thin, or
unverified, plus prerequisite gaps, as `{topic, gap, evidence_state,
severity}` entries. `get_readiness(topic)` returns a structured estimate
(driven by mastery, evidence coverage and assessment history) with the inputs
included, so a low score is explainable rather than mysterious.

## 8. Learner adaptation

`adaptation_profile(topic)` reads P6 only:

| Profile | Rule |
| --- | --- |
| `weak` | No history, or mastery < 40%. |
| `developing` | 40% ≤ mastery < 80%. |
| `strong` | mastery ≥ 80%, calibration within ±0.15. |
| `overconfident` | mastery ≥ 80% and calibration gap ≥ 0.15. |
| `underconfident` | mastery ≥ 80% and calibration gap ≤ −0.15. |

The profile is **metadata**: it changes which mission modes are suggested and
which artifact sections are emphasized. It is never written back into P6, and
it never changes the canonical content — only its rendering.

Verified end to end: nine real P6 events moved mastery `0.437 → 0.303`,
flipping the profile `developing → weak`; the same `content_id` then rendered a
study guide containing the `Suggested first step` scaffold that the developing
render did not contain.

## 9. Data model (V10)

| Table | Role |
| --- | --- |
| `study_plans` | Versioned plans; superseded plans are retired, not deleted. |
| `study_missions` | One mission per attempt, with steps, engine ids and results. |
| `study_actions` | Append-only planned-action log (the study record). |
| `content_items` | Canonical content, one row per (topic, sources, config, prompt, model). |
| `content_artifacts` | Rendered artifacts with version, checksum, status, provenance. |

`core/database/migrate_v10.py::ensure_study_v10` is additive (five `CREATE`s),
runs `ensure_assessment_v9` first so curriculum FKs resolve, verifies
`integrity_check` + `foreign_key_check`, and is idempotent. Backup:
`backups/medforge_pre_v10_backup.db`.

## 10. Privacy

Private data = canonical content, bounded evidence excerpts and learner history;
it stays in the local SQLite database. Learner identity used by missions is the
local learner key. Nothing in the study flow transmits learner history,
transcripts or products to a network service: the only outbound call in the
canonical path is the local model. Distributable output is limited to rendered
artifacts plus citation locators.

## 11. Limitations

- Topic state thresholds (`40%`, `85%`, 7-day review cutoff) are heuristics, not
  validated pedagogy; they are centralized so they can be tuned in one place.
- The planner is greedy over a priority list; it does not solve an optimal
  multi-day schedule, and it deliberately never reshuffles a user's plan on its
  own.
- `harvest_mission_results` returns aggregates, not per-item diagnosis; deep
  analysis stays in P6/P8.
- With no syllabus imported, the orchestrator returns an empty curriculum plus
  the SR/assessment counts it does know — correct, but not useful until
  curriculum data exists.
- Local-model canonical generation is stochastic: unsanitizable output falls
  back to deterministic evidence sentences, so the model mode is not guaranteed
  on every run (the fallback is always safe and always cited).

## 12. Privacy/limitations cross-reference

See `docs/PRODUCT_FACTORY.md` for the canonical content model, caching,
versioning and rendering, and `docs/P9_MATRIX.md` §4 for the executed evidence.
