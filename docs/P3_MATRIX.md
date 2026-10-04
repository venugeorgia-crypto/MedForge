# P3 Textbook Provenance — File-Level Implementation Matrix

**Audit date:** 2026-10-05 · **Base commit:** `0fc09c7` (P2 complete) · live DB checked read-only.

Live DB before P3: `chunks=47, curriculum_nodes=0, spaced_repetition_queue=119,
schema_migrations={3.0.0, 4.0.0}`, **zero `textbook*` tables**.

## Requirement → current state → gap

| Requirement | Current implementation | Actual behavior | Verdict |
|---|---|---|---|
| 1. Document/edition entities | none — PDFs are anonymous `chunks` rows (`source=<relpath>`) | No title/authors/publisher/edition/ISBN/subject/source_type/checksum/status anywhere | **MISSING** |
| 2. Book→Chapter→Section→Page→Chunk | none — only word-window chunks | `ingest_pdfs()` chunks a page's text with 300/50 windows; hierarchy is lost | **MISSING** |
| 3. Stable locators | `locator="page N"`, deterministic chunk id `sha256("pdf|key|digest|page|idx")` | Page survives; document/edition/chapter/section/order do not | **PARTIAL** |
| 4. PDF ingestion (scanned detection) | page-wise extraction; prints "may need OCR" | No per-page status, no OCR field, no counts | **PARTIAL** |
| 5. Provenance representation | none beyond chunk row | No reusable structure a future claim (P4) can point to | **MISSING** |
| 6. Curriculum linking | P2 `curriculum_nodes` exists; no cross-links | Topic cannot discover textbook evidence | **MISSING** |
| 7. Versioning/dedup | pdf-index.json caches by file hash for re-index only | No entity identity; changed file replaces chunks in place | **MISSING** |
| 8. Source priority | implicit: course_pdf 0.90, pubmed 0.96, web = domain trust | No explicit hierarchy constant; web pages numerically outrank textbooks | **PARTIAL** |
| 9. Migration safety | V3/V4 precedent: backup, integrity, idempotent, self-heal | Pattern proven; V5 needed | **READY TO EXTEND** |
| 10–11. Tests/regression | 61 tests green | Nothing covers textbooks | **MISSING** |
| 12. Performance (8 GB M1) | page-wise `PdfReader` iteration; batch upserts of 4 | Pattern reusable; must avoid whole-book buffers | **READY** |
| 13. UI/CLI | 8 dashboard tabs; CLI commands | No inspection surface for textbooks | **MISSING** |
| 14. Documentation | ARCHITECTURE/V3_SCHEMA/CLI_USAGE/IMPLEMENTATION_MATRIX exist | No textbook docs | **MISSING** |

## Files to change (planned smallest-safe-patch)

| File | Change | Risk |
|---|---|---|
| `core/database/schema.py` | + `V5_SCHEMA_DDL` (5 tables), `TEXTBOOK_*` enums | additive only |
| `core/database/migrate_v5.py` | **new** — ensure_textbook_v5(): DDL, migration row `5.0.0`, backup option, integrity probe | mirrors migrate_v4 |
| `medforge/types.py` | + `SOURCE_PRIORITY`, textbook enum mirrors | additive |
| `medforge/storage.py` | `upsert_records(records, embed_text=True)` — flag skips Chroma/embedding; default preserves behavior | default-safe |
| `medforge/textbook.py` | **new** engine: register/ingest/structure/link/discovery | isolated |
| `medforge/ingestion.py` | `ingest_pdfs()` skips files registered as textbooks (defensive; no-op when tables absent) | one guard |
| `medforge/__init__.py` | exports | additive |
| `medforge_core.py` | CLI: `textbooks`, `textbook-add`, `textbook-info`, `textbook-link`, `textbook-evidence` | additive |
| `dashboard.py` | + TEXTBOOKS tab (list, structure, provenance, linking) | additive |
| `tests/test_textbook.py` | **new** ~13 tests | isolated fixture |
| docs | `TEXTBOOKS.md` new; V3_SCHEMA/ARCHITECTURE/CLI_USAGE/IMPLEMENTATION_MATRIX updates | docs only |

Not touched: `learner.py`, `curriculum.py`, `retrieval.py`, `generation.py`, `export.py`,
`product.py` (retrieval integration works through the existing `quality` filter —
textbook chunks carry 0.95, and `kind="textbook"` is not gated by any filter).

## Design decisions (why)

