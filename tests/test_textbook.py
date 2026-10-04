"""P3 textbook provenance tests (offline-safe, fixture DB + generated PDFs).

Covers: registration + metadata resolution, edition identity, duplicate
re-import, changed-edition preservation, page/chapter/section provenance,
chunk-to-document linkage, no-text/OCR reporting, curriculum linking +
discovery, read-only suggestions, V5 migration safety, and the generic
ingester skip. Embedding is disabled everywhere (embed_text=False) so the
suite never needs Ollama.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

import medforge.types as T
from medforge import curriculum as C
from medforge import textbook as TB
from medforge.ingestion import ingest_pdfs

from core.database.schema import LEGACY_SCHEMA_DDL, V3_SCHEMA_DDL
from core.database.migrate_v5 import ensure_textbook_v5, is_v5_applied


CHAPTER1 = ["Chapter 1 Introduction to Endocrinology",
            "The endocrine system coordinates physiology.",
            "Hormones act through specific receptors."]
SECTION1 = ["1.1 Hormone Signaling",
            "Signaling begins with ligand binding.",
            "Receptors amplify the downstream signal."]
SECTION2 = ["1.2 Feedback Loops",
            "Negative feedback stabilizes hormone levels.",
            "Positive feedback amplifies a stimulus."]
BLANK = None
CHAPTER2 = ["Chapter 2 The Pituitary Gland",
            "The pituitary sits in the sella turcica.",
            "It releases multiple hormones."]
PAGES = [CHAPTER1, SECTION1, SECTION2, BLANK, CHAPTER2]


def make_pdf(path: Path, pages, pdf_title: str = "") -> Path:
    """Generate a small test PDF (title '' so filename fallback is testable)."""
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    c.setTitle(pdf_title)
    for lines in pages:
        if lines:
            y = 760
            for line in lines:
                c.drawString(72, y, line)
                y -= 16
        c.showPage()
    c.save()
    return path


@pytest.fixture()
def tb_env():
    """Fresh temp META_DB + PRODUCTS + DOCS dirs; restore after the test."""
    orig_db, orig_products, orig_docs = T.META_DB, T.PRODUCTS, T.DOCS
    fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(db_path)
    products = Path(tempfile.mkdtemp())
    docs = Path(tempfile.mkdtemp())
    T.META_DB, T.PRODUCTS, T.DOCS = Path(db_path), products, docs
    try:
        yield Path(db_path), products, docs
    finally:
        T.META_DB, T.PRODUCTS, T.DOCS = orig_db, orig_products, orig_docs
        Path(db_path).unlink(missing_ok=True)
        shutil.rmtree(products, ignore_errors=True)
        shutil.rmtree(docs, ignore_errors=True)


def _fetch(db: Path, sql: str, params=()):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


# ─── Registration + identity ───


def test_register_metadata_and_identity(tb_env):
    db, _products, docs = tb_env
    pdf = make_pdf(docs / "endocrinology.pdf", PAGES)
    res = TB.register_textbook(
        pdf, title="Endocrine Physiology", authors="Boron & Boulpaep",
        publisher="Elsevier", edition="3rd ed.", publication_year=2017,
        isbn="978-1-4557-4377-3", subject="Physiology",
    )
    assert res["created"] is True
    doc, ed = res["document"], res["edition"]
    assert doc["title"] == "Endocrine Physiology"
    assert doc["authors"] == "Boron & Boulpaep"
    assert doc["publisher"] == "Elsevier" and doc["edition_label"] == "3rd ed."
    assert doc["publication_year"] == 2017 and doc["isbn"] == "978-1-4557-4377-3"
    assert doc["source_type"] == "textbook"
    assert ed["ingest_status"] == "REGISTERED" and ed["chunk_count"] == 0
    # Deterministic ids.
    assert doc["id"] == TB.document_id_for("Endocrine Physiology")
    assert ed["id"] == TB.edition_id_for(doc["id"], res["content_hash"])
    assert res["content_hash"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    listed = TB.list_textbooks()
    assert len(listed) == 1 and len(listed[0]["editions"]) == 1


def test_same_file_reimport_is_idempotent(tb_env):
    _db, _products, docs = tb_env
    pdf = make_pdf(docs / "phys.pdf", PAGES)
    first = TB.register_textbook(pdf, title="Physiology")
    second = TB.register_textbook(pdf, title="Physiology")
    assert first["created"] is True and second["created"] is False
    assert first["edition"]["id"] == second["edition"]["id"]
    rows = _fetch(T.META_DB, "SELECT count(*) FROM textbook_editions")
    assert rows[0][0] == 1


def test_changed_file_creates_new_edition_and_preserves_old(tb_env):
    _db, _products, docs = tb_env
    pdf = make_pdf(docs / "phys.pdf", PAGES)
    v1 = TB.register_textbook(pdf, title="Physiology", edition="1st ed.")
    make_pdf(pdf, PAGES + [["Extra page from the revision."]])
    v2 = TB.register_textbook(pdf, title="Physiology", edition="1st ed.")
    assert v1["edition"]["id"] != v2["edition"]["id"]
    assert v2["document"]["id"] == v1["document"]["id"]  # same family
    editions = _fetch(T.META_DB,
                      "SELECT id, content_hash FROM textbook_editions"
                      " WHERE document_id=?", (v1["document"]["id"],))
    assert len(editions) == 2
    assert {e[0] for e in editions} == {v1["edition"]["id"], v2["edition"]["id"]}
    assert v1["content_hash"] in {e[1] for e in editions}  # old preserved


# ─── Ingestion + provenance ───


def test_ingest_structure_and_provenance(tb_env):
    _db, _products, docs = tb_env
    pdf = make_pdf(docs / "endo.pdf", PAGES)
    summary = TB.ingest_textbook(
        pdf, embed_text=False, title="Endocrine Physiology", edition="3rd ed.",
    )
    assert summary["skipped"] is False
    assert summary["pages"] == 5
    assert summary["pages_extracted"] == 4 and summary["pages_no_text"] == 1
    assert summary["chapters"] == 2 and summary["sections"] == 2
    assert summary["subsections"] == 0
    assert summary["ingest_status"] == "PARTIAL"
    assert summary["chunks_total"] == 4 and summary["chunks_created"] == 4

    ed_id = summary["edition_id"]
    nodes = dict(_fetch(
        T.META_DB,
        "SELECT title, node_type FROM textbook_nodes WHERE edition_id=?",
        (ed_id,),
    ))
    assert nodes.get("Introduction to Endocrinology") == "Chapter"
    assert nodes.get("The Pituitary Gland") == "Chapter"
    assert nodes.get("Hormone Signaling") == "Section"
    assert nodes.get("Feedback Loops") == "Section"

    # Page → node mapping survives.
    page_nodes = {
        p: n for p, n in _fetch(
            T.META_DB,
            "SELECT page_number, node_id FROM textbook_pages"
            " WHERE edition_id=? ORDER BY page_number", (ed_id,),
        )
    }
    chapter1 = _fetch(T.META_DB,
                      "SELECT id FROM textbook_nodes WHERE title='Introduction to"
                      " Endocrinology' AND edition_id=?", (ed_id,))[0][0]
    section1 = _fetch(T.META_DB,
                      "SELECT id FROM textbook_nodes WHERE title='Hormone Signaling'"
                      " AND edition_id=?", (ed_id,))[0][0]
    assert page_nodes[1] == chapter1 and page_nodes[2] == section1
    assert _fetch(T.META_DB, "SELECT extraction_status FROM textbook_pages"
                  " WHERE edition_id=? AND page_number=4", (ed_id,))[0][0] == "no_text"

    # Chunk → document/edition/page/node linkage + locator.
    rows = _fetch(
        T.META_DB,
        "SELECT id, document_id, page_number, node_id, chunk_index, locator"
        " FROM textbook_chunks WHERE edition_id=? ORDER BY page_number", (ed_id,),
    )
    assert len(rows) == 4
    for cid, doc_id, page_no, node_id, idx, locator in rows:
        assert doc_id == summary["document_id"]
        assert page_no in (1, 2, 3, 5) and idx >= 1
        assert locator.startswith(f"p. {page_no}")
    page2 = [r for r in rows if r[2] == 2][0]
    assert "Hormone Signaling" in page2[5]

    # Shared retrieval store mirrors the chunks with textbook provenance.
    shared = _fetch(
        T.META_DB, "SELECT id, kind, quality, locator FROM chunks WHERE kind='textbook'",
    )
    assert len(shared) == 4
    assert {s[0] for s in shared} == {r[0] for r in rows}
    assert all(s[1] == "textbook" and abs(s[2] - 0.95) < 1e-9 for s in shared)

    # Node page ranges span their content — including the no-text page 4,
    # which physically sits inside chapter 1 (before chapter 2 starts on 5).
    start_end = _fetch(T.META_DB,
                       "SELECT start_page, end_page FROM textbook_nodes WHERE id=?",
                       (chapter1,))[0]
    assert start_end == (1, 4)
    assert is_v5_applied(T.META_DB)


def test_duplicate_ingest_adds_nothing(tb_env):
    _db, _products, docs = tb_env
    pdf = make_pdf(docs / "endo.pdf", PAGES)
    first = TB.ingest_textbook(pdf, embed_text=False, title="Endocrine Physiology")
    before = (
        _fetch(T.META_DB, "SELECT count(*) FROM textbook_chunks")[0][0],
        _fetch(T.META_DB, "SELECT count(*) FROM chunks")[0][0],
    )
    second = TB.ingest_textbook(pdf, embed_text=False, title="Endocrine Physiology")
    assert second["skipped"] is True
    after = (
        _fetch(T.META_DB, "SELECT count(*) FROM textbook_chunks")[0][0],
        _fetch(T.META_DB, "SELECT count(*) FROM chunks")[0][0],
    )
    assert before == after
    assert second["chunks_total"] == first["chunks_total"]


def test_no_structure_pdf_gets_body_chapter(tb_env):
    _db, _products, docs = tb_env
    pdf = make_pdf(docs / "plain.pdf", [["Just ordinary prose without headings."],
                                        ["More prose on the second page."]])
    summary = TB.ingest_textbook(pdf, embed_text=False, title="Plain Notes")
    assert summary["chapters"] == 1
    title = _fetch(T.META_DB, "SELECT title FROM textbook_nodes WHERE node_type='Chapter'")[0][0]
    assert title == "Body"
    assert summary["ingest_status"] == "EXTRACTED"


# ─── Curriculum linking ───


def _ingested(tb_env):
    _db, _products, docs = tb_env
    pdf = make_pdf(docs / "endo.pdf", PAGES)
    summary = TB.ingest_textbook(
        pdf, embed_text=False, title="Endocrine Physiology", edition="3rd ed.",
    )
    section_id = _fetch(
        T.META_DB,
        "SELECT id FROM textbook_nodes WHERE title='Hormone Signaling'",
    )[0][0]
    return summary, section_id


def test_curriculum_link_discovery_and_unlink(tb_env):
    summary, section_id = _ingested(tb_env)
    C.import_syllabus("Week 1: Endocrinology\n- Hormone Signaling\n",
                      subject_title="Physiology")
    link = TB.link_curriculum_text("Hormone Signaling", summary["edition_id"],
                                   node_id=section_id, link_type="primary")
    assert link["created"] is True
    again = TB.link_curriculum_text("Hormone Signaling", summary["edition_id"],
                                    node_id=section_id, link_type="primary")
    assert again["created"] is False
    assert _fetch(T.META_DB, "SELECT count(*) FROM curriculum_text_links")[0][0] == 1

    ev = TB.textbook_evidence_for_topic("Hormone Signaling")
    assert ev["count"] == 1
    entry = ev["links"][0]
    assert entry["document_title"] == "Endocrine Physiology"
    assert entry["textbook_node_title"] == "Hormone Signaling"
    assert entry["edition_label"] == "3rd ed."
    assert entry["previews"] and "Signaling begins" in entry["previews"][0]["text"]

    assert TB.unlink_curriculum_text(entry["link_id"]) == 1
    assert TB.textbook_evidence_for_topic("Hormone Signaling")["count"] == 0


def test_link_validation_errors(tb_env):
    summary, section_id = _ingested(tb_env)
    C.import_syllabus("Week 1: Endocrinology\n- Hormone Signaling\n",
                      subject_title="Physiology")
    with pytest.raises(ValueError):
        TB.link_curriculum_text("Hormone Signaling", summary["edition_id"],
                                link_type="best-friend")
    with pytest.raises(ValueError):
        TB.link_curriculum_text("Hormone Signaling", "no-such-edition")
    with pytest.raises(ValueError):
        TB.link_curriculum_text("Hormone Signaling", summary["edition_id"],
                                node_id="no-such-node")
    with pytest.raises(ValueError):
        TB.link_curriculum_text("Unmapped Topic", summary["edition_id"])


def test_suggestions_are_read_only(tb_env):
    summary, _section = _ingested(tb_env)
    C.import_syllabus("Week 1: Endocrinology\n- Hormone Signaling\n",
                      subject_title="Physiology")
    before = (
        _fetch(T.META_DB, "SELECT count(*) FROM curriculum_text_links")[0][0],
        _fetch(T.META_DB, "SELECT count(*) FROM textbook_nodes")[0][0],
    )
    sug = TB.suggest_curriculum_links("Hormone Signaling")
    assert sug["count"] >= 1
    assert sug["candidates"][0]["title"] == "Hormone Signaling"
    assert sug["candidates"][0]["score"] > 0
    after = (
        _fetch(T.META_DB, "SELECT count(*) FROM curriculum_text_links")[0][0],
        _fetch(T.META_DB, "SELECT count(*) FROM textbook_nodes")[0][0],
    )
    assert before == after


# ─── Inspection + metadata ───


def test_book_structure_bounded(tb_env):
    summary, _section = _ingested(tb_env)
    info = TB.book_structure(summary["edition_id"], page_limit=3)
    assert info["pages_total"] == 5 and len(info["pages"]) == 3
    assert info["pages_truncated"] is True
    assert len(info["nodes"]) == 2  # two chapter roots
    ch1 = info["nodes"][0]
    assert ch1["node_type"] == "Chapter" and len(ch1["children"]) == 2


def test_sidecar_and_filename_metadata(tb_env):
    _db, _products, docs = tb_env
    pdf = make_pdf(docs / "renal_pathophysiology.pdf", [["Some content."]])
    sidecar = pdf.with_name(pdf.stem + ".meta.json")
    sidecar.write_text(json.dumps({"title": "Renal Pathophysiology",
                                   "authors": "A. Author",
                                   "edition": "2nd ed."}), encoding="utf-8")
    res = TB.register_textbook(pdf)
    assert res["document"]["title"] == "Renal Pathophysiology"
    assert res["document"]["authors"] == "A. Author"
    assert res["document"]["edition_label"] == "2nd ed."

    plain = make_pdf(docs / "cardiac_physiology_basics.pdf", [["Content."]])
    res2 = TB.register_textbook(plain)
    assert res2["document"]["title"] == "cardiac physiology basics"  # filename fallback


# ─── Generic ingest interaction + migration safety ───


def test_registered_textbook_skipped_by_generic_ingest(tb_env):
    _db, _products, docs = tb_env
    pdf = make_pdf(docs / "endo.pdf", PAGES)
    TB.ingest_textbook(pdf, embed_text=False, title="Endocrine Physiology")
    assert str(pdf.resolve()) in TB.registered_source_paths()
    chapters_before = _fetch(T.META_DB,
                             "SELECT count(*) FROM chunks WHERE kind='course_pdf'")[0][0]
    assert ingest_pdfs() == 0  # skip = no new indexing for the registered file
    chapters_after = _fetch(T.META_DB,
                            "SELECT count(*) FROM chunks WHERE kind='course_pdf'")[0][0]
    assert chapters_before == chapters_after == 0


def test_v5_migration_preserves_data_and_is_idempotent(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(db)
    con.executescript(LEGACY_SCHEMA_DDL)
    con.executescript(V3_SCHEMA_DDL)
    con.execute(
        "INSERT INTO chunks (id, text, source, locator, kind, url, quality, updated_at)"
        " VALUES('keep1', 'preserved text', 'legacy.pdf', 'page 1', 'course_pdf', '',"
        " 0.9, '2026-01-01T00:00:00Z')"
    )
    con.execute(
        "INSERT INTO curriculum_nodes (id, node_type, title, created_at, updated_at)"
        " VALUES('topic1', 'Topic', 'Preserved Topic', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z')"
    )
    con.commit()
    con.close()

    result = ensure_textbook_v5(db)
    assert result["status"] == "success" and result["integrity"] == "ok"
    assert is_v5_applied(db)
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT count(*) FROM chunks WHERE id='keep1'").fetchone()[0] == 1
        assert con.execute(
            "SELECT count(*) FROM curriculum_nodes WHERE id='topic1'"
        ).fetchone()[0] == 1
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"textbook_documents", "textbook_editions", "textbook_nodes",
                "textbook_pages", "textbook_chunks", "curriculum_text_links"} <= tables
    finally:
        con.close()

    again = ensure_textbook_v5(db)  # idempotent rerun
    assert again["status"] == "success"
    assert _fetch(db, "SELECT count(*) FROM chunks WHERE id='keep1'")[0][0] == 1
