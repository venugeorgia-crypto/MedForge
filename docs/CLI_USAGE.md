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
(`mastery_score`, `confidence_score`, attempts), and any recorded weaknesses.

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

### `medforge mastery` — Mastery & Weakness Report

Print a JSON snapshot of learner tracking across all topics: per-topic mastery
(weakest first), unresolved weaknesses by severity, and recent sessions.

```bash
medforge mastery
```

The same data powers the **STUDY → Mastery & weaknesses** panel in the
dashboard. Mastery is the running mean of session scores; confidence is the
share of attempts scoring 70/100 or better. Weaknesses escalate in severity
when re-observed and can be resolved from the V3 tables once mastered.

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

### `medforge migrate` — Run V3 Migration

Explicitly run the V3 database migration (normally auto-run on first use).

```bash
medforge migrate
```

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
| `MEDFORGE_OFFLINE` | `0` | Disable network research |

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