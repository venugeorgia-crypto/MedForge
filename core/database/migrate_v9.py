#!/usr/bin/env python3
"""MedForge V9 Migration: question-level assessment engine tables.

WHY:  P8 needs a first-class, versioned item bank (stable item_id + immutable
      item_version), deterministic blueprints, persistent assessment sessions
      (separate from tutor sessions), question-level attempts that survive
      restarts, and append-only item-quality analytics. Nothing today stores a
      graded, reproducible assessment.
WHAT: adds assessment_items, assessment_item_versions, assessment_blueprints,
      assessment_sessions, assessment_attempts and assessment_item_quality
      (plus indexes) and records migration 9.0.0; verifies integrity and
      foreign keys. Purely additive — no existing table, column or row is
      altered. Historical tutor/learner/evidence data is untouched.
RISK: low — new tables + indexes only (no rebuild, no data rewrite).
MIGRATION PLAN: idempotent (CREATE TABLE/INDEX IF NOT EXISTS); runs V8 → V7 →
      V6 → V5 → V4 first, so curriculum/session foreign keys always resolve and
      the runner self-heals on first assessment use, exactly like V3–V8.
TEST PLAN: tests/test_assessment.py (fresh DB, idempotent rerun, existing-data
      preservation) + full regression suite.
ROLLBACK PLAN: drop the six new tables (no existing data is touched);
      optionally restore the pre-V9 snapshot backup.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from core.database.schema import (
        V9_SCHEMA_DDL,
        ASSESSMENT_ITEM_TYPES,
        ASSESSMENT_ITEM_STATUSES,
        ASSESSMENT_MODES,
        ASSESSMENT_SESSION_STATUSES,
        ASSESSMENT_SCOPE_TYPES,
    )
except ImportError:  # direct execution from a subdirectory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.database.schema import (
        V9_SCHEMA_DDL,
        ASSESSMENT_ITEM_TYPES,
        ASSESSMENT_ITEM_STATUSES,
        ASSESSMENT_MODES,
        ASSESSMENT_SESSION_STATUSES,
        ASSESSMENT_SCOPE_TYPES,
    )

V9_VERSION = "9.0.0"
MIGRATION_NAME = "v9_assessment_engine"

V9_TABLES = (
    "assessment_items",
    "assessment_item_versions",
    "assessment_blueprints",
    "assessment_sessions",
    "assessment_attempts",
    "assessment_item_quality",
)


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_assessment_v9(db_path: Path | str, create_backup: bool = False) -> Dict[str, Any]:
    """Bring the active database to the V9 assessment schema (idempotent).

    Runs the V8 tutor migration first (which runs V7 → V6 → V5 → V4 and the
    base V3 DDL), so assessment_items' foreign keys into curriculum_nodes always
    resolve.
    """
    from core.database.migrate_v8 import ensure_tutor_v8

    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    ensure_tutor_v8(target)

    con = sqlite3.connect(target)
    backup_file = None
    try:
        con.execute("PRAGMA foreign_keys=ON;")
        if create_backup:
            from core.database.migrate_v3 import create_snapshot_backup

            backup_file = create_snapshot_backup(
                target,
                target.parent.parent / "backups" / "medforge_pre_v9_backup.db",
            )

        con.executescript(V9_SCHEMA_DDL)

        existing = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = [t for t in V9_TABLES if t not in existing]
        if missing:
            raise RuntimeError(f"Missing expected V9 tables after migration: {missing}")

        con.execute(
            """INSERT INTO schema_migrations (version, name, applied_at)
               VALUES (?, ?, ?)
               ON CONFLICT(version) DO UPDATE SET applied_at=excluded.applied_at""",
            (V9_VERSION, MIGRATION_NAME, _utcnow()),
        )
        con.commit()

        integrity = con.execute("PRAGMA integrity_check;").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Database integrity compromised after V9: {integrity}")
        fk_errors = con.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign-key violations after V9: {fk_errors[:5]}")

        return {
            "status": "success",
            "version": V9_VERSION,
            "database": str(target),
            "backup": str(backup_file) if backup_file else None,
            "tables": list(V9_TABLES),
            "item_types": list(ASSESSMENT_ITEM_TYPES),
            "item_statuses": list(ASSESSMENT_ITEM_STATUSES),
            "modes": list(ASSESSMENT_MODES),
            "session_statuses": list(ASSESSMENT_SESSION_STATUSES),
            "scope_types": list(ASSESSMENT_SCOPE_TYPES),
            "integrity": integrity[0],
        }
    finally:
        con.close()


def is_v9_applied(db_path: Path | str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=?", (V9_VERSION,)
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
    parser = argparse.ArgumentParser(description="MedForge V9 assessment engine migration")
    parser.add_argument("--db", type=str, default=str(default_db))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(f"Applying V9 assessment engine migration on {args.db} ...")
    result = ensure_assessment_v9(args.db, create_backup=not args.no_backup)
    print("Migration finished:", result["status"], "| integrity:", result["integrity"])
    if result["backup"]:
        print("  Backup:", result["backup"])
    print("  Tables:", ", ".join(result["tables"]))
