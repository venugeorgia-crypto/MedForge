#!/usr/bin/env python3
"""MedForge V3 Migration Runner.

Performs non-destructive migration from MedForge 2.1 to MedForge V3:
- Creates verified snapshot backup before any changes
- Preserves existing data (chunks, study_sessions, weaknesses)
- Installs V3 Learner & Curriculum schemas:
  * curriculum_nodes (360 ECTS hierarchy)
  * prerequisites (topic dependency graph)
  * learner_mastery (mastery scores, confidence, attempts)
  * learner_weaknesses (misconceptions, errors, severity, resolved)
  * interactive_sessions (5 session types, scores, durations)
  * spaced_repetition_queue (SM-2 / FSRS scheduling)
  * medical_sources (evidence provenance with human vs animal study flag)
- Records migration history in schema_migrations
- Runs SQLite integrity verification
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, Any, Tuple

# Support direct execution and package imports
try:
    from core.database.schema import V3_SCHEMA_DDL, LEGACY_SCHEMA_DDL
except ImportError:
    # If run directly or from subdirectory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.database.schema import V3_SCHEMA_DDL, LEGACY_SCHEMA_DDL

V3_VERSION = "3.0.0"
MIGRATION_NAME = "v3_phase1_learner_curriculum_database"

DEFAULT_BASE = Path(os.environ.get("MEDFORGE_HOME", str(Path.home() / "MedForge"))).expanduser().resolve()
DEFAULT_DB = DEFAULT_BASE / "database" / "medforge.sqlite3"
DEFAULT_BACKUP = DEFAULT_BASE / "backups" / "medforge_v2.1_backup.db"

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def create_snapshot_backup(src_path: Path, backup_path: Path | None = None) -> Path:
    """Create a verified snapshot backup using SQLite backup API."""
    src_path = Path(src_path).resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"Database not found at {src_path}")

    if backup_path is None:
        backup_path = src_path.parent.parent / "backups" / "medforge_v2.1_backup.db"
    backup_path = Path(backup_path).resolve()
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    src_conn = sqlite3.connect(src_path)
    bck_conn = sqlite3.connect(backup_path)
    try:
        src_conn.backup(bck_conn)
    finally:
        bck_conn.close()
        src_conn.close()

    # Integrity verification of backup
    chk_conn = sqlite3.connect(backup_path)
    try:
        res = chk_conn.execute("PRAGMA integrity_check;").fetchone()
        if not res or res[0] != "ok":
            raise RuntimeError(f"Integrity check failed on backup {backup_path}: {res}")
    finally:
        chk_conn.close()

    return backup_path

def get_table_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Retrieve row counts for existing tables (skipping internal FTS shadow tables)."""
    cur = conn.cursor()
    tables = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'chunks_fts%'"
    ).fetchall()
    counts = {}
    for (tbl,) in tables:
        try:
            cnt = cur.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
            counts[tbl] = cnt
        except sqlite3.OperationalError:
            pass
    return counts

def is_v3_migrated(conn: sqlite3.Connection) -> bool:
    """Check if V3 migration has already been recorded."""
    cur = conn.cursor()
    has_migration_table = cur.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()[0]
    if not has_migration_table:
        return False
    row = cur.execute(
        "SELECT count(*) FROM schema_migrations WHERE version=?", (V3_VERSION,)
    ).fetchone()
    return bool(row and row[0] > 0)

