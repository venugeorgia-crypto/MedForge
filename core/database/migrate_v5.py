#!/usr/bin/env python3
"""MedForge V5 Migration: textbook provenance tables.

WHY:  P3 needs first-class textbook documents/editions with chapter/section/
      page/chunk provenance and curriculum links; today PDFs are anonymous
      chunk rows.
WHAT: adds textbook_documents, textbook_editions, textbook_nodes,
      textbook_pages, textbook_chunks and curriculum_text_links (plus
      indexes), records migration 5.0.0, verifies integrity. Purely additive.
RISK: low — new tables only; no existing table is altered or rebuilt.
MIGRATION PLAN: idempotent (CREATE TABLE/INDEX IF NOT EXISTS); self-heals on
      first textbook use, exactly like the V3/V4 runners.
TEST PLAN: tests/test_textbook.py (fresh DB, populated DB preservation,
      idempotent rerun) + full regression suite.
ROLLBACK PLAN: drop the six new tables (no existing data is touched);
      optionally restore the pre-V5 snapshot backup.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from core.database.schema import (
        V5_SCHEMA_DDL,
        TEXTBOOK_NODE_TYPES,
        TEXTBOOK_SOURCE_TYPES,
        TEXTBOOK_PAGE_STATUS,
        TEXTBOOK_OCR_STATUS,
        TEXTBOOK_INGEST_STATUS,
        CURRICULUM_TEXT_LINK_TYPES,
    )
except ImportError:  # direct execution from a subdirectory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.database.schema import (
        V5_SCHEMA_DDL,
        TEXTBOOK_NODE_TYPES,
        TEXTBOOK_SOURCE_TYPES,
        TEXTBOOK_PAGE_STATUS,
        TEXTBOOK_OCR_STATUS,
        TEXTBOOK_INGEST_STATUS,
        CURRICULUM_TEXT_LINK_TYPES,
    )

V5_VERSION = "5.0.0"
MIGRATION_NAME = "v5_textbook_provenance"

V5_TABLES = (
    "textbook_documents",
    "textbook_editions",
    "textbook_nodes",
    "textbook_pages",
    "textbook_chunks",
    "curriculum_text_links",
)


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_textbook_v5(db_path: Path | str, create_backup: bool = False) -> Dict[str, Any]:
    """Bring the active database to the V5 textbook schema (idempotent).

    Runs the V4 curriculum migration first so curriculum_text_links always has
    its referenced table, then applies the additive V5 DDL.
    """
    from core.database.migrate_v4 import ensure_curriculum_v4

    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    # V4 first (idempotent, self-healing) — curriculum_nodes must exist.
    ensure_curriculum_v4(target)

    con = sqlite3.connect(target)
    backup_file = None
    try:
        con.execute("PRAGMA foreign_keys=ON;")
        if create_backup:
            from core.database.migrate_v3 import create_snapshot_backup

            backup_file = create_snapshot_backup(
                target,
                target.parent.parent / "backups" / "medforge_pre_v5_backup.db",
            )

        con.executescript(V5_SCHEMA_DDL)

        # Verify every expected table exists post-DDL.
        existing = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = [t for t in V5_TABLES if t not in existing]
        if missing:
            raise RuntimeError(f"Missing expected V5 tables after migration: {missing}")

        con.execute(
            """INSERT INTO schema_migrations (version, name, applied_at)
               VALUES (?, ?, ?)
               ON CONFLICT(version) DO UPDATE SET applied_at=excluded.applied_at""",
            (V5_VERSION, MIGRATION_NAME, _utcnow()),
        )
        con.commit()

        integrity = con.execute("PRAGMA integrity_check;").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Database integrity compromised after V5: {integrity}")

        return {
            "status": "success",
            "version": V5_VERSION,
            "database": str(target),
            "backup": str(backup_file) if backup_file else None,
            "tables": list(V5_TABLES),
            "integrity": integrity[0],
        }
    finally:
        con.close()


def is_v5_applied(db_path: Path | str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=?", (V5_VERSION,)
        ).fetchone()
        return bool(row and row[0] > 0)
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


if __name__ == "__main__":
    import argparse
    import os

    default_db = Path(
        os.environ.get("MEDFORGE_HOME", str(Path.home() / "MedForge"))
    ).expanduser().resolve() / "database" / "medforge.sqlite3"
    parser = argparse.ArgumentParser(description="MedForge V5 textbook migration")
    parser.add_argument("--db", type=str, default=str(default_db))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(f"Applying V5 textbook migration on {args.db} ...")
    result = ensure_textbook_v5(args.db, create_backup=not args.no_backup)
    print("Migration finished:", result["status"], "| integrity:", result["integrity"])
    if result["backup"]:
        print("  Backup:", result["backup"])
