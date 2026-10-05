#!/usr/bin/env python3
"""MedForge V7 Migration: recency-weighted learner model tables.

WHY:  P6 needs append-only learning-performance events and a materialized,
      versioned, recency-weighted learner state; today mastery is only an
      incremental mean on learner_mastery (no recency, uncertainty or
      calibration), and weaknesses lack score/failure-rate/recovery fields.
WHAT: adds learning_attempts and learner_model_state (plus indexes) and
      additively extends learner_weaknesses with analysis columns; records
      migration 7.0.0; verifies integrity. Existing tables/rows untouched.
RISK: low — new tables + ADD COLUMN only (no rebuild, no data rewrite).
MIGRATION PLAN: idempotent (CREATE TABLE/INDEX IF NOT EXISTS; ADD COLUMN
      guarded by PRAGMA table_info); runs V6 → V5 → V4 first so foreign keys
      (interactive_sessions, curriculum_nodes) always resolve and self-heals
      on first learner use, exactly like the V3–V6 runners.
TEST PLAN: tests/test_learner_model.py (fresh DB, populated DB preservation,
      idempotent rerun, column addition) + full regression suite.
ROLLBACK PLAN: drop the two new tables and ignore/leave the additive weakness
      columns (no existing data is touched); optionally restore the pre-V7
      snapshot backup.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

try:
    from core.database.schema import (
        V7_SCHEMA_DDL,
        V7_WEAKNESS_COLUMNS,
        LEARNING_ATTEMPT_ITEM_TYPES,
        LEARNING_ATTEMPT_SOURCES,
        LEARNER_WEAKNESS_ORIGINS,
    )
except ImportError:  # direct execution from a subdirectory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.database.schema import (
        V7_SCHEMA_DDL,
        V7_WEAKNESS_COLUMNS,
        LEARNING_ATTEMPT_ITEM_TYPES,
        LEARNING_ATTEMPT_SOURCES,
        LEARNER_WEAKNESS_ORIGINS,
    )

V7_VERSION = "7.0.0"
MIGRATION_NAME = "v7_recency_weighted_learner"

V7_TABLES = (
    "learning_attempts",
    "learner_model_state",
)


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_weakness_columns(con: sqlite3.Connection) -> List[str]:
    """ADD COLUMN the V7 weakness analysis fields that are missing (idempotent).

    ALTER TABLE ADD COLUMN never rewrites existing rows: legacy weaknesses keep
    their data and read back the documented defaults.
    """
    existing = {row[1] for row in con.execute("PRAGMA table_info(learner_weaknesses)")}
    added: List[str] = []
    for name, ddl in V7_WEAKNESS_COLUMNS:
        if name not in existing:
            con.execute(f"ALTER TABLE learner_weaknesses ADD COLUMN {name} {ddl}")
            added.append(name)
    return added


def ensure_learner_model_v7(db_path: Path | str, create_backup: bool = False) -> Dict[str, Any]:
    """Bring the active database to the V7 learner-model schema (idempotent).

    Runs the V6 evidence migration first (which runs V5 → V4 and the base V3
    DDL), so learning_attempts' foreign keys into interactive_sessions and
    curriculum_nodes always resolve, then applies the additive V7 DDL and the
    guarded weakness-column additions.
    """
    from core.database.migrate_v6 import ensure_evidence_v6

    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    ensure_evidence_v6(target)

    con = sqlite3.connect(target)
    backup_file = None
    try:
        con.execute("PRAGMA foreign_keys=ON;")
        if create_backup:
            from core.database.migrate_v3 import create_snapshot_backup

            backup_file = create_snapshot_backup(
                target,
                target.parent.parent / "backups" / "medforge_pre_v7_backup.db",
            )

        con.executescript(V7_SCHEMA_DDL)
        added_columns = ensure_weakness_columns(con)

        # Verify every expected table exists post-DDL.
        existing = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = [t for t in V7_TABLES if t not in existing]
        if missing:
            raise RuntimeError(f"Missing expected V7 tables after migration: {missing}")

        # Verify every expected weakness column exists post-ALTER.
        columns = {row[1] for row in con.execute("PRAGMA table_info(learner_weaknesses)")}
        missing_cols = [n for n, _ddl in V7_WEAKNESS_COLUMNS if n not in columns]
        if missing_cols:
            raise RuntimeError(
                f"Missing expected V7 weakness columns after migration: {missing_cols}"
            )

        con.execute(
            """INSERT INTO schema_migrations (version, name, applied_at)
               VALUES (?, ?, ?)
               ON CONFLICT(version) DO UPDATE SET applied_at=excluded.applied_at""",
            (V7_VERSION, MIGRATION_NAME, _utcnow()),
        )
        con.commit()

        integrity = con.execute("PRAGMA integrity_check;").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Database integrity compromised after V7: {integrity}")
        fk_errors = con.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign-key violations after V7: {fk_errors[:5]}")

        return {
            "status": "success",
            "version": V7_VERSION,
            "database": str(target),
            "backup": str(backup_file) if backup_file else None,
            "tables": list(V7_TABLES),
            "weakness_columns_added": added_columns,
            "integrity": integrity[0],
        }
    finally:
        con.close()


def is_v7_applied(db_path: Path | str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=?", (V7_VERSION,)
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
    parser = argparse.ArgumentParser(description="MedForge V7 learner-model migration")
    parser.add_argument("--db", type=str, default=str(default_db))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(f"Applying V7 recency-weighted learner migration on {args.db} ...")
    result = ensure_learner_model_v7(args.db, create_backup=not args.no_backup)
    print("Migration finished:", result["status"], "| integrity:", result["integrity"])
    if result["backup"]:
        print("  Backup:", result["backup"])
    if result["weakness_columns_added"]:
        print("  Added weakness columns:", ", ".join(result["weakness_columns_added"]))
