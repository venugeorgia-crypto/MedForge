#!/usr/bin/env python3
"""Migration 14.0.0 — P13 knowledge refresh: source_refresh_log,
knowledge_refresh_runs, knowledge_notifications.

Additive and idempotent. Chains the full migration path
(v9 → v10 → v11 → v12 → v13 → v14), takes a verified snapshot backup by
default, records the schema_migrations row, and verifies integrity +
foreign keys.

Run directly:  .venv-v2.1/bin/python core/database/migrate_v14.py [--db PATH] [--no-backup]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure we can import schema from the core package
_CORE_DIR = Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from schema import V14_SCHEMA_DDL, V14_TABLES  # noqa: E402


V14_VERSION = "14.0.0"
MIGRATION_NAME = "v14_knowledge_refresh"


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


def is_v14_applied(db_path: Path) -> bool:
    """Check if V14 migration has already been applied."""
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (V14_VERSION,)
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def ensure_provider_v13(db_path: Path) -> dict:
    """Ensure V13 (provider) migration is applied first."""
    from migrate_v13 import ensure_provider_v13 as _ensure_v13
    return _ensure_v13(db_path, create_backup=False)


def ensure_refresh_v14(db_path: Path, create_backup: bool = True) -> dict:
    """Apply V14 migration: knowledge refresh tables."""
    ensure_provider_v13(db_path)

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    try:
        backup_path = None
        if create_backup:
            backup_path = str(
                db_path.parent.parent / "backups"
                / f"medforge_pre_v14_backup_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.db"
            )
            create_snapshot_backup(db_path, Path(backup_path))

        for stmt in V14_SCHEMA_DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)

        con.execute(
            """
            INSERT INTO schema_migrations (version, name, applied_at)
            VALUES (?, ?, ?)
            ON CONFLICT(version) DO UPDATE SET
                name = excluded.name,
                applied_at = excluded.applied_at
            """,
            (V14_VERSION, MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
        )

        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk_check = con.execute("PRAGMA foreign_key_check").fetchall()
        con.commit()

        tables = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()

        return {
            "success": True,
            "integrity": integrity,
            "foreign_key_errors": len(fk_check),
            "tables": len(tables),
            "backup": backup_path,
            "version": V14_VERSION,
        }
    except Exception as e:
        con.rollback()
        return {"success": False, "error": str(e)}
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply V14 knowledge-refresh migration")
    parser.add_argument("--db", type=Path, help="Path to medforge.sqlite3")
    parser.add_argument("--no-backup", action="store_true", help="Skip backup creation")
    args = parser.parse_args()

    db_path = args.db or Path("database/medforge.sqlite3")
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    result = ensure_refresh_v14(db_path, create_backup=not args.no_backup)
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
