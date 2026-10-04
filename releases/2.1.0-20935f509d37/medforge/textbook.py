"""MedForge P3 textbook provenance engine.

First-class documents/editions with Book → Chapter → Section → Page → Chunk
provenance on top of the existing pipeline:

- deterministic ids: document = sha1(title_key); edition = sha1(doc|sha256(file))
  so re-importing the same file is idempotent and a changed edition creates a
  new row (the previous edition is never overwritten)
- page-wise streaming extraction (8 GB-RAM friendly); no whole-book buffers
- textbook chunks are mirrored into the shared `chunks`+FTS store (and Chroma
  when embedding is enabled) with kind="textbook" and the SOURCE_PRIORITY
  weight, so existing retrieval sees them with zero retrieval changes
- scanned/no-text pages are recorded as `no_text` with ocr_status='pending';
  no text is ever invented (OCR integration point left explicit)
- curriculum_text_links connect curriculum nodes (P2) to textbook evidence

Depends on medforge.types/utils/storage + medforge.curriculum (title_key).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import medforge.types as T
from medforge.utils import mkdirs, utcnow
from medforge.curriculum import title_key
from medforge.storage import upsert_records

__all__ = [
    "ensure_textbook_tables", "resolve_metadata", "register_textbook",
    "ingest_textbook", "list_textbooks", "book_structure",
    "link_curriculum_text", "unlink_curriculum_text",
    "textbook_evidence_for_topic", "suggest_curriculum_links",
]

MAX_HEADING_SCAN_LINES = 12   # conservative: headings live at page starts
MAX_HEADING_LEN = 100
EMBED_FLUSH_EVERY = 32        # bound RAM while batching embeddings
# Textbook chunks keep short pages: utils.chunks() drops pieces under 20 words
# (a retrieval-noise rule), but in structural ingestion a 10-word page still
# carries real provenance and must not vanish. 5 words filters only fragments.
MIN_PAGE_CHUNK_WORDS = 5


def _page_chunks(text: str, max_words: int = 300, overlap: int = 50,
                 min_words: int = MIN_PAGE_CHUNK_WORDS) -> List[str]:
    """Overlapping word windows for one page; keeps short but real content."""
    words = re.sub(r"\s+", " ", text or "").strip().split()
    if not words:
        return []
    step = max(1, max_words - overlap)
    out: List[str] = []
    for i in range(0, len(words), step):
        piece = " ".join(words[i:i + max_words]).strip()
        if len(piece.split()) >= min_words:
            out.append(piece)
    return out


# ─── Schema self-healing ───


def _core_import():
    """Import core.database.migrate_v5, walking candidate bases like P2."""
    here = Path(__file__).resolve()
    candidates = [str(T.BASE)] + [str(p) for p in here.parents]
    for base_str in candidates:
        if base_str not in sys.path:
            sys.path.insert(0, base_str)
        try:
            from core.database.migrate_v5 import ensure_textbook_v5

            return ensure_textbook_v5
        except ImportError:
            continue
    raise ImportError(
        "core.database.migrate_v5 not importable from: " + ", ".join(candidates)
    )


def ensure_textbook_tables() -> Dict[str, Any]:
    """Bring the active META_DB to the V5 textbook schema (idempotent)."""
    mkdirs()
    ensure_v5 = _core_import()
    return ensure_v5(T.META_DB)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    return con


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return None if row is None else {k: row[k] for k in row.keys()}


def _sha256_file(path: Path) -> str:
    """Streamed SHA-256 (1 MB blocks) — never loads the file into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def document_id_for(title: str) -> str:
    """Stable document family id from the normalized title."""
    return hashlib.sha1(title_key(title).encode("utf-8")).hexdigest()[:16]


def edition_id_for(document_id: str, content_hash: str) -> str:
    """Stable edition id: same file -> same id; changed content -> new id."""
    return hashlib.sha1(f"{document_id}|{content_hash}".encode("utf-8")).hexdigest()[:16]


# ─── Metadata resolution ───


_SIDECAR_SUFFIX = ".meta.json"


