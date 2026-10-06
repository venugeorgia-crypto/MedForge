# MedForge CLI Usage Guide

## Installation

```bash
# Download and run the installer
bash "$HOME/Downloads/MEDFORGE-ONE-COMMAND.command"

# Or with a topic to generate immediately
bash "$HOME/Downloads/MEDFORGE-ONE-COMMAND.command" "cardiac cycle"
```

After installation, the following commands are available in your shell:

| Command | Alias | Description |
|---------|-------|-------------|
| `medforge` | `product` | Main CLI entry point |
| `medstudy` | `study` | Interactive study mission |
| `medask` | `ask` | Question answering |
| `medforge dashboard` | — | Open web dashboard |

---

## Commands

Commands: `product`, `ask`, `study`, `study-log`, `mastery [topic]`, `learner`,
`weaknesses`, `history`, `recalculate`, `study-priority`, `due`, `review`,
`import-cards`, `syllabus`, `curriculum`, `path`, `prereq`, `textbooks`,
`textbook-add`, `textbook-info`, `textbook-link`, `textbook-evidence`, `claims`,
`claim-info`, `verify-pack`, `evidence-status`, `tutor`, `assessment`,
`study-intel`, `product-intel`, `migrate`, `doctor`, `status`.

### `medforge product <topic>` — Generate Study Pack

Create a complete evidence-based study pack for a medical topic.

```bash
# Basic usage
medforge product "cardiac cycle"

# With options
medforge product "heart failure" --offline  # Use only local evidence
```

**Output:** `~/MedForge/products/cardiac-cycle/v001/`
- `study-guide.md` / `.pdf` — High-yield study guide
- `workbook.pdf` — Combined guide + quiz + viva + cases
- `cheat-sheet.md` / `.pdf` — One-page reference
- `quiz.md` — 20 MCQs with rationales
- `viva.md` — 15 oral exam questions
- `case-exercises.md` — 5 mini-cases
- `flashcards.csv` / `.apkg` — Anki deck (30 cards)
- `mindmap.md` — Text mind map
- `short-script.txt` — 60-second video script
- `explainer-3min.txt` — 3-minute explainer script
- `instagram-carousel.md` — 8-slide carousel
- `references.md` — Source bibliography
- `evidence-report.html` — Citation audit report
- `source-map.json` — Evidence metadata
- `source-text.txt` — Raw evidence blocks

**Resume:** Re-run the same command to resume from last completed step.

**Spaced repetition:** Every generated flashcard is also scheduled in the V3
`spaced_repetition_queue` (due immediately, state `new`). Rerunning or
rebuilding a pack never duplicates or resets existing schedules.

---

### `medforge study <topic>` — Interactive Study Mission

Generate a 20-minute guided study session.

```bash
medforge study "cardiac cycle"
```

**Includes:**
1. Due card recall round (when the spaced repetition queue has due cards)
2. 3-minute blind recall prompt
3. 7-minute mechanism/build task
4. 5 escalating Socratic questions
5. Clinical application question
6. 5 flash review prompts
7. Self-score rubric (out of 10)

When cards are due, the mission opens with their questions only (this topic's
cards first, then up to 10 others) so the session starts from real recall
practice. Starting a mission also opens a tracking session in the V3 learner
tables (`interactive_sessions`). Log the rubric score when you finish and it is
completed in place, updating mastery and weaknesses.

---

### `medforge study-log <topic> <score>` — Log a Finished Session

Record the self-score from a study mission (rubric 0-10 or raw 0-100), update
the topic's mastery, and optionally record weaknesses.

```bash
# Score 8/10 on the mission rubric
medforge study-log "cardiac cycle" 8

# Raw percentage instead of the rubric
medforge study-log "cardiac cycle" 85
```

**Output:** JSON with the completed session id, the new mastery row
(`mastery_score`, `confidence_score`, attempts), any recorded weaknesses, and
the updated recency-weighted learner state (`learner_model`). Each logged score
is also appended as a P6 learning attempt linked to its `session_id`, so the
learner model can always be recalculated from history.

