# P9 — Study Intelligence / Product Factory Integration (V10)

Status: **audit + design; execution recorded at the end of this file.**
Baseline entering P9: P8 `69c6f30`, migration `9.0.0`, 210/210 tests green,
learner model `p6-rwm-v1`, tutor `p7-tutor-v1`, assessment `p8-assessment-v1`.

## 1. Forensic audit — public API map

### Engines P9 must orchestrate (all KEEP — authoritative, unchanged)

| Module | P9 uses (public API only) |
| --- | --- |
| `curriculum.py` (P2) | `curriculum_tree`, `curriculum_progress`, `topic_path`, `find_node`, `get_children`, `import_syllabus` |
| `textbook.py` (P3) | `textbook_evidence_for_topic`, register/ingest/link (indirect through tutor + product flows) |
| `evidence.py` (P4) | `evidence_snapshot`, `claims_list`, `verify_product_claims`, `store_evidence` |
| `learner_model.py` (P6) | `get_mastery`, `get_recent_performance`, `get_weaknesses`, `get_prerequisite_risks`, `study_priority`, `record_learning_event`, `model_state_rows` |
| `learner.py` (SR) | `card_item_id`, `import_flashcards`, `due_items`, `start_session`, `log_study_result`, `review_card`, `review_analytics` |
| `tutor.py` (P7) | `start_tutor_session`, `get_tutor_state`, `select_tutor_target`, `complete_tutor_session`, `get_tutor_summary`, `resume_tutor_session` |
| `assessment.py` (P8) | `create_assessment`, `get_assessment_state`, `submit_assessment_answer`, `complete_assessment`, `get_assessment_result`, `get_assessment_remediation`, `create_blueprint` |

### Product factory (the main P9 EXTEND target)

| Symbol | Verdict | Why |
| --- | --- | --- |
| `product.build_product` | **EXTEND** | Ten independent `generate_text` calls per topic (study guide, quiz, viva, cases, cheat sheet, mind map, two scripts, carousel, description) — the exact contradiction risk P9's canonical content model removes. Kept as the legacy path; P9 adds a canonical renderer beside it. |
| `product.study` | **EXTEND** | Static 20-minute LLM mission with no learner state, no plan, no persistence. P9 replaces the dashboard/CLI flow with real missions while keeping `study()` for compatibility. |

CLI naming decision (this phase): the P9 surface is `study-intel` — `status`,
`recommend`, `plan`, `today`, `start`, `engines`, `next`, `complete`, `finish`,
`history`, `gaps`, `readiness`, `profile`, `missions`, `mission`. The legacy
`study` command (P1 product study helper) and `product` command are untouched.
New `product-intel` subcommands (all deterministic, P3/P4-grounded):
`product-intel build <topic>[|types|outdir]` (one canonical generation +
rendering, evidence-free topics refused with an honest error), `status`,
`inspect <content_id>`, `artifacts [content_id]`, `provenance <artifact_id>`,
`consistency <content_id>`, `regenerate <content_id>[|outdir]` (renders again
from stored canonical; source changes produce a NEW content_id — history is
never rewritten).
| `product.ask`, `product.status` | KEEP | unchanged. |
| `generation.generate_text` | REUSE | single LLM entry point P9 also uses. |
| `generation.citation_audit`, `parse_tsv_cards`, `parse_cards_lenient` | REUSE | reused for canonical-rendered artifacts. |
| `retrieval.source_pack` / `hybrid_retrieve` | REUSE | P9 retrieval primitive for products without textbook links. |
| `export.write_pdf`, `make_anki` | REUSE | rendering targets. `make_anki` derives deck id from topic — stable across regenerations. |
| `learner.card_item_id` | REUSE | content-addressed card identity already stable; P9 keys flashcards on it (dedup requirement already half-solved). |
| `curriculum.curriculum_progress` | **EXTEND** | joins mastery + P6 state per topic; P9 adds status classification (`NOT_STARTED…REVIEW_DUE`), evidence coverage and next action — computed in P9, not by editing P2. |
| `dashboard.py` STUDY tab | **EXTEND** | static `mf.study` mission → STUDY HOME (today plan, why, one-click start). |

### MISSING (built in P9)

- Study intelligence layer: `get_study_status`, `recommend_next_action`
  (machine-readable reasons), knowledge-gap detection, readiness estimate.
- Persistent study plans (reproducible, versioned) + daily plan with time
  budgets.
- Study missions built from real state, with explicit completion rules and
  resumability.
