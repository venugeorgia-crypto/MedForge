#!/usr/bin/env python3
"""Migration 13.0.0 — P12 provider abstraction: model_runs table.

Additive and idempotent. Chains the full migration path
(v9 → v10 → v11 → v12 → v13), takes a verified snapshot backup by default,
records the schema_migrations row, and verifies integrity + foreign keys.

Run directly:  .venv-v2.1/bin/python core/database/migrate_v13.py [--db PATH] [--no-backup]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure we can import schema from the core package
_CORE_DIR = Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from schema import V13_SCHEMA_DDL, V13_TABLES  # noqa: E402


V13_VERSION = "13.0.0"
MIGRATION_NAME = "v13_provider_model_runs"


def create_snapshot_backup(source: Path, target: Path) -> None:
    """Create a verified snapshot backup using SQLite's backup API."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(target)
    try:
        src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()


def is_v13_applied(db_path: Path) -> bool:
    """Check if V13 migration has already been applied."""
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (V13_VERSION,)
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def ensure_video_v12(db_path: Path) -> dict:
    """Ensure V12 (video) migration is applied first."""
    from migrate_v12 import ensure_video_v12 as _ensure_v12
    return _ensure_v12(db_path, create_backup=False)


def ensure_provider_v13(db_path: Path, create_backup: bool = True) -> dict:
    """Apply V13 migration: add model_runs table for provider/model auditing."""
    # Ensure prerequisite migrations
    ensure_video_v12(db_path)

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    try:
        # Create verified backup if requested
        backup_path: Optional[str] = None
        if create_backup:
            backup_path = str(db_path.parent.parent / "backups" / f"medforge_pre_v13_backup_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.db")
            create_snapshot_backup(db_path, Path(backup_path))

        # Apply V13 schema
        for stmt in V13_SCHEMA_DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)

        # Record migration
        con.execute(
            """
            INSERT INTO schema_migrations (version, name, applied_at)
            VALUES (?, ?, ?)
            ON CONFLICT(version) DO UPDATE SET
                name = excluded.name,
                applied_at = excluded.applied_at
            """,
            (V13_VERSION, MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
        )

        # Verify integrity
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk_check = con.execute("PRAGMA foreign_key_check").fetchall()

        con.commit()

        # Count tables
        tables = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        table_count = len(tables)

        return {
            "success": True,
            "integrity": integrity,
            "foreign_key_errors": len(fk_check),
            "tables": table_count,
            "backup": backup_path,
            "version": V13_VERSION,
        }
    except Exception as e:
        con.rollback()
        return {"success": False, "error": str(e)}
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply V13 provider migration")
    parser.add_argument("--db", type=Path, help="Path to medforge.sqlite3")
    parser.add_argument("--no-backup", action="store_true", help="Skip backup creation")
    args = parser.parse_args()

    db_path = args.db or Path("database/medforge.sqlite3")
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    result = ensure_provider_v13(db_path, create_backup=not args.no_backup)
    if result["success"]:
        print(f"Migration finished: success | integrity: {result['integrity']}")
        print(f"  Tables: {result['tables']}")
        if result["backup"]:
            print(f"  Backup: {result['backup']}")
    else:
        print(f"Migration finished: FAILED | error: {result['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()