Weaknesses are recorded via the dashboard (concept + misconception + severity
fields in the STUDY tab) or programmatically:

```python
import medforge_core as mf
mf.log_study_result("cardiac cycle", 7, weaknesses=[
    {"concept": "isovolumetric contraction",
     "misconception": "aortic valve opens at the start",
     "severity": "high"},
])
```

---

### `medforge mastery [topic]` — Mastery Report / Recency-Weighted State

With no argument: the legacy JSON snapshot of learner tracking across all
topics — per-topic mastery (weakest first), unresolved weaknesses by severity,
and recent sessions.

```bash
medforge mastery
```

With a topic: the **P6 recency-weighted estimate** for that topic, including
evidence strength, recency split, confidence calibration and prerequisite
risks.

```bash
medforge mastery "GH axis"
```

```json
{
  "mastery": 0.3511, "mastery_percent": 35.1, "evidence_count": 3,
  "uncertainty": 0.5, "consistency": 0.9589,
  "recent_performance": 0.2767, "historical_performance": null,
  "model_version": "p6-rwm-v1",
  "confidence": {"confidence_estimate": null, "calibration_gap": null,
                 "has_confidence_data": false, "mismatch": false},
  "recent": {"recent_window_days": 14.0, "trend": null},
  "prerequisite_risks": {"node_id": "…", "weak_count": 0, "risks": []}
}
```

The same data powers the **STUDY → Mastery & weaknesses** panel and the
**LEARNER** tab in the dashboard. The legacy snapshot's `mastery_score` remains
the running mean of session scores (kept for compatibility); the P6 estimate is
a decayed, shrunk estimate — see `docs/LEARNER_MODEL.md`.

---

### `medforge learner` — Learner-Model Summary

Runs weakness detection, then prints the model-level summary: topics tracked,
total events, average mastery, known vs possible (low-confidence) weaknesses,
calibration mismatches, strongest/weakest/improving/declining topics, and the
current study priorities.

```bash
medforge learner
```

---

### `medforge weaknesses` — Unresolved Weaknesses

Runs weakness detection first (creating/updating/recovering learner-model rows),
then lists every unresolved weakness with its score, severity, failure count,
recent failure rate, prerequisite impact and confidence flag.

```bash
medforge weaknesses
```

---

### `medforge history <topic>` — Attempt History

Append-only learning-event history for a topic, newest first (score, source,
item type, session id, confidence, timestamps).

```bash
medforge history "GH axis"
```

---

### `medforge recalculate <topic|all>` — Rebuild From History

Recomputes a topic's state (or every topic's) from stored attempts at the
stored evaluation time. `matches_stored: true` means the materialized state is
exactly reproducible from history.

```bash
medforge recalculate "GH axis"
medforge recalculate all
```

---

### `medforge study-priority` — Recommended Study Order

Deterministic priority signal over all tracked topics:
`0.35·weakness + 0.20·uncertainty + 0.20·overdue + 0.15·recent_failure +
0.10·prerequisite_impact`, with every component and weight returned for
inspection. This is a signal for the P7 tutor, not a planner.

```bash
medforge study-priority
```

---

### `medforge import-cards [topic]` — Schedule Flashcards

Import flashcards from generated packs into the V3 spaced repetition queue.
With no topic, every saved pack is scanned.

```bash
medforge import-cards                 # schedule all saved packs
medforge import-cards "cardiac cycle" # just this topic's newest pack
```

**Output:** JSON per pack with `imported` (new cards) and `scheduled` (cards in
the queue for that topic). Import is idempotent — cards are keyed by a hash of
their question text, so re-importing adds nothing.

---

### `medforge due` — Due Cards & Review Analytics

Print the spaced repetition queue state, every card due for review, and the
review analytics computed from `review_log`.

```bash
medforge due
```

