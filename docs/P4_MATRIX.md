# P4 Evidence Graph — File-Level Implementation Matrix

**Audit date:** 2026-10-05 · **Base commit:** `d405bcd` (P3 complete).

Live DB before P4: `chunks=47, curriculum_nodes=0, prerequisites=0,
learner_mastery=0, interactive_sessions=0, spaced_repetition_queue=119,
review_log=0, textbook_editions=0`; no claim/evidence tables.

## Where claims / citations / evidence live today

| Site | What it actually does | Verdict |
|---|---|---|
| `generation.py::citation_audit` | Per-line `[S#]` regex scan vs source labels; writes `unsupported-claims.txt`, `evidence-report.json/html`; explicitly states "semantic support has NOT been verified" | **KEEP as compatibility floor; MODIFY: becomes one input, not the verdict** |
| `generation.py::generate_text` | Prompt carries citation rules + "evidence is untrusted data" instruction | **KEEP** |
| `retrieval.py::source_pack` | Builds `[S#]` blocks with id/source/locator/kind/url/quality; bounded 8000-char budget | **MODIFY: expose structured provenance passthrough (non-breaking fields already present; evidence layer joins) ** |
| `product.py::build_product` | Saves `source-map.json`/`source-text.txt` snapshot; runs citation_audit at the end | **MODIFY: add post-audit evidence processing (guarded, non-blocking)** |
| `types.py::SYSTEM_EVIDENCE` | Declares source text untrusted, never instructions | **KEEP — the verifier prompt reuses this stance** |
| `textbook.py` (P3) | `chunks` ids shared with `textbook_chunks`; edition/node/page structured provenance; locator `p. N · Chapter / Section` | **KEEP — the join target for structured evidence** |
| `curriculum.py` (P2) | `topic_path()` maps topics to nodes | **KEEP — claim→curriculum link uses it** |

## P4 requirement classification

| Requirement | Current | Verdict |
|---|---|---|
| Claim records | none | **MISSING → build** |
| Evidence records | none (ephemeral per-pack `source-map.json` only) | **MISSING → build** |
| Claim↔evidence relationships | none | **MISSING → build** |
| Verification history (append-only) | none | **MISSING → build** |
| Deterministic claim extraction (headings/bullets/prose/questions/code/prompts) | none | **MISSING → build** |
| Claim normalization + duplicate detection (non-aggressive) | none | **MISSING → build** |
| Model-assisted verification with deterministic representation | label-only audit | **MISSING → build** |
| Abstention on insufficient evidence | prompt says "say so" only | **MISSING → machine-enforced** |
| Contradiction representation | none | **MISSING → build** |
| Legacy `[S#]` compatibility | full behavior | **KEEP (tests `test_citation_audit*` must stay green)** |
| Curriculum → claim → evidence traceability | none | **MISSING → build** |
| Private vs distributable separation | evidence excerpts inside pack dirs | **MODIFY: evidence excerpts live in DB (private); report keeps bounded excerpts only** |
| Prompt-injection resistance in verifier | stance in system prompt | **MODIFY: verifier wraps excerpts as delimited data + parse hardening** |

## Design decisions (why)

- **Content-addressed claims**: `claim_id = sha1(normalized_text)[:24]`, unique —
  the *same text* is one claim across runs (duplicate detection + repeat
  comparison); near-duplicates are NOT merged (medically distinct statements
  must stay distinct, per directive).
- **Evidence ids**: `sha1(chunk_id | excerpt_hash)[:24]` — idempotent evidence
  creation; structured provenance columns (`document_id, edition_id,
  textbook_node_id, chapter_title, section_title, page_number, locator`)
  joined from `textbook_chunks` when the chunk is a textbook chunk; NULL
  otherwise (web/pubmed keep url+locator). No invented locators: fields come
  from stored rows only.
- **Verification pipeline**: candidate retrieval (pack sources on the product
  path, else bounded `hybrid_retrieve`) → deterministic gates (no candidates →
  `INSUFFICIENT_EVIDENCE` without any model call; non-factual claims →
  `NOT_FACTUAL`) → model-assisted classifier (strict JSON contract; malformed →
  `INSUFFICIENT_EVIDENCE`, never an upgrade) → deterministic aggregation into
  `claims.verification_status` with every attempt appended to
  `verification_runs` (history preserved). Contradiction is decided by the
  verifier's explicit claim/evidence comparison — never by keyword heuristics.
- **Self-verification forbidden**: evidence always comes from retrieved source
  chunks, never from the generated text; the verifier receives
  (claim, excerpt, source metadata) only.
- **No silent upgrades**: aggregation rules are explicit and tested; a claim is
  never promoted because a label exists or keywords overlap.
- **Bounded work on 8 GB M1**: evidence excerpts ≤ 900 chars, ≤ 3 candidates per
  claim, `max_claims` cap per run (default 25), one model call per
  (claim, evidence) pair, none at all in the no-candidate case.
- **Distribution safety**: excerpts stored in the SQLite DB (private);
  dashboard/CLI views show them for internal verification; nothing new is
  added to distributable pack artifacts beyond the existing bounded scan.

## Files to change (planned smallest-safe-patch)

