# MedForge Textbook Provenance (V5)

P3 of the master build directive: textbooks become first-class, traceable
evidence sources with stable edition identity and page-level provenance —
the foundation a later claim/evidence graph (P4) points at.

## Identity model

| Entity | Id | Stability |
| --- | --- | --- |
| Document (book family) | `sha1(title_key(title))[:16]` | Stable across editions |
| Edition (exact content) | `sha1(document_id \| sha256(file))[:16]` | Re-importing the same file is idempotent; changed content = **new edition row**, previous preserved |
| Chapter/Section/Subsection | `sha1("tbnode\|edition\|parent\|type\|title_key")[:16]` | Deterministic per edition |
| Page | `"{edition_id}:p{N}"` | One row per PDF page |
| Chunk | `sha256("tb\|edition\|page\|index")` | Deterministic → re-ingest inserts nothing |

Stored per edition: `content_hash`, `source_path`, `page_count`,
`extracted_pages`, `skipped_pages`, `chunk_count`, `ocr_status`,
`ocr_confidence` (reserved), `ingest_status` (`REGISTERED` → `EXTRACTED` /
`PARTIAL` / `FAILED`), `ingested_at`. Documents carry title, authors,
publisher, edition label, publication year, ISBN, subject, source type.

**Metadata resolution order:** explicit arguments → sidecar JSON → PDF
metadata (`/Title`, `/Author`, `/Subject`) → filename (underscores/dashes →
spaces). The sidecar convention is `<pdf name>.meta.json` next to the PDF:

```json
{"title": "Guyton and Hall Textbook of Medical Physiology",
 "authors": "Hall, J.E.", "edition": "14th ed.", "publisher": "Elsevier",
 "year": 2021, "isbn": "978-0-323-59712-5", "subject": "Physiology"}
```

`edition`/`year` are friendly aliases for `edition_label`/`publication_year`.
Nothing is ever invented: missing fields stay empty.

## Ingestion (page-wise, 8 GB-RAM friendly)

`ingest_textbook(pdf_or_edition_id, embed_text=True, force=False, **meta)`

1. Registers the file when new (idempotent by content hash).
2. Reads the PDF **page by page** — text is extracted once per page, chunked,
   flushed in small batches; the whole book is never held in memory.
3. Structure detection: PDF bookmarks first (top level → Chapter, level 2 →
   Section, deeper → Subsection); otherwise a **conservative** regex pass over
   the first 12 non-empty lines of each page (`Chapter N`, `N.M Title`,
   `N.M.K Title`, short lines only). No detectable structure → one implicit
   `Body` chapter.
4. Every chunk keeps: `edition_id`, `document_id`, `node_id` (chapter/section),
   `page_number`, `chunk_index`, `word_count`, `text_hash`, and a human
   `locator` (`p. 123 · Chapter / Section`).
5. Chunks are mirrored into the shared `chunks` + FTS store (and Chroma when
   `embed_text=True`) with `kind="textbook"` and
   `quality=SOURCE_PRIORITY["textbook"]=0.95`, so the existing retrieval
   pipeline (products, `ask`, study missions) cites them with zero retrieval
   changes. A textbook that is ingested here is **skipped** by the generic
   `ingest_pdfs()` path, so its content is never indexed twice.
6. Unreadable pages: `extraction_status='no_text'`, edition `ocr_status =
   'pending'`. Scanned books report exactly how many pages need OCR; no text
   is invented. OCR integration point: replace those pages later and re-ingest
   with `force=True`.

`ingest_status`: `EXTRACTED` (all pages read) · `PARTIAL` (some pages need
OCR) · `FAILED` (no readable text at all).

## Source priority

`SOURCE_PRIORITY` in `medforge/types.py` defines the ranking hints (not
accuracy scores): textbook `0.95`, pubmed `0.96`, guideline `0.92`,
course_pdf `0.90`, web (`domain trust`). Textbooks linked to the curriculum
are the intended first choice for curriculum learning; web content is never
silently treated as equivalent.

## Curriculum linking

```python
from medforge import link_curriculum_text, textbook_evidence_for_topic, \
                     suggest_curriculum_links

link_curriculum_text("GH axis", edition_id, node_id=section_id, link_type="primary")
textbook_evidence_for_topic("GH axis")   # linked chapters/sections + page previews
suggest_curriculum_links("GH axis")      # read-only title-overlap candidates
```

Links are idempotent (unique on curriculum node + edition + chapter/section;
`link_type` ∈ `primary`, `supporting`, `supplementary`). Discovery returns
document/edition metadata, page ranges, and bounded text previews — the shape
a P4 claim record will consume. Suggestions never write.

## CLI

```bash
medforge textbooks                     # registered documents + editions + counts
medforge textbook-add book.pdf|title|edition|authors|publisher|year|isbn|subject[|embed=0]
medforge textbook-info <edition_id>    # metadata + chapter/section tree + pages
medforge textbook-link "GH axis|<edition_id>[|<node_id>][|primary|supporting|supplementary]"
medforge textbook-evidence "GH axis"   # linked evidence with page previews
```

`embed=0` stores SQLite + FTS only (keyword-searchable) without calling
Ollama — fast registration, no embeddings. The dashboard **TEXTBOOKS** tab
offers the same surfaces (register/ingest, inspection, linking, discovery).

## Database safety

Migration `5.0.0` (`core/database/migrate_v5.py`) is purely additive: six new
tables (`textbook_documents`, `textbook_editions`, `textbook_nodes`,
`textbook_pages`, `textbook_chunks`, `curriculum_text_links`), idempotent,
self-healing on first use, with a verified snapshot backup
(`backups/medforge_pre_v5_backup.db`) on the explicit migration path.
Rollback: drop the six tables — no existing data is touched.

## Known limitations (honest list)

- Structure detection is heuristic (bookmarks or conservative heading
  regexes); books with decorative headings may fall back to `Body`.
- No OCR yet — scanned pages are reported and parked (`ocr_status='pending'`),
  never guessed.
- `pdf-index.json`-style bookmarks are not used for page labels (printed page
  numbers ≠ PDF page indexes); `page_number` is the PDF page.
- Document identity is title-derived: two different books sharing one title
  would share a document family (rare; rename the file/title if it matters).
- Embedding is per-batch during ingestion; re-embedding chunks ingested with
  `embed=0` requires a re-ingest with `force=True` and `embed_text=True`.
- Chunk text is stored in SQLite (`textbook_chunks`) *and* the shared chunks
  table — deliberate duplication for provenance vs. retrieval, bounded by
  chunk count.
