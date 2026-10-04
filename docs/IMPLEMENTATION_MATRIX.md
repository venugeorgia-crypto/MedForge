# MedForge Implementation Matrix — Phase 0 Forensic Audit

**Audit date:** 2026-10-04 · **Code audited:** `releases/2.1.0-20935f509d37` (the running 2.1.0 build),
`core/database/`, `tests/`, `docs/`, `tools/`, live DB `database/medforge.sqlite3`.

**Method:** every source module read directly (types, storage, learner, product, generation,
retrieval, ingestion, export, models, utils, dashboard, CLI, launchers, installer builder),
schema + migration read in full, live DB queried read-only, all tests and docs reviewed.
"COMPLETE" below means working end-to-end with validation; "PARTIAL" means real but incomplete;
verdicts are KEEP / FIX / REFACTOR / REPLACE / MISSING.

Live DB row counts at audit time: `curriculum_nodes=0, prerequisites=0, learner_mastery=0,
learner_weaknesses=0, interactive_sessions=0, spaced_repetition_queue=119, review_log=0,
chunks=47, schema_migrations=1`.

---

## P1 Foundation (keep-list verification)

| Requirement | Current implementation | Source file | Actual behavior | Test coverage | Risk | Verdict |
|---|---|---|---|---|---|---|
| SQLite metadata store | chunks + chunks_fts (FTS5), legacy study_sessions/weaknesses | `medforge/storage.py`, `core/database/schema.py` | Verified: 47 chunks live; FTS used by retrieval | `tests/test_phase1_db.py`, `test_retrieval.py` | Low | **KEEP** |
| Chroma vector store | PersistentClient, per-embed-model collection name | `medforge/storage.py` | Working; falls back to keyword-only when down | `test_retrieval.py` | Low | **KEEP** |
| Ollama chat + embeddings | loopback-only guard, capability checks, 3.6 GB size cap, model unload | `medforge/models.py` | Enforces 127.0.0.1; `keep_alive` + `stop_model()` free RAM; qwen3:4b-instruct fallback chain | mocked tests (test_generation/models) | Low | **KEEP** |
| Streamlit dashboard | 7 tabs (PRODUCT/STUDY/REVIEW/ASK/LIBRARY/HISTORY/STATUS) | `dashboard.py` | Working, verified in browser | manual verification only | Low | **KEEP** |
| PDF ingestion | pypdf page extraction, sha256 change-detection, stale-chunk retirement | `medforge/ingestion.py` | Page-level chunks → Chroma+SQLite; reports "may need OCR" for scanned PDFs | `test_ingestion.py` (fixtures) | Medium (no OCR) | **KEEP; OCR MISSING** |
| PubMed import | E-utilities esearch/efetch XML, chunk+index | `medforge/ingestion.py` | Works online; degrades gracefully offline | fixture tests | Low | **KEEP** |
| Trusted web retrieval | HTTPS-only, port 443, domain allowlist, resolved-IP private-range check, size/time caps | `medforge/ingestion.py` `_resolve_and_validate` | SSRF-hardened (no localhost/private IPs, no credentials-in-URL) | `test_ingestion.py` | Low | **KEEP** |
| Learner tables | learner_mastery, learner_weaknesses, interactive_sessions | `core/database/schema.py`, `medforge/learner.py` | Actively used by study/REVIEW flows; self-healing DDL | `test_learner.py`, `test_spaced_repetition.py` | Low | **KEEP** |
| Spaced repetition (SM-2/FSRS) | SM-2 + FSRS-4.5, lapse→relearning, review_log, analytics, leeches | `medforge/learner.py` | 119 scheduled cards live; weakness lifecycle verified | `test_spaced_repetition.py` (14 tests) | Low | **KEEP** |
| Anki export | genanki deck, escaped fields, NeedsReview tag | `medforge/export.py` | Works from validated flashcards.csv | `test_export.py` | Low | **KEEP** |
| Resumable products | state.json step checksums, atomic writes, resume detection | `medforge/product.py`, `medforge/utils.py` | Resume verified; source-snapshot digest stops on evidence change | `test_spaced_repetition.py` (auto-import) | Low | **KEEP** |
| Backup/migration | verified snapshot backup, integrity check, rollback, data-loss detector | `core/database/migrate_v3.py` | Tested | `test_phase1_db.py` | Low | **KEEP** |
| Local launcher | `medforge_launch.py`, `MEDFORGE.command` → `~/bin/medforge` | launchers | macOS-only; requires prior install | none (out of unit-test scope) | Low | **KEEP** |

