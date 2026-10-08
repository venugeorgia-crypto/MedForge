"""P15 doctor/recovery tests (fixture home, no Ollama, no live data touched).

Covers: full diagnostics (schema, integrity, disk, backups, provider, pending
migrations), backup verification (good file, corrupt file, missing file),
restore (data round-trip, safety copy kept, unverified backup refused), and
deterministic repair (FTS rebuild, VACUUM guard).
"""

from __future__ import annotations

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
from medforge import doctor as D

from core.database.schema import LEGACY_SCHEMA_DDL, V3_SCHEMA_DDL
from core.database.migrate_v15 import ensure_automation_v15


@pytest.fixture()
def doc_env():
    orig = {name: getattr(T, name) for name in
            ("BASE", "META_DB", "PRODUCTS", "DOCS", "LOGS", "TMP")}
    base = Path(tempfile.mkdtemp())
    for sub in ("database", "products", "docs", "logs", "tmp", "backups"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    db = base / "database" / "medforge.sqlite3"
    T.BASE = base
    T.META_DB = db
    T.PRODUCTS = base / "products"
    T.DOCS = base / "docs"
    T.LOGS = base / "logs"
    T.TMP = base / "tmp"
    try:
        yield base, db
    finally:
        for name, value in orig.items():
            setattr(T, name, value)
        shutil.rmtree(base, ignore_errors=True)


def _seed_legacy(db: Path) -> None:
    con = sqlite3.connect(db)
    con.executescript(LEGACY_SCHEMA_DDL)
    con.executescript(V3_SCHEMA_DDL)
    con.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES ('3.0.0','v3','now')"
    )
    con.commit()
    con.close()


def _snapshot(db: Path, dest: Path) -> None:
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


# ─── Diagnostics ───

class TestDiagnose:
    def test_reports_missing_database(self, doc_env):
        _base, db = doc_env
        report = D.diagnose()
        assert report["database_present"] is False
        assert report["healthy"] is False
        assert "missing" in report["problems"][0]

    def test_full_report_on_seeded_db(self, doc_env):
        base, db = doc_env
        _seed_legacy(db)
        ensure_automation_v15(db, create_backup=False)
        _snapshot(db, base / "backups" / "medforge_nightly_20260101.db")

        report = D.diagnose()
        assert report["database_present"] is True
        assert report["integrity_ok"] is True
        assert report["foreign_key_violations"] == 0
        assert report["migrations_pending"] == []
        assert "15.0.0" in report["migrations_applied"]
        assert report["backups"]["count"] == 1
        assert report["backups"]["newest_verified"] is True
        assert report["tables"] > 10
        assert isinstance(report["provider"], dict)
        # Provider is very likely unreachable in CI; that must be reported, not hidden.
        if not report["provider"]["alive"]:
            assert any("provider" in p for p in report["problems"])

    def test_detects_pending_migrations(self, doc_env):
        _base, db = doc_env
        _seed_legacy(db)
        assert "15.0.0" in D.pending_migrations()
        report = D.diagnose()
        assert not report["healthy"]
        assert any("pending migrations" in p for p in report["problems"])


# ─── Backup verification ───

class TestVerifyBackup:
    def test_good_backup_verifies(self, doc_env):
        base, db = doc_env
        _seed_legacy(db)
        ensure_automation_v15(db, create_backup=False)
        bak = base / "backups" / "good.db"
        _snapshot(db, bak)
        result = D.verify_backup(bak)
        assert result["ok"] is True
        assert result["integrity"] == "ok"
        assert result["latest_version"] == "15.0.0"
        assert result["tables"] > 10 and result["size_bytes"] > 0

    def test_missing_file_is_reported(self, doc_env):
        base, _db = doc_env
        result = D.verify_backup(base / "backups" / "nope.db")
        assert result["ok"] is False and result["exists"] is False

    def test_corrupt_file_is_reported(self, doc_env):
        base, _db = doc_env
        bad = base / "backups" / "corrupt.db"
        bad.write_bytes(b"this is not a sqlite database at all" * 10)
        result = D.verify_backup(bad)
        assert result["ok"] is False
        assert "not a SQLite database" in result["reason"]


# ─── Restore ───

class TestRestore:
    def test_restore_round_trip_keeps_safety_copy(self, doc_env):
        base, db = doc_env
        _seed_legacy(db)
        con = sqlite3.connect(db)
        con.execute("INSERT INTO study_sessions (created_at, topic, score, notes)"
                    " VALUES ('t1', 'gh', 0.9, '')")
        con.commit()
        con.close()

        bak = base / "backups" / "before-change.db"
        _snapshot(db, bak)

        # Damage the live database: delete the row, then restore.
        con = sqlite3.connect(db)
        con.execute("DELETE FROM study_sessions")
        con.commit()
        con.close()
        assert _count(db, "study_sessions") == 0

        result = D.restore_backup(bak)
        assert result["restored"] is True
        assert result["verification"]["ok"] is True
        assert _count(db, "study_sessions") == 1
        assert result["safety_copy"] and Path(result["safety_copy"]).is_file()
        assert D.verify_backup(result["safety_copy"])["ok"] is True

    def test_restore_refuses_unverified_backup(self, doc_env):
        base, db = doc_env
        _seed_legacy(db)
        bad = base / "backups" / "corrupt.db"
        bad.write_bytes(b"not a database")
        with pytest.raises(ValueError):
            D.restore_backup(bad)
        with pytest.raises(ValueError):
            D.restore_backup(base / "backups" / "missing.db")

    def test_restore_into_explicit_target(self, doc_env):
        base, db = doc_env
        _seed_legacy(db)
        bak = base / "backups" / "copy.db"
        _snapshot(db, bak)
        target = base / "database" / "restored.sqlite3"
        result = D.restore_backup(bak, target_db=target, make_safety_copy=False)
        assert result["restored"] is True and target.is_file()
        assert _count(target, "schema_migrations") >= 1


# ─── Repair ───

class TestRepair:
    def test_repair_rebuilds_fts(self, doc_env):
        _base, db = doc_env
        _seed_legacy(db)
        con = sqlite3.connect(db)
        con.execute("INSERT INTO chunks (id, text, source, locator, kind, url,"
                    " quality, updated_at) VALUES ('c1','cardiac cycle basics',"
                    " 'notes','p.1','course_pdf','',0.9,'now')")
        con.commit()
        # Simulate a stale FTS index.
        con.execute("DELETE FROM chunks_fts")
        con.commit()
        con.close()

        result = D.repair(rebuild_fts=True, vacuum=False)
        assert result["status"] == "ok"
        assert any(s["step"] == "fts_rebuild" and s["status"] == "ok"
                   for s in result["steps"])
        con = sqlite3.connect(db)
        try:
            hit = con.execute(
                "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'cardiac'"
            ).fetchone()[0]
        finally:
            con.close()
        assert hit >= 1, "FTS rebuild must make the row searchable again"

    def test_repair_reports_broken_database(self, doc_env):
        _base, db = doc_env
        db.write_bytes(b"definitely not sqlite")
        result = D.repair(rebuild_fts=True)
        # A file that is not a database cannot be 'repaired' — report the truth.
        assert result["status"] == "failed" or any(
            s["status"] == "failed" for s in result["steps"])


def _count(db: Path, table: str) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
