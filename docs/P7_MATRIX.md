# P7 — Interactive Adaptive Tutor: Audit & Design Matrix

**Audit date:** 2026-10-05 · **Base commit:** `3d1b668` (P6 complete, 126/126
tests, migration baseline 7.0.0, learner model `p6-rwm-v1`).

Live DB before P7: `chunks=47, claims=5, evidence=18, claim_evidence=10,
verification_runs=10, spaced_repetition_queue=119, learning_attempts=0,
learner_model_state=0, curriculum_nodes=0, prerequisites=0,
textbook_documents/editions=0, interactive_sessions=0, review_log=0` —
the tutor must work with an empty learner model and no curriculum/evidence,
so "no evidence" is a normal, handled state rather than a crash.

## 1. Forensic audit — what P7 can reuse (nothing is rebuilt)

| Component | File | Reuse decision |
|---|---|---|
| Learner state / estimates | `learner_model.py` | **CONSUME READ-ONLY** — `get_mastery`, `get_weaknesses`, `get_recent_performance`, `get_confidence`, `get_prerequisite_risks`, `study_priority`/`get_review_priority`. The tutor never recomputes mastery. |
| Learner evidence intake | `learner_model.py::record_learning_event` | **ONLY WRITE PATH** — every graded interaction, confidence report and completion goes through it (`item_type='question'`, `source='session'`, `content_version=TUTOR_VERSION`). No direct writes to `learner_mastery`/`learner_model_state`. |
| Weakness/misconception store | `learner_weaknesses` + `learner.py::record_weakness` | **EXTEND (reuse)** — conceptual errors are recorded through `record_weakness` with a new `origin='tutor'` value; the V7 column is plain TEXT (no CHECK), so P6's auto-recovery (`origin='learner_model'`) still ignores tutor rows. No second system. |
| Evidence graph | `evidence.py` | **CONSUME** — `store_evidence` (persists retrieved chunks with P3 provenance), `classify_pair`/`aggregate_verdicts` (model-assisted verification), `claims`/`claim_evidence` status as the authority for the tutor's communication policy. |
| Textbook evidence | `textbook.py::textbook_evidence_for_topic` | **PRIMARY EVIDENCE PATH** — curriculum-linked textbook chunks, page/section locators. |
| Retrieval | `retrieval.py::hybrid_retrieve`, `source_pack` | **FALLBACK EVIDENCE PATH** — bounded (≤ 6 sources), no new embedding of identical text (chunk ids are stable). |
| Curriculum | `curriculum.py::topic_path`, `add_prerequisite`, `curriculum_progress` | **CONSUME** — target resolution (node id/title/slug) and prerequisite chains. |
| Spaced repetition | `learner.py::review_card`, `spaced_repetition_queue` | **CONSUME** — the tutor may *create* a topic review opportunity (`item_type='topic'`) and grade it through `review_card`, which keeps SM-2/FSRS math untouched. |
| Models | `models.py::chat`, `ollama_alive`, `ensure_models` | **BOUNDED USE** — only explanation, Socratic guidance and free-text grading. One call per step, bounded context, `keep_alive=90s`. |
| Sessions | `interactive_sessions` | **NOT REUSED for tutor state** — it models self-scored study missions (CHECK-constrained types, no stage machine). P7 adds its own session tables instead of overloading it. |
| Schemas | `schema.py`, `migrate_v7.py` | **PATTERN REUSE** — V8 follows the same additive/idempotent/self-healing runner design. |

### Tables inspected

| Table | Current role | P7 verdict |
|---|---|---|
| `interactive_sessions` | self-scored study missions | **KEEP unchanged** (tutor sessions are a different lifecycle) |
| `study_sessions` | legacy, not present in V3+ | **ABSENT / irrelevant** |
| `learning_attempts` | P6 append-only evidence | **KEEP** — tutor writes through `record_learning_event` |
| `learner_model_state` | P6 materialized estimate | **KEEP read-only** |
| `claims`, `evidence`, `claim_evidence`, `verification_runs` | P4 graph | **KEEP** — read status; write evidence via `store_evidence` |
| `curriculum_nodes`, `prerequisites` | P2 graph | **KEEP read-only** |
| `spaced_repetition_queue`, `review_log` | SM-2/FSRS | **KEEP** — via `review_card` only |
| `textbook_*` | P3 provenance | **KEEP read-only** |