## P2 Curriculum engine

| Requirement | Current implementation | Source file | Actual behavior | Test coverage | Risk | Verdict |
|---|---|---|---|---|---|---|
| Curriculum hierarchy tables | `curriculum_nodes` (Year/Semester/Course/Module/Topic/Subtopic/LO) | `core/database/schema.py` | **Table exists but is DEAD** — 0 rows, no app code reads/writes it | schema tests only | High if left dead | **MISSING** → build on it (this session) |
| Prerequisite graph | `prerequisites` table | `core/database/schema.py` | **DEAD** — 0 rows, no app code | schema tests only | High | **MISSING** → build on it |
| Weekly syllabus import | none | — | — | — | — | **MISSING** → implement (this session) |
| Seminar import / topic normalization / duplicate detection / ordering / progress | none | — | — | — | — | **MISSING** → implement (this session) |
| Topic traceability to curriculum | none (topics are free-text slugs) | `medforge/learner.py` | mastery keyed on `slugify(topic)` only | — | High | **MISSING** → implement (this session) |

## P3 Textbook knowledge engine

| Requirement | Current implementation | Source file | Actual behavior | Test coverage | Risk | Verdict |
|---|---|---|---|---|---|---|
| Textbook metadata (title/authors/edition/year/chapter/section/page) | only generic `chunks(source, locator='page N', kind='course_pdf')` | `medforge/ingestion.py` | Page locators preserved from PDF extraction; no textbook entity, no edition/authors | partial (ingestion) | Medium | **PARTIAL → MISSING** |
| OCR status/confidence | none (reports "may need OCR") | `medforge/ingestion.py` | Scanned PDFs skipped with message | — | Medium | **MISSING** |
| Tables / figure references | none | — | — | — | — | **MISSING** |
| TOPIC ↔ TEXTBOOK mapping | none | — | — | — | — | **MISSING** |

## P4 Evidence graph

