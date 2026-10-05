# MedForge Recency-Weighted Learner Model (P6)

**Model version:** `p6-rwm-v1` · **Migration:** `7.0.0` / `v7_recency_weighted_learner`

> The learner-model score is an **estimate built from this learner's own
> evidence**, not a validated psychometric measurement. It is deterministic,
> inspectable, and recomputable from stored history — nothing more is claimed.

P6 replaces "mastery is the running mean of session scores" with a persistent,
recency-weighted learner model. The legacy running mean is **kept** (see
[Compatibility](#compatibility-with-the-legacy-model)) so existing behaviour and
tests stay valid; the new model adds the parts a tutor needs: recency,
confidence, uncertainty, weakness severity, prerequisite impact, review
priority, and reproducibility.

---

## 1. Data model

### `learning_attempts` (append-only evidence)

One row per observed performance event. Nothing is derived here; recalculation
always reads these rows.

| Column | Notes |
|---|---|
| `attempt_id` | PK, monotonic |
| `session_id` | FK → `interactive_sessions` (`ON DELETE SET NULL`) when the event belongs to a session |
| `curriculum_node_id` | FK → `curriculum_nodes` (`ON DELETE SET NULL`) when the topic is mapped |
| `mastery_key` | topic slug — the join key of the model |
| `item_type` | `session`, `card`, `question`, `concept`, `topic`, `manual` (`LEARNING_ATTEMPT_ITEM_TYPES`) |
| `item_id` | card digest / question id where available |
| `presented_at`, `answered_at` | ISO-8601; `answered_at` drives decay |
| `score` | 0..1 (normalized from the 0–10 rubric or 0–100) |
| `correct` | 0/1 where correctness applies, NULL otherwise |
| `learner_confidence` | learner-reported 0..1 where collected, NULL otherwise |
| `response_time_seconds` | where available |
| `source` | `session`, `review`, `manual`, `backfill` (`LEARNING_ATTEMPT_SOURCES`) |
| `content_version` | pack/content version or hash where available |
| `created_at` | insert time |

Indexes: `(mastery_key, answered_at)`, `(session_id)`, `(curriculum_node_id)`,
`(item_type, item_id)`.

### `learner_model_state` (materialized estimate)

One row per `mastery_key`, upserted on every new attempt:

| Column | Meaning |
|---|---|
| `mastery_score` | recency-weighted estimate, 0..1 |
| `weighted_evidence` | `Σ w` — decayed evidence mass |
| `evidence_count` | number of attempts seen (never reduced) |
| `recent_performance` | weighted mean inside the recent window (14 d) |
| `historical_performance` | weighted mean of attempts older than the recent window |
| `consistency` | `max(0, 1 − 2σ)` over weighted observations |
| `uncertainty` | `1 / sqrt(1 + ESS)` |
| `confidence_estimate` | mean reported confidence (NULL when no observations) |
| `confidence_calibration` | `confidence_estimate − mastery_score` (signed; NULL when no data) |
| `last_attempt_at`, `last_success_at`, `last_failure_at` | timestamps |
| `model_version`, `half_life_days` | provenance of the computation |
| `mastery_updated_at` | evaluation timestamp used for decay |

### `learner_weaknesses` (extended, additive)

Ten columns are added (`curriculum_node_id`, `origin`, `weakness_score`,
`failure_count`, `recent_failure_rate`, `prerequisite_impact`,
`review_priority`, `low_confidence`, `last_failure_at`, `recovered_at`).
`ALTER TABLE ADD COLUMN` never rewrites rows: legacy weaknesses keep their data
and default to `origin='manual'`, `low_confidence=0`.

---

## 2. Decay and mastery

**Decay (configurable, explicit):**

```
w(age) = exp(-ln2 · age_days / half_life_days)        half_life = 21 days
```

`T.LEARNER_HALF_LIFE_DAYS = 21.0` — an attempt 21 days old counts half as much
as one observed now; 42 days old, a quarter. `decay_weight()` clamps `age < 0`
to weight 1 and rejects `half_life <= 0`.

**Mastery (shrunk weighted mean):**

```
mastery = (Σ wᵢ·sᵢ + k₀·p₀) / (Σ wᵢ + k₀)     p₀ = 0.5, k₀ = 1.5
```

`T.LEARNER_PRIOR_MASTERY = 0.5` and `T.LEARNER_PRIOR_STRENGTH = 1.5` act as
pseudo-observations. Consequences, all deliberate:

- One success is **not** mastery (one 0.90 → 0.66).
- Ten old successes do not outvote several recent failures once decay is
  applied (verified: ten 0.95 attempts then three 0.30 failures → mastery
  < 0.60).
- Sustained daily success converges toward the observed score; the 0.9
  asymptote is about 0.88 because the prior never fully disappears.

**Evidence strength:**

```
ESS = (Σw)² / Σw²          uncertainty = 1 / sqrt(1 + ESS)  ∈ (0, 1]
```

ESS is the Kish effective sample size: 1 observation → ESS 1 → uncertainty
0.707; 25 consistent observations → uncertainty < 0.2. Uncertainty describes
*evidence mass*, not a statistical confidence interval.

**Consistency:** weighted population standard deviation `σ`;
`consistency = max(0, 1 − 2σ)`. A wildly oscillating topic scores below 0.5.

**Recent vs historical:** attempts inside `LEARNER_RECENT_WINDOW_DAYS = 14`
form `recent_performance`; older attempts form `historical_performance`. Their
difference is the trend surface used by the dashboard.

**Thresholds:** `LEARNER_PASS_THRESHOLD = 0.70`,
`LEARNER_WEAK_THRESHOLD = 0.60`,
`LEARNER_RECOVERY_THRESHOLD = 0.75`.

### Worked example (deterministic, from the live demonstration)

| Day | Score | Mastery after | Evidence | Uncertainty |
|---|---|---|---|---|
| 1 | 0.90 | 0.6600 | 1 | 0.7071 |
| 7 | 0.85 | 0.7042 | 2 | 0.5792 |
| 14 | 0.90 | 0.7378 | 3 | 0.5057 |
| 60 | 0.45 | **0.5532** | 4 | — |

The recent failure moved the estimate by −0.1846 (trend Δ −0.2878) even though
three of four attempts were ≥ 0.85 — exactly what the old mean could not do
(the legacy mean would read 0.775).

Recovery: Day 61 = 0.80 → 0.6142; Day 68 = 0.90 → **0.67**, with all six
attempts retained and the weakness auto-resolved.

---

## 3. Confidence calibration (separate variable)

Mastery and learner confidence are different quantities and are stored
separately:

- `confidence_estimate` = mean reported confidence over attempts that carried it
- `calibration_gap` = `confidence_estimate − mastery_score`
- `mismatch` = `|gap| ≥ 0.2`; `direction` ∈ `overconfident`, `underconfident`, `aligned`

When no attempt reported confidence, every calibration field is `NULL` /
`has_confidence_data = false` — calibration is **never fabricated**.

| Case | Observed | Result |
|---|---|---|
| high confidence / low performance | 3 × 0.30 at confidence 0.95 | estimate 0.95, mastery 0.376, gap **+0.574**, `overconfident`, mismatch |
| low confidence / high performance | 3 × 0.95 at confidence 0.15 | estimate 0.15, mastery 0.7789, gap **−0.6289**, `underconfident`, mismatch |
| no confidence data | performance-only attempts | estimate `None`, gap `None`, direction `None` |

---

## 4. Weakness detection

`detect_weaknesses(topic=None, now=None)` derives weaknesses from the
materialized states. Only rows with `origin='learner_model'` are created,
updated, or recovered; manual and review rows are never touched.

| Condition | Result |
|---|---|
| mastery < 0.60, evidence < 3 | **possible** weakness — `low_confidence=1`, severity `low` |
| mastery < 0.60, evidence ≥ 3, recent failure (`recent_failure_rate > 0`) | **known** weakness — severity from the estimate: `< 0.35` critical, `< 0.50` high, else medium |
| mastery < 0.60, evidence ≥ 3, no recent failure | possible / low-confidence again (old failures only) |
| mastery ≥ 0.75 and `recent_performance ≥ 0.70` | **auto-recovered** (row kept: `is_resolved=1`, `recovered_at` set, `review_priority=0`) |
| mastery ≥ 0.65, the two newest attempts both pass, mean of last two ≥ 0.75 | **auto-recovered** (fast path after a genuine comeback) |

One isolated mistake never becomes a verdict: a single 0.30 attempt yields
`severity='low'`, `low_confidence=1`, message
"…(limited evidence)".

`weakness_score = clamp((0.60 − mastery) / 0.60, 0, 1)`;
`failure_count` counts attempts below the pass threshold;
`recent_failure_rate` is the failure share of the newest three attempts.

---

## 5. Prerequisite awareness

`get_prerequisite_risks(topic)` is **read-only**. It resolves the topic through
P2 `topic_path()`, then reads the `prerequisites` graph and reports each
prerequisite's own mastery, evidence count, a `weak` flag (< 0.60) and an
`impact` value `(0.60 − prereq_mastery) / 0.60`. A topic's own estimate is
never modified by having a weak prerequisite — the gap is surfaced *and fed to
the priority signal*, not silently deducted from mastery.

Only prerequisites that are themselves tracked appear as risks; a topic outside
the curriculum returns `node_id: null` with no invented risks.

---

## 6. Review / study priority

`study_priority()` (alias `get_review_priority()`) scores every tracked topic
deterministically:

```
priority = 0.35·weakness
         + 0.20·uncertainty
         + 0.20·overdue            (min(1, overdue_days / 7))
         + 0.15·recent_failure     (failure share of the newest three attempts)
         + 0.10·prerequisite_impact
```

Weights live in `T.LEARNER_PRIORITY_WEIGHTS`. Every component is normalized to
0..1 and returned per item (`components`, `reasons`) so a recommendation is
always explainable. Sorting is by `(−priority, topic)` — byte-identical output
for identical data. `overdue` comes from `spaced_repetition_queue` due dates
for the same mastery key.

This is a **signal**, not a planner: P7 will consume it.

---

## 7. Recalculation and reproducibility

`recalculate_mastery(topic)` re-reads the stored attempts and rebuilds the
state at the stored `mastery_updated_at`. Because the decay math is a pure
function of `(attempts, evaluation_time, half_life, version)`, the rebuild must
equal the materialized row field-for-field — `matches_stored: true`.
`recalculate_all()` reports every key and any mismatch (expected: none).

Passing an explicit `now` re-evaluates decay as of that time, re-materializes
the state, and correctly reports `matches_stored: false` first (the stored row
was evaluated earlier), after which the rebuilt state is once again exactly
reproducible. History length never changes: recalculation never deletes
attempts.

### P7 interface surface

All stable, tested, and used by the CLI/dashboard:

```python
get_mastery(topic)              # current estimate + evidence strength
get_weaknesses(include_resolved=False)
get_recent_performance(topic)   # recent vs historical + trend
get_confidence(topic)           # estimate, calibration gap, direction
get_prerequisite_risks(topic)   # read-only prerequisite gaps
get_review_priority(limit)      # deterministic priority signal
record_learning_event(...)      # append evidence (+ re-materialize)
recalculate_mastery(topic)      # rebuild from history
```

Supporting readers: `learner_history`, `model_state_rows`, `learner_summary`,
`curriculum_report`.

---

## 8. Evidence sources (hooks)

| Flow | Attempt recorded as |
|---|---|
| `record_session` (scored) | `source='session'`, `item_type='session'`, linked to the session |
| `log_study_result` / `complete_session` | same, with the explicit completed session id |
| `review_card(item_id, grade)` | `source='review'`, `item_type='card'`, score `grade/5`, `correct = grade ≥ 3` |
| direct/backfill | `source='manual'` or `'backfill'` |

Failed attempt-recording never breaks the legacy flow: the bridge returns
`learner_model_error` instead of raising, so an SR review or session log cannot
be lost because of a model problem.

---

## 9. Compatibility with the legacy model

- `learner_mastery` (incremental mean, pass-ratio confidence) is **still
  written and read** by `learner_snapshot`, the STUDY tab, and
  `curriculum_progress`. P6 fields are *added* alongside it
  (`rwm_mastery`, `rwm_uncertainty`, `rwm_evidence_count`,
  `rwm_recent_performance`).
- `learner_snapshot()` gains a `model` key — the legacy keys are unchanged.
- SM-2/FSRS scheduling is untouched; reviews simply also record evidence.

## 10. Limitations

1. **Estimate, not measurement.** The score is a deterministic summary of this
   learner's attempts. It is not calibrated against any external standard and
   makes no psychometric claim.
2. **Self-reported session scores.** Mission self-scores are noisy inputs; P6
   adds question-level fields (`item_type`, `correct`, `confidence`, response
   time) but the full assessment engine is P8.
3. **Uncertainty is evidence mass, not a confidence interval.** Low evidence
   widens the shrinkage toward the 0.5 prior instead of producing a formal
   interval.
4. **Fixed half-life per model version.** One global half-life (21 days) is a
   documented default; per-topic or per-item half-lives are future work. The
   half-life is stored per state row, so changing it is a versioned decision.
5. **Prerequisite impact is first-order.** Only direct prerequisite edges are
   considered; transitive chains are not propagated.
6. **Recovery is heuristic.** Two strong recent passes (or mastery ≥ 0.75 with
   recent performance ≥ 0.70) resolve a model weakness; a resolved row keeps
   its history but the rule is not a formal mastery test.
7. **No forgetting of stored evidence.** Attempts are append-only; only their
   *weight* decays. Storage grows with usage.
8. **`mastery_key` is the topic slug.** Topics are joined by slug, matching the
   existing `learner_mastery.topic_id` namespace; a topic rename would create a
   new key.

## 11. Versioning

`T.LEARNER_MODEL_VERSION = "p6-rwm-v1"` is stored on every state row and
returned by every API. Changing decay, prior, thresholds, or weights requires a
new version string (and a recalculation pass), so a stored estimate is never
silently reinterpreted under different math.
