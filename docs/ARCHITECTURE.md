# MedForge Architecture Overview

## System Context

```
┌─────────────────────────────────────────────────────────────────┐
│                        USER'S MAC                                │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │   Terminal   │  │   Browser    │  │   File System        │  │
│  │  (CLI cmds)  │  │ (Dashboard)  │  │ ~/MedForge/          │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬────────────┘  │
│         │                 │                      │              │
│         ▼                 ▼                      ▼              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              MEDFORGE CORE (Python 3.10+)                 │  │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐  │  │
│  │  │ Ingestion│ │ Retrieval│ │Generation│ │  Export    │  │  │
│  │  │  Module  │ │  Module  │ │  Module  │ │  Module    │  │  │
│  │  └──────────┘ └──────────┘ └──────────┘ └────────────┘  │  │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐  │  │
│  │  │  Models  │ │ Product  │ │ Storage  │ │   Utils    │  │  │
│  │  │  Module  │ │ Pipeline │ │  Module  │ │  Module    │  │  │
│  │  └──────────┘ └──────────┘ └──────────┘ └────────────┘  │  │
│  └──────────────────────────────────────────────────────────┘  │
│         │                 │                      │              │
│         ▼                 ▼                      ▼              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │   Ollama     │  │   SQLite     │  │   ChromaDB           │  │
│  │  (Local LLM) │  │  (Metadata)  │  │  (Vectors)           │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
│         │                                              ▲        │
│         │                    ┌─────────────────────────┘        │
│         ▼                    ▼                                  │
│  ┌──────────────┐  ┌──────────────────────────┐                │
│  │   Network    │  │   Core Database (V3)     │                │
│  │  (PubMed,    │  │   Schema Migration       │                │
│  │   DDGS,      │  │   (Non-destructive)      │                │
│  │   Web Fetch) │  └──────────────────────────┘                │
│  └──────────────┘                                              │
└─────────────────────────────────────────────────────────────────┘
```

---

## Module Architecture

### 1. `medforge/types.py` — Constants & Configuration
- All paths, enums, trusted domains, system prompts
- Single source of truth for configuration
- Runtime mutable state (`ACTIVE_CHAT`, `MODEL_INFO`)

### 2. `medforge/storage.py` — Data Persistence
- **SQLite**: Legacy schema (chunks, FTS5, study_sessions, weaknesses) + V3 migration
- **ChromaDB**: Vector embeddings with cosine HNSW
- **Operations**: `init_db()`, `upsert_records()`, `get_collection()`, `migrate_database()`
- **Chunking**: Overlapping word windows (default 300 words, 50 overlap)

### 3. `medforge/curriculum.py` — Canonical Curriculum Engine (V4)
- **Hierarchy**: SEMESTER → SUBJECT → WEEK → SEMINAR → TOPIC → SUBTOPIC →
  LEARNING OBJECTIVE in the `curriculum_nodes` tree + `prerequisites` graph
- **Syllabus import**: line-based parser (weeks, seminars, bullets, LOs);
  idempotent; normalization + duplicate detection; `week_number` mode for
  seminar-level imports
- **Traceability**: `topic_path()` maps a studied topic (title or slug) to its
  full curriculum chain; `curriculum_progress()` joins `learner_mastery`
- **Schema**: `core/database/migrate_v4.py` widens the node-type CHECK
  (row-preserving, self-healing); see `docs/CURRICULUM.md`

### 4. `medforge/textbook.py` — Textbook Provenance (V5)
- **Identity**: deterministic document/edition ids (title key + content
  hash); duplicate re-import idempotent, changed editions versioned
- **Ingestion**: page-wise streaming extraction; PDF bookmarks or
  conservative heading regex for Chapter→Section→Subsection; no-text pages
  parked with `ocr_status='pending'` (no invented text)
- **Provenance**: chunks keep edition/document/chapter/section/page/index and
  a human locator; mirrored into the shared chunks store with
  `kind='textbook'` and `SOURCE_PRIORITY['textbook']` so existing retrieval
  cites them unchanged
- **Linking**: `curriculum_text_links` connects P2 curriculum nodes to
  chapters/sections; `textbook_evidence_for_topic()` returns bounded previews;
  suggestions are read-only
- See `docs/TEXTBOOKS.md`

### 5. `medforge/evidence.py` — Evidence Graph (V6)
- **Claims**: deterministic extraction from generated text (prose, bullets,
  table rows; headings/code/prompts/questions/instructions skipped) with
  conservative normalization and content-addressed ids — duplicate-safe,
  never merging medically distinct statements