def migrate_database(db_path: Path | str | None = None, create_backup: bool = True) -> Dict[str, Any]:
    """Execute non-destructive migration to MedForge V3.
    
    Returns a status dict containing before/after row counts, verification result,
    and backup path.
    """
    target_db = Path(db_path or DEFAULT_DB).resolve()
    target_db.parent.mkdir(parents=True, exist_ok=True)

    backup_file = None
    counts_before: Dict[str, int] = {}

    if target_db.exists() and target_db.stat().st_size > 0:
        if create_backup:
            backup_file = create_snapshot_backup(target_db)

        # Record pre-migration counts
        pre_conn = sqlite3.connect(target_db)
        try:
            counts_before = get_table_counts(pre_conn)
        finally:
            pre_conn.close()
    else:
        # If DB doesn't exist yet, ensure parent directory and create legacy tables first
        conn = sqlite3.connect(target_db)
        conn.executescript(LEGACY_SCHEMA_DDL)
        conn.commit()
        conn.close()

    conn = sqlite3.connect(target_db)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        # Apply V3 Schema
        conn.executescript(V3_SCHEMA_DDL)

        # Record migration
        now = utcnow()
        conn.execute(
            """INSERT INTO schema_migrations (version, name, applied_at)
               VALUES (?, ?, ?)
               ON CONFLICT(version) DO UPDATE SET applied_at=excluded.applied_at""",
            (V3_VERSION, MIGRATION_NAME, now),
        )
        conn.commit()

        # Validate integrity
        integrity = conn.execute("PRAGMA integrity_check;").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Database integrity compromised: {integrity}")

        # Post-migration counts
        counts_after = get_table_counts(conn)

        # Verify no data loss in pre-existing tables
        for tbl, count in counts_before.items():
            if tbl in counts_after and counts_after[tbl] < count:
                raise RuntimeError(
                    f"Data loss detected in table '{tbl}': {count} -> {counts_after[tbl]}"
                )

        # Verify new V3 tables exist
        required_v3_tables = [
            "curriculum_nodes",
            "prerequisites",
            "learner_mastery",
            "learner_weaknesses",
            "interactive_sessions",
            "spaced_repetition_queue",
            "medical_sources",
            "schema_migrations",
        ]
        existing_tables = set(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        )
        missing = [t for t in required_v3_tables if t not in existing_tables]
        if missing:
            raise RuntimeError(f"Missing expected V3 tables after migration: {missing}")

        return {
            "status": "success",
            "database": str(target_db),
            "backup": str(backup_file) if backup_file else None,
            "version": V3_VERSION,
            "applied_at": now,
            "integrity": integrity[0],
            "counts_before": counts_before,
            "counts_after": counts_after,
            "v3_tables": required_v3_tables,
        }
    finally:
        conn.close()

def rollback_migration(db_path: Path | str, backup_path: Path | str) -> bool:
    """Restore database directly from a verified backup snapshot."""
    import shutil
    db_p = Path(db_path).resolve()
    bk_p = Path(backup_path).resolve()
    if not bk_p.exists():
        raise FileNotFoundError(f"Backup file does not exist: {bk_p}")

    chk = sqlite3.connect(bk_p)
    res = chk.execute("PRAGMA integrity_check;").fetchone()
    chk.close()
    if not res or res[0] != "ok":
        raise RuntimeError("Backup file failed integrity check; refusing to restore.")

    shutil.copy2(bk_p, db_p)
    return True

def main():
    parser = argparse.ArgumentParser(description="MedForge V3 Schema Migration Runner")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB), help="Path to SQLite database")
    parser.add_argument("--no-backup", action="store_true", help="Skip creating backup")
    parser.add_argument("--verify", action="store_true", help="Verify existing V3 migration status")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if args.verify:
        if not db_path.exists():
            print(f"Database does not exist at {db_path}")
            sys.exit(1)
        conn = sqlite3.connect(db_path)
        try:
            migrated = is_v3_migrated(conn)
            tables = get_table_counts(conn)
            print(f"Database: {db_path}")
            print(f"V3 Migration applied: {migrated}")
            print("Tables & Counts:")
            for t, c in sorted(tables.items()):
                print(f"  - {t}: {c} rows")
        finally:
            conn.close()
        return

    print(f"Starting MedForge V3 migration on {db_path}...")
    result = migrate_database(db_path, create_backup=not args.no_backup)
    print("Migration finished successfully!")
    print(f"  Backup: {result['backup']}")
    print(f"  Integrity: {result['integrity']}")
    print(f"  V3 Version: {result['version']}")
    print("  Table counts:")
    for t, c in sorted(result["counts_after"].items()):
        print(f"    {t}: {c} rows")

if __name__ == "__main__":
    main()