## 2. Requirements classification

| Requirement | Verdict |
|---|---|
| Persistent, restart-surviving tutor session with stage machine | **MISSING → `tutor_sessions` (V8)** |
| Per-turn transcript, stable question ids, idempotent events | **MISSING → `tutor_turns`, `tutor_questions` (V8)** |
| P6-driven target selection (weak / overdue / prerequisite / declining / new / reinforcement) | **MISSING → deterministic `select_tutor_target`** |
| Evidence-first teaching with source priority | **MISSING → `gather_evidence` over P3 → retrieval → `store_evidence`** |
| Verification-aware behaviour (no self-verification) | **MISSING → `assess_evidence` + `verification_policy`; model output can never set a status** |
| Teaching modes (7) | **MISSING → `TUTOR_SESSION_MODES`** |
| Adaptive decisions from real responses | **MISSING → deterministic `adapt_tutor`** |
| Minimum question system (mcq/recall/short_answer/clinical_reasoning) | **MISSING → `build_question` with content-addressed ids** |
| Deterministic MCQ grading + model-assisted free-text grading | **MISSING → `grade_mcq`, `grade_free_text` (safe failure)** |
| Misconception capture, conceptual vs minor | **EXTEND (reuse `learner_weaknesses`) → `origin='tutor'`** |
| Learning-event integration | **REUSE `record_learning_event`** |
| Session summary with learner-model change | **MISSING → `get_tutor_summary`** |
| Spaced-repetition integration | **REUSE `review_card` on a topic review opportunity** |
| Dashboard TUTOR tab | **MISSING → 12th tab at index 4** |
| CLI `tutor` subcommands | **MISSING → in the existing one-shot `medforge_core` chain** |
| Prompt-injection containment / privacy / model-failure safety | **MISSING → delimiting + flags + retryable states** |

## 3. Design

### Stages
`TEACH → ASK → WAITING_FOR_ANSWER → EVALUATE → EXPLAIN → ADAPT → COMPLETE`
(persisted in `tutor_sessions.stage`; a refresh/restart resumes from the stored
stage, question and pending answer).

### Modes
`explain`, `socratic`, `drill`, `correct`, `case`, `review`,
`prerequisite_repair` — one explicit column plus the same value on each turn.

### Session goals
`quick`=3, `10min`=5, `20min`=8, `30min`=12 interactions (deterministic counts;
no timers, no background scheduling), or an explicit interaction count. A hard
`TUTOR_MAX_TURNS` bound guarantees no endless loop.

### Target selection (deterministic)
Explicit topic → resolved through `topic_path` (node id / title / slug) and
always wins. Otherwise the recommendation is derived from P6 only:
`study_priority` items ranked by `(−priority, topic)`, with the reason recorded
(weak topic, overdue review, recently declining, prerequisite weakness, new
curriculum topic, reinforcement). No model call, no randomness.

### Evidence assessment (no self-verification)
1. Gather bounded evidence (`textbook_evidence_for_topic` first, then
   `hybrid_retrieve`), persist each item through `store_evidence` (P4 record).
2. Derive the concept and its evidence-backed key points (sentences that
   overlap the concept) — **a question whose key points cannot be grounded is
   never asked**.
3. Status: no evidence → `INSUFFICIENT_EVIDENCE`; a stored P4
   `CONTRADICTED`/`UNSUPPORTED` claim for the same text wins;
   otherwise deterministic lexical coverage over the key points
   (≥ 0.8 → `SUPPORTED`, ≥ 0.4 → `PARTIALLY_SUPPORTED`, else
   `INSUFFICIENT_EVIDENCE`). When a model is available, P4's
   `classify_pair`/`aggregate_verdicts` may *refine* this, but the tutor's own
   generated text can never set or upgrade a status.
4. `verification_policy(status)` decides behaviour: teach normally /
   qualify / surface disagreement / abstain with an explicit message.

### Adaptation (deterministic, after P6 is re-queried)
| Response | Action |
|---|---|
| correct + high confidence | difficulty +1, progress |
| correct + low confidence | reinforce with rationale, confidence pattern recorded, difficulty held |
| incorrect + high confidence | `correct` mode, error-targeted explanation, overconfidence flagged, difficulty −1 |
| incorrect + low confidence | stronger scaffolding, simpler question type, difficulty −1 |
| 2 consecutive failures | `prerequisite_repair` on the weakest prerequisite (or simplify if none) |
| 2 consecutive successes | difficulty +1 and advance concept |
| stop conditions | interactions reached · objective achieved (mastery ≥ 0.80 with recent ≥ 0.80) · insufficient evidence (blocked) · learner ends |