- **Evidence**: retrieved source chunks stored with exact P3 provenance
  (document/edition/chapter/section/page/chunk + bounded excerpt); sourced
  from the pack snapshot or bounded `hybrid_retrieve()`, never from the
  generated text itself
- **Verification**: model-assisted per (claim, evidence) pair inside a strict
  JSON contract with delimited data-only prompts; malformed/absent answers and
  missing evidence degrade to `INSUFFICIENT_EVIDENCE` (zero model calls when
  there are no candidates); deterministic aggregation never upgrades
  UNSUPPORTED/PARTIALLY_SUPPORTED; every attempt is appended to
  `verification_runs`
- **Surfaces**: `claims`/`claim-info`/`verify-pack`/`evidence-status` CLI +
  the dashboard EVIDENCE tab; `build_product()` runs a guarded, bounded pass
  and writes an excerpt-free `evidence-graph.json`
- See `docs/EVIDENCE.md`

### 5b. `medforge/learner_model.py` — Recency-Weighted Learner Model (V7)
- **Evidence**: append-only `learning_attempts` (topic slug, item type/id,
  timestamps, score, correctness, learner confidence, response time, source,
  content version, optional session/curriculum links)
- **Estimate**: materialized `learner_model_state` per topic — exponential decay
  (`half-life 21 d`) over all attempts, shrunk toward a 0.5 prior, with
  recent/historical split, consistency, ESS-based uncertainty and evidence
  count; versioned `p6-rwm-v1`
- **Confidence**: tracked separately from mastery; signed calibration gap and
  over/under-confident direction, `None` when no confidence was reported
- **Weaknesses**: deterministic detector over the estimates — known vs
  possible/low-confidence, severity from the estimate, prerequisite impact, and
  auto-recovery of model-created rows (manual/review rows are never touched)
- **Priority**: deterministic weighted signal (weakness, uncertainty, overdue,
  recent failure, prerequisite impact) with every component exposed
- **Reproducibility**: pure recalculation from stored attempts; a rebuild at the
  stored evaluation time must equal the materialized row (`matches_stored`)
- **Surfaces**: `learner`/`mastery`/`weaknesses`/`history`/`recalculate`/
  `study-priority` CLI + the dashboard LEARNER tab
- See `docs/LEARNER_MODEL.md`

### 6. `medforge/ingestion.py` — Evidence Acquisition
- **PDFs**: `pypdf` extraction, SHA-256 change detection, incremental re-index
- **PubMed**: NCBI E-utilities (esearch + efetch), XML parsing
- **Web Research**: DDGS search → domain allowlist → hardened fetch → trafilatura extraction
- **Security**: DNS validation, IP allowlist (global only), size limits, redirect limits

### 7. `medforge/retrieval.py` — Hybrid Search
- **Keyword**: SQLite FTS5 BM25
- **Vector**: ChromaDB cosine similarity
- **Fusion**: Reciprocal Rank Fusion (RRF) with quality weighting
- **Filtering**: Quality thresholds, keyword-match requirement for web, distance threshold for vector

### 8. `medforge/models.py` — LLM Management
- **Ollama lifecycle**: Health check, auto-start, model pull
- **Model selection**: Embedding readiness → chat model test → fallback chain
- **Resource management**: Stop unused models, disk space checks
- **Generation**: Chat + embeddings with retries, thinking token handling

### 9. `medforge/generation.py` — Content Generation
- **Prompt templates**: Evidence + task → structured output
- **Citation audit**: Label validation, HTML/JSON reports (compatibility floor —
  the V6 evidence graph is the verification mechanism; see `docs/EVIDENCE.md`)
- **Flashcard parsing**: TSV with source label verification

### 10. `medforge/export.py` — Output Formats
- **PDF**: ReportLab with custom fonts, headers/footers
- **Anki**: genanki with evidence-backed cards, HTML formatting

### 11. `medforge/product.py` — Pipeline Orchestration
- **State machine**: Versioned directories, SHA-256 content hashing, resumable
- **Steps**: Ingestion → Source snapshot → Generation → Export → Audit →
  Evidence graph (guarded, bounded, never blocks the pack)
- **Concurrency**: File locking (`.job.lock`) for serialization

### 12. `medforge/utils.py` — Shared Utilities
- Filesystem, locking, hashing, shell, time, decorators

---

## Data Flow

### Product Generation Pipeline

