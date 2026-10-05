# P6 — Recency-Weighted Learner Model: Audit & Design Matrix

**Audit date:** 2026-10-05 · **Base commit:** `9fe0e3c` (P4 complete, 94/94 tests,
migration baseline 6.0.0).

Live DB before P6: `learner_mastery=0, learner_weaknesses=0, study_sessions=0,
weaknesses=0, interactive_sessions=0, spaced_repetition_queue=119,
review_log=0, curriculum_nodes=0` — no learner history exists yet, so P6 can
materialize new state without inventing history; the 119 queued cards must be
preserved untouched.

## Forensic audit of every learner field

| Field (table) | Written by | Read by | Verdict |
|---|---|---|---|
| `learner_mastery.mastery_score` | `update_mastery` (incremental mean), `record_session`, `log_study_result` | `learner_snapshot`, `curriculum_progress`, `_weak_topic_ids` | **KEEP** as the legacy compatibility surface (existing tests pin its exact values); the recency-weighted estimate lives in new state |
| `learner_mastery.confidence_score` | same (pass-ratio %) | snapshot, progress | **KEEP** (legacy semantics); P6 confidence is a separate, explicitly named estimate |
| `learner_mastery.total_attempts / successful_attempts / last_attempt_at` | same | snapshot, progress | **KEEP** |
| `learner_weaknesses.*` (concept, error_count, severity, is_resolved, resolved_at, first/last_observed_at) | `record_weakness`, `resolve_weakness`, `review_card` lapses, `log_study_result` | snapshot, `due_items` weak ordering, `resolve_weakness` | **EXTEND** (additive columns only; escalation/resolution behavior untouched) |
| `interactive_sessions.*` | `start_session`, `record_session`, `log_study_result` | snapshot, learner tests | **MODIFY** — `log_study_result` gains an explicit `session_id`; completion can no longer be ambiguous when an id is given (legacy topic-default retained for compatibility) |
| `spaced_repetition_queue.*` | `import_flashcards`, `review_card` | `due_items`, snapshots, dashboard | **KEEP** — scheduling math untouched; P6 consumes grades as learning evidence |
| `review_log.*` | `review_card` | `review_analytics` | **KEEP** (append-only history) |
| `curriculum_nodes` / `prerequisites` | P2 | progress, tree, links | **KEEP** — P6 only reads (topic resolution + prerequisite graph) |
| Study session scores → mastery | `log_study_result` → `update_mastery` | — | **MODIFY** — sessions become learning attempts that feed the recency-weighted model |
| Review grades → learner state | only lapse/graduation weakness side-effects | — | **MODIFY** — every review records a learning attempt and updates model state |
| Recency weighting, uncertainty, consistency, calibration, priority, prerequisite impact, recalculation, model version | — | — | **MISSING → build** |

## P6 requirement classification

| Requirement | Verdict |
|---|---|
| Persistent learning-attempt/performance events | **MISSING → `learning_attempts` (V7)** |
| Materialized current learner state | **MISSING → `learner_model_state` (V7)**; legacy `learner_mastery` kept and still written |
| Recency weighting | **MISSING → explicit exponential decay, configurable half-life** |
| Historical evidence retained | events are append-only; model reads all of them with decay |
| Uncertainty / evidence strength | **MISSING → effective sample size (Kish) → uncertainty** |
| Confidence distinct from mastery + calibration | **MISSING → attempt-level confidence, estimate, signed calibration gap (None when no data)** |
| Weakness state enriched (score, failure rate, low-confidence vs known, recovery) | **EXTEND `learner_weaknesses` + deterministic detector** |
| Prerequisite awareness | **MISSING → read-only risk API over the P2 graph (no auto-punishment)** |
| SR integration | **MODIFY `review_card`** to record evidence + update model (scheduling unchanged) |
| Explicit session completion | **MODIFY `log_study_result`/`record_session`** + new `complete_session` |
| Curriculum progress consumes new state | **MODIFY `curriculum_progress`** (additive fields) + `curriculum_report()` |
| Adaptive study priority | **MISSING → deterministic weighted score, components exposed** |
| Recalculation / reproducibility | **MISSING → per-key deterministic rebuild, verified against materialized state** |
| Model versioning | **MISSING → `p6-rwm-v1` stored per state row** |
| Legacy data compatibility | **KEEP** — no legacy table is rebuilt/dropped; `update_mastery` behavior unchanged; new weakness columns default sanely for legacy rows |