- Canonical content representation (`generate_canonical_content`) + product
  rendering from it (`render_study_products`) with per-artifact provenance.
- Learner-adapted generation (weak/strong/under/overconfident profiles read
  from P6, no P6 math changes).
- Product run persistence: content version, status (`DRAFT…BLOCKED`), caching,
  regeneration without history loss.
- Private vs distributable bundle separation.
- V10 migration: `study_plans`, `study_missions`, `study_actions`,
  `content_items` (canonical), `content_artifacts`, plus indexes.

### CONFLICTS and resolutions

1. **`product.study` static mission vs P9 missions** — keep `study()`
   untouched (compat), P9 missions live in `study.py` and the dashboard/CLI
   switch to them. No deletion, no signature change.
2. **`build_product` regeneration duplicates flashcards** — already mitigated
   by `card_item_id` idempotent import; P9's canonical renderer preserves the
   same property by deriving card ids from canonical fact ids.
3. **`make_anki` deck id = sha1(topic)** — stable per topic; regenerated
   content adds/updates notes rather than new decks. P9 keeps one deck per
   topic and passes stable card ids through.
4. **Legacy `weaknesses`/`learner_mastery` tables vs P6** — P9 reads P6 only
   for decisions; V3 tables remain for legacy flows.
5. **Web research automatic in `build_product`** — P9 product flow makes web
   expansion opt-in (`include_web=False` default in the canonical path);
   existing secure fetch (HTTPS/domain/size limits in `ingestion.py`) is
   preserved untouched.

### Model orchestration policy (P9)

Deterministic-first: selection, scoring, scheduling, budget fitting, gap
detection, readiness, caching and validation are pure Python. LLM is used for
exactly one canonical generation per (topic, source-set, config) and optional
adaptation phrasing. Everything else renders deterministically from the
canonical content.

## 2. Design

### 2.1 Study status and targets

`get_study_status()` aggregates (read-only): `curriculum_progress()` + P6
`get_mastery`/`get_weaknesses`/`get_prerequisite_risks`/`study_priority` + SR
`due_items` + P8 assessment history (from `assessment_attempts`/
`learning_attempts`) + evidence coverage per topic (chunks linked via P3
`textbook_evidence_for_topic`, cached per call). Curriculum topics get a
derived state: `NOT_STARTED | IN_PROGRESS | STUDIED | ASSESSING |
MASTERED_ESTIMATE | REVIEW_DUE` (explicit thresholds, documented; never
"read once = mastered").

`recommend_next_action()` scores candidate topics deterministically:
P6 `study_priority` (weakness/decline/overdue/prerequisite risk) + assessment
gap penalty (studied but never assessed) + evidence-availability gate +
review-due boost + curriculum adjacency + goal modifier. Output carries
machine-readable `reasons: [{code, detail, weight}]` — never just a score.

### 2.2 Study actions

Action selection maps state → action deterministically:

| State | Action |
| --- | --- |
| new topic, no evidence of study | `NEW_TEACHING` (tutor) |
| weak + repeated failures | `DRILL` (tutor drill mode) |
| prerequisite weak | `PREREQUISITE_REPAIR` |
| strong + reviews due | `SPACED_REVIEW` |
| studied, never/poorly assessed | `ASSESS` (P8) |
| assessment remediation open | `REMEDIATION` |
| recall-only need | `RECALL` |

Each action knows its engine entry point and estimated minutes.

### 2.3 Plans and the daily plan

`study_plans` rows persist: plan_id, learner, scope, objective, target date,
daily minutes, generated actions (JSON), planner_version, status, seed,
created/updated. `build_study_plan()` is deterministic given
(curriculum, learner, review, assessment, config) — same inputs, same plan.
`get_today_plan(plan_id)` fits actions to `daily_minutes` by priority order
(greedy, never splitting an action; leftovers reported, never padded).
Plans are versioned; regeneration creates a new version row — history is
never rewritten.

### 2.4 Missions

`study_missions` rows: mission_id, plan_id (nullable), target topic/node,
objective, action sequence (JSON steps), estimated minutes, evidence basis
(evidence_ids), expected outcome, completion criteria (JSON), status
(`created → active → completed | abandoned`), started/completed_at, results.
`start_study_mission()` materializes steps from the chosen action (e.g.
TUTOR → teach/recall/practice/review steps bound to P7 session id;
ASSESS → P8 assessment id; SPACED_REVIEW → due card count). Completion rules
are explicit per step; `complete_mission_action()` records results, writes
learning events through the owning engines only, and advances the plan.
Interruption-safe: every step result persists at completion time; a new
process resumes via `get_mission_state()`.

