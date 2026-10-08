#!/usr/bin/env python3
"""MedForge V12 Migration: P11 video production (video_renders table).

WHY:  P11 renders real narrated MP4s per aspect; every render attempt must be
      persistent, auditable and restart-safe.
WHAT: adds video_renders (video_id PK, content_id FK CASCADE, aspect, status,
      path, manifest_path, duration_s, width, height, size_bytes, scene_count,
      renderer_version, storyboard_checksum, error, created_at, updated_at)
      plus content/status indexes.
RISK: low — one new table only; no existing row is touched.
MIGRATION PLAN: idempotent (IF NOT EXISTS). Runs ensure_publication_v11 first
      so the content_items foreign key resolves, mirroring V3–V11.
TEST PLAN: tests/test_video.py (fresh DB, idempotent rerun) + full regression.
ROLLBACK PLAN: drop video_renders (existing data untouched).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from core.database.schema import V12_SCHEMA_DDL
except ImportError:  # direct execution from a subdirectory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.database.schema import V12_SCHEMA_DDL

V12_VERSION = "12.0.0"
MIGRATION_NAME = "v12_video_production"

V12_TABLES = ("video_renders",)


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_video_v12(db_path: Path | str, create_backup: bool = False) -> Dict[str, Any]:
    """Bring the database to the V12 video schema (idempotent, additive)."""
    from core.database.migrate_v11 import ensure_publication_v11

    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    # V11 first: video_renders references content_items.
    ensure_publication_v11(target)

    con = sqlite3.connect(target)
    backup_file = None
    try:
        con.execute("PRAGMA foreign_keys=ON;")
        if create_backup:
            from core.database.migrate_v3 import create_snapshot_backup

            backup_file = create_snapshot_backup(
                target, target.parent.parent / "backups" / "medforge_pre_v12_backup.db"
            )

        con.executescript(V12_SCHEMA_DDL)

        con.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?,?,?)"
            " ON CONFLICT(version) DO UPDATE SET name=excluded.name",
            (V12_VERSION, MIGRATION_NAME, _utcnow()),
        )
        con.commit()

        existing = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = [t for t in V12_TABLES if t not in existing]
        if missing:
            raise RuntimeError(f"Missing expected V12 tables after migration: {missing}")

        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok":
            raise RuntimeError(f"integrity_check failed: {integrity}")
        if fk_violations:
            raise RuntimeError(f"foreign_key_check violations: {fk_violations[:5]}")
    finally:
        con.close()

    return {
        "status": "success",
        "version": V12_VERSION,
        "name": MIGRATION_NAME,
        "tables": list(V12_TABLES),
        "integrity": integrity,
        "fk_violations": len(fk_violations),
        "backup": str(backup_file) if backup_file else None,
        "db_path": str(target),
    }


def is_v12_applied(db_path: Path | str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=?", (V12_VERSION,)
        ).fetchone()
        return bool(row and row[0] > 0)
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


if __name__ == "__main__":
    import argparse
    import os

    default_db = (
        Path(os.environ.get("MEDFORGE_HOME", str(Path.home() / "MedForge")))
        .expanduser()
        .resolve()
        / "database"
        / "medforge.sqlite3"
    )
    parser = argparse.ArgumentParser(description="MedForge V12 video migration")
    parser.add_argument("--db", type=str, default=str(default_db))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(f"Applying V12 video migration on {args.db} ...")
    result = ensure_video_v12(args.db, create_backup=not args.no_backup)
    print("Migration finished:", result["status"], "| integrity:", result["integrity"])
    if result["backup"]:
        print("  Backup:", result["backup"])