## Mathematical model (deterministic, documented defaults)

- **Decay:** `w = exp(-ln2 · age_days / half_life)`, `half_life = 21 days`
  (`T.LEARNER_HALF_LIFE_DAYS`), `age = now − answered_at`. Explicit, versioned,
  numerically stable; no opaque magic coefficients.
- **Mastery estimate (0..1):** shrinkage toward an uninformed prior
  `mastery = (Σ wᵢsᵢ + k₀·p₀) / (Σ wᵢ + k₀)` with `p₀ = 0.5`, `k₀ = 1.5`
  pseudo-observations. This is why one success is not "mastered" and ten old
  successes do not outvote several recent failures.
- **Evidence strength:** effective sample size `ESS = (Σw)² / Σw²`;
  `uncertainty = 1 / sqrt(1 + ESS)` ∈ (0,1]. It is a model estimate, **not** a
  statistical confidence interval.
- **Consistency:** weighted std-dev `σ`, `consistency = max(0, 1 − 2σ)`.
- **Recent vs historical:** weighted mean within / older than
  `T.LEARNER_RECENT_WINDOW_DAYS = 14`.
- **Confidence calibration:** `confidence_estimate = mean(reported confidence)`,
  `calibration_gap = confidence_estimate − mastery` (positive = overconfident).
  `None` when no confidence observations exist — never fabricated.
- **Weakness:** known when `evidence_count ≥ 3` and mastery `< 0.60` with a
  recent failure or `recent_performance < 0.60`; possible/low-confidence when
  mastery `< 0.60` but evidence `< 3`; recovery when mastery `≥ 0.75` and
  `recent_performance ≥ 0.70` (auto rows only, never manual/review rows).
- **Study priority:** `0.35·weakness + 0.20·uncertainty + 0.20·overdue +
  0.15·recent_failure + 0.10·prerequisite_impact`, each normalized 0..1 and
  returned per component for inspection.

## Files to change (planned smallest-safe patch)

| File | Change | Risk |
|---|---|---|
| `core/database/schema.py` | `V7_SCHEMA_DDL` (2 tables) + weakness column list | additive |
| `core/database/migrate_v7.py` | **new** — `ensure_learner_model_v7()`, 7.0.0, idempotent, backup option, integrity | mirrors v6 |
| `releases/.../medforge/types.py` | model version + half-life/window/threshold/weight constants | additive |
| `releases/.../medforge/learner_model.py` | **new** — events, RWM state, weaknesses, prerequisite risk, priority, recalculation, report APIs | isolated |
| `releases/.../medforge/learner.py` | explicit `session_id` + `complete_session`; attempt hooks in `review_card`, `log_study_result`, `record_session`; snapshot extra keys | 3 small hook blocks |
| `releases/.../medforge/curriculum.py` | additive learner fields in `curriculum_progress` | 1 block |
| `releases/.../medforge/__init__.py`, `medforge_core.py` | exports; `learner`, `mastery`, `weaknesses`, `history`, `recalculate`, `study-priority` | additive |
| `releases/.../dashboard.py` | LEARNER tab (insert after REVIEW, shift later indexes) | additive |
| `tests/test_learner_model.py` | **new** — 30-item directive list, offline, explicit timestamps | isolated |
| docs | `P6_MATRIX.md` (this), `LEARNER_MODEL.md` new; IMPLEMENTATION_MATRIX / ARCHITECTURE / V3_SCHEMA / CLI_USAGE | docs |

Not touched: `evidence.py`/P4 tables, `textbook.py`, `retrieval.py`,
`generation.py`, `product.py`, `storage.py`, SR scheduling math.

## Acceptance criteria → proof