### Learning-event idempotence
Each turn stores its `attempt_id`; a resumed/retried turn with an
`attempt_id` already set never writes a second event. The pending answer and
confidence are persisted **before** grading so a crash cannot lose them.

### Model usage / failure
Model calls happen only for: explanation generation, Socratic guidance,
free-text grading, misconception wording. Every call is wrapped: on failure or
malformed output the tutor falls back to an evidence-quoting deterministic
explanation or marks grading `retryable` (answer preserved, no learning event,
no fabricated grade), records `model_error`, and the dashboard/CLI show a
retry path. No crash.

### Prompt-injection containment
Evidence excerpts, question text and learner answers are passed only inside
explicitly delimited blocks preceded by a "data, not instructions" system
notice; an `injection_suspected` flag is stored on the turn when known
injection markers appear, and verification status is never taken from model
output. Learner answers that contain instructions are graded as answers.

### Privacy
Learner answers, confidence, misconceptions and transcripts live only in
SQLite. Nothing in `product.py`/`export.py` reads tutor tables; `public_view()`
redacts answer/confidence fields for anything user-facing or shareable.

### Non-goals (P8 boundary)
No exams, no chapter-wide assessment engine, no question banks, no psychometric
scoring, no video. P7 builds only the minimum interactive question/answer system
plus the stable interface P8 will extend.

## 4. Planned files

| File | Change |
|---|---|
| `core/database/schema.py` | `V8_SCHEMA_DDL` (3 tables + indexes), `tutor` added to `LEARNER_WEAKNESS_ORIGINS` |
| `core/database/migrate_v8.py` | **new** — `ensure_tutor_v8()`, 8.0.0, idempotent, backup, integrity + FK check |
| `medforge/tutor.py` | **new** — session/target/evidence/question/grading/adaptation/summary APIs |
| `medforge/types.py` | tutor enums, goals, thresholds, version |
| `medforge/__init__.py`, `medforge_core.py` | exports + `tutor` CLI |
| `dashboard.py` | TUTOR tab (index 4), later tabs shifted |
| `tests/test_tutor.py` | **new** — the 32-item directive list, offline, mocked model |
| docs | `P7_MATRIX.md` (this), `TUTOR.md` new; IMPLEMENTATION_MATRIX / ARCHITECTURE / V3_SCHEMA / CLI_USAGE |

Untouched: `learner_model.py`, `learner.py`, `evidence.py`, `textbook.py`,
`retrieval.py`, `product.py`, `generation.py`, `export.py`, `storage.py`,
`models.py`, P6 migration history.

---

# Execution results

Full behaviour documentation: `docs/TUTOR.md`. Migration record:
`docs/V3_SCHEMA.md` (§V8).

## Acceptance criteria → proof

