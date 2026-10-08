"""P13 knowledge-refresh tests (offline-safe, fixture DB + generated PDFs).

Covers: V14 schema (fresh + idempotent), textbook hash-change detection and new
edition registration through P3, chunk-level diffing, missing-source handling,
PubMed baseline→change detection with an injected fetcher, web conditional-GET
(304 = unchanged), claim re-verification through P4's verify_claim with an
injected verifier (upgrade/downgrade accounting, honest abstention when the new
edition has no chunks), refresh-run audit rows, notifications + acknowledgement,
and offline skipping of network sources.

No live Ollama anywhere: verification is injected via ``verifier_fn``.
"""

from __future__ import annotations

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
from medforge import evidence as E
from medforge import knowledge_refresh as KR
from medforge import textbook as TB

from core.database.schema import LEGACY_SCHEMA_DDL, V3_SCHEMA_DDL
from core.database.migrate_v14 import ensure_refresh_v14, is_v14_applied


PAGES_V1 = [
    ["Chapter 1 Growth", "Growth hormone promotes longitudinal bone growth.",
     "Growth hormone acts largely through IGF-1."],
]
PAGES_V2 = [
    ["Chapter 1 Growth", "Growth hormone promotes longitudinal bone growth.",
     "Growth hormone acts largely through IGF-1 at the growth plate."],
]


def make_pdf(path: Path, pages, pdf_title: str = "") -> Path:
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    c.setTitle(pdf_title)
    for lines in pages:
        y = 760
        for line in lines:
            c.drawString(72, y, line)
            y -= 16
        c.showPage()
    c.save()
    return path


@pytest.fixture()
def kr_env():
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


def fake_verifier(result, log=None):
    queue = list(result) if isinstance(result, (list, tuple)) else None

    def fn(claim, excerpt, meta):
        if log is not None:
            log.append({"claim": claim, "excerpt": excerpt})
        rel = queue.pop(0) if queue is not None else result
        return json.dumps({
            "relationship": rel, "confidence": 0.8, "reason": f"test:{rel}",
            "context_mismatch": False,
        })

    return fn


def _seed_document(env, pages=PAGES_V1, claim_text="Growth hormone promotes linear growth. [S1]"):
    """Register + ingest a PDF through P3, then store one verified claim whose
    evidence points at that document. Returns (document_id, pdf_path, claim_id)."""
    db, _products, docs = env
    pdf = make_pdf(docs / "endo.pdf", pages)
    reg = TB.register_textbook(pdf, title="Endocrine Physiology",
                               authors="A. Author", edition="1st ed.")
    TB.ingest_textbook(reg["edition"]["id"], embed_text=False)

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        chunk = dict(con.execute(
            "SELECT * FROM textbook_chunks WHERE edition_id=? LIMIT 1",
            (reg["edition"]["id"],)).fetchone())
    finally:
        con.close()

    source = {
        "id": chunk["id"], "text": chunk["text"], "source": "Endocrine Physiology",
        "locator": chunk["locator"], "kind": "textbook", "url": "", "quality": 0.95,
    }
    claims = E.extract_claims(claim_text, source_file="study-guide.md",
                              topic="Growth hormone")
    assert claims, "fixture claim did not extract"
    E.store_claims(claims, topic="Growth hormone")
    cid = claims[0]["claim_id"]
    out = E.verify_claim(cid, verifier_fn=fake_verifier("SUPPORTED"),
                         candidates=[source])
    assert out["status"] == "SUPPORTED"
    return reg["document"]["id"], pdf, cid


# ─── V14 schema ───