**Output:** JSON with queue counts (`due_cards`, `new_cards`, `learning_cards`,
`review_cards`, `relearning_cards`, `next_due_at`, `reviews_today`, `retention`,
`streak_days`, `leeches`), the due list with each card's question/answer/source
labels resolved from its pack, and an `analytics` block (30-day retention,
day streak, per-day review counts, leech list). Cards of weak topics sort
first.

---

### `medforge review <item_id> <grade 0-5> [sm2|fsrs]` — Grade a Card

Grade a due card and reschedule it (0 blackout, 3 pass, 5 perfect). Two
schedulers are available: `sm2` (default) and `fsrs` (FSRS-4.5 with the
published default parameters).

```bash
medforge review "cardiac-cycle:1f4c8a2b99de0011" 4          # SM-2
medforge review "cardiac-cycle:1f4c8a2b99de0011" 4 fsrs     # FSRS-4.5
```

**Output:** JSON of the updated queue row (`state`, `repetition_count`,
`interval_days`, `ease_factor`, `stability`, `difficulty`, `due_date`,
`weakness_recorded`, `weakness_resolved`). Grades below 3 are lapses: the card
returns after 10 minutes in `relearning`; grades 3+ advance the interval
(SM-2: 1 day → 6 days → interval × ease; FSRS: next interval = stability).
Every review is appended to `review_log`; a lapse of a graduated card records
a weakness, and graduating back to `review` resolves it.

---

### `medforge ask <question>` — Question Answering

Answer a specific question using your evidence library.

```bash
medforge ask "What happens during isovolumetric contraction?"
```

**Output:** Socratic explanation with citations + 3 retrieval questions.

---

### `medforge status` — System Status

Show system health and statistics.

```bash
medforge status
```

**Output:**
```json
{
  "version": "2.1.0",
  "base": "/Users/you/MedForge",
  "chunks": 142,
  "pdfs": 3,
  "products": 5,
  "ollama": true,
  "models": ["embeddinggemma", "qwen3:4b-instruct"]
}
```

---

### `medforge doctor` — Health Check

Full system diagnostic: Ollama, embeddings, chat model, SQLite, vector store.

```bash
medforge doctor
```

---

### `medforge syllabus [file]` — Import a Syllabus

Convert a pasted weekly (or seminar-level) syllabus into structured curriculum
data: Semester → Subject → Week → Seminar → Topic → Subtopic → Learning
Objective. Idempotent: rerunning the same text creates nothing new.

```bash
medforge syllabus semester3.txt     # from a file
cat syllabus.txt | medforge syllabus  # or pipe it in
```

Accepted line forms, duplicate detection, and traceability are documented in
[CURRICULUM.md](CURRICULUM.md).

---

### `medforge curriculum` — Progress & Health

Per-subject/week/seminar progress joined with your mastery data, plus a
duplicate-node report.

```bash
medforge curriculum
```

---

### `medforge path <topic>` — Curriculum Traceability

Resolve a studied topic (by title or slug) to its curriculum position.

```bash
medforge path "GH and IGF-1 axis"
# "position": "Semester: Semester / Subject: Endocrinology Block / Week: ... / Topic: ..."
```

---

### `medforge prereq "<topic>|<prerequisite>[|type]"` — Add a Prerequisite

Pipe-delimited (topic titles contain spaces). Type is `strict` (default),
`recommended` or `co-requisite`; the edge is idempotent.

```bash
medforge prereq "Growth plate physiology|GH axis"
medforge prereq "IGF-1 mediation|GH axis|recommended"
```

---

### `medforge textbooks` — List Registered Textbooks

Documents with their editions, chapter counts and link counts.

```bash
medforge textbooks
```

---

### `medforge textbook-add <pdf>[|fields]` — Register & Ingest a Textbook

Pipe-delimited optional fields: `title|edition|authors|publisher|year|isbn|subject`,
plus `embed=0` to skip embeddings (SQLite + FTS only, no Ollama call).
Idempotent by file content hash.

```bash
medforge textbook-add "Guyton.pdf|Guyton and Hall|14th ed.|Hall, J.E.|Elsevier|2021|978-0-323-59712-5|Physiology"
medforge textbook-add "notes.pdf|Lecture Notes|embed=0"
```