- **Identity:** `documents.id = sha1(title_key)[:16]` (stable across editions);
  `editions.id = sha1(doc_id | sha256(file bytes))[:16]` → same file re-import is
  idempotent, changed content creates a **new edition row** (never overwrites).
- **Hierarchy:** pypdf outline first, conservative regex heading detection
  (`Chapter N`, `N.M` / `N.M.K` numbered headings, short lines) second, implicit
  `Body` chapter last. Page→node mapping stored on `textbook_pages`.
- **Locators:** chunk rows carry `edition_id`, `node_id`, `page_number`,
  `chunk_index`, and a human `locator` string (`Ch. 5 § 5.2 — p. 123`) reused for
  citations. Chunk ids are deterministic → re-ingest is `INSERT OR IGNORE`.
- **Retrieval integration:** textbook chunks are upserted into the shared
  `chunks` + FTS (+ Chroma when embedding enabled) with `kind="textbook"` and
  `quality=SOURCE_PRIORITY["textbook"]=0.95`, so the existing retrieval pipeline
  finds them with zero changes to `retrieval.py`.
- **Curriculum links:** `curriculum_text_links` with a COALESCE-unique index
  (lesson from P2: SQLite treats NULLs as distinct). Discovery via
  `textbook_evidence_for_topic()`; suggestions are read-only.
- **OCR:** pages with no extractable text are recorded `extraction_status='no_text'`,
  edition `ocr_status='pending'`; no text is invented. Architecture ready for a
  later OCR provider.
- **Performance:** stream page-by-page, chunk per page, upsert per page batch;
  never hold the whole book in memory.

## Acceptance criteria → proof

| Criterion | Proof |
|---|---|
| metadata first-class | test: register → documents+editions rows complete |
| edition identity stable | test: same file twice → same edition id; edited file → new edition, old preserved |
| page/chapter/section provenance | test: nodes detected, pages mapped, chunks carry edition+node+page |
| duplicate import idempotent | test: second ingest adds zero rows/chunks |
| curriculum linkage works | test: link → `textbook_evidence_for_topic` returns it; suggest read-only |
| migration preserves data | test: V5 on populated DB keeps rows; idempotent rerun |
| regression green | full suite 61 pre-existing tests |
| live DB integrity | backup + `PRAGMA integrity_check` = ok; counts before/after |
| diff planned only | `git diff --stat` review |
| commit pushed | `git log` + `git push` |

**Explicitly out of scope (P4+):** semantic claim verification, interactive tutor,
video rendering, learner-model changes.

---

## Execution results (post-implementation)

| Criterion | Result |
|---|---|
| metadata first-class | ✅ PASS — documents+editions rows with all fields; tests green |
| edition identity stable | ✅ PASS — same file → same edition id; changed file → new edition, old row preserved |
| page/chapter/section provenance | ✅ PASS — nodes + page mapping + chunk locators verified end-to-end |
| duplicate import idempotent | ✅ PASS — re-ingest short-circuits, zero new rows/chunks |
| curriculum linkage works | ✅ PASS — link → discovery with previews; idempotent; unlink works; suggestions read-only |
| migration preserves data | ✅ PASS — V5 applied to live DB; counts before = after (chunks 47, SR 119); integrity ok; backup written |
| regression green | ✅ PASS — 74/74 (61 pre-existing + 13 new) |
| live DB integrity | ✅ PASS — `PRAGMA integrity_check` ok on DB and backup |
| diff planned only | ✅ PASS — see git diff review in the commit |
| commit pushed | ✅ PASS — P3 commit pushed to `origin/main` |

**Bugs the loop caught and fixed during implementation:**
1. Whole-file `read_bytes()` magic check → streamed 5-byte read (8 GB RAM rule).
2. `utils.chunks()`'s 20-word minimum silently dropped short textbook pages →
   textbook-specific `_page_chunks()` (5-word floor) with rationale.
3. Sidecar `"edition"`/`"year"` keys were dropped → friendly aliases mapped.
4. Test expectation vs. correct behavior for no-text page ranges (blank page
   inherits the active chapter's span) — expectation corrected, behavior kept.
5. `SOURCE_PRIORITY`/`CURRICULUM_TEXT_LINK_TYPES` missing from the CLI import
   list → added.

**Remaining P3 limitations:** no OCR engine (detection only), no table/figure
modeling, heuristic structure detection, title-derived document identity,
re-embedding requires `force=True` after `embed=0` ingestion. Full list in
`docs/TEXTBOOKS.md`.
