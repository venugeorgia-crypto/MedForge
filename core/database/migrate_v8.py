#!/usr/bin/env python3
"""MedForge V8 Migration: interactive adaptive tutor tables.

WHY:  P7 needs a persistent, restart-surviving tutor session (stage machine,
      pending answer, current question), an append-only turn transcript with
      idempotent learning-event linkage, and stable content-addressed question
      items. Nothing today stores an interactive teaching loop.
WHAT: adds tutor_sessions, tutor_turns and tutor_questions (plus indexes) and
      records migration 8.0.0; verifies integrity and foreign keys. Purely
      additive — no existing table, column or row is altered.
RISK: low — new tables + indexes only (no rebuild, no data rewrite).
MIGRATION PLAN: idempotent (CREATE TABLE/INDEX IF NOT EXISTS); runs V7 → V6 →
      V5 → V4 first so the curriculum/session foreign keys always resolve and
      the runner self-heals on first tutor use, exactly like V3–V7.
TEST PLAN: tests/test_tutor.py (fresh DB, idempotent rerun, existing-data
      preservation) + full regression suite.
ROLLBACK PLAN: drop the three new tables (no existing data is touched);
      optionally restore the pre-V8 snapshot backup.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from core.database.schema import (
        V8_SCHEMA_DDL,
        TUTOR_SESSION_MODES,
        TUTOR_STAGES,
        TUTOR_SESSION_STATUSES,
        TUTOR_QUESTION_TYPES,
    )
except ImportError:  # direct execution from a subdirectory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.database.schema import (
        V8_SCHEMA_DDL,
        TUTOR_SESSION_MODES,
        TUTOR_STAGES,
        TUTOR_SESSION_STATUSES,
        TUTOR_QUESTION_TYPES,
    )

V8_VERSION = "8.0.0"
MIGRATION_NAME = "v8_interactive_tutor"

V8_TABLES = (
    "tutor_sessions",
    "tutor_turns",
    "tutor_questions",
)


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_tutor_v8(db_path: Path | str, create_backup: bool = False) -> Dict[str, Any]:
    """Bring the active database to the V8 tutor schema (idempotent).

    Runs the V7 learner-model migration first (which runs V6 → V5 → V4 and the
    base V3 DDL), so tutor_sessions' foreign keys into curriculum_nodes always
    resolve.
    """
    from core.database.migrate_v7 import ensure_learner_model_v7

    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    ensure_learner_model_v7(target)

    con = sqlite3.connect(target)
    backup_file = None
    try:
        con.execute("PRAGMA foreign_keys=ON;")
        if create_backup:
            from core.database.migrate_v3 import create_snapshot_backup

            backup_file = create_snapshot_backup(
                target,
                target.parent.parent / "backups" / "medforge_pre_v8_backup.db",
            )

        con.executescript(V8_SCHEMA_DDL)

        existing = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = [t for t in V8_TABLES if t not in existing]
        if missing:
            raise RuntimeError(f"Missing expected V8 tables after migration: {missing}")

        con.execute(
            """INSERT INTO schema_migrations (version, name, applied_at)
               VALUES (?, ?, ?)
               ON CONFLICT(version) DO UPDATE SET applied_at=excluded.applied_at""",
            (V8_VERSION, MIGRATION_NAME, _utcnow()),
        )
        con.commit()

        integrity = con.execute("PRAGMA integrity_check;").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Database integrity compromised after V8: {integrity}")
        fk_errors = con.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign-key violations after V8: {fk_errors[:5]}")

        return {
            "status": "success",
            "version": V8_VERSION,
            "database": str(target),
            "backup": str(backup_file) if backup_file else None,
            "tables": list(V8_TABLES),
            "modes": list(TUTOR_SESSION_MODES),
            "stages": list(TUTOR_STAGES),
            "statuses": list(TUTOR_SESSION_STATUSES),
            "question_types": list(TUTOR_QUESTION_TYPES),
            "integrity": integrity[0],
        }
    finally:
        con.close()


def is_v8_applied(db_path: Path | str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=?", (V8_VERSION,)
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
    parser = argparse.ArgumentParser(description="MedForge V8 interactive tutor migration")
    parser.add_argument("--db", type=str, default=str(default_db))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(f"Applying V8 interactive tutor migration on {args.db} ...")
    result = ensure_tutor_v8(args.db, create_backup=not args.no_backup)
    print("Migration finished:", result["status"], "| integrity:", result["integrity"])
    if result["backup"]:
        print("  Backup:", result["backup"])
    print("  Tables:", ", ".join(result["tables"]))