---

### `medforge textbook-info <edition_id>` — Inspect Provenance

Edition metadata, chapter/section tree with page ranges, and the first 60
page rows (extraction + OCR status).

```bash
medforge textbook-info a310036ac208599e
```

---

### `medforge textbook-link <topic>|<edition_id>[|node_id][|type]` — Link Evidence

Connects a curriculum topic to a textbook chapter/section (or the whole
edition). Idempotent; type is `primary` (default), `supporting` or
`supplementary`.

```bash
medforge textbook-link "GH axis|a310036ac208599e"
medforge textbook-link "GH axis|a310036ac208599e|<section_id>|supporting"
```

---

### `medforge textbook-evidence <topic>` — Discover Linked Evidence

Linked textbook sources for a curriculum topic with bounded page previews.

```bash
medforge textbook-evidence "GH axis"
```

---

### `medforge claims [STATUS]` — List Extracted Claims

Bounded claim listing (newest first, up to 50) with optional verification-status
filter: `PENDING`, `SUPPORTED`, `PARTIALLY_SUPPORTED`, `UNSUPPORTED`,
`CONTRADICTED`, `INSUFFICIENT_EVIDENCE`, `NOT_FACTUAL`, `HUMAN_REVIEWED`.

```bash
medforge claims
medforge claims UNSUPPORTED
```

---

### `medforge claim-info <claim_id>` — Claim → Evidence Trace

Full inspection: the claim, its verification status and confidence, every
evidence relationship with bounded excerpt, the provenance chain
(source → edition → chapter → section → page), and the append-only
verification history.

```bash
medforge claim-info 110c2536499afb1a01b13052
```

---

### `medforge verify-pack [pack_dir]` — Verify an Existing Pack

Extracts claims from a generated pack's markdown, reuses its saved
`source-map.json` sources as candidate evidence (no new embedding), verifies
with the local model and writes an excerpt-free `evidence-graph.json`. Bounded
for an 8 GB M1: ≤12 claims × ≤2 candidates, one model call per pair; with no
argument it picks the newest pack under `products/`.

```bash
medforge verify-pack "products/prolactinoma/v001"
```

---

### `medforge evidence-status` — Evidence Graph Counts

Claims, evidence records, relationships and verification runs with a status
breakdown and the `needs_review` count.

```bash
medforge evidence-status
```

---

### `medforge tutor "<action>|<args...>"` — Interactive Adaptive Tutor

A persistent teaching loop (P7): P6 picks what to study, P3/P4 supply verified
evidence, the tutor teaches, asks, grades, explains and adapts, and every graded
interaction is recorded through the P6 learner model. Sessions survive a
refresh, a new CLI process or a rebuild. Full behaviour: `TUTOR.md`.

```bash
medforge tutor targets                          # P6 recommendation + reason
medforge tutor "start|Growth Hormone Physiology|explain|quick"
medforge tutor start "Growth Plate Physiology"  # spaced form works too
medforge tutor status                           # resume newest session
medforge tutor "answer|1|GHRH and somatostatin control GH release.|0.8"
medforge tutor next 1
medforge tutor summary 1
medforge tutor end 1
medforge tutor sessions
```

Modes: `explain`, `socratic`, `drill`, `correct`, `case`, `review`,
`prerequisite_repair` (blank/`auto` = chosen from the target). Goals: `quick`
(3 interactions), `10min` (5), `20min` (8), `30min` (12) or a raw count. Answers
with spaces work in the pipe form; `|` separates the confidence (0–1).

Behaviour worth knowing:
- a topic with insufficient evidence **abstains** (a `blocked` session is stored
  with the reason) instead of inventing content;
- model-assisted teaching/grading uses the local chat model automatically
  (`MEDFORGE_TUTOR_AUTO_MODEL=0` to disable); a model failure keeps the answer
  and marks the turn retryable — resubmit to grade it;
- a completed session cannot be answered again, and a stale resubmit returns
  `already_graded` without a second learning event.

---