| Criterion | Proof |
|---|---|
| persistent stateful session (not a chatbot) | `tutor_sessions` stage machine + `tutor_turns` transcript; restart test; every CLI invocation below is a new process |
| target selection uses P6, not randomness | `select_tutor_target` = explicit topic, else `study_priority` reason + components, else new curriculum topic |
| teaches a focused concept | evidence-quoted fallback or model text constrained to retrieved key points |
| asks questions | 4 grounded question types, content-addressed ids, no repeated item in a session |
| answers evaluated | deterministic MCQ + rubric coverage + strict-JSON model grading |
| adapts after success | live run: correct+confident → `progress`; two in a row → difficulty +1 |
| adapts after failure | live run: incorrect+confident → `explain_error`, mode `correct`, difficulty −1, `overconfident` flag |
| confidence mismatch changes behaviour | correct+low → `reinforce_confidence`; incorrect+high → overconfidence flagged |
| misconceptions recorded | conceptual error → weakness `origin='tutor'`, curriculum node attached (live id=1) |
| events through P6 | `learning_attempts` rows for every graded interaction; turn stores `attempt_id` |
| learner state updates | P6 mastery/recent/confidence before→after recorded per interaction and in the summary |
| prerequisite repair | repeated failure → prerequisite re-route; with no evidenced prerequisite the decision degrades to `simplify` and says so |
| session summary | structured summary (counts, mean, confidence pattern, learner-model delta, weaknesses, verification statuses, abstentions, injection flags, stop reason, review) |
| spaced repetition integration | one `<mastery_key>:tutor-review` item synced through `review_card` (SM-2/FSRS untouched) |
| model failure recoverable | live dead-endpoint run: `retryable`, answer preserved verbatim, 0 learning events, resumed by a new process, retry graded (partial 0.5) with exactly one event |
| prompt injection contained | injection tests + live grader prompt check; delimiters neutralized; flags counted in the summary |
| learner data private | `public_view()` recursive redaction (nested dicts *and* lists) + tests |
| dashboard TUTOR tab | browser verification (resume, question, submit, model-graded feedback) |
| CLI works | `tutor targets/start/status/answer/next/summary/end/sessions` exercised live |
| recovery demonstration | see the table above |
| evidence demonstration | SUPPORTED session live; PARTIALLY_SUPPORTED policy unit-tested; INSUFFICIENT_EVIDENCE live abstention |
| migration additive/idempotent | V8 tests (fresh DB, rerun, pre-existing data preserved) |
| live data preserved | before/after counts, integrity + FK checks, verified backup |
| previous tests green | 126/126 baseline retained in the 163/163 full run |
| docs + commit/push | this section + `TUTOR.md` + updated matrices + final report |

## Tests

| Stage | Result |
|---|---|
| Baseline (P6 complete) | 126/126 passing, migration 7.0.0 |
| New P7 suite (`tests/test_tutor.py`) | **37/37 passing** (0 failures) |
| Full regression after all edits | **163/163 passing** (`pytest tests/ -q`) |

The suite is offline and deterministic: evidence comes from a seeded P3
textbook, retrieval and the chat model are injected, so no test needs Ollama.

## Live database migration (8.0.0)

| Step | Result |
|---|---|
| Before | chunks 47 · claims 5 · evidence 18 · claim_evidence 10 · verification_runs 10 · SR queue 119 · learning_attempts 0 · learner_model_state 0 · curriculum_nodes 0 · textbooks 0 · migrations 3.0.0–7.0.0 · integrity ok · FK ok |
| Backup | `backups/medforge_pre_v8_backup.db` — valid pre-V8 snapshot (no tutor tables, migrations to 7.0.0, integrity ok) |
| Migration | `python -m core.database.migrate_v8 --db database/medforge.sqlite3` → `status: success`, integrity ok, FK clean, 3 tables + 6 indexes created |
| After | every pre-existing count **unchanged**; `tutor_sessions`/`tutor_turns`/`tutor_questions` = 0/0/0; migrations now `3.0.0 … 8.0.0` with exactly one `8.0.0` row (`v8_interactive_tutor`) |

## Isolated end-to-end run (`MEDFORGE_HOME=/tmp/mf-p7-demo`)

Real syllabus import, real PDF ingest through P3 (`embed=0`), curriculum links
to both chapters, then the CLI tutor loop (new process per command) with the
live local model (`qwen3:4b-instruct`):

```
[start]    session=1 topic=Growth Hormone Physiology source=explicit
           assessment=SUPPORTED strategy=textbook+retrieval
           policy={status: SUPPORTED, teach: true, qualify: false, abstain: false}
           teaching model=qwen3:4b-instruct (error=None)
           Q1[recall] From memory: state what you know about Growth Hormone Physiology.
              - The growth hormone axis is regulated by hypothalamic GHRH and somatostatin…
              - Growth hormone stimulates hepatic IGF-1 production, and IGF-1 then mediates…
              - Growth hormone excess before epiphyseal closure causes gigantism…
           model_calls=1 difficulty=2
[answer 1] correct score=1.0 graded_by=model → adaptation=progress flags=['confident_correct']
           P6={mastery: 0.7, recent: 1.0, confidence: 0.9} → Q2=short_answer
[answer 2] incorrect error_type=conceptual graded_by=model
           → adaptation=explain_error mode=correct difficulty=1 flags=['overconfident']
           misconception id=1 origin=tutor severity=high
           P6={mastery: 0.5, recent: 0.5, confidence: 0.925} → Q3=clinical_reasoning
[answer 3] correct score=1.0 → adaptation=progress flags=['confident_correct']
           stop_reason='session length reached' status=completed interactions=3
[summary]  interactions=3 correct=2 incorrect=1 mean=0.6667
           verification_statuses=['SUPPORTED'] abstentions=0 injection_flagged=0
           model_calls=5 stop='session length reached'
           learner_model_change={evidence_count: 3, mastery_before: null, mastery_now: 0.6111,
                                 recent_performance: 0.6667, uncertainty: 0.5}
           confidence_pattern={high: 3, low: 0, mean: 0.8833, observations: 3}
           weaknesses={open: 1, recovered_in_session: 0} misconceptions=[1]
           review={item_id: growth-hormone-physiology:tutor-review, grade: 2,
                   scheduler: sm2, state: relearning, review_logged: true}
```