### 2.5 Canonical content + rendering

`content_items` (V10): one canonical, evidence-grounded content model per
(topic, source-set digest, config): learning objectives, key facts, mechanisms,
definitions, relationships, clinical correlations, misconceptions, high-yield
points — every element carrying `evidence_refs` (P4 evidence ids + locators)
and a stable `content_id` (sha1 of topic+sources+config+prompt version).
Generated once via `generate_text` (LLM) with strict JSON parsing and a
deterministic fallback built from P3/P4 rows when the model is unavailable.

`render_study_products()` renders artifacts deterministically from the
canonical item: study guide, cheat sheet, flashcards (id = canonical fact id),
quiz questions (mapped to P8 DRAFT items when requested), mind map, script.
Each `content_artifacts` row stores artifact type, version, path, checksum,
status (`DRAFT | VALIDATING | READY | NEEDS_REVIEW | BLOCKED`) and provenance
chain — artifact → content element → evidence → source. Cross-artifact
consistency is structural (same facts), verified by a consistency check
(fact ids referenced ⊆ canonical facts; no contradictory duplicates).

### 2.6 Learner adaptation

`adaptation_profile(learner_state)` → one of `weak | developing | strong |
underconfident | overconfident` from P6 mastery/recent/confidence — rendered
artifacts choose section emphasis and question difficulty; missions choose
modes. Profile is metadata on the artifact/mission, never written back into
P6.

### 2.7 Privacy, caching, performance

Private bundle = canonical content + bounded evidence excerpts + learner
history; distributable = rendered artifacts + locators only (no learner data,
no long excerpts, no prompts). Cache key = sha1(content inputs + prompt
version + model); learner-adapted fields are rendered deterministically, so
the canonical cache is learner-independent. Planning is pure SQL + Python
(single-digit ms on the demo DB); no full-textbook loads; one model at a time.

### 2.8 V10 migration

`v10_study_intelligence` (additive): `study_plans`, `study_missions`,
`study_actions`, `content_items`, `content_artifacts` + indexes. Mirror of
`migrate_v9.py` (`ensure_study_v10`, backup, integrity, idempotent). No
existing table altered.

## 3. Verification

### 3.1 Acceptance gates

| Gate | Result |
| --- | --- |
| P9 orchestrates P2/P3/P4/P6/P7/P8 without duplicating them | Yes — every decision reads a public engine API; no new mastery, evidence, grading, tutor or assessment math was written. |
| Deterministic-first | Yes — selection, scoring, budget fitting, gaps, readiness, caching and rendering are pure Python. The LLM is used for exactly one canonical generation per (topic, source-set, config). |
| Machine-readable WHY | Yes — `reasons: [{code, detail, weight}]` on every recommendation, plus a rendered English sentence. |
| Time budgeting honest | Yes — per-action minutes are real; actions that do not fit are reported in `did_not_fit`, never padded or silently dropped. |
| Mission ↔ engine integration real | Yes — a TUTOR mission opens a genuine P7 session; an ASSESS mission opens a genuine P8 assessment. Launch failures are recorded as honest abstentions, never faked. |
| Restart-safe | Yes — mission state, engine ids and step results persist; a new process resumes with no duplicated `study_actions`. |
| Model output never trusted uncited | Yes — `_sanitize` drops any element without a citation that resolves to a supplied `[S#]` label; empty result falls back to deterministic evidence sentences. |
| Migration additive + idempotent | Yes — V10 only `CREATE`s five tables; re-running is a no-op. |
| Live data preserved | Yes — 438 pre-existing rows unchanged; only the new `schema_migrations` row was added. |
| Prior phases still green | Yes — 210/210 baseline tests pass unchanged. |

### 3.2 Bugs found and fixed during the P9 run

These were all found by executing the isolated E2E, not by inspection:

1. **Nested curriculum topics could not be resolved.** `find_node("Topic", …)` only
   matches root-level nodes, so missions carried `curriculum_node_id: null` and
   P8 launches were rejected. Fixed with a depth-first curriculum lookup; pinned
   by `test_mission_resolves_nested_curriculum_node`.
2. **Tutor objective column name.** The mission writer used `objective`; the P7
   session column is `session_objective`. Missions recorded a null objective.