### `medforge assessment "<action>|<args...>"` — Question-Level Assessments

A persistent assessment system (P8): a versioned item bank with evidence
provenance, blueprints, deterministic selection, timed sessions and per-item
analytics. Grading reuses the tutor's `evaluate_answer` dispatch; every graded
answer is recorded through the P6 learner model. Full behaviour:
`ASSESSMENT.md`.

```bash
medforge assessment list                        # assessment sessions
medforge assessment blueprints                  # blueprint inventory
medforge assessment items                       # item bank inventory
medforge assessment item-info it-b6c2a183284d   # one item, all versions
medforge assessment stats it-b6c2a183284d       # item statistics (sample size shown)
medforge assessment blueprint-new               # guided blueprint creation
medforge assessment item-new                    # author an item manually
medforge assessment create bp-b4f59fd8151b      # assessment from blueprint
medforge assessment start as-895a02d18dea
medforge assessment status as-895a02d18dea
medforge assessment next as-895a02d18dea
medforge assessment previous as-895a02d18dea
medforge assessment "answer|as-895a02d18dea|A|0.9"   # option key(s) or free text
medforge assessment "flag|as-895a02d18dea|3"
medforge assessment submit as-895a02d18dea
medforge assessment result as-895a02d18dea
medforge assessment review as-895a02d18dea      # EXAM review, after submission
medforge assessment remediate as-895a02d18dea   # P6-driven remediation payload
```

Actions accept pipe (`"answer|<id>|<answer>|<confidence>"`) or space-separated
forms like the tutor command. In EXAM mode feedback is withheld until
submission; timing is server-side (`expires_at`), so a client cannot extend a
time limit.

---

### `medforge study-intel "<action>|<args...>"` — Study Intelligence (P9)

The orchestrator over P2/P3/P4/P6/P7/P8 + spaced repetition. It decides what to
study next and **says why**, then launches real engine sessions. See
`docs/STUDY_INTELLIGENCE.md`.

Actions accept pipe (`"plan|20|1|objective"`) or space-separated forms.

| Action | What it does |
| --- | --- |
| `status` | Curriculum tree annotated with mastery, uncertainty, evidence state and derived topic state (`NOT_STARTED … REVIEW_DUE`). |
| `recommend [topic][|goal]` | Next action for a topic (or globally) with machine-readable `reasons` and a rendered WHY sentence. |
| `gaps` | Knowledge gaps (missing/thin evidence, prerequisite gaps). |
| `readiness <topic>` | Structured readiness estimate for a topic. |
| `profile <topic>` | Adaptation profile: `weak`, `developing`, `strong`, `underconfident`, `overconfident`. |
| `plan <minutes>|<days>|<objective>` | Build a persistent plan. Re-planning appends a new `plan_version` and retires the previous one. |
| `today [plan_id]` | Today's actions fitted to the minute budget, plus `did_not_fit`. |
| `start <topic\|--recommended>` | Start a mission and open the real P7 tutor / P8 assessment. |
| `engines <mission_id>` | Idempotently re-attach a mission to its engines (for resuming). |
| `next <mission_id>` / `mission <id>` | Current mission state, steps and harvested engine results. |
| `complete <mission_id>` | Record one step and advance. |
| `finish <mission_id>` | Close the mission, marking remaining steps done. |
| `missions [status]` | List missions, optionally filtered by status. |
| `history` | Append-only study action history. |

```bash
medforge study-intel status
medforge study-intel "recommend"
medforge study-intel "recommend|Growth Hormone Physiology"
medforge study-intel "plan|30|1|Close endocrine gaps"
medforge study-intel "today"
medforge study-intel "start|--recommended"
medforge study-intel "complete|mission-1d0601c63139"
medforge study-intel "finish|mission-1d0601c63139"
medforge study-intel history
```

Example recommendation output (abridged):