```
USER TOPIC
    │
    ▼
┌─────────────────────────────────────┐
│  INGESTION (parallel)               │
│  ├─ PDFs (~/MedForge/docs)          │
│  ├─ PubMed (NCBI E-utilities)       │
│  └─ Web (DDGS + allowlist fetch)    │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  SOURCE SNAPSHOT                    │
│  hybrid_retrieve(topic, 6)          │
│  → source_pack() → labeled blocks   │
│  Saved: source-map.json, source-text.txt
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  GENERATION (sequential)            │
│  For each of 10 output formats:     │
│  generate_text(model, topic,        │
│    sources, task_prompt)            │
│  Saved: *.md, content-hashed        │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  EXPORT                             │
│  ├─ Flashcards (TSV → CSV + .apkg)  │
│  ├─ PDFs (study-guide, workbook,    │
│  │       cheat-sheet)               │
│  └─ References.md                   │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  CITATION AUDIT (compatibility)     │
│  Scan all generated text for        │
│  [S#] labels → evidence-report.*    │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  EVIDENCE GRAPH (V6, bounded)       │
│  extract claims → store evidence    │
│  (exact P3 provenance) → model-     │
│  assisted verification → aggregate  │
│  status + append-only history       │
│  Saved: evidence-graph.json         │
│  (excerpt-free); excerpts stay in   │
│  the private SQLite database        │
└─────────────────────────────────────┘
```

---

## Security Model

### Network Boundaries
- **Ollama**: Loopback only (`127.0.0.1:11434`) — enforced in `http_json()`
- **Web Fetch**: HTTPS only, allowlisted domains, global IP validation
- **PubMed**: NCBI E-utilities (HTTPS, rate-limited)

### Data Isolation
- All user data in `~/MedForge/` — no cloud sync
- Private Python venv (`~/.venv-v2.1/`)
- No telemetry (`ANONYMIZED_TELEMETRY=False`)

### Input Validation
- URL parsing + scheme/host validation before fetch
- SQL parameterization (no string interpolation)
- File locks for concurrent access prevention

---

## Concurrency Model

| Resource | Lock Mechanism | Scope |
|----------|---------------|-------|
| Product pipeline | `.job.lock` (fcntl) | Process-wide |
| Dashboard | `.dashboard.lock` (fcntl) | Process-wide |
| Installer | `.install.lock` (fcntl) | Process-wide |
| SQLite | WAL mode + connection per operation | Thread-safe reads |
| ChromaDB | Single PersistentClient | Thread-safe |

---

## Resource Management

### Memory (8GB Target)
- Stop embedding model before chat generation
- Stop chat model before embeddings
- Batch upserts (4 records at a time)
- Stream PDF generation

### Disk
- Pre-flight check: 500MB minimum for product
- Model download check: 4GB minimum
- Automatic cleanup of old backup IDs
- Incremental PDF indexing (SHA-256)

### Models
- Embedding: `embeddinggemma` (~1GB)
- Chat: `qwen3:4b-instruct` (~2.5GB) preferred, `qwen3:1.7b` fallback
- Max model size: 3.6GB for 8GB systems

---

## Extensibility Points

### Adding New Output Formats
1. Add task prompt to `tasks` dict in `product.py`
2. Add export logic in generation loop
3. Include in `citation_audit()` texts dict

### Custom Retrieval Strategies
- Subclass/extend `hybrid_retrieve()` in `retrieval.py`
- Add new `vector_results()` / `keyword_results()` variants

### Additional Evidence Sources
- Add function to `ingestion.py` returning chunk records
- Call in `build_product()` ingestion phase
- Update `source_pack()` filtering if needed

### V3 Schema Integration
- New tables created by migration
- Wire into product pipeline via `curriculum_nodes` lookup
- Add mastery/weakness tracking to study sessions

### Learner Model Integration (P6, V7)
- `learning_attempts` is append-only evidence; add new evidence sources by
  calling `record_learning_event(...)` (validate `item_type`/`source` against
  `T.LEARNING_ATTEMPT_*`) — no schema change needed for a new question source
- `learner_model_state` is materialized; never write it directly — always go
  through `record_learning_event` / `recalculate_mastery` so decay stays
  reproducible
- Changing decay, prior, thresholds or priority weights requires a new
  `LEARNER_MODEL_VERSION` and a recalculation pass
- P7/P8 consume the read API only: `get_mastery`, `get_weaknesses`,
  `get_recent_performance`, `get_confidence`, `get_prerequisite_risks`,
  `get_review_priority`

### Tutor Integration (P7, V8)
- The tutor is a **stateful loop**, not a chat surface: `tutor_sessions` holds
  the stage machine, the current question, the pending answer and the stored
  summary, so a refresh or restart resumes the same session. Never keep tutor
  state in Streamlit session state or module globals.