3. **Tutor goal vocabulary.** `goal="short"` was rejected by P7; goals must be
   `10min|20min|30min|quick` or an interaction count.
4. **P8 item grounding.** Mission-created assessment items needed a real
   `evidence_refs` payload to reach ACTIVE.
5. **Empty day on a 10-minute budget.** Every action was hardcoded at 15 minutes,
   so a 10-minute plan produced nothing. Actions now carry honest per-type
   durations and are shortened to fit before being dropped.
6. **Model mode was effectively dead.** The prompt asks for a `refs` array, but
   the local model cites inline (`"fact": "… [S1]"`) and omits `refs`; the
   sanitizer required `refs`, so every real generation was discarded and
   silently replaced by the deterministic fallback. `_element_refs()` now accepts
   and materializes both forms, dropping only genuinely uncited elements.
7. **Adaptive re-renders shadowed each other.** All profiles wrote the same
   `<type>.md`, so a later profile overwrote an earlier one's bytes and the
   consistency report misreported a normal re-render as tampering. Each profile
   now gets its own file (`study_guide.weak.md`).
8. **`medforge_core status` was dead (pre-existing, dated to P8).** `main()` is a
   single giant function, so two local `status = …` assignments silently shadowed
   the module-level `status()` command and made it raise `UnboundLocalError`.
   Renamed; `test_main_locals_do_not_shadow_module_level_commands` now parses the
   CLI with `ast` and fails if any name `main()` both binds and calls shadows a
   module-level object (the same guard flags `status` in the P8 file).
9. **The completeness gate misjudged curated artifacts.** `cheat_sheet`,
   `mind_map` and `script` render deliberately partial subsets, so the
   "every fact rendered" check marked them `NEEDS_REVIEW` and dragged the
   product's overall status down for perfectly good output — exactly the kind of
   noisy signal users learn to ignore. Those types are now declared as summary
   artifacts and the consistency block reports `coverage_scope`
   (`full`/`summary`) so the distinction is explicit rather than implied.
10. **Duplicated citation labels.** Canonical facts arrive with inline `[S1]`
    markers, and `_cite()` appended the refs list unconditionally, so every
    rendered artifact showed `[S1] [S1]`. `_cite()` now appends only labels the
    prose does not already carry.

## 4. Execution results

Environment: macOS, Python 3.12, `.venv-v2.1`, local model `qwen3:4b-instruct`
via Ollama at `127.0.0.1:11434`. Isolated demo home `MEDFORGE_HOME=/tmp/mf-p9-demo`
built by the P8 setup script (real P3 handouts ingested, real evidence-backed
items, a completed 20-question assessment).

### 4.1 Tests

| Metric | Value |
| --- | --- |
| Baseline (P8) | 210 passing |
| New P9 tests | 51 (`test_study_intelligence.py` 28, `test_product_integration.py` 23) |
| Final total | **261 passing in ~35s** |
| Regressions | 0 |

### 4.2 Migration (live database)

| Step | Result |
| --- | --- |
| Before | `9.0.0`, 40 tables, 438 rows |
| After | `10.0.0`, 45 tables, 439 rows (+1 `schema_migrations` row) |
| New tables | `study_plans`, `study_missions`, `study_actions`, `content_items`, `content_artifacts` (all empty) |
| Integrity | `integrity_check` ok, `quick_check` ok, `foreign_key_check` 0 violations |
| Backup | `backups/medforge_pre_v10_backup.db` — versions through `9.0.0`, no V10 tables, integrity ok |
| Idempotence | Re-run with `--no-backup` → still 45 tables, single `10.0.0` row |

### 4.3 Study loop E2E (`/tmp/mf-p9-demo`)

1. **Recommendation with WHY.** `Growth Hormone Physiology → TUTOR`, priority
   0.063, reason `weakness: mastery 57.7%`; alternatives ranked with their own
   reasons.
2. **Real tutor.** `start_study_mission` opened P7 session `2`; driving it
   produced `questions_attempted: 2, correct: 2, mean_score: 1.0`, harvested as a
   bounded aggregate.
3. **Real assessment.** An ASSESS mission opened P8 assessment
   `as-5439b7191426` (blueprint `bp-df38759e0b48`, scope topic `883e561b12df0640`),
   was answered through the public item flow and completed with a real result.
4. **Plans + budget.** `build_study_plan(30)` ranked four topics; `plan(10)` fits
   the day honestly and reports what did not fit. Re-planning creates
   `plan_version 2, 3…` and retires the previous version — history is never
   rewritten.
