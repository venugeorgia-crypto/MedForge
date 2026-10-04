#!/usr/bin/env python3
"""MedForge V4 Migration: canonical curriculum engine node types.

WHY:  the V3 curriculum_nodes table shipped with a CHECK constraint limited to
      the 360-ECTS enum (Year/Semester/Course/Module/Topic/Subtopic/Learning
      Objective) and zero application code — a dead table. The canonical
      curriculum engine needs Subject/Week/Seminar nodes.
WHAT: rebuilds curriculum_nodes with an extended CHECK (row-preserving copy),
      adds unique/index constraints for dedup and ordering, records migration
      4.0.0 in schema_migrations, and verifies integrity.
RISK: low — audited live DB holds 0 rows in curriculum_nodes; the rebuild
      copies rows verbatim and refuses to proceed if the copy loses any.
MIGRATION PLAN: idempotent; detection is by inspecting the table's CHECK sql.
TEST PLAN: tests/test_curriculum.py (old-CHECK rebuild, idempotence, fresh DB).
ROLLBACK PLAN: restore the pre-migration snapshot backup (migrate_v3 helpers);
      the DDL change is a constraint widening only — old rows stay valid.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from core.database.schema import (
        LEGACY_SCHEMA_DDL,
        V3_SCHEMA_DDL,
        V4_CURRICULUM_NODES_DDL,
        V4_SCHEMA_INDEX_DDL,
    )
except ImportError:  # direct execution from a subdirectory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.database.schema import (
        LEGACY_SCHEMA_DDL,
        V3_SCHEMA_DDL,
        V4_CURRICULUM_NODES_DDL,
        V4_SCHEMA_INDEX_DDL,
    )

V4_VERSION = "4.0.0"
MIGRATION_NAME = "v4_canonical_curriculum_engine"

_COPY_COLUMNS = (
    "id, parent_id, node_type, code, title, description, year, semester, "
    "ects_weight, order_index, created_at, updated_at"
)


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _table_sql(con: sqlite3.Connection, name: str) -> str:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row[0] if row and row[0] else ""


def _needs_check_rebuild(con: sqlite3.Connection) -> bool:
    """True when curriculum_nodes exists but its CHECK lacks the V4 types."""
    sql = _table_sql(con, "curriculum_nodes")
    if not sql:
        return False
    return "'Subject'" not in sql and "'Week'" not in sql


def ensure_curriculum_v4(db_path: Path | str, create_backup: bool = False) -> Dict[str, Any]:
    """Bring the active database to the V4 curriculum schema (idempotent).

    Self-healing by design: safe to call on every engine entry point, exactly
    like learner.ensure_v3_tables(). With create_backup=True a snapshot backup
    is taken before any rebuild (used by the explicit `migrate` CLI path).
    """
    from core.database.migrate_v3 import create_snapshot_backup

    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(target)
    backup_file = None
    try:
        # 1. Ensure the base schema exists (fresh databases).
        con.executescript(LEGACY_SCHEMA_DDL)
        con.executescript(V3_SCHEMA_DDL)
        con.commit()

        rows_before = 0
        if _needs_check_rebuild(con):
            rows_before = con.execute("SELECT count(*) FROM curriculum_nodes").fetchone()[0]
            if create_backup:
                backup_file = create_snapshot_backup(
                    target,
                    target.parent.parent / "backups" / "medforge_pre_v4_backup.db",
                )

            # Standard SQLite table-rebuild recipe (foreign_keys OFF).
            con.execute("PRAGMA foreign_keys=OFF;")
            try:
                con.executescript(
                    "DROP TABLE IF EXISTS curriculum_nodes_v4_tmp;"
                    + V4_CURRICULUM_NODES_DDL
                    + ";"
                )
                con.execute(
                    f"INSERT INTO curriculum_nodes_v4_tmp ({_COPY_COLUMNS})"
                    f" SELECT {_COPY_COLUMNS} FROM curriculum_nodes"
                )
                copied = con.execute(
                    "SELECT count(*) FROM curriculum_nodes_v4_tmp"
                ).fetchone()[0]
                if copied != rows_before:
                    raise RuntimeError(
                        f"V4 curriculum rebuild would lose rows: {rows_before} -> {copied};"
                        " aborted without committing."
                    )
                con.execute("DROP TABLE curriculum_nodes")
                con.execute(
                    "ALTER TABLE curriculum_nodes_v4_tmp RENAME TO curriculum_nodes"
                )
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.execute("PRAGMA foreign_keys=ON;")
        else:
            con.execute("PRAGMA foreign_keys=ON;")

        # 2. Indexes (idempotent).
        con.executescript(V4_SCHEMA_INDEX_DDL)

        # 3. Record the migration.
        con.execute(
            """INSERT INTO schema_migrations (version, name, applied_at)
               VALUES (?, ?, ?)
               ON CONFLICT(version) DO UPDATE SET applied_at=excluded.applied_at""",
            (V4_VERSION, MIGRATION_NAME, _utcnow()),
        )
        con.commit()

        # 4. Integrity verification.
        integrity = con.execute("PRAGMA integrity_check;").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Database integrity compromised after V4: {integrity}")

        probe = "SELECT count(*) FROM curriculum_nodes WHERE node_type='Subject'"
        con.execute(probe)  # raises sqlite3.IntegrityError if the CHECK is still old

        return {
            "status": "success",
            "version": V4_VERSION,
            "database": str(target),
            "backup": str(backup_file) if backup_file else None,
            "rows_copied": rows_before,
            "integrity": integrity[0],
        }
    finally:
        con.close()


def is_v4_applied(db_path: Path | str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=?", (V4_VERSION,)
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
    parser = argparse.ArgumentParser(description="MedForge V4 curriculum migration")
    parser.add_argument("--db", type=str, default=str(default_db))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(f"Applying V4 curriculum migration on {args.db} ...")
    result = ensure_curriculum_v4(args.db, create_backup=not args.no_backup)
    print("Migration finished:", result["status"], "| integrity:", result["integrity"])
    if result["backup"]:
        print("  Backup:", result["backup"])
