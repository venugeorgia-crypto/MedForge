"""P15 performance checks — measured on realistic fixtures, loose bounds.

These are not micro-benchmarks of the machine; they guard against accidental
algorithmic regressions (a query that goes O(n^2), a planner that loads
everything) on the 8 GB M1 target. Every test prints its measurement so the
number can be quoted in the final report. No Ollama, no network.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

import medforge.types as T
from medforge import doctor as D

from core.database.schema import LEGACY_SCHEMA_DDL, V3_SCHEMA_DDL


@pytest.fixture()
def perf_env():
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


def _seed(db: Path) -> None:
    con = sqlite3.connect(db)
    con.executescript(LEGACY_SCHEMA_DDL)
    con.executescript(V3_SCHEMA_DDL)
    con.commit()
    con.close()


def _time(fn):
    start = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - start


class TestPerformance:
    def test_fts_search_scales_to_10k_chunks(self, perf_env):
        _base, db = perf_env
        _seed(db)
        con = sqlite3.connect(db)
        rows = [(f"c{i}", f"cardiac cycle physiology paragraph number {i}",
                 "notes", f"p.{i % 200}", "course_pdf", "", 0.9, "now")
                for i in range(10_000)]
        con.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)", rows)
        con.executemany(
            "INSERT INTO chunks_fts (id, text, source, locator) VALUES (?,?,?,?)",
            [(r[0], r[1], r[2], r[3]) for r in rows])
        con.commit()
        con.close()

        def search():
            con = sqlite3.connect(db)
            try:
                return con.execute(
                    "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'cardiac'"
                ).fetchone()[0]
            finally:
                con.close()

        hits, elapsed = _time(search)
        print(f"[perf] FTS match over 10k chunks: {elapsed*1000:.1f} ms ({hits} hits)")
        assert hits == 10_000
        assert elapsed < 1.0, f"FTS search too slow: {elapsed:.2f}s"

    def test_claims_listing_stays_bounded(self, perf_env):
        _base, db = perf_env
        _seed(db)
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE IF NOT EXISTS claims (claim_id TEXT PRIMARY KEY,"
            " claim_text TEXT, normalized_text TEXT UNIQUE, claim_type TEXT,"
            " topic TEXT, verification_status TEXT, created_at TEXT, updated_at TEXT);")
        rows = [(f"cl{i}", f"claim {i}", f"claim {i}", "fact", "topic",
                 "SUPPORTED", "now", "now") for i in range(5_000)]
        con.executemany("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()

        def listing():
            con = sqlite3.connect(db)
            try:
                return con.execute(
                    "SELECT claim_id FROM claims WHERE verification_status='SUPPORTED'"
                    " LIMIT 50").fetchall()
            finally:
                con.close()

        rows, elapsed = _time(listing)
        print(f"[perf] claim listing (LIMIT 50) over 5k claims: {elapsed*1000:.2f} ms")
        assert len(rows) == 50  # LIMIT bounds the work regardless of table size
        assert elapsed < 0.25

    def test_diagnose_completes_quickly(self, perf_env):
        _base, db = perf_env
        _seed(db)
        report, elapsed = _time(D.diagnose)
        print(f"[perf] diagnose(): {elapsed*1000:.1f} ms "
              f"(integrity={report['integrity']}, tables={report['tables']})")
        assert report["integrity_ok"] is True
        assert elapsed < 2.0

    def test_backup_and_verify_scale(self, perf_env):
        base, db = perf_env
        _seed(db)
        con = sqlite3.connect(db)
        rows = [(f"c{i}", f"text {i}", "notes", f"p.{i}", "course_pdf", "", 0.9, "now")
                for i in range(2_000)]
        con.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()

        from medforge import automation as A
        result, elapsed = _time(lambda: A.backup_step(retention=7))
        print(f"[perf] backup of {result['size_bytes']/1024:.0f} KB db: {elapsed*1000:.0f} ms")
        assert result["status"] == "ok"
        verified, verify_elapsed = _time(lambda: D.verify_backup(result["path"]))
        assert verified["ok"] is True
        print(f"[perf] verify backup: {verify_elapsed*1000:.0f} ms")
        assert elapsed < 5.0 and verify_elapsed < 2.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