def resolve_metadata(pdf_path: Path, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve book metadata: explicit overrides > sidecar JSON > PDF metadata > filename.

    The sidecar convention is `<pdf name>.meta.json` next to the PDF:
        {"title": "...", "authors": "...", "edition": "3rd ed.", ...}
    Every field is optional; missing fields stay empty (documented limitation —
    no inventing metadata).
    """
    overrides = {k: v for k, v in (overrides or {}).items() if v not in (None, "")}
    meta: Dict[str, Any] = {
        "title": "", "authors": "", "publisher": "", "edition_label": "",
        "publication_year": None, "isbn": "", "subject": "",
        "source_type": "textbook",
    }

    # 1. PDF metadata (cheap header read — not a full parse)
    try:
        from pypdf import PdfReader

        info = PdfReader(str(pdf_path)).metadata or {}
        meta["title"] = (info.get("/Title") or "").strip()
        meta["authors"] = (info.get("/Author") or "").strip()
        subject = (info.get("/Subject") or "").strip()
        if subject:
            meta["subject"] = subject
    except Exception:
        pass

    # 2. Sidecar JSON (friendly aliases tolerated)
    sidecar = pdf_path.with_name(pdf_path.stem + _SIDECAR_SUFFIX)
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            aliases = {"edition": "edition_label", "year": "publication_year"}
            if isinstance(data, dict):
                for key, value in data.items():
                    key = aliases.get(key, key)
                    if key in meta and value not in (None, ""):
                        meta[key] = value
        except (OSError, ValueError):
            pass

    # 3. Explicit overrides
    meta.update(overrides)

    # 4. Filename fallback for the title
    if not meta["title"]:
        meta["title"] = re.sub(r"[_\-]+", " ", pdf_path.stem).strip()
    try:
        if meta["publication_year"] is not None:
            meta["publication_year"] = int(meta["publication_year"])
    except (TypeError, ValueError):
        meta["publication_year"] = None
    if meta["source_type"] not in T.TEXTBOOK_SOURCE_TYPES:
        raise ValueError(
            f"source_type must be one of {T.TEXTBOOK_SOURCE_TYPES},"
            f" got {meta['source_type']!r}."
        )
    return meta


# ─── Registration ───


def register_textbook(
    pdf_path: Path | str,
    title: Optional[str] = None,
    authors: Optional[str] = None,
    publisher: Optional[str] = None,
    edition: Optional[str] = None,
    publication_year: Optional[int] = None,
    isbn: Optional[str] = None,
    subject: Optional[str] = None,
    source_type: str = "textbook",
) -> Dict[str, Any]:
    """Register a PDF as a textbook edition (idempotent by content hash).

    Returns the document/edition rows plus `created` (False when this exact
    file was already registered). A changed file (new edition) creates a new
    edition row and leaves the previous one intact.
    """
    path = Path(pdf_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Textbook PDF not found: {path}")
    with open(path, "rb") as f:
        magic = f.read(5)
    if path.suffix.lower() != ".pdf" or magic != b"%PDF-":
        raise ValueError(f"Not a PDF file: {path}")

    overrides = {
        "title": title, "authors": authors, "publisher": publisher,
        "edition_label": edition, "publication_year": publication_year,
        "isbn": isbn, "subject": subject, "source_type": source_type,
    }
    meta = resolve_metadata(path, overrides)
    content_hash = _sha256_file(path)
    doc_id = document_id_for(meta["title"])
    ed_id = edition_id_for(doc_id, content_hash)
    now = utcnow()

    ensure_textbook_tables()
    con = _connect()
    try:
        existing = con.execute(
            "SELECT id FROM textbook_editions WHERE content_hash=?", (content_hash,)
        ).fetchone()
        document = _row_dict(con.execute(
            "SELECT * FROM textbook_documents WHERE id=?", (doc_id,)
        ).fetchone())
        if document is None:
            con.execute(
                """INSERT INTO textbook_documents
                   (id, title, authors, publisher, edition_label, publication_year,
                    isbn, subject, source_type, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (doc_id, meta["title"], meta["authors"], meta["publisher"],
                 meta["edition_label"], meta["publication_year"], meta["isbn"],
                 meta["subject"], meta["source_type"], now, now),
            )
        else:
            con.execute(
                "UPDATE textbook_documents SET updated_at=? WHERE id=?", (now, doc_id)
            )
        created = existing is None
        if created:
            con.execute(
                """INSERT INTO textbook_editions
                   (id, document_id, content_hash, source_path, created_at, updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (ed_id, doc_id, content_hash, str(path), now, now),
            )
        con.commit()
        edition = _row_dict(con.execute(
            "SELECT * FROM textbook_editions WHERE content_hash=?", (content_hash,)
        ).fetchone())
        document = _row_dict(con.execute(
            "SELECT * FROM textbook_documents WHERE id=?", (doc_id,)
        ).fetchone())
    finally:
        con.close()
    return {
        "created": created,
        "content_hash": content_hash,
        "document": document,
        "edition": edition,
        "source_path": str(path),
    }


def list_textbooks() -> List[Dict[str, Any]]:
    """Registered documents with their editions and summary counts."""
    ensure_textbook_tables()
    con = _connect()
    try:
        docs = [_row_dict(r) for r in con.execute(
            "SELECT * FROM textbook_documents ORDER BY title COLLATE NOCASE"
        )]
        for doc in docs:
            doc["editions"] = [_row_dict(r) for r in con.execute(
                "SELECT * FROM textbook_editions WHERE document_id=?"
                " ORDER BY created_at, id", (doc["id"],),
            )]
            for ed in doc["editions"]:
                ed["chapters"] = con.execute(
                    "SELECT count(*) FROM textbook_nodes WHERE edition_id=?"
                    " AND node_type='Chapter'", (ed["id"],),
                ).fetchone()[0]
                ed["links"] = con.execute(
                    "SELECT count(*) FROM curriculum_text_links WHERE edition_id=?",
                    (ed["id"],),
                ).fetchone()[0]
    finally:
        con.close()
    return docs


# ─── Structure detection ───

_CHAPTER_RE = re.compile(
    r"^\s*(?:chapter|ch\.?)\s+([0-9]{1,2}|[IVXLC]{1,6})\b[:.\s\-]*(.*)$",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(r"^\s*(\d{1,2}\.\d{1,2})\s+([A-Z].{2,80})$")
_SUBSECTION_RE = re.compile(r"^\s*(\d{1,2}\.\d{1,2}\.\d{1,2})\s+([A-Z].{2,80})$")


def _node_id(edition_id: str, parent_id: Optional[str], node_type: str, title: str) -> str:
    raw = f"tbnode|{edition_id}|{parent_id or 'ROOT'}|{node_type}|{title_key(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _outline_events(reader) -> Dict[int, List[Tuple[int, str]]]:
    """PDF bookmarks → {page_number(1-based): [(level, title), ...]} (when present)."""
    events: Dict[int, List[Tuple[int, str]]] = {}
    try:
        outline = reader.outline or []
    except Exception:
        return events
    if not outline:
        return events

    def walk(items, level: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            t = (getattr(item, "title", None) or "").strip()
            if not t:
                continue
            try:
                page_no = int(reader.get_destination_page_number(item)) + 1
            except Exception:
                continue
            if page_no < 1:
                continue
            events.setdefault(page_no, []).append((level, t[:MAX_HEADING_LEN]))

    walk(outline, 1)
    return events


def _page_headings(text: str) -> List[Tuple[str, str, str]]:
    """Conservative heading detection: (node_type, code, title) per page.

    Scans only the first MAX_HEADING_SCAN_LINES non-empty lines (headings live
    at page starts) and accepts short, well-formed lines only: `Chapter N`,
    `N.M Title`, `N.M.K Title`. Returns [] when nothing matches — no guessing.
    """
    found: List[Tuple[str, str, str]] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines[:MAX_HEADING_SCAN_LINES]:
        if len(line) > MAX_HEADING_LEN:
            continue
        m = _SUBSECTION_RE.match(line)
        if m:
            found.append(("Subsection", m.group(1), m.group(2).strip()))
            continue
        m = _SECTION_RE.match(line)
        if m:
            found.append(("Section", m.group(1), m.group(2).strip()))
            continue
        m = _CHAPTER_RE.match(line)
        if m:
            title = (m.group(2) or "").strip() or f"Chapter {m.group(1)}"
            found.append(("Chapter", m.group(1), title[:MAX_HEADING_LEN]))
    return found


def _ensure_node(
    con: sqlite3.Connection,
    edition_id: str,
    parent_id: Optional[str],
    node_type: str,
    title: str,
    code: str = "",
) -> Dict[str, Any]:
    """Deterministic-id node, idempotent (INSERT OR IGNORE + read back)."""
    nid = _node_id(edition_id, parent_id, node_type, title)
    row = con.execute("SELECT * FROM textbook_nodes WHERE id=?", (nid,)).fetchone()
    if row is None:
        order = con.execute(
            "SELECT max(order_index) FROM textbook_nodes WHERE edition_id=?"
            " AND parent_id IS ?", (edition_id, parent_id),
        ).fetchone()[0] or 0
        now = utcnow()
        con.execute(
            """INSERT OR IGNORE INTO textbook_nodes
               (id, edition_id, parent_id, node_type, code, title, order_index,
                created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (nid, edition_id, parent_id, node_type, code, title, order + 1, now, now),
        )
        con.commit()
        row = con.execute("SELECT * FROM textbook_nodes WHERE id=?", (nid,)).fetchone()
    return _row_dict(row) or {}


# ─── Ingestion (page-wise streaming extraction) ───


def registered_source_paths() -> set:
    """Resolved paths of *ingested* textbook editions (chunk_count > 0).

    Used by the generic PDF ingester to avoid double-indexing the same file
    under two id schemes. Returns an empty set when the V5 tables do not exist
    yet or the database is unreachable — always safe to call.
    """
    out: set = set()
    try:
        con = sqlite3.connect(T.META_DB)
        try:
            has = con.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
                " AND name='textbook_editions'"
            ).fetchone()[0]
            if not has:
                return out
            rows = con.execute(
                "SELECT source_path FROM textbook_editions"
                " WHERE source_path != '' AND chunk_count > 0"
            ).fetchall()
        finally:
            con.close()
        for (sp,) in rows:
            try:
                out.add(str(Path(sp).expanduser().resolve()))
            except OSError:
                continue
    except sqlite3.Error:
        pass
    return out


def _edition_summary(con: sqlite3.Connection, edition: Dict[str, Any]) -> Dict[str, Any]:
    """Counts derived from the database for a fixed edition row."""
    counts = {r["node_type"]: r["n"] for r in con.execute(
        "SELECT node_type, count(*) AS n FROM textbook_nodes WHERE edition_id=?"
        " GROUP BY node_type", (edition["id"],),
    )}
    chunks_total = con.execute(
        "SELECT count(*) FROM textbook_chunks WHERE edition_id=?", (edition["id"],),
    ).fetchone()[0]
    return {
        "edition_id": edition["id"],
        "document_id": edition["document_id"],
        "pages": edition["page_count"],
        "pages_extracted": edition["extracted_pages"],
        "pages_no_text": edition["skipped_pages"],
        "chapters": counts.get("Chapter", 0),
        "sections": counts.get("Section", 0),
        "subsections": counts.get("Subsection", 0),
        "chunks_total": chunks_total,
        "ocr_status": edition["ocr_status"],
        "ingest_status": edition["ingest_status"],
        "ingested_at": edition["ingested_at"],
    }


def ingest_textbook(
    pdf_or_edition_id: Path | str,
    embed_text: bool = True,
    force: bool = False,
    **overrides: Any,
) -> Dict[str, Any]:
    """Extract structure + provenance for a textbook PDF (idempotent).

    Accepts a PDF path (registers it first when new) or an existing edition id.
    A page whose text cannot be extracted is recorded as `no_text` with
    ocr_status='pending' — no text is invented. Re-running on an already
    ingested edition short-circuits unless force=True. When embed_text=False,
    chunks are written to SQLite + FTS only (keyword-searchable); the default
    embeds so vector retrieval finds them too.
    """
    ensure_textbook_tables()
    value = str(pdf_or_edition_id)
    con = _connect()
    try:
        edition = _row_dict(con.execute(
            "SELECT * FROM textbook_editions WHERE id=?", (value,)
        ).fetchone())
    finally:
        con.close()

    registration: Optional[Dict[str, Any]] = None
    if edition is None:
        registration = register_textbook(pdf_or_edition_id, **overrides)
        edition = registration["edition"]

    con = _connect()
    try:
        document = _row_dict(con.execute(
            "SELECT * FROM textbook_documents WHERE id=?", (edition["document_id"],)
        ).fetchone()) or {}
        if (
            not force
            and edition["ingest_status"] in ("EXTRACTED", "PARTIAL")
            and int(edition["chunk_count"] or 0) > 0
        ):
            summary = _edition_summary(con, edition)
            summary.update({
                "title": document.get("title", ""),
                "edition_label": document.get("edition_label", ""),
                "skipped": True,
                "created": bool(registration and registration.get("created")),
                "embedded": bool(embed_text),
            })
            return summary
    finally:
        con.close()

    source_path = Path(edition["source_path"] or "")
    if not source_path.is_file():
        raise FileNotFoundError(
            f"Registered textbook source is missing: {source_path}; re-register the file."
        )

    from pypdf import PdfReader

    reader = PdfReader(str(source_path))
    page_count = len(reader.pages)
    outline_events = _outline_events(reader)
    use_outline = bool(outline_events)

    edition_id = edition["id"]
    document_id = edition["document_id"]
    base_source = document.get("title", "") + (
        f" ({document.get('edition_label')})" if document.get("edition_label") else ""
    )
    quality = T.SOURCE_PRIORITY["textbook"]

    chapter = section = subsection = None
    pages_ok = pages_no_text = 0
    chunks_created = 0
    buffer: List[Dict[str, Any]] = []

    con = _connect()
    try:
        def current_node() -> Optional[Dict[str, Any]]:
            return subsection or section or chapter

        def assign_chapter(title: str, code: str) -> None:
            nonlocal chapter, section, subsection
            chapter = _ensure_node(con, edition_id, None, "Chapter", title, code)
            section = subsection = None

        def assign_section(title: str, code: str) -> None:
            nonlocal section, subsection
            parent = chapter
            section = _ensure_node(
                con, edition_id, parent["id"] if parent else None, "Section", title, code,
            )
            subsection = None

        def assign_subsection(title: str, code: str) -> None:
            nonlocal subsection
            parent = section or chapter
            subsection = _ensure_node(
                con, edition_id, parent["id"] if parent else None, "Subsection", title, code,
            )

        for page_no, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
                page_status = "ok" if text.strip() else "no_text"
            except Exception:
                text, page_status = "", "error"

            if page_status == "ok":
                if use_outline:
                    for level, htitle in outline_events.get(page_no, []):
                        if level <= 1:
                            assign_chapter(htitle, "")
                        elif level == 2:
                            assign_section(htitle, "")
                        else:
                            assign_subsection(htitle, "")
                else:
                    for ntype, code, htitle in _page_headings(text):
                        if ntype == "Chapter":
                            assign_chapter(htitle, code)
                        elif ntype == "Section":
                            assign_section(htitle, code)
                        else:
                            assign_subsection(htitle, code)

            node = current_node()
            node_id = node["id"] if node else None
            now = utcnow()
            page_id = f"{edition_id}:p{page_no}"
            if page_status == "ok":
                pages_ok += 1
                con.execute(
                    """INSERT OR REPLACE INTO textbook_pages
                       (id, edition_id, page_number, node_id, text_chars,
                        extraction_status, ocr_status, created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (page_id, edition_id, page_no, node_id, len(text), "ok",
                     "not_needed", now),
                )
                loc_prefix = " / ".join(
                    x["title"][:60] for x in (chapter, section, subsection) if x
                )
                locator = f"p. {page_no}" + (f" · {loc_prefix}" if loc_prefix else "")
                for idx, piece in enumerate(_page_chunks(text), 1):
                    cid = hashlib.sha256(
                        f"tb|{edition_id}|{page_no}|{idx}".encode()
                    ).hexdigest()
                    cur = con.execute(
                        """INSERT OR IGNORE INTO textbook_chunks
                           (id, edition_id, document_id, node_id, page_number,
                            chunk_index, text, text_hash, word_count, locator, created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (cid, edition_id, document_id, node_id, page_no, idx, piece,
                         hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                         len(piece.split()), locator, now),
                    )
                    chunks_created += cur.rowcount
                    buffer.append({
                        "id": cid,
                        "text": piece,
                        "metadata": {
                            "source": base_source, "locator": locator,
                            "kind": "textbook", "url": "", "quality": quality,
                        },
                    })
                con.commit()
                if len(buffer) >= EMBED_FLUSH_EVERY:
                    upsert_records(buffer, embed_text=embed_text)
                    buffer = []
            else:
                pages_no_text += 1
                con.execute(
                    """INSERT OR REPLACE INTO textbook_pages
                       (id, edition_id, page_number, node_id, text_chars,
                        extraction_status, ocr_status, created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (page_id, edition_id, page_no, node_id, 0, page_status,
                     "pending", now),
                )
                con.commit()

        if buffer:
            upsert_records(buffer, embed_text=embed_text)
            buffer = []

        # Implicit chapter when the PDF has no detectable structure at all.
        if chapter is None and pages_ok:
            chapter = _ensure_node(con, edition_id, None, "Chapter", "Body", "")
            con.execute(
                "UPDATE textbook_pages SET node_id=?"
                " WHERE edition_id=? AND node_id IS NULL",
                (chapter["id"], edition_id),
            )
            con.commit()

        # Node page ranges include descendant pages (chapters span sections).
        nodes = [_row_dict(r) for r in con.execute(
            "SELECT * FROM textbook_nodes WHERE edition_id=?", (edition_id,)
        )]
        direct_pages: Dict[str, List[int]] = {}
        for r in con.execute(
            "SELECT node_id, page_number FROM textbook_pages"
            " WHERE edition_id=? AND node_id IS NOT NULL", (edition_id,),
        ):
            direct_pages.setdefault(r["node_id"], []).append(r["page_number"])
        children: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for n in nodes:
            children.setdefault(n["parent_id"], []).append(n)

        def pages_below(node_id: str) -> List[int]:
            acc = list(direct_pages.get(node_id, []))
            for child in children.get(node_id, []):
                acc.extend(pages_below(child["id"]))
            return acc

        for n in nodes:
            ps = pages_below(n["id"])
            if ps:
                con.execute(
                    "UPDATE textbook_nodes SET start_page=?, end_page=?, updated_at=?"
                    " WHERE id=?",
                    (min(ps), max(ps), utcnow(), n["id"]),
                )
        con.commit()

        chunk_total = con.execute(
            "SELECT count(*) FROM textbook_chunks WHERE edition_id=?", (edition_id,),
        ).fetchone()[0]
        ocr_status = "pending" if pages_no_text else "not_needed"
        if pages_ok == 0:
            ingest_status = "FAILED"
        elif pages_no_text:
            ingest_status = "PARTIAL"
        else:
            ingest_status = "EXTRACTED"
        now = utcnow()
        con.execute(
            """UPDATE textbook_editions SET page_count=?, extracted_pages=?,
               skipped_pages=?, chunk_count=?, ocr_status=?, ingest_status=?,
               ingested_at=?, updated_at=? WHERE id=?""",
            (page_count, pages_ok, pages_no_text, chunk_total, ocr_status,
             ingest_status, now, now, edition_id),
        )
        con.commit()
        updated = _row_dict(con.execute(
            "SELECT * FROM textbook_editions WHERE id=?", (edition_id,)
        ).fetchone()) or edition
        summary = _edition_summary(con, updated)
    finally:
        con.close()

    summary.update({
        "title": document.get("title", ""),
        "edition_label": document.get("edition_label", ""),
        "source_path": str(source_path),
        "chunks_created": chunks_created,
        "skipped": False,
        "created": bool(registration and registration.get("created")),
        "embedded": bool(embed_text),
    })
    return summary


# ─── Inspection ───


def book_structure(edition_id: str, page_limit: int = 60) -> Dict[str, Any]:
    """Edition metadata + chapter/section tree + bounded page list (for UI/CLI)."""
    ensure_textbook_tables()
    con = _connect()
    try:
        edition = _row_dict(con.execute(
            "SELECT * FROM textbook_editions WHERE id=?", (edition_id,)
        ).fetchone())
        if edition is None:
            raise ValueError(f"No textbook edition with id {edition_id!r}.")
        document = _row_dict(con.execute(
            "SELECT * FROM textbook_documents WHERE id=?", (edition["document_id"],)
        ).fetchone()) or {}
        nodes = [_row_dict(r) for r in con.execute(
            "SELECT * FROM textbook_nodes WHERE edition_id=?"
            " ORDER BY order_index, title COLLATE NOCASE", (edition_id,),
        )]
        chunk_counts = {r[0]: r[1] for r in con.execute(
            "SELECT node_id, count(*) FROM textbook_chunks"
            " WHERE edition_id=? AND node_id IS NOT NULL GROUP BY node_id", (edition_id,),
        )}
        pages_total = con.execute(
            "SELECT count(*) FROM textbook_pages WHERE edition_id=?", (edition_id,),
        ).fetchone()[0]
        pages = [_row_dict(r) for r in con.execute(
            "SELECT page_number, node_id, text_chars, extraction_status, ocr_status"
            " FROM textbook_pages WHERE edition_id=? ORDER BY page_number LIMIT ?",
            (edition_id, int(page_limit)),
        )]
    finally:
        con.close()

    children: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for n in nodes:
        n = dict(n)
        n["chunks"] = chunk_counts.get(n["id"], 0)
        children.setdefault(n["parent_id"], []).append(n)

    def build(node: Dict[str, Any]) -> Dict[str, Any]:
        return {**node, "children": [build(c) for c in children.get(node["id"], [])]}

    roots = [n for n in nodes if n["parent_id"] is None]
    return {
        "document": document,
        "edition": edition,
        "nodes": [build(r) for r in roots],
        "pages": pages,
        "pages_total": pages_total,
        "pages_truncated": pages_total > len(pages),
    }


# ─── Curriculum linking ───


def _resolve_curriculum_node(value: str, con: sqlite3.Connection) -> Dict[str, Any]:
    """Resolve a curriculum node by exact id or by normalized title."""
    value = (value or "").strip()
    if not value:
        raise ValueError("A curriculum topic or node id is required.")
    row = con.execute("SELECT * FROM curriculum_nodes WHERE id=?", (value,)).fetchone()
    if row is not None:
        return _row_dict(row)
    want = title_key(value)
    fallback: Optional[Dict[str, Any]] = None
    for r in con.execute("SELECT * FROM curriculum_nodes"):
        if title_key(r["title"]) == want:
            d = _row_dict(r)
            if r["node_type"] in ("Topic", "Subtopic"):
                return d
            fallback = fallback or d
    if fallback is None:
        raise ValueError(
            f"No curriculum node matches {value!r}; import a syllabus first"
            " (medforge syllabus <file>)."
        )
    return fallback


def link_curriculum_text(
    topic_or_node: str,
    edition_id: str,
    node_id: Optional[str] = None,
    page_start: Optional[int] = None,
    page_end: Optional[int] = None,
    link_type: str = "primary",
    note: str = "",
) -> Dict[str, Any]:
    """Link curriculum node → textbook evidence (idempotent).

    `node_id` may be a chapter/section/subsection; without it the link covers
    the whole edition (optionally narrowed by page_start/page_end).
    """
    if link_type not in T.CURRICULUM_TEXT_LINK_TYPES:
        raise ValueError(
            f"link_type must be one of {T.CURRICULUM_TEXT_LINK_TYPES},"
            f" got {link_type!r}."
        )
    ensure_textbook_tables()
    con = _connect()
    try:
        curriculum = _resolve_curriculum_node(topic_or_node, con)
        edition = con.execute(
            "SELECT id FROM textbook_editions WHERE id=?", (edition_id,)
        ).fetchone()
        if edition is None:
            raise ValueError(f"No textbook edition with id {edition_id!r}.")
        if node_id:
            tnode = con.execute(
                "SELECT id FROM textbook_nodes WHERE id=? AND edition_id=?",
                (node_id, edition_id),
            ).fetchone()
            if tnode is None:
                raise ValueError(
                    f"Textbook node {node_id!r} does not belong to edition {edition_id!r}."
                )
        cur = con.execute(
            """INSERT OR IGNORE INTO curriculum_text_links
               (curriculum_node_id, edition_id, node_id, page_start, page_end,
                link_type, note, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (curriculum["id"], edition_id, node_id, page_start, page_end,
             link_type, note, utcnow()),
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM curriculum_text_links WHERE curriculum_node_id=?"
            " AND edition_id=? AND COALESCE(node_id,'')=COALESCE(?,'')",
            (curriculum["id"], edition_id, node_id),
        ).fetchone()
        return {
            "created": bool(cur.rowcount > 0),
            "link": _row_dict(row),
            "curriculum_node": curriculum,
        }
    finally:
        con.close()


def unlink_curriculum_text(link_id: int) -> int:
    """Delete a curriculum↔textbook link; returns rows removed."""
    ensure_textbook_tables()
    con = _connect()
    try:
        cur = con.execute(
            "DELETE FROM curriculum_text_links WHERE id=?", (int(link_id),)
        )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def textbook_evidence_for_topic(
    topic_or_node_id: str,
    preview_chars: int = 280,
) -> Dict[str, Any]:
    """Curriculum node → its linked textbook evidence with bounded previews."""
    ensure_textbook_tables()
    con = _connect()
    try:
        curriculum = _resolve_curriculum_node(topic_or_node_id, con)
        rows = [_row_dict(r) for r in con.execute(
            """SELECT l.id AS link_id, l.edition_id, l.node_id, l.page_start,
                      l.page_end, l.link_type, l.note,
                      tn.node_type AS textbook_node_type,
                      tn.title AS textbook_node_title,
                      tn.start_page, tn.end_page,
                      d.title AS document_title, d.authors,
                      d.edition_label, d.publisher, d.publication_year, d.isbn,
                      e.ingest_status, e.ocr_status
               FROM curriculum_text_links l
               JOIN textbook_editions e ON e.id = l.edition_id
               JOIN textbook_documents d ON d.id = e.document_id
               LEFT JOIN textbook_nodes tn ON tn.id = l.node_id
               WHERE l.curriculum_node_id = ?
               ORDER BY l.link_type, d.title COLLATE NOCASE""",
            (curriculum["id"],),
        )]
        links: List[Dict[str, Any]] = []
        for r in rows:
            hunt: List[sqlite3.Row] = []
            if r["node_id"]:
                hunt = con.execute(
                    "SELECT text, locator, page_number FROM textbook_chunks"
                    " WHERE edition_id=? AND node_id=?"
                    " ORDER BY page_number, chunk_index LIMIT 2",
                    (r["edition_id"], r["node_id"]),
                ).fetchall()
            if not hunt and r["page_start"] and r["page_end"]:
                hunt = con.execute(
                    "SELECT text, locator, page_number FROM textbook_chunks"
                    " WHERE edition_id=? AND page_number BETWEEN ? AND ?"
                    " ORDER BY page_number, chunk_index LIMIT 2",
                    (r["edition_id"], r["page_start"], r["page_end"]),
                ).fetchall()
            if not hunt:
                hunt = con.execute(
                    "SELECT text, locator, page_number FROM textbook_chunks"
                    " WHERE edition_id=? ORDER BY page_number, chunk_index LIMIT 2",
                    (r["edition_id"],),
                ).fetchall()
            links.append({
                **r,
                "previews": [
                    {"page": h["page_number"], "locator": h["locator"],
                     "text": h["text"][: int(preview_chars)]}
                    for h in hunt
                ],
            })
        return {
            "curriculum_node": {
                "id": curriculum["id"], "title": curriculum["title"],
                "node_type": curriculum["node_type"],
            },
            "count": len(links),
            "links": links,
        }
    finally:
        con.close()


_STOP_WORDS = {
    "the", "and", "of", "in", "to", "for", "a", "an", "with", "on", "by",
    "part", "chapter", "section", "introduction", "overview",
}


def suggest_curriculum_links(topic_or_node_id: str, limit: int = 10) -> Dict[str, Any]:
    """Read-only candidates: textbook chapters/sections whose titles overlap a topic.

    Writes nothing — linking is always an explicit, reviewable action.
    """
    ensure_textbook_tables()
    con = _connect()
    try:
        curriculum = _resolve_curriculum_node(topic_or_node_id, con)
        want = set(re.findall(r"[a-z0-9]+", title_key(curriculum["title"]))) - _STOP_WORDS
        candidates: List[Dict[str, Any]] = []
        if want:
            rows = con.execute(
                """SELECT tn.id, tn.node_type, tn.title, tn.start_page, tn.end_page,
                          e.id AS edition_id, d.title AS document_title,
                          d.edition_label
                   FROM textbook_nodes tn
                   JOIN textbook_editions e ON e.id = tn.edition_id
                   JOIN textbook_documents d ON d.id = e.document_id"""
            ).fetchall()
            for r in rows:
                got = set(re.findall(r"[a-z0-9]+", title_key(r["title"]))) - _STOP_WORDS
                if not got:
                    continue
                overlap = len(want & got) / len(want | got)
                if overlap > 0:
                    candidates.append({
                        "textbook_node_id": r["id"],
                        "node_type": r["node_type"],
                        "title": r["title"],
                        "edition_id": r["edition_id"],
                        "document_title": r["document_title"],
                        "edition_label": r["edition_label"],
                        "start_page": r["start_page"], "end_page": r["end_page"],
                        "score": round(overlap, 3),
                    })
        candidates.sort(key=lambda x: (-x["score"], x["document_title"], x["title"]))
        return {
            "curriculum_node": {
                "id": curriculum["id"], "title": curriculum["title"],
                "node_type": curriculum["node_type"],
            },
            "count": len(candidates),
            "candidates": candidates[: int(limit)],
        }
    finally:
        con.close()