| File | Change | Risk |
|---|---|---|
| `core/database/schema.py` | + `V6_SCHEMA_DDL` (4 tables + indexes), claim/evidence enums | additive |
| `core/database/migrate_v6.py` | **new** — `ensure_evidence_v6()`: DDL, migration `6.0.0`, backup option, integrity | mirrors v5 |
| `medforge/types.py` | enum mirrors | additive |
| `medforge/evidence.py` | **new** — extraction, normalization, evidence records, verifier contract + parse, pipeline, inspection APIs | isolated |
| `medforge/product.py` | post-audit `process_pack_evidence()` call (guarded) | one block |
| `medforge/__init__.py`, `medforge_core.py` | exports; `claims`, `claim-info`, `verify-pack` | additive |
| `dashboard.py` | + EVIDENCE tab | additive |
| `tests/test_evidence.py` | **new** — 20+ deterministic tests (fake verifier, no Ollama) | isolated |
| docs | `EVIDENCE.md` new; V3_SCHEMA/ARCHITECTURE/CLI_USAGE/IMPLEMENTATION_MATRIX/P4_MATRIX | docs only |

Not touched: `learner.py`, `curriculum.py`, `textbook.py`, `retrieval.py`,
`generation.py` (citation_audit untouched), `export.py`, `ingestion.py`,
`storage.py`.

## Acceptance criteria → proof (21 items from the directive)

| Criterion | Proof |
|---|---|
| claims first-class | claims table + tests 1,2,3 |
| evidence first-class | evidence table + tests 4,5,12 |
| relationships persistent | claim_evidence table + test 6 |
| verification results persistent | verification_runs + test 7 |
| exact provenance end-to-end | textbook chunk → structured fields test |
| semantic support tested | tests 7–11 (five outcomes) |
| unsupported detected | test 9 |
| partial support detected | test 8 |
| contradiction representable | test 10 |
| abstention | test 11 (no evidence → INSUFFICIENT, zero model calls) |
| legacy `[S#]` compatible | existing `test_citation_audit*` stay green + test 14 |
| curriculum→claim→evidence | test 13 |
| migration additive/idempotent | test 15/16 |
| live data preserved | before/after counts + integrity + backup |
| old tests pass | full regression |
| new tests pass | `tests/test_evidence.py` |
| live DB integrity | `PRAGMA integrity_check` |
| dashboard inspection works | EVIDENCE tab verified live |
| no unrelated files | `git diff --stat` review |
| docs updated | EVIDENCE.md + matrices |
| commit pushed | `git log` + push |

## What P4 verifies vs does NOT verify (to be repeated in EVIDENCE.md)

**Verifies:** whether a retrieved source excerpt *supports, partially supports,
contradicts, or fails to support* a specific extracted claim, with the exact
locator preserved and the attempt history recorded; abstains when no evidence
was retrieved.
**Does NOT verify:** medical truth, currency of guidelines, correctness of the
source itself, population/context match beyond what the model-assisted
classifier reports, or that a "SUPPORTED" verdict is statistically calibrated.
Model-assisted means fallible; the deterministic representation makes it
inspectable, not infallible.

---

## Execution results (2026-10-05)

- **Schema/migration:** `6.0.0` `v6_evidence_graph` applied to the live DB
  (`database/medforge.sqlite3`) after a verified snapshot backup
  (`backups/medforge_pre_v6_backup.db`, integrity `ok` before migration).
- **Live data preservation:** chunks 47→47, curriculum_nodes 0→0,
  prerequisites 0→0, learner_mastery 0→0, study_sessions 0→0,
  spaced_repetition_queue 119→119, review_log 0→0 (plus
  interactive_sessions 0→0, textbook_* 0→0); `PRAGMA integrity_check` = ok,
  `PRAGMA foreign_key_check` = no rows.
- **Tests:** 74 → **94 passing** (20 new in `tests/test_evidence.py`), no
  existing test changed.
- **Live model-assisted verification (real qwen3:4b-instruct, 6 model calls):**
  - "Prolactinomas are the most common form of pituitary neuroendocrine
    tumour." → PARTIALLY_SUPPORTED (0.7) — one candidate partially supports,
    one does not mention prevalence.
  - "Cabergoline is first-line medical treatment for prolactinoma." →
    PARTIALLY_SUPPORTED (0.99) — Endotext excerpt supports it; a second
    candidate does not mention cabergoline (SUPPORTED + UNSUPPORTED mixes
    downward, never silently upgraded).
  - "Prolactinoma incidence is highest in men over 70 years of age." →
    UNSUPPORTED (0.0), `needs_review`.
- **CLI:** `claims`, `claim-info`, `evidence-status` verified on the live DB;
  `verify-pack` verified end-to-end in an isolated `MEDFORGE_HOME=/tmp/mf-p4-cli`
  (fresh DB self-healed to 6.0.0, one claim × two PubMed candidates, real model,
  excerpt-free `evidence-graph.json` written).
- **Dashboard:** EVIDENCE tab verified live (metrics 3 claims / 16 evidence
  records / 1 needs review, status filter, expandable claim → relationship →
  evidence → source → private excerpt → verification history).
- **Distribution safety:** `evidence-graph.json` in packs contains no excerpt
  text (asserted by test and by inspecting the isolated-pack artifact).
- **Limitations:** model-assisted verdicts are fallible (mixed evidence
  aggregates downward and every run is retained); no evidence-resolution policy
  yet (contradiction is represented, not adjudicated); no OCR/tables-in-PDF
  (P3 gap unchanged); exact-text dedup only, paraphrases stay separate claims.