5. **Mission lifecycle.** Steps advanced one per `complete_mission_action`, with
   engine results harvested at each step and an explicit `completed` terminal
   state.
6. **Recovery.** A second process (`pid 56937`) resumed the mission left active:
   status/step count/`tutor_session_id`/adaptation profile all intact,
   `launch_mission_engines` re-attached idempotently, the mission finished, and
   `study_actions` for that mission was 1 before and 1 after — no duplicated
   events.

### 4.4 Learner adaptation E2E (`/tmp/mf-p9-demo`)

Nine real P6 events on `Growth Hormone Physiology` (scores 0.10–0.05) moved
mastery `0.437 → 0.303` and calibration gap `0.335 → 0.468`, flipping the
profile `developing → weak`. The recommendation priority rose `0.063 → 0.182`
with reasons `weakness` + `recent_failure`. The **same canonical content**
(`8361e17930ce892e`) rendered twice produced different emphasis purely because
the learner changed: `study_guide.md` (developing, no scaffold) vs
`study_guide.weak.md` (weak, contains `Suggested first step`).

### 4.5 Product factory E2E (`/tmp/mf-p9-demo`)

| Property | Evidence |
| --- | --- |
| One generation → many artifacts | Real model call produced mode `model` with 5 key facts, 3 definitions, 3 relationships, 2 mechanisms, 2 clinical correlations, 2 misconceptions; `model_calls_total: 1` for **six** artifact types |
| Cache | Identical inputs → `cached: true`, `extra_model_calls: 0`, `content_rows_delta: 0` |
| Deterministic render | study_guide 1714 B, cheat_sheet 1183 B, flashcards 1771 B, quiz 2586 B, mind_map 630 B, script 457 B; 10/10 facts rendered, 0 unknown fact ids, `coverage_scope: full`, overall status `READY` |
| Versioning | Re-render → `study_guide: [1, 2]`, `flashcards: [1, 2]` |
| Source sensitivity | Changed sources → new `content_id`, new row, first item untouched (append-only) |
| Consistency | 10 artifact rows (6 types + weak/strong study guides + second versions), all on-disk checksums match; CLI `product-intel consistency` → `consistent: True` |
| Tamper detection | Injecting a line into `study_guide.md` → `consistent: false`; restoring the file → `consistent: true` |
| Adaptation | Same canonical → `study_guide.weak.md` vs `study_guide.strong.md`, both distinct, canonical bytes unchanged |
| Provenance | artifact → content_item → `evidence_refs` → P3/P4, with `sources_digest`, `prompt_version`, `model` |
| Fallback honesty | With the model forced to raise, mode `deterministic_fallback`, 8 facts, **every fact a substring of the source text**, every fact carrying refs |

### 4.6 CLI

Verified on the populated demo home: `study-intel status|recommend|gaps|readiness|`
`profile|plan|today|missions|history` and `product-intel artifacts|status|`
`consistency|provenance|build`. `study-intel plan "20|1|…"` created plan
version 3; `today` fit 15 of 20 minutes and listed the three actions that did
not fit; `product-intel consistency` returned `consistent: True` across the
fresh artifact set. Legacy commands were re-checked after the rename:
`mastery`, `due`, `claims`, `assessment items` and `status` all work (the last
was broken before this phase).

Live home (`MEDFORGE_HOME` = repo root) read-only smoke: `study-intel status`
returns an empty curriculum with 119 due SR cards — correct for a home with no
syllabus yet, and proof the orchestrator degrades honestly rather than crashing.

### 4.7 Dashboard

Streamlit was started on **port 8502** (the user's own instance on 8501 was left
untouched) against the demo home. The STUDY tab renders the P9 Study Home:

- Today's budget `15/20 min`, cards due `0`, overdue reviews `0`.
- Today's plan expander: `1. Growth Hormone Physiology — TUTOR (15 min)` plus a
  `Did not fit today's budget (3)` expander.
- Knowledge gaps (0) → `No gaps detected.`
- Declining topics table: `growth-plate-physiology`, mastery 66.5, recent 0.6915,
  trend −0.3085 (real P6 recency-weighted trend).
- `Next up: Growth Hormone Physiology → TUTOR (priority 0.077)` with the WHY
  sentence and a `Start recommended mission` button.
- Readiness checker and the legacy self-study logger preserved in an expander.

No browser console errors. The dashboard was stopped and port 8502 released
after verification.