| Criterion | Proof |
|---|---|
| events persistent | `learning_attempts` + tests |
| current state persistent | `learner_model_state` + tests |
| mastery recency-weighted | decay test (old 90s vs recent 45) |
| historical evidence retained | append-only events; recalculation reads all |
| uncertainty/evidence strength | ESS + uncertainty tests (1 vs 25 observations) |
| confidence ≠ mastery | separate fields + mismatch test |
| calibration when data exists | calibration gap tests (over/under-confident) |
| weaknesses auto-detected / recover | detector tests + recovery test |
| prerequisite relationships exposed | prerequisite risk tests |
| SR integrates | review failure/success tests |
| explicit session ids | two-sessions test (completing one does not close the other) |
| curriculum progress consumes state | curriculum report tests |
| deterministic priority | priority component tests |
| full recalculation | rebuild == materialized equality test |
| versioned model | `p6-rwm-v1` persistence test |
| migration additive/idempotent | V7 migration tests |
| live data preserved | before/after counts + integrity + backup |
| prev 94 tests green | full regression |
| live integrity | `PRAGMA integrity_check` + `foreign_key_check` |
| CLI + dashboard | live E2E + browser verification |
| docs + commit/push | final report |

## What P6 does NOT do

No P7 tutor, no P8 assessment framework (only the event pathway + hooks), no
LLM-based math (all arithmetic deterministic and local), no schema redesign of
P2–P4, no replacement of SM-2/FSRS, no psychometric validation claims — the
mastery value is a learner-model **estimate**, explicitly versioned and
recomputable from history.

---

# Execution results

Full model documentation: `docs/LEARNER_MODEL.md`. Migration record:
`docs/V3_SCHEMA.md` (§V7).

## Tests

| Stage | Result |
|---|---|
| Baseline (P4 complete) | 94/94 passing, migration 6.0.0 |
| New P6 suite (`tests/test_learner_model.py`) | **32/32 passing** (0 failures) |
| Full regression after all edits | **126/126 passing** (`pytest tests/ -q`) |
| Regression re-run after the CLI wiring fix | **126/126 passing** |

## Live database migration (7.0.0)

Backup: `backups/medforge_pre_v7_backup.db` (696,320 bytes, `integrity_check`
`ok`, migrations present: 3.0.0–6.0.0 → i.e. a true pre-V7 snapshot).
Applied with `core/database/migrate_v7.py --db database/medforge.sqlite3`.

| Table | Before | After |
|---|---|---|
| chunks | 47 | 47 |
| curriculum_nodes / prerequisites | 0 / 0 | 0 / 0 |
| medical_sources | 0 | 0 |
| textbook_documents / textbook_editions | 0 / 0 | 0 / 0 |
| claims | 5 | 5 |
| evidence | 18 | 18 |
| claim_evidence | 10 | 10 |
| verification_runs | 10 | 10 |
| learner_mastery | 0 | 0 |
| learner_weaknesses | 0 | 0 |
| study_sessions / interactive_sessions | 0 / 0 | 0 / 0 |
| spaced_repetition_queue | 119 | 119 |
| review_log | 0 | 0 |
| **learning_attempts** | absent | 0 |
| **learner_model_state** | absent | 0 |

Migration version 7.0.0 recorded; `PRAGMA integrity_check` = `ok`;
`PRAGMA foreign_key_check` = no violations; 10 weakness columns added
exactly once (idempotent re-run adds none).

## Isolated end-to-end run (`MEDFORGE_HOME=/tmp/mf-p6-cli`)

CLI: `syllabus` (2 weeks, 3 topics) → `prereq "Growth plate physiology|GH axis|strict"` →
three `study-log "GH axis"` (30/28/25) + two `study-log "Growth plate physiology"` (90/85):