| Requirement | Current implementation | Source file | Actual behavior | Test coverage | Risk | Verdict |
|---|---|---|---|---|---|---|
| Citation-label validation | `citation_audit`: per-line [S#] label scan vs source labels, HTML/JSON report | `medforge/generation.py` | Honest: report states "semantic support has NOT been verified" | `test_generation.py` | Low | **KEEP (as floor)** |
| Claim records (claim_id, text, source, locator, excerpt, statuses) | none | — | — | — | High | **MISSING** |
| Semantic support / contradiction / population / date / causation checks | none | — | — | — | High | **MISSING** |
| Explicit INSUFFICIENT state | partially: prompts say "If evidence is insufficient, explicitly say so" | `medforge/types.py` | Model-behavior only; not machine-checked | — | High | **PARTIAL** |

## P5 Source hierarchy

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Per-task source ranking | single generic `quality` weight (domain trust 0.55–1.0) | `medforge/types.py`, `medforge/retrieval.py` | No task-conditioned hierarchy; quality is a *ranking hint only* (labeled as such in prompts/reports) | `test_retrieval.py` | Medium | **PARTIAL** |
| Preserve authority separately from rank | `publication_type` exists in `medical_sources` (dead table) | `core/database/schema.py` | Not connected to retrieval | — | Medium | **MISSING wiring** |

## P6 Learner model

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Topic mastery + confidence | incremental mean over attempts + pass-share | `medforge/learner.py::update_mastery` | Real, persisted, tested | yes | Low | **KEEP** |
| Recency-weighted / question-level evidence | mastery is a running mean (no recency decay); review_log has per-review grades | `learner.py`, schema | Review history exists; mastery not recency-weighted | partial | Medium | **PARTIAL → FIX later** |
| Concept/subtopic mastery, learning velocity, forgetting | weaknesses per (topic, concept) with error escalation; FSRS stability tracks forgetting per card | `learner.py` | No concept-level mastery aggregation; velocity not computed | partial | Medium | **PARTIAL** |
| Survives restart/upgrade/regeneration | SQLite-backed; content-addressed card ids survive pack rebuild | `learner.py` | Verified by idempotent import tests | yes | Low | **KEEP** |

## P7 Adaptive tutor

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Interactive ask→answer→evaluate→adapt loop | **MISSING.** STUDY emits a static generated mission; one self-score slider | `medforge/product.py::study`, `dashboard.py` | No answer evaluation, no dynamic adaptation | — | High | **MISSING** |
| Weak-concept remediation flow | mission opens with due-card recall; weaknesses recorded from lapses/self-report | `product.py`, `learner.py` | Partial: uses SR weakness signal | `test_spaced_repetition.py` | High | **PARTIAL** |

## P8 Assessment engine

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Question-level result recording | none — quiz.md is generated text; self-score only | `product.py` | — | — | High | **MISSING** |
| MCQ/viva/case generation | generated formats exist with citation rules | `product.py GENERATION_TASKS` | Format-enforced, not graded | — | Medium | **PARTIAL** |
| Remediation automation | none | — | — | — | High | **MISSING** |

## P9 Spaced learning

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| SM-2/FSRS preserved | yes, both, with review_log | `learner.py` | Verified | yes | Low | **KEEP** |
| Card→curriculum/concept/source links | card → topic slug only (item_id prefix); sources string on card | `learner.py` | No curriculum node / concept / claim links | partial | Medium | **PARTIAL** |
| Leech → revised card generation | leech detection exists (3+ lapses) | `learner.py::review_analytics` | Detection only; no auto-rewrite | yes (detection) | Medium | **PARTIAL** |

## P10 Product factory / P11 PDF engine / P12 video engine

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| 15 output types from one snapshot | 10 generated artifacts + flashcards + 3 PDFs from one frozen source snapshot | `product.py` | Study notes/guide/cheat/workbook/MCQ/viva/case/Anki/mindmap/scripts/carousel/captions/description/refs ✔; **mechanism diagram MISSING** | product tests partial | Medium | **PARTIAL** |
| Consistent derived outputs | single source_text + checksums per artifact | `product.py` | Verified resume/consistency mechanism | partial | Low | **KEEP** |
| PDF quality (headings/tables/callouts/pages/pearls) | reportlab headings/bullets/footer; no tables/diagrams/callouts | `export.py` | Plain but clean draft PDFs | `test_export.py` | Medium | **PARTIAL** |
| OCR for scanned PDFs | none | — | Reported as limitation in-app | — | Medium | **MISSING** |
| Video pipeline | scripts only (short/3-min); no MP4 rendering | `product.py` | Honest script outputs | — | High | **PARTIAL → MISSING renderer** |

## P13 Model router / P14 Resource manager

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Provider interfaces | implicit: `models.chat/embed` direct Ollama calls behind 4 functions | `models.py` | Loopback-only guard; no provider abstraction | partial | Medium | **PARTIAL** |
| Model metadata on artifacts | `state["chat_model"]` saved per product | `product.py` | Per-pack, not per-artifact | — | Low | **PARTIAL** |
| 8 GB discipline | one-job lock (`job_lock`/`serialized`), model unload after embed phase, 3.6 GB model cap, disk-quota check (500 MB), keep_alive tuning | `utils.py`, `models.py`, `product.py` | Verified patterns; no memory *measurement* harness | partial | Low | **KEEP; measurement MISSING** |

## P15 Private vs distributable output

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Private review bundle | full pack zip incl. source-text.txt / evidence reports | `dashboard.py::show_pack` | Private-safe by default | — | Low | **PARTIAL** |
| Distributable bundle (no copyrighted excerpts) | none — no redaction distinction | — | — | — | High | **MISSING** |

## P16 Testing

| Requirement | Current | Files | Verdict |
|---|---|---|---|
| Unit tests | 46 passing across learner/SR, DB, export, generation, ingestion, retrieval, models | `tests/` | **KEEP**, extend |
| Integration (live Ollama) | marker defined (`integration`), unused | `conftest.py` | **PARTIAL** |
| Recovery/migration tests | backup/integrity/data-loss/rollback covered | `test_phase1_db.py` | **KEEP** |
| Claim-verification / tutor / video / OCR tests | N/A — features missing | — | **MISSING with features** |

## P17 Security

| Requirement | Current | Source file | Actual behavior | Verdict |
|---|---|---|---|---|
| SSRF / private-network fetch | HTTPS-only, allowlist, resolved-IP check, 443-only, size+time caps | `ingestion.py` | Solid | **KEEP** |
| Prompt injection | system prompt declares source data untrusted; no instruction-execution path from source text | `types.py`, `generation.py` | Reasonable for local single-user; no injection-strip filter | **PARTIAL** |
| Shell safety | subprocess list-args everywhere, no shell=True; `subprocess.run(["open", path])` only | `utils.py`, `models.py`, `product.py` | Safe | **KEEP** |
| Path traversal | PDF upload name-sanitized (`Path(name.replace("\\","/")).name`), content-type check `%PDF-` | `dashboard.py` | Good | **KEEP** |
| Secrets in logs | no API keys used at all (local-first); Ollama loopback only | `models.py` | No cloud = small surface | **KEEP** |
| Accidental cloud upload | none possible in current code (no cloud calls) | — | — | **KEEP** |

## P18 Documentation reality check

| Item | Status |
|---|---|
| `docs/V3_SCHEMA.md` | Matches schema.py incl. review_log; will need node-type update after P2 |
| `docs/CLI_USAGE.md` | Matches CLI commands incl. import-cards/due/review; verified against `medforge_core.py` |
| `docs/ARCHITECTURE.md` | Describes pipeline accurately; curriculum described as "schema ready" → now misleading (dead tables) |
| Release README (`releases/…/README.md`) | References `MEDFORGE-ONE-COMMAND.command` which is not in repo (installer artifact git-ignored) — **stale for GitHub context** |
| "OCR is not yet included" (dashboard) | Accurate |

---

## Headline audit conclusions

1. **The 2.1 core loop is real and tested** — ingestion → hybrid retrieval → cited generation → exports → SR review, with resumability, backups, and genuine security hardening. **Preserve it (P1 satisfied).**
2. **The biggest documented-but-not-implemented gap is the curriculum layer**: V3 schema tables exist, are empty, and are referenced by zero lines of application code. All P2 acceptance criteria are unmet today.
3. **Citation labels ≠ verification (P4)**: the system is *honest* about this (reports say so), but claim/evidence structures are absent.
4. **Tutor is not interactive (P7)** and assessment has no question-level records (P8).
5. Dead `medical_sources` table means P5's authority model is unwired.

## Change record

| Change | Phase | Status |
|---|---|---|
| Curriculum engine (schema extension + engine + CLI + dashboard + tests) | P2 | **DONE this session** — see `docs/CURRICULUM.md`; 15 new tests, 61/61 passing; V4 migration applied to the live DB with a verified pre-migration backup (SR queue 119 and 47 chunks preserved) |
| Textbook engine (P3), evidence graph (P4), source hierarchy (P5), recency mastery (P6), interactive tutor (P7), assessment records (P8), card links + leech rewrite (P9), PDF/OCR upgrade (P11), video renderer (P12), provider abstraction (P13), distributable bundle (P15) | P3–P15 | **NOT STARTED — planned in dependency order; each with its own WHY/WHAT/RISK/MIGRATION/TEST/ROLLBACK record at implementation time** |