```json
{
  "recommendation": {
    "topic": "Growth Hormone Physiology", "action": "TUTOR", "priority": 0.077,
    "mastery_percent": 55.2, "evidence_state": "PARTIAL",
    "reasons": [
      {"code": "weakness", "detail": "mastery 55.2%", "weight": 0.067},
      {"code": "recent_failure", "detail": "recent performance 0.56", "weight": 0.01}
    ]
  },
  "reason": "Growth Hormone Physiology selected for TUTOR because: mastery 55.2%; recent performance 0.56"
}
```

---

### `medforge product-intel "<action>|<args...>"` — Product Factory (P9)

One canonical, evidence-cited content model per topic, then deterministic
rendering of every artifact from that single fact list. See
`docs/PRODUCT_FACTORY.md`.

```bash
medforge product-intel "build|Thyroid Hormone Physiology"
medforge product-intel "build|Thyroid Hormone Physiology|study_guide,flashcards"
medforge product-intel "status|<content_id>"
medforge product-intel "inspect|<content_id>"
medforge product-intel "artifacts"
medforge product-intel "provenance|<artifact_id>"
medforge product-intel "consistency|<content_id>"
medforge product-intel "regenerate|<content_id>"
```

| Action | What it does |
| --- | --- |
| `build <topic>[|types|outdir]` | Generate (or reuse from cache) the canonical item and render artifacts. A topic with no evidence is refused with `{"created": false, "refused": ...}`. |
| `status <content_id>` | Per-artifact status + overall status (`READY` / `NEEDS_REVIEW` / `BLOCKED`). |
| `inspect <content_id>` | Canonical content + evidence refs. |
| `artifacts [content_id]` | Artifact rows (type, version, profile, path, checksum, status). |
| `provenance <artifact_id>` | artifact → content item → evidence → source, with prompt version and sources digest. |
| `consistency <content_id>` | Re-verifies every artifact's on-disk checksum (tamper detection). |
| `regenerate <content_id>[|outdir]` | Renders again from the stored canonical item; a changed source set produces a **new** `content_id`. |

Files land in `PRODUCTS/<topic-slug>/p9/`, one per (artifact type, adaptation
profile) — `<type>.md` for the default `developing` profile, otherwise
`<type>.<profile>.md`. Nothing is published; `build` only writes locally.

---

### `medforge migrate` — Run V3 Migration

Explicitly run the V3 database migration (normally auto-run on first use).
V4–V10 migrations (curriculum, textbook provenance, evidence graph,
recency-weighted learner model, interactive tutor, assessment engine,
study intelligence / product factory) run automatically on first use of their
feature, or explicitly via their runners:

```bash
medforge migrate
.venv-v2.1/bin/python -m core.database.migrate_v8 --db database/medforge.sqlite3
.venv-v2.1/bin/python -m core.database.migrate_v9 --db database/medforge.sqlite3
.venv-v2.1/bin/python -m core.database.migrate_v10 --db database/medforge.sqlite3
```

Every runner verifies `integrity_check` and `foreign_key_check` before
reporting success, and takes a verified snapshot into `backups/` unless
`--no-backup` is passed. All are additive and idempotent: re-running is a no-op.

---

### `medforge dashboard` — Web UI

Launch the Streamlit dashboard (loopback only, random port 8501-8520).

```bash
medforge dashboard
```

**Dashboard Tabs:**
- **PRODUCT** — Generate study packs
- **STUDY** — Interactive missions
- **REVIEW** — Flashcard session (question → reveal → grade), due list,
  retention/streak/leech analytics, scheduler choice, and pack scheduling
- **ASK** — Question answering
- **CURRICULUM** — Syllabus import, progress metrics, curriculum tree,
  topic-traceability lookup, prerequisite form
- **TEXTBOOKS** — Register/ingest textbooks, inspect editions + chapters +
  page provenance, link curriculum topics to textbook evidence, discover
  linked sources (see `TEXTBOOKS.md`)
- **LEARNER** — Recency-weighted mastery, confidence, weaknesses, priority
- **TUTOR** — Interactive adaptive tutor session (start/resume, teach, ask,
  answer, grade, adapt, summary, evidence expander) — see `TUTOR.md`