| Command | Observed |
|---|---|
| `learner` | topics_tracked 2, events_total 5, avg_mastery 53.3, **known_weaknesses 1**; weakest `gh-axis` 35.1 |
| `mastery "GH axis"` | mastery 0.3511, evidence 3, uncertainty 0.5, consistency 0.9589, recent_performance 0.2767, model `p6-rwm-v1`; confidence `has_confidence_data=false`, calibration `null` |
| `mastery "Growth plate physiology"` | mastery 0.7143 (unchanged by the risk read); risks: `GH axis` mastery 0.3511, weak=true, impact 0.4148 |
| `weaknesses` | `gh-axis`, severity **high**, `low_confidence=0`, failure_count 3, "Recency-weighted mastery estimate is 35% with a recent failure" |
| `history "GH axis"` | 3 attempts, each `source=session`, linked to its own `session_id` (1/2/3) |
| `recalculate all` | keys 2, **mismatches []**, version `p6-rwm-v1` |
| `study-priority` | weights exposed; `gh-axis` 0.4362 (weakness 0.53 + uncertainty 0.50 + recent_failure 1.00); `growth-plate-physiology` 0.1736 incl. **prerequisite_impact 0.4148** |
| `curriculum` | topics show legacy mastery **and** `rwm_mastery/rwm_uncertainty/rwm_evidence_count`; `learner_model_tracked=2`; duplicates [] |
| `mastery` (no arg) | legacy snapshot unchanged (keys: mastery, model, recent_sessions, summary, weaknesses) |

Two CLI defects were found by this end-to-end run and fixed: a duplicated
`mastery` branch made the new topic form dead code, and `get_recent_performance`
was missing from the `medforge_core` imports. Both verified after the fix.

## Mandatory demonstration (actual outputs, `MEDFORGE_HOME=/tmp/mf-p6-demo`)

```
2026-01-01  score=0.90  -> mastery=0.66    ev=1  unc=0.7071
2026-01-07  score=0.85  -> mastery=0.7042  ev=2  unc=0.5792
2026-01-14  score=0.90  -> mastery=0.7378  ev=3  unc=0.5057
2026-02-28  score=0.45  -> mastery=0.5532  (was 0.7378)  recent_performance=0.45  Δ=-0.2878
```

Weakness at Day 60: `created=1`, severity **medium**, `weakness_score=0.078`,
`failure_count=1`, `recent_failure_rate=0.3333`, `low_confidence=0`,
`origin=learner_model`, message "Recency-weighted mastery estimate is 55% with a
recent failure".

Recovery: Day 61 = 0.80 → 0.6142; Day 68 = 0.90 → **0.67**; detect →
`recovered=1`; unresolved list empty; resolved row `is_resolved=1`,
`recovered_at=2026-03-08T09:00:00Z`, `review_priority=0.0`; **6 attempts
preserved**; `get_recent_performance` = recent 0.7341 vs historical 0.8838
(Δ −0.1497).

Confidence (separate variable): high confidence/low performance → estimate
0.95, mastery 0.376, gap **+0.574**, `overconfident`, mismatch true; low
confidence/high performance → estimate 0.15, mastery 0.7789, gap **−0.6289**,
`underconfident`, mismatch true; no confidence observations → estimate `None`,
`has_confidence_data=false` (calibration not fabricated).

Single isolated failure (low-confidence rule): one 0.30 attempt → severity
**low**, `low_confidence=1`, message "…(limited evidence)".

Recalculation: `matches_stored: true`, rebuilt = stored = 0.67, model
`p6-rwm-v1`; `recalculate_all` → keys 3, mismatches []).

## Dashboard verification

Restarted Streamlit on `http://127.0.0.1:8501/` (the pre-existing process held a
stale `medforge_core` in memory and showed
`module 'medforge_core' has no attribute 'learner_summary'`). After restart the
LEARNER tab (index 3 of 11) rendered the empty-state honestly: topics tracked
0, avg mastery 0%, events 0, known/possible weaknesses 0/0, calibration
mismatches 0, "Model p6-rwm-v1 · half-life 21 days · overdue cards 119" — the
live DB has no learner data yet, so nothing was fabricated.

A second instance with `MEDFORGE_HOME=/tmp/mf-p6-demo` on port 8502 (live DB
untouched, stopped afterwards) proved the data path: topics tracked 4, avg
mastery 56%, events 13, possible weaknesses 1, **calibration mismatches 2**
(`renal-physiology` 0.95 vs 0.376; `cardiac-cycle` 0.15 vs 0.7789), strongest/
weakest tables, "Recently declining" (`growth-hormone-physiology` Δ −0.1497),
recommended priorities with weakness column, and the topic detail with
"No learner confidence observations yet — calibration is not fabricated."
Clicking **Recalculate this topic from history** returned
"Materialized state matches recalculation."
