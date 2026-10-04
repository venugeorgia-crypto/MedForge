#!/usr/bin/env python3
"""MedForge V6 Migration: evidence graph tables.

WHY:  P4 turns generated text containing [S#] labels into explicit claim
      records with inspectable evidence relationships and an append-only
      verification history; today no such records exist anywhere.
WHAT: adds claims, evidence, claim_evidence and verification_runs (plus
      indexes), records migration 6.0.0, verifies integrity. Purely additive.
RISK: low — new tables only; no existing table is altered or rebuilt.
MIGRATION PLAN: idempotent (CREATE TABLE/INDEX IF NOT EXISTS); runs the V5
      textbook migration first (claims reference curriculum_nodes, evidence
      references textbook_editions/textbook_nodes) and self-heals on first use,
      exactly like the V3/V4/V5 runners.
TEST PLAN: tests/test_evidence.py (fresh DB, populated DB preservation,
      idempotent rerun) + full regression suite.
ROLLBACK PLAN: drop the four new tables (no existing data is touched);
      optionally restore the pre-V6 snapshot backup.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from core.database.schema import (
        V6_SCHEMA_DDL,
        CLAIM_TYPES,
        CLAIM_VERIFICATION_STATUS,
        CLAIM_REVIEW_STATUS,
        EVIDENCE_TYPES,
        CLAIM_EVIDENCE_RELATIONSHIPS,
        VERIFICATION_RESULTS,
    )
except ImportError:  # direct execution from a subdirectory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.database.schema import (
        V6_SCHEMA_DDL,
        CLAIM_TYPES,
        CLAIM_VERIFICATION_STATUS,
        CLAIM_REVIEW_STATUS,
        EVIDENCE_TYPES,
        CLAIM_EVIDENCE_RELATIONSHIPS,
        VERIFICATION_RESULTS,
    )

V6_VERSION = "6.0.0"
MIGRATION_NAME = "v6_evidence_graph"

V6_TABLES = (
    "claims",
    "evidence",
    "claim_evidence",
    "verification_runs",
)


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_evidence_v6(db_path: Path | str, create_backup: bool = False) -> Dict[str, Any]:
    """Bring the active database to the V6 evidence schema (idempotent).

    Runs the V5 textbook migration first so claims/evidence always have their
    referenced curriculum and textbook-provenance tables, then applies the
    additive V6 DDL.
    """
    from core.database.migrate_v5 import ensure_textbook_v5

    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    # V5 first (idempotent, self-healing) — textbook_editions/node and
    # curriculum_nodes must exist for the V6 foreign keys.
    ensure_textbook_v5(target)

    con = sqlite3.connect(target)
    backup_file = None
    try:
        con.execute("PRAGMA foreign_keys=ON;")
        if create_backup:
            from core.database.migrate_v3 import create_snapshot_backup

            backup_file = create_snapshot_backup(
                target,
                target.parent.parent / "backups" / "medforge_pre_v6_backup.db",
            )

        con.executescript(V6_SCHEMA_DDL)

        # Verify every expected table exists post-DDL.
        existing = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = [t for t in V6_TABLES if t not in existing]
        if missing:
            raise RuntimeError(f"Missing expected V6 tables after migration: {missing}")

        con.execute(
            """INSERT INTO schema_migrations (version, name, applied_at)
               VALUES (?, ?, ?)
               ON CONFLICT(version) DO UPDATE SET applied_at=excluded.applied_at""",
            (V6_VERSION, MIGRATION_NAME, _utcnow()),
        )
        con.commit()

        integrity = con.execute("PRAGMA integrity_check;").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Database integrity compromised after V6: {integrity}")

        return {
            "status": "success",
            "version": V6_VERSION,
            "database": str(target),
            "backup": str(backup_file) if backup_file else None,
            "tables": list(V6_TABLES),
            "integrity": integrity[0],
        }
    finally:
        con.close()


def is_v6_applied(db_path: Path | str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=?", (V6_VERSION,)
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
    parser = argparse.ArgumentParser(description="MedForge V6 evidence graph migration")
    parser.add_argument("--db", type=str, default=str(default_db))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(f"Applying V6 evidence graph migration on {args.db} ...")
    result = ensure_evidence_v6(args.db, create_backup=not args.no_backup)
    print("Migration finished:", result["status"], "| integrity:", result["integrity"])
    if result["backup"]:
        print("  Backup:", result["backup"])
