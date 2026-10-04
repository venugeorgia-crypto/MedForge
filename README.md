# MedForge

MedForge is a local-first AI study-material generator for medical topics. It
runs entirely on your machine — Ollama for chat and embeddings, a local SQLite
database, and a Streamlit dashboard — and turns a topic into a complete study
pack: indexed evidence, study guide / workbook / cheat-sheet PDFs, flashcards
(Anki `.apkg` + CSV), quizzes, viva questions, case exercises and a mind map,
with full source citations for every claim.

## Features

- **Topic → study pack pipeline** — PDF ingestion, PubMed abstracts, fetched
  authoritative web text, hybrid keyword + vector retrieval (Chroma +
  `embeddinggemma`), and generation with a local Ollama chat model
  (`qwen3:4b-instruct` by default).
- **Spaced repetition** — SM-2 and FSRS-4.5 schedulers, an append-only
  `review_log`, due-card sessions in the dashboard, retention / streak
  analytics and leech detection.
- **Study missions** — guided study sessions anchored on the cards you are
  about to forget.
- **Local dashboard** — Streamlit UI bound to `127.0.0.1` with generation,
  library, analytics and review tabs.
- **CLI** — `medforge_core.py` for product generation, asking the indexed
  evidence, and reviewing cards.
- **Reliability** — fixed source snapshots, atomic state writes, file
  checksums for resumed generation, versioned products and diagnostic logs.

## Repository layout

| Path | What it is |
| --- | --- |
| `releases/2.1.0-20935f509d37/` | Application source: the `medforge` package, `dashboard.py`, `medforge_core.py` (CLI), `medforge_launch.py`, `bootstrap.py`, `requirements.txt` |
| `core/` | Database schema (V3) and migration tooling |
| `docs/` | `ARCHITECTURE.md`, `V3_SCHEMA.md`, `CLI_USAGE.md` |
| `tests/` | Pytest suite (46 tests) |
| `tools/` | `build_installer.py` |
| `products/` | Sample generated study packs (acromegaly, prolactinoma, pituitary topics) |

> On a live install, a `current` symlink points at the active release. It is
> machine-specific and intentionally not committed.

## Requirements

- macOS with Python 3.12 (developed and tested on macOS)
- [Ollama](https://ollama.com) running locally (`127.0.0.1:11434`) with
  `qwen3:4b-instruct` and `embeddinggemma` pulled

## Quick start (from source)

```bash
git clone https://github.com/venuchowdary/MedForge.git
cd MedForge/releases/2.1.0-20935f509d37
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# check models + database readiness
python medforge_core.py doctor

# generate a study pack
python medforge_core.py product "cardiac cycle"

# or launch the dashboard
streamlit run dashboard.py
```

## CLI quick reference

```bash
python medforge_core.py product "cardiac cycle"    # generate a full pack
python medforge_core.py ask "what triggers GH release?"
python medforge_core.py import-cards "cardiac cycle"  # pack -> spaced repetition
python medforge_core.py due                        # list cards due for review
python medforge_core.py review <item_id> 4 fsrs    # grade a review (0–5)
```

See [docs/CLI_USAGE.md](docs/CLI_USAGE.md) for the full command reference and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pipeline fits
together.

## Tests

```bash
python -m pytest tests/ -q
```

## Medical evidence status

Every generated pack is a **draft requiring source review**. The citation scan
checks citation-label format, not whether a cited passage actually proves the
claim. All factual material remains subject to human review before use in
teaching or publication.