### Evidence-policy demonstration

| Case | Live result |
|---|---|
| SUPPORTED | session 1 above: textbook-linked chapter + retrieval, `teach: true` |
| PARTIALLY_SUPPORTED | policy unit-tested: one evidence record → `PARTIALLY_SUPPORTED`, `qualify: true` (teaching is qualified, never flat) |
| INSUFFICIENT_EVIDENCE | `tutor start|Diabetes Insipidus Pathophysiology` (no linked textbook) → `abstained: true`, `INSUFFICIENT_EVIDENCE`, stored session `stage=COMPLETE status=blocked`, message: “I don't have enough evidence in your current knowledge base to teach that confidently.” |

### Recovery demonstration

```
[dead-model] OLLAMA_URL=http://127.0.0.1:9, model=qwen3:4b-instruct
             retryable=True grading_status=retryable
             error='Could not reach Ollama at http://127.0.0.1:9: [Errno 61] Connection refused'
             pending answer preserved verbatim | stage=WAITING_FOR_ANSWER
             learning events: 4 → 4 (no fabricated grade, no event)
[resume]     a NEW process reads stage=WAITING_FOR_ANSWER, pending=yes, model_error recorded
[retry]      graded=graded source=model correctness=partial score=0.5 → exactly one new event
```

## Dashboard verification

Streamlit on a second port with the demo home (12 tabs; TUTOR at index 4):
resumed the unfinished session after a full page load (objective, interactions
1/3, difficulty, the teaching text, the question, answer box, confidence slider,
submit/end, evidence expander), submitted an answer in the browser and got
model-graded feedback (“Partially correct (0.5) …”) rendered in the panel.
Verification server stopped afterwards; the user's own dashboard on 8501 was
left running.

## Defects found during live validation (and fixed)

1. **Model-assisted teaching crashed silently** — `TUTOR_SYSTEM_TEACH` was
   referenced without the `T.` prefix, so every teaching model call raised and
   fell back to the evidence-quoted text (`model_error` recorded on the
   session). The hermetic tests never exercised a real call; the live E2E did.
2. **`model_calls` undercounted** — grading model calls were not counted (and
   the grading model was not attributed to the turn). Now both are recorded.
3. **Cross-topic rubric contamination** — evidence rows were re-ordered by rowid
   and a retrieval hit from another chapter could fill the rubric; chapter
   headings glued to the first sentence made off-topic text look
   concept-relevant. Fixed by preserving the session's evidence priority order,
   stripping heading prefixes (with a locator fallback when the provenance
   column is empty) and requiring ≥ 50% concept-term coverage from an evidence
   row before it can contribute key points.
4. **Duplicate evidence records** — the same chunk arriving through both the
   textbook link and retrieval was stored twice (duplicating rubric points and
   inflating the evidence count). Exact duplicates are now collapsed before
   storage.

## Limitations (honest list)

- Live retrieval in the demo home is keyword-based (`embed=0`); vector search
  needs Ollama embeddings, and very small chapters therefore yield 1–2 evidence
  records (the SUPPORTED threshold is ≥ 2 distinct records).
- The deterministic rubric grader is lexical: paraphrases with little term
  overlap can under-score when no model is available; ungraded lexical mismatch
  is reported as `unknown`, not as a misconception.
- The tutor asks a single concept per session and one question at a time; the
  Socratic mode guides with one opening question rather than a full dialogue
  tree.
- Misconception text is bounded and model/phrase-derived — it records what was
  missed, not a validated diagnostic label.
- MCQ items need ≥ 2 genuine distractors from the sources; small evidence sets
  therefore fall back to recall questions instead of inventing options.
