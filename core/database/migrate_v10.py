#!/usr/bin/env python3
"""MedForge V10 Migration: study-intelligence orchestration tables.

WHY:  P9 needs persistent, reproducible study plans, resumable study missions
      with explicit completion rules, an append-only action history, and a
      canonical content model whose rendered artifacts keep provenance and
      quality-gate status. Nothing today persists an orchestrated study
      workflow.
WHAT: adds study_plans, study_missions, study_actions, content_items and
      content_artifacts (plus indexes) and records migration 10.0.0; verifies
      integrity and foreign keys. Purely additive — no existing table, column
      or row is altered. All P2–P8 data is untouched.
RISK: low — new tables + indexes only (no rebuild, no data rewrite).
MIGRATION PLAN: idempotent (CREATE TABLE/INDEX IF NOT EXISTS); runs V9 → V8 →
      V7 → V6 → V5 → V4 first, so curriculum foreign keys always resolve and
      the runner self-heals on first study use, exactly like V3–V9.
TEST PLAN: tests/test_study_intelligence.py (fresh DB, idempotent rerun,
      existing-data preservation) + full regression suite.
ROLLBACK PLAN: drop the five new tables (no existing data is touched);
      optionally restore the pre-V10 snapshot backup.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from core.database.schema import (
        V10_SCHEMA_DDL,
        STUDY_PLAN_STATUSES,
        STUDY_MISSION_STATUSES,
        STUDY_ACTION_TYPES,
        STUDY_ACTION_STATUSES,
        STUDY_TOPIC_STATES,
        CONTENT_ARTIFACT_TYPES,
        CONTENT_ARTIFACT_STATUSES,
        ADAPTATION_PROFILES,
    )
except ImportError:  # direct execution from a subdirectory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.database.schema import (
        V10_SCHEMA_DDL,
        STUDY_PLAN_STATUSES,
        STUDY_MISSION_STATUSES,
        STUDY_ACTION_TYPES,
        STUDY_ACTION_STATUSES,
        STUDY_TOPIC_STATES,
        CONTENT_ARTIFACT_TYPES,
        CONTENT_ARTIFACT_STATUSES,
        ADAPTATION_PROFILES,
    )

V10_VERSION = "10.0.0"
MIGRATION_NAME = "v10_study_intelligence"

V10_TABLES = (
    "study_plans",
    "study_missions",
    "study_actions",
    "content_items",
    "content_artifacts",
)


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_study_v10(db_path: Path | str, create_backup: bool = False) -> Dict[str, Any]:
    """Bring the active database to the V10 study-intelligence schema (idempotent).

    Runs the V9 assessment migration first (which runs V8 → V7 → V6 → V5 → V4
    and the base V3 DDL), so curriculum_nodes foreign keys always resolve.
    """
    from core.database.migrate_v9 import ensure_assessment_v9

    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    ensure_assessment_v9(target)

    con = sqlite3.connect(target)
    backup_file = None
    try:
        con.execute("PRAGMA foreign_keys=ON;")
        if create_backup:
            from core.database.migrate_v3 import create_snapshot_backup

            backup_file = create_snapshot_backup(
                target,
                target.parent.parent / "backups" / "medforge_pre_v10_backup.db",
            )

        con.executescript(V10_SCHEMA_DDL)

        existing = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = [t for t in V10_TABLES if t not in existing]
        if missing:
            raise RuntimeError(f"Missing expected V10 tables after migration: {missing}")

        con.execute(
            """INSERT INTO schema_migrations (version, name, applied_at)
               VALUES (?, ?, ?)
               ON CONFLICT(version) DO UPDATE SET applied_at=excluded.applied_at""",
            (V10_VERSION, MIGRATION_NAME, _utcnow()),
        )
        con.commit()

        integrity = con.execute("PRAGMA integrity_check;").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Database integrity compromised after V10: {integrity}")
        fk_errors = con.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign-key violations after V10: {fk_errors[:5]}")

        return {
            "status": "success",
            "version": V10_VERSION,
            "database": str(target),
            "backup": str(backup_file) if backup_file else None,
            "tables": list(V10_TABLES),
            "action_types": list(STUDY_ACTION_TYPES),
            "topic_states": list(STUDY_TOPIC_STATES),
            "artifact_statuses": list(CONTENT_ARTIFACT_STATUSES),
            "integrity": integrity[0],
        }
    finally:
        con.close()


def is_v10_applied(db_path: Path | str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=?", (V10_VERSION,)
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
    parser = argparse.ArgumentParser(description="MedForge V10 study-intelligence migration")
    parser.add_argument("--db", type=str, default=str(default_db))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(f"Applying V10 study-intelligence migration on {args.db} ...")
    result = ensure_study_v10(args.db, create_backup=not args.no_backup)
    print("Migration finished:", result["status"], "| integrity:", result["integrity"])
    if result["backup"]:
        print("  Backup:", result["backup"])
    print("  Tables:", ", ".join(result["tables"]))
