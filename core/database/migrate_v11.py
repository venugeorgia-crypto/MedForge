#!/usr/bin/env python3
"""MedForge V11 Migration: publication / review / approval workflow.

WHY:  P10 needs a formal content lifecycle. No generated medical product may
      jump from generation to publication: approval must be an explicit,
      recorded judgement backed by executable gates and a review queue.
WHAT: adds review_queue, review_history (append-only) and approval_records
      (plus indexes), and seven nullable lifecycle columns on content_artifacts
      (publication_status, approved_at, approved_by, published_at,
      published_path, retired_at, retired_reason). Records migration 11.0.0;
      verifies integrity and foreign keys.
RISK: low — additive tables + nullable columns only; no existing row is
      rewritten (existing artifacts read as publication_status='UNREVIEWED').
MIGRATION PLAN: idempotent (IF NOT EXISTS everywhere; the column additions are
      guarded by a pragma check). Runs ensure_study_v10 first so the
      content_items foreign key always resolves, mirroring V3–V10.
TEST PLAN: tests/test_publication.py (fresh DB, idempotent rerun, preservation)
      + full regression suite.
ROLLBACK PLAN: drop review_queue, review_history, approval_records; the added
      columns are nullable and can be ignored (SQLite cannot drop columns
      pre-3.35 semantics we rely on elsewhere — rollback documents this).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from core.database.schema import V11_SCHEMA_DDL
except ImportError:  # direct execution from a subdirectory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.database.schema import V11_SCHEMA_DDL

V11_VERSION = "11.0.0"
MIGRATION_NAME = "v11_publication_workflow"

V11_TABLES = ("review_queue", "review_history", "approval_records")

V11_ARTIFACT_COLUMNS = (
    ("publication_status", "TEXT NULL DEFAULT 'UNREVIEWED'"),
    ("approved_at", "TEXT NULL"),
    ("approved_by", "TEXT NULL"),
    ("published_at", "TEXT NULL"),
    ("published_path", "TEXT NULL"),
    ("retired_at", "TEXT NULL"),
    ("retired_reason", "TEXT NULL"),
)


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _existing_columns(con: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_publication_v11(db_path: Path | str, create_backup: bool = False) -> Dict[str, Any]:
    """Bring the database to the V11 publication schema (idempotent, additive)."""
    from core.database.migrate_v10 import ensure_study_v10

    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    # V10 first: review_queue/approval_records reference content_items.
    ensure_study_v10(target)

    con = sqlite3.connect(target)
    backup_file = None
    try:
        con.execute("PRAGMA foreign_keys=ON;")
        if create_backup:
            from core.database.migrate_v3 import create_snapshot_backup

            backup_file = create_snapshot_backup(
                target, target.parent.parent / "backups" / "medforge_pre_v11_backup.db"
            )

        con.executescript(V11_SCHEMA_DDL)

        # Additive lifecycle columns on content_artifacts (guarded, idempotent).
        cols = _existing_columns(con, "content_artifacts")
        if not cols:
            raise RuntimeError("content_artifacts missing before V11")
        for name, decl in V11_ARTIFACT_COLUMNS:
            if name not in cols:
                con.execute(f"ALTER TABLE content_artifacts ADD COLUMN {name} {decl}")

        existing = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = [t for t in V11_TABLES if t not in existing]
        if missing:
            raise RuntimeError(f"Missing expected V11 tables after migration: {missing}")

        con.execute(
            """INSERT INTO schema_migrations (version, name, applied_at)
               VALUES (?, ?, ?)
               ON CONFLICT(version) DO UPDATE SET applied_at=excluded.applied_at""",
            (V11_VERSION, MIGRATION_NAME, _utcnow()),
        )
        con.commit()

        integrity = con.execute("PRAGMA integrity_check;").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Database integrity compromised after V11: {integrity}")
        fk_errors = con.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign-key violations after V11: {fk_errors[:5]}")

        return {
            "status": "success",
            "version": V11_VERSION,
            "database": str(target),
            "backup": str(backup_file) if backup_file else None,
            "tables": list(V11_TABLES),
            "columns_added": [c for c, _ in V11_ARTIFACT_COLUMNS],
            "integrity": integrity[0],
        }
    finally:
        con.close()


def is_v11_applied(db_path: Path | str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=?", (V11_VERSION,)
        ).fetchone()
        return bool(row and row[0] > 0)
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


if __name__ == "__main__":
    import argparse
    import os

    default_db = (
        Path(os.environ.get("MEDFORGE_HOME", str(Path.home() / "MedForge")))
        .expanduser()
        .resolve()
        / "database"
        / "medforge.sqlite3"
    )
    parser = argparse.ArgumentParser(description="MedForge V11 publication migration")
    parser.add_argument("--db", type=str, default=str(default_db))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(f"Applying V11 publication migration on {args.db} ...")
    result = ensure_publication_v11(args.db, create_backup=not args.no_backup)
    print("Migration finished:", result["status"], "| integrity:", result["integrity"])
    if result["backup"]:
        print("  Backup:", result["backup"])
