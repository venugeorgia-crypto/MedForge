#!/usr/bin/env python3
"""Migration 15.0.0 — P14 automation: automation_jobs + automation_runs.

Additive and idempotent. Chains the full migration path
(v9 → v10 → v11 → v12 → v13 → v14 → v15), takes a verified snapshot backup by
default, records the schema_migrations row, and verifies integrity + foreign
keys.

Run directly:  .venv-v2.1/bin/python core/database/migrate_v15.py [--db PATH] [--no-backup]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from schema import V15_SCHEMA_DDL, V15_TABLES  # noqa: E402


V15_VERSION = "15.0.0"
MIGRATION_NAME = "v15_automation"


def create_snapshot_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(target)
    try:
        src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()


def is_v15_applied(db_path: Path) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (V15_VERSION,)
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def ensure_refresh_v14(db_path: Path) -> dict:
    from migrate_v14 import ensure_refresh_v14 as _ensure_v14
    return _ensure_v14(db_path, create_backup=False)


def ensure_automation_v15(db_path: Path, create_backup: bool = True) -> dict:
    """Apply V15 migration: automation jobs + runs."""
    ensure_refresh_v14(db_path)

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    try:
        backup_path = None
        if create_backup:
            backup_path = str(
                db_path.parent.parent / "backups"
                / f"medforge_pre_v15_backup_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.db"
            )
            create_snapshot_backup(db_path, Path(backup_path))

        # executescript handles comments/embedded semicolons correctly (a naive
        # split(';') breaks on a ';' inside a '--' comment).
        con.executescript(V15_SCHEMA_DDL)

        con.execute(
            """
            INSERT INTO schema_migrations (version, name, applied_at)
            VALUES (?, ?, ?)
            ON CONFLICT(version) DO UPDATE SET
                name = excluded.name,
                applied_at = excluded.applied_at
            """,
            (V15_VERSION, MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
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
            "version": V15_VERSION,
        }
    except Exception as e:
        con.rollback()
        return {"success": False, "error": str(e)}
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply V15 automation migration")
    parser.add_argument("--db", type=Path, help="Path to medforge.sqlite3")
    parser.add_argument("--no-backup", action="store_true", help="Skip backup creation")
    args = parser.parse_args()

    db_path = args.db or Path("database/medforge.sqlite3")
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    result = ensure_automation_v15(db_path, create_backup=not args.no_backup)
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