- **ASSESSMENTS** — Item bank, blueprints, assessment sessions, answering,
  results, review, remediation — see `ASSESSMENT.md`
- **EVIDENCE** — Claims → evidence → verification trace
- **LIBRARY** — Upload PDFs
- **HISTORY** — Browse generated packs
- **STATUS** — System health

---

## Adding Evidence

### PDFs
Place PDFs in `~/MedForge/docs/` or upload via Dashboard → LIBRARY.
- Re-indexed automatically on next `product` run
- OCR not included — scanned PDFs need manual OCR first

### PubMed
Automatic import on `product` run (8 results by relevance).
- Set `MEDFORGE_EMAIL` for higher NCBI rate limits
- Disable with `--offline` or `MEDFORGE_OFFLINE=1`

### Web Research
Automatic search of trusted medical domains (8 results).
- Domains: PubMed, NIH, FDA, WHO, Cochrane, guidelines, major journals
- Disable with `--offline` or `MEDFORGE_OFFLINE=1`

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDFORGE_HOME` | `~/MedForge` | Base directory |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama endpoint (loopback only) |
| `MEDFORGE_EMBED_MODEL` | `embeddinggemma` | Embedding model |
| `MEDFORGE_MODEL` | (auto) | Preferred chat model |
| `MEDFORGE_EMAIL` | (none) | NCBI email for PubMed |
| `MEDFORGE_OFFLINE` | `0` | Disable network research (also disables tutor model use) |
| `MEDFORGE_TUTOR_AUTO_MODEL` | `1` | Let the tutor use the local chat model for teaching + free-text grading |
| `MEDFORGE_ASSESSMENT_AUTO_MODEL` | `1` | Let assessment free-text grading use the local chat model |

---

## Configuration Files

| File | Purpose |
|------|---------|
| `~/MedForge/model-status.json` | Active chat model + timestamp |
| `~/MedForge/dashboard-runtime.json` | Dashboard process info |
| `~/MedForge/database/medforge.sqlite3` | Metadata + FTS5 |
| `~/MedForge/database/chroma/` | Vector embeddings |
| `~/MedForge/products/<topic>/v###/state.json` | Product pipeline state |

---

## Troubleshooting

### "No free local dashboard port"
```bash
# Kill any stuck Streamlit processes
pkill -f "streamlit run.*dashboard"
```

### "Ollama not found"
```bash
# Install Ollama
brew install ollama
# Or download from https://ollama.com/download
```

### "Model too large for 8GB profile"
Set a smaller preferred model:
```bash
export MEDFORGE_MODEL="qwen3:1.7b"
```

### "Less than 500 MB free disk space"
```bash
# Clean old backups
rm -rf ~/MedForge/backups/*
# Or free disk space
```

### Reset Everything
```bash
rm -rf ~/MedForge
bash "$HOME/Downloads/MEDFORGE-ONE-COMMAND.command"
```

---

## Advanced Usage

### Custom Topic with Resume
```bash
# Start
medforge product "aortic stenosis"

# Interrupted? Resume:
medforge product "aortic stenosis"
```

### Batch Generation (Script)
```bash
#!/bin/bash
topics=("cardiac cycle" "heart failure" "arrhythmias" "valvular disease")
for topic in "${topics[@]}"; do
    medforge product "$topic"
done
```

### Programmatic API
```python
from medforge import (build_product, ask, study, status, due_items,
                      review_card, review_analytics, spaced_repetition_snapshot)

# Generate pack (flashcards are scheduled automatically)
path = build_product("cardiac cycle")

# Ask question
answer = ask("What is the Frank-Starling mechanism?")

# Study mission (opens with due cards when the queue has any)
mission = study("cardiac cycle")

# Spaced repetition: list due cards, grade the first one "good" (SM-2 or fsrs)
cards = due_items(limit=10)
if cards:
    review_card(cards[0]["item_id"], 4, scheduler="fsrs")

# Retention, streaks, per-day counts, leeches
print(review_analytics())
print(spaced_repetition_snapshot()["summary"])
```