- Evidence must be retrieved and persisted **before** teaching: P3
  curriculum-linked textbook chunks first, bounded retrieval second, each stored
  through P4 `store_evidence`. Headings are stripped and off-concept rows are
  excluded, so a retrieval hit from another chapter cannot become this topic's
  rubric.
- The tutor never verifies its own output: the P4 status decides whether the
  tutor teaches, qualifies or abstains. A model call may refine a verdict but
  never upgrades it.
- Learner evidence is recorded only through `record_learning_event`; the tutor
  never writes mastery tables. Each turn stores its `attempt_id`, so a resumed
  or retried turn cannot duplicate an event.
- Adaptation (`adapt_tutor`) is a deterministic table over the graded answer,
  the stored streaks and freshly re-read P6 state; changing it changes tutor
  behaviour, so it needs a `TUTOR_VERSION` bump and the P7/P8 test suites.
- Session summaries sync one spaced-repetition review through the existing
  `review_card` interface; SM-2/FSRS math stays in `learner.py`.

### Assessment Integration (P8, V9)
- The item bank is **versioned**: `assessment_items` holds identity +
  lifecycle, `assessment_item_versions` holds immutable content per
  `(item_id, item_version)`. A material change (wording, answer, rubric,
  evidence basis) creates a new version — old versions are never mutated, so
  historical assessments replay exactly.
- Every medically substantive item carries a P4 evidence basis: `evidence_refs`
  + `claim_refs` captured from P3/P4 rows and an `evidence_state` computed by
  `tutor.assess_evidence`. `approve_item()` refuses `UNSUPPORTED`,
  `CONTRADICTED` or `INSUFFICIENT_EVIDENCE` items; `PARTIALLY_SUPPORTED` needs
  an explicit recorded review. Generation creates DRAFTs only — approval is a
  separate, explicit gate.
- Grading reuses P7's `evaluate_answer` dispatch; the only addition is the
  deterministic `grade_multi_select` scorer beside `grade_mcq` for `MCQ_MULTI`
  items governed by item-level scoring policy. There is no second grading
  engine.
- Assessment sessions live in `assessment_sessions` (separate from
  `tutor_sessions`). Each attempt row persists the presented item snapshot
  (stem, choices, rubric, evidence refs), so later item edits cannot change a
  historical attempt. Graded answers are never overwritten; retryable answers
  are re-graded in place with `grading_history` appended.
- Timing, selection, scoring and statistics are deterministic and model-free:
  server-side `started_at`/`expires_at`, seeded stable selection with
  reported relaxations, transparent statistics that always carry `sample_size`
  ("insufficient sample" below the floor). This is lightweight reporting, not
  validated psychometrics.
- Learner evidence flows only through `record_learning_event` (one event per
  attempt, guarded by `learning_attempt_id`); remediation reads P6 weakness/
  priority signals and returns a proposal with `launch: false` — starting a
  tutor session is always an explicit learner action.

---

## Deployment Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Installer  │────▶│  Release    │────▶│  Current    │
│  (bash+py)  │     │  (versioned)│     │  (symlink)  │
└─────────────┘     └─────────────┘     └─────────────┘
                           │                    │
                           ▼                    ▼
                    ┌─────────────┐     ┌─────────────┐
                    │  Backups    │     │  Commands   │
                    │  (venv, DB) │     │  (~/bin/)   │
                    └─────────────┘     └─────────────┘
```

- **Installer**: Self-contained base64+gzip payload (release files + `core/` V3 modules) with SHA-256 verification; rebuild it with `tools/build_installer.py`
- **Releases**: Immutable versioned directories (`releases/2.1.0-<hash>/`), content-addressed by the payload SHA-256
- **Current**: Symlink to active release, atomic swap on upgrade
- **Commands**: Shell shims in `~/bin/` pointing to `current/medforge_launch.py`
- **Rollback**: `previous-release.txt` tracks last version

---

## Technology Stack

| Layer | Technology | Version |
|-------|------------|---------|
| Language | Python | 3.10-3.14 |
| LLM Runtime | Ollama | Latest |
| Embedding Model | embeddinggemma | Via Ollama |
| Chat Models | qwen3:4b-instruct, qwen3:1.7b | Via Ollama |
| Vector DB | ChromaDB | 1.x |
| Metadata DB | SQLite | 3.x (FTS5) |
| PDF Gen | reportlab | 4.x |
| Anki Gen | genanki | 0.13.x |
| Web Search | ddgs | 9.x |
| Web Extract | trafilatura | 2.x |
| PDF Parse | pypdf | 5.x |
| Dashboard | Streamlit | 1.45+ |
| HTTP | urllib (stdlib) / requests | - |
| Concurrency | fcntl (file locks) | - |