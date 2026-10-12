# MedForge

MedForge is a local-first, evidence-grounded medical study platform for Apple Silicon Macs. It runs entirely on your machine — Ollama for chat/embeddings, SQLite for metadata, ChromaDB for vectors, Streamlit for the dashboard — and turns a curriculum topic into a complete, traceable study workflow: evidence retrieval → learner analysis → adaptive tutoring → assessment → canonical content generation → quality gates → private/distributable exports.

## Features

- **Curriculum engine (P2)** — Semester → Subject → Week → Seminar → Topic → Subtopic → Learning Objective hierarchy; syllabus import; prerequisite graph; topic traceability.
- **Textbook provenance (P3)** — Document/edition identity via content hash; chapter/section/page/chunk extraction; stable locators; curriculum ↔ textbook links.
- **Evidence graph (P4)** — Claim extraction from generated text; evidence with exact provenance (doc/edition/chapter/section/page); model-assisted verification (SUPPORTED/PARTIALLY_SUPPORTED/UNSUPPORTED/CONTRADICTED/INSUFFICIENT_EVIDENCE); abstention when no candidates.
- **Recency-weighted learner model (P6)** — Exponential decay (half-life 21d), prior shrinkage, confidence calibration, weakness detection with auto-recovery, deterministic study priority.
- **Interactive adaptive tutor (P7)** — Persistent stage machine (TEACH→ASK→WAIT→EVALUATE→EXPLAIN→ADAPT→COMPLETE); evidence-first teaching; 7 modes; deterministic + model-assisted grading; P6 integration.
- **Question-level assessment engine (P8)** — Versioned item bank (6 types); blueprints; deterministic selection; PRACTICE/EXAM/REVIEW modes; P7 grading reuse; item statistics; P6 remediation.
- **Study intelligence + canonical content factory (P9)** — Orchestrates P2/P3/P4/P6/P7/P8/SR; deterministic recommendations with reasons; persistent plans/missions; one canonical content model → 6 artifact types (study guide, cheat sheet, flashcards, quiz, mind map, script); learner-adapted rendering; provenance + checksums.
- **Publication workflow (P10)** — 9 approval gates (curriculum alignment, evidence coverage, P4 verdicts, citation continuity, consistency, medical-risk scan, copyright bound, formatting, artifact generation); review queue; private/distributable export separation.
- **Video production (P11)** — Storyboard → frames → TTS → FFmpeg → FFprobe QA → SRT/VTT captions → MP4; 16:9, 9:16, 1:1 aspects.
- **Provider abstraction + model router (P12)** — Ollama (default), Mock, OpenAI (opt-in); task-aware routing (teaching→large context, grading→small/fast, reasoning→thinking-capable); audit trail.
- **Knowledge refresh (P13)** — Textbook hash diff → new edition + chunk diff; PubMed PMID set diff; web ETag/body hash; claim re-verification via P4; notifications.
- **Automation (P14)** — Nightly workflow (maintenance → refresh → verified backup → study prep); launchd scheduling; audit trail.
- **Reliability/security/hardening (P15)** — Diagnostics, verified backup/restore/repair, URL credential rejection, FTS repair, health checks.
- **Unified orchestration (P18)** — `start_todays_study()` single entry point; unified history tracing; dashboard HOME tab with "Start Today's Study" button.

## Repository layout

| Path | What it is |
| --- | --- |
| `releases/2.1.0-20935f509d37/` | Application source: the `medforge` package, `dashboard.py`, `medforge_core.py` (CLI), `medforge_launch.py`, `bootstrap.py`, `requirements.txt` |
| `core/` | Database schema (V3→V16) and migration tooling |
| `docs/` | `ARCHITECTURE.md`, `V3_SCHEMA.md`, `CLI_USAGE.md`, `STUDY_INTELLIGENCE.md`, `PRODUCT_FACTORY.md`, `PUBLICATION.md`, `VIDEO.md`, `MODEL_ROUTER.md`, `KNOWLEDGE_REFRESH.md`, `AUTOMATION.md`, `SECURITY.md`, `OPERATIONS.md`, `EVALUATION.md` |
| `tests/` | Pytest suite (454 tests: unit, integration, migration, CLI, privacy, E2E) |
| `tools/` | `build_installer.py` |
| `products/` | Generated study packs |

> On a live install, a `current` symlink points at the active release. It is machine-specific and intentionally not committed.

## Requirements

- macOS with Python 3.12 (developed and tested on M1 MacBook Air, 8 GB RAM, ~256 GB storage)
- [Ollama](https://ollama.com) running locally (`127.0.0.1:11434`) with `qwen3:4b-instruct` and `embeddinggemma` pulled
- For video: `ffmpeg`, `ffprobe`, `pillow`, macOS `say` (or pluggable TTS)

## Quick start (from source)

```bash
git clone https://github.com/venugeorgia-crypto/MedForge.git
cd MedForge/releases/2.1.0-20935f509d37
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# check models + database readiness
python medforge_core.py doctor

# unified study workflow (requires Ollama)
python medforge_core.py study-today

# generate a study pack
python medforge_core.py product "cardiac cycle"

# launch the dashboard
streamlit run dashboard.py
```

## CLI quick reference

```bash
# Unified study workflow
medforge_core.py study-today [topic] [|budget_minutes] [|run_tutor] [|run_assessment] [|generate_products] [|run_quality_gates] [|export_mode]

# Study history tracing
medforge_core.py study-history [topic] [|format] [|output_path]

# Core commands
medforge_core.py product "cardiac cycle"        # generate full pack
medforge_core.py ask "what triggers GH release?" # question answering
medforge_core.py study "cardiac cycle"           # interactive mission
medforge_core.py due                            # spaced repetition due cards
medforge_core.py review <item_id> 4 fsrs        # grade a review (0–5)
medforge_core.py launch verify|run|restart|upgrade|rollback|diagnose
```

See [docs/CLI_USAGE.md](docs/CLI_USAGE.md) for the full command reference.

## Dashboard tabs

HOME • PRODUCT • STUDY • REVIEW • LEARNER • TUTOR • ASSESSMENTS • ASK • CURRICULUM • TEXTBOOKS • EVIDENCE • VIDEOS • STUDY PLAN • LIBRARY • HISTORY • SYSTEM • EVALUATION

## Tests

```bash
python -m pytest tests/ -q
# 454 passed, 1 skipped
```

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the pipeline overview, [docs/STUDY_INTELLIGENCE.md](docs/STUDY_INTELLIGENCE.md) for the orchestrator, [docs/PRODUCT_FACTORY.md](docs/PRODUCT_FACTORY.md) for canonical content, [docs/PUBLICATION.md](docs/PUBLICATION.md) for approval workflow, [docs/VIDEO.md](docs/VIDEO.md) for video production.

## Medical evidence status

Every generated pack is a **draft requiring source review**. The citation scan checks citation-label format, not whether a cited passage actually proves the claim. P4 verification provides SUPPORTED/PARTIALLY_SUPPORTED/UNSUPPORTED/CONTRADICTED/INSUFFICIENT_EVIDENCE statuses with deterministic aggregation. All factual material remains subject to human review before use in teaching or publication.

## Privacy

Private exports contain learner history, evidence excerpts, misconceptions. Distributable exports contain only rendered artifacts + reference locators — learner data, prompts, and long verbatim runs are rejected at export time. Verified by 18 privacy tests.