class TestV14Schema:
    def test_migration_fresh_and_idempotent(self, kr_env):
        db, _p, _d = kr_env
        con = sqlite3.connect(db)
        con.executescript(LEGACY_SCHEMA_DDL)
        con.executescript(V3_SCHEMA_DDL)
        con.commit()
        con.close()

        first = ensure_refresh_v14(db, create_backup=False)
        assert first["success"] is True and first["integrity"] == "ok"
        assert first["foreign_key_errors"] == 0
        names = {r[0] for r in _fetch(db, "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("source_refresh_log", "knowledge_refresh_runs",
                      "knowledge_notifications"):
            assert table in names
        row = _fetch(db, "SELECT version FROM schema_migrations WHERE version='14.0.0'")
        assert row and is_v14_applied(db)

        second = ensure_refresh_v14(db, create_backup=False)
        assert second["success"] is True
        assert _fetch(db, "SELECT count(*) FROM schema_migrations WHERE version='14.0.0'")[0][0] == 1

    def test_ensure_refresh_tables_from_module(self, kr_env):
        db, _p, _d = kr_env
        con = sqlite3.connect(db)
        con.executescript(LEGACY_SCHEMA_DDL)
        con.executescript(V3_SCHEMA_DDL)
        con.commit()
        con.close()
        KR.ensure_refresh_tables()
        assert is_v14_applied(db)


# ─── Textbook update detection ───

class TestTextbookUpdates:
    def test_unchanged_textbook_reports_ok(self, kr_env):
        db, _p, _d = kr_env
        doc_id, _pdf, _cid = _seed_document(kr_env)
        results = KR.check_textbook_updates(register_changes=True)
        entry = next(r for r in results if r["source_id"] == doc_id)
        assert entry["status"] == "ok" and entry["changes_detected"] == 0
        log = _fetch(db, "SELECT status, changes_detected FROM source_refresh_log"
                         " WHERE source_type='textbook' AND source_id=?", (doc_id,))
        assert log[-1] == ("ok", 0)
        # No new edition was created.
        assert _fetch(db, "SELECT count(*) FROM textbook_editions"
                          " WHERE document_id=?", (doc_id,))[0][0] == 1

    def test_changed_textbook_registers_new_edition(self, kr_env):
        db, _p, docs = kr_env
        doc_id, pdf, _cid = _seed_document(kr_env)
        old_edition = _fetch(db, "SELECT id, content_hash FROM textbook_editions")[0][0]
        # Rewrite the same file with changed content.
        make_pdf(pdf, PAGES_V2)
        results = KR.check_textbook_updates(register_changes=True, ingest_new=True)
        entry = next(r for r in results if r["source_id"] == doc_id)
        assert entry["status"] == "changed"
        assert entry["old_edition_id"] == old_edition
        assert entry["new_edition_id"] != old_edition
        editions = _fetch(db, "SELECT id, ingest_status FROM textbook_editions"
                              " WHERE document_id=? ORDER BY created_at", (doc_id,))
        assert len(editions) == 2, "old edition must be preserved"
        # New edition was ingested keyword-only (no Ollama) and diff was computed.
        assert entry["diff"]["unchanged_chunks"] >= 0
        assert _fetch(db, "SELECT count(*) FROM textbook_chunks WHERE edition_id=?",
                      (entry["new_edition_id"],))[0][0] > 0
        log = _fetch(db, "SELECT status FROM source_refresh_log"
                         " WHERE source_type='textbook' AND source_id=?", (doc_id,))
        assert ("changed",) in log

    def test_detect_without_registering(self, kr_env):
        db, _p, docs = kr_env
        doc_id, pdf, _cid = _seed_document(kr_env)
        make_pdf(pdf, PAGES_V2)
        results = KR.check_textbook_updates(register_changes=False)
        entry = next(r for r in results if r["source_id"] == doc_id)
        assert entry["status"] == "changed"
        assert "new_edition_id" not in entry
        assert _fetch(db, "SELECT count(*) FROM textbook_editions"
                          " WHERE document_id=?", (doc_id,))[0][0] == 1

    def test_missing_source_file_skipped(self, kr_env):
        db, _p, _d = kr_env
        doc_id, pdf, _cid = _seed_document(kr_env)
        pdf.unlink()
        results = KR.check_textbook_updates()
        entry = next(r for r in results if r["source_id"] == doc_id)
        assert entry["status"] == "skipped"
        assert "not available" in entry["reason"]


# ─── PubMed update detection ───

class TestPubmedUpdates:
    def test_baseline_then_change(self, kr_env):
        calls = {"n": 0}

        def fetcher(query, retmax):
            calls["n"] += 1
            return [["1", "2"], ["1", "2", "3"]][calls["n"] - 1]

        first = KR.check_pubmed_updates(["growth hormone"], fetcher=fetcher)
        assert first[0]["status"] == "ok" and first[0]["baseline"] is True
        second = KR.check_pubmed_updates(["growth hormone"], fetcher=fetcher)
        assert second[0]["status"] == "changed"
        assert second[0]["new_pmids"] == ["3"]
        stored = _fetch(T.META_DB, "SELECT last_pmid_list FROM source_refresh_log"
                                   " ORDER BY rowid DESC LIMIT 1")[0][0]
        assert json.loads(stored) == ["1", "2", "3"]

    def test_offline_skips_pubmed(self, kr_env, monkeypatch):
        monkeypatch.setattr(T, "OFFLINE", True)
        results = KR.check_pubmed_updates(["growth hormone"],
                                          fetcher=lambda q, r: ["1"])
        assert results[0]["status"] == "skipped"
        stored = _fetch(T.META_DB, "SELECT count(*) FROM source_refresh_log")
        assert stored[0][0] == 0, "skipped checks are not logged as source state"

    def test_fetcher_error_recorded(self, kr_env):
        def boom(query, retmax):
            raise RuntimeError("network down")

        results = KR.check_pubmed_updates(["growth hormone"], fetcher=boom)
        assert results[0]["status"] == "error"
        row = _fetch(T.META_DB, "SELECT status, error_msg FROM source_refresh_log")[0]
        assert row[0] == "error" and "network down" in row[1]


# ─── Web update detection ───

class TestWebUpdates:
    def test_hash_change_and_304(self, kr_env):
        state = {"body": "<p>Version A</p>", "status": 200}

        def fetcher(url, etag):
            return {"status": state["status"], "text": state["body"],
                    "etag": "etag-1", "final_url": url}

        first = KR.check_web_updates(["https://example.org/page"], fetcher=fetcher)
        assert first[0]["status"] == "ok" and first[0]["changes_detected"] == 0
        state["body"] = "<p>Version B</p>"
        second = KR.check_web_updates(["https://example.org/page"], fetcher=fetcher)
        assert second[0]["status"] == "changed" and second[0]["changes_detected"] == 1
        state["status"] = 304
        third = KR.check_web_updates(["https://example.org/page"], fetcher=fetcher)
        assert third[0]["status"] == "ok" and third[0].get("not_modified") is True

    def test_offline_skips_web(self, kr_env, monkeypatch):
        monkeypatch.setattr(T, "OFFLINE", True)
        results = KR.check_web_updates(["https://example.org/page"],
                                       fetcher=lambda u, e: {"status": 200, "text": "x"})
        assert results[0]["status"] == "skipped"


# ─── Claim re-verification (P4 owns verification) ───

class TestReverify:
    def test_downgrade_on_changed_edition(self, kr_env):
        db, _p, _d = kr_env
        doc_id, pdf, cid = _seed_document(kr_env)
        make_pdf(pdf, PAGES_V2)
        KR.check_textbook_updates(register_changes=True, ingest_new=True)
        summary = KR.reverify_claims_for_document(
            doc_id, verifier_fn=fake_verifier("PARTIALLY_SUPPORTED"))
        assert summary["reverified"] >= 1
        assert summary["downgraded"] >= 1
        status = _fetch(db, "SELECT verification_status FROM claims WHERE claim_id=?",
                        (cid,))[0][0]
        assert status == "PARTIALLY_SUPPORTED"
        # P4 history is append-only: both runs exist.
        runs = _fetch(db, "SELECT result FROM verification_runs WHERE claim_id=?", (cid,))
        assert ("SUPPORTED",) in runs and ("PARTIALLY_SUPPORTED",) in runs

    def test_upgrade_on_changed_edition(self, kr_env):
        db, _p, _d = kr_env
        doc_id, pdf, cid = _seed_document(kr_env)
        # Start from PARTIALLY_SUPPORTED so re-verification can upgrade.
        con = sqlite3.connect(db)
        con.execute("UPDATE claims SET verification_status='PARTIALLY_SUPPORTED'"
                    " WHERE claim_id=?", (cid,))
        con.commit()
        con.close()
        make_pdf(pdf, PAGES_V2)
        KR.check_textbook_updates(register_changes=True, ingest_new=True)
        summary = KR.reverify_claims_for_document(
            doc_id, verifier_fn=fake_verifier("SUPPORTED"))
        assert summary["upgraded"] >= 1
        assert _fetch(db, "SELECT verification_status FROM claims WHERE claim_id=?",
                      (cid,))[0][0] == "SUPPORTED"

    def test_abstains_when_new_edition_has_no_chunks(self, kr_env):
        db, _p, _d = kr_env
        doc_id, pdf, cid = _seed_document(kr_env)
        make_pdf(pdf, PAGES_V2)
        # Register the new edition but DO NOT ingest it.
        KR.check_textbook_updates(register_changes=True, ingest_new=False)
        log: list = []
        summary = KR.reverify_claims_for_document(
            doc_id, verifier_fn=fake_verifier("SUPPORTED", log=log))
        # No candidate chunks → deterministic abstention, zero model calls.
        assert log == [], "verifier must not be called without candidate evidence"
        assert summary["reverified"] >= 1
        assert _fetch(db, "SELECT verification_status FROM claims WHERE claim_id=?",
                      (cid,))[0][0] == "INSUFFICIENT_EVIDENCE"


# ─── Orchestration, audit trail, notifications ───

class TestOrchestration:
    def test_run_refresh_records_run_and_notifications(self, kr_env):
        db, _p, docs = kr_env
        doc_id, pdf, _cid = _seed_document(kr_env)
        make_pdf(pdf, PAGES_V2)
        result = KR.run_refresh(
            triggered_by="manual",
            sources=["textbook"],
            verifier_fn=fake_verifier("SUPPORTED"),
        )
        assert result["status"] == "completed"
        assert result["sources_checked"] >= 1
        assert result["sources_changed"] >= 1
        assert result["claims_reverified"] >= 1
        assert result["notifications_created"] >= 1

        run = _fetch(db, "SELECT status, sources_changed, claims_reverified,"
                         " notifications_created FROM knowledge_refresh_runs"
                         " WHERE run_id=?", (result["run_id"],))[0]
        assert run[0] == "completed" and run[1] >= 1 and run[2] >= 1 and run[3] >= 1

        notes = KR.list_notifications(acknowledged=0)
        assert notes and notes[0]["source_type"] == "textbook"
        assert notes[0]["severity"] == "warning"
        assert notes[0]["affected_claims"] >= 1

        status = KR.get_refresh_status()
        assert status["last_run"]["run_id"] == result["run_id"]
        assert status["open_notifications"] >= 1

        acked = KR.acknowledge_notification(notes[0]["notification_id"])
        assert acked["acknowledged"] == 1 and acked["acknowledged_at"]
        assert KR.get_refresh_status()["open_notifications"] == 0
        with pytest.raises(ValueError):
            KR.acknowledge_notification("ntf-does-not-exist")

    def test_run_refresh_offline_skips_network(self, kr_env, monkeypatch):
        monkeypatch.setattr(T, "OFFLINE", True)
        _seed_document(kr_env)
        result = KR.run_refresh(
            triggered_by="scheduled",
            sources=["textbook", "pubmed", "web"],
            pubmed_queries=["growth hormone"],
            web_urls=["https://example.org/page"],
            verifier_fn=fake_verifier("SUPPORTED"),
        )
        assert result["status"] == "completed"
        kinds = {c["source_type"] for c in result["changes"]}
        assert "pubmed" not in kinds and "web" not in kinds
        # The textbook check still ran (unchanged → ok).
        assert any(c["source_type"] == "textbook" and c["status"] == "ok"
                   for c in result["changes"])

    def test_run_refresh_rejects_bad_trigger(self, kr_env):
        _seed_document(kr_env)
        with pytest.raises(ValueError):
            KR.run_refresh(triggered_by="whenever", sources=[])

    def test_diff_reports_chunk_counts(self, kr_env):
        db, _p, _d = kr_env
        doc_id, pdf, _cid = _seed_document(kr_env)
        old_edition = _fetch(db, "SELECT id FROM textbook_editions")[0][0]
        make_pdf(pdf, PAGES_V2)
        entry = next(r for r in KR.check_textbook_updates(register_changes=True)
                     if r["source_id"] == doc_id)
        diff = KR.diff_textbook_editions(old_edition, entry["new_edition_id"])
        for key in ("added_chunks", "removed_chunks", "unchanged_chunks",
                    "pages_added", "pages_removed"):
            assert key in diff


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
