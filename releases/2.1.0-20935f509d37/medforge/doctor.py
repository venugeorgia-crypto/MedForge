"""P15 — Diagnostics, backup verification, restore and repair (p15-doctor-v1).

One trustworthy place to answer: "is this install healthy, and if not, what
exactly is wrong?" Nothing here invents success: every check reports the raw
evidence it used (PRAGMA results, file sizes, versions, exit codes).

    diagnose()        full health report (schema, integrity, disk, backups,
                      provider, migrations pending)
    verify_backup()   is this file a usable MedForge database?
    restore_backup()  restore a verified backup, keeping a pre-restore copy
    repair()          quick_check + FTS rebuild (+ optional VACUUM)
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import medforge.types as T
from medforge.utils import mkdirs, utcnow

DOCTOR_VERSION = "p15-doctor-v1"

# The migrations this build knows how to apply, in order.
EXPECTED_MIGRATIONS = (
    "3.0.0", "4.0.0", "5.0.0", "6.0.0", "7.0.0", "8.0.0", "9.0.0",
    "10.0.0", "11.0.0", "12.0.0", "13.0.0", "14.0.0", "15.0.0",
)


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or T.META_DB)
    con.row_factory = sqlite3.Row
    return con


def _integrity(con: sqlite3.Connection) -> Dict[str, Any]:
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    return {"integrity": integrity, "integrity_ok": integrity == "ok",
            "foreign_key_violations": len(fk)}


def _version_key(version: str) -> tuple:
    """Numeric ordering — lexicographic sorting puts '9.0.0' after '15.0.0'."""
    parts = []
    for piece in str(version).split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def applied_migrations(db: Optional[Path] = None) -> List[str]:
    con = _connect(db)
    try:
        try:
            rows = con.execute("SELECT version FROM schema_migrations").fetchall()
        except sqlite3.OperationalError:
            return []
        return sorted((r[0] for r in rows), key=_version_key)
    finally:
        con.close()


def baseline_v3_present(db: Optional[Path] = None) -> bool:
    """V3 is the baseline schema: homes created by the chained newer runners
    have its tables but may never have written an explicit `3.0.0` row."""
    con = _connect(db)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()
    return {"schema_migrations", "chunks", "curriculum_nodes"} <= names


def pending_migrations(db: Optional[Path] = None) -> List[str]:
    applied = set(applied_migrations(db))
    missing = [v for v in EXPECTED_MIGRATIONS if v not in applied]
    if "3.0.0" in missing and baseline_v3_present(db):
        missing = [v for v in missing if v != "3.0.0"]
    return missing


def verify_backup(path: Path | str) -> Dict[str, Any]:
    """Verify a backup file is a readable, intact MedForge SQLite database."""
    p = Path(path).expanduser()
    result: Dict[str, Any] = {"path": str(p), "exists": p.is_file()}
    if not p.is_file():
        result.update({"ok": False, "reason": "file not found"})
        return result
    result["size_bytes"] = p.stat().st_size
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            check = _integrity(con)
            tables = con.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'").fetchone()[0]
            try:
                versions = sorted(
                    (r[0] for r in con.execute("SELECT version FROM schema_migrations")),
                    key=_version_key)
            except sqlite3.OperationalError:
                versions = []
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        result.update({"ok": False, "reason": f"not a SQLite database: {exc}"})
        return result
    result.update({
        "ok": check["integrity_ok"],
        "integrity": check["integrity"],
        "tables": tables,
        "schema_versions": versions,
        "latest_version": versions[-1] if versions else None,
    })
    if not check["integrity_ok"]:
        result["reason"] = "integrity check failed"
    return result


def restore_backup(
    backup_path: Path | str,
    target_db: Optional[Path | str] = None,
    make_safety_copy: bool = True,
) -> Dict[str, Any]:
    """Restore a verified backup over the live database.

    Refuses unverified files. Unless disabled, the current database is first
    copied to `backups/medforge_prerestore_<timestamp>.db` so a mistaken
    restore can itself be undone.
    """
    src = Path(backup_path).expanduser()
    target = Path(target_db).expanduser() if target_db else T.META_DB
    verification = verify_backup(src)
    if not verification.get("ok"):
        raise ValueError(
            f"Backup is not restorable: {verification.get('reason', 'verification failed')}"
        )

    safety: Optional[str] = None
    if make_safety_copy and target.is_file():
        mkdirs()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safety_path = T.BASE / "backups" / f"medforge_prerestore_{stamp}.db"
        safety_path.parent.mkdir(parents=True, exist_ok=True)
        src_con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        dst_con = sqlite3.connect(safety_path)
        try:
            src_con.backup(dst_con)
        finally:
            src_con.close()
            dst_con.close()
        safety = str(safety_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_con = sqlite3.connect(target)
    try:
        src_con.backup(dst_con)
    finally:
        src_con.close()
        dst_con.close()

    after = verify_backup(target)
    return {
        "restored": bool(after.get("ok")),
        "backup": str(src),
        "target": str(target),
        "safety_copy": safety,
        "verification": after,
        "restored_at": utcnow(),
    }


def repair(rebuild_fts: bool = True, vacuum: bool = False) -> Dict[str, Any]:
    """Deterministic repairs that never delete learner data.

    - quick_check first (a broken file is reported, not 'repaired' by guessing)
    - rebuild the chunks FTS index (search index, derivable from chunks)
    - optional VACUUM (compaction; requires free disk equal to db size)
    """
    mkdirs()
    steps: List[Dict[str, Any]] = []
    con = sqlite3.connect(T.META_DB)
    try:
        try:
            quick = con.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            # A file that is not a database at all cannot be repaired by guessing.
            quick = f"unreadable: {exc}"
        steps.append({"step": "quick_check", "status": "ok" if quick == "ok" else "failed",
                      "detail": quick})
        if quick == "ok" and rebuild_fts:
            # chunks_fts is a plain FTS5 table (not external-content), so the
            # repair is: clear the index and re-derive it from `chunks` —
            # deterministic, and the index is fully derivable from the data.
            try:
                con.execute("DELETE FROM chunks_fts")
                con.execute(
                    "INSERT INTO chunks_fts (id, text, source, locator)"
                    " SELECT id, text, source, locator FROM chunks")
                con.commit()
                rows = con.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
                steps.append({"step": "fts_rebuild", "status": "ok", "rows": rows})
            except sqlite3.OperationalError as exc:
                steps.append({"step": "fts_rebuild", "status": "skipped",
                              "detail": str(exc)})
        if vacuum:
            free = shutil.disk_usage(T.BASE).free
            size = T.META_DB.stat().st_size if T.META_DB.is_file() else 0
            if free < size * 2:
                steps.append({"step": "vacuum", "status": "skipped",
                              "detail": "not enough free disk for a safe VACUUM"})
            else:
                con.execute("VACUUM")
                steps.append({"step": "vacuum", "status": "ok"})
    finally:
        con.close()
    return {
        "doctor_version": DOCTOR_VERSION,
        "status": "ok" if all(s["status"] != "failed" for s in steps) else "failed",
        "steps": steps,
    }


def _provider_status() -> Dict[str, Any]:
    try:
        from medforge.providers import create_provider
        provider = create_provider()
        alive = provider.is_alive()
        models: List[str] = []
        error = None
        if alive:
            try:
                models = provider.list_models()
            except Exception as exc:
                error = str(exc)
        return {"provider": provider.provider_id, "alive": alive,
                "models": models, "error": error}
    except Exception as exc:
        return {"provider": None, "alive": False, "models": [], "error": str(exc)}


def _backup_status() -> Dict[str, Any]:
    backups_dir = T.BASE / "backups"
    if not backups_dir.is_dir():
        return {"dir": str(backups_dir), "count": 0, "newest": None,
                "newest_verified": None}
    files = sorted(backups_dir.glob("*.db"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    newest = str(files[0]) if files else None
    verified = verify_backup(files[0]).get("ok") if files else None
    return {"dir": str(backups_dir), "count": len(files),
            "newest": newest, "newest_verified": verified}


def diagnose() -> Dict[str, Any]:
    """Full health report. Every field is evidence, not opinion."""
    mkdirs()
    report: Dict[str, Any] = {
        "doctor_version": DOCTOR_VERSION,
        "checked_at": utcnow(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "home": str(T.BASE),
        "offline": bool(T.OFFLINE),
    }

    if not T.META_DB.is_file():
        report.update({"database_present": False, "healthy": False,
                       "problems": ["metadata database missing"]})
        return report

    con = _connect()
    try:
        check = _integrity(con)
        tables = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'").fetchone()[0]
    finally:
        con.close()

    applied = applied_migrations()
    pending = pending_migrations()
    disk = shutil.disk_usage(T.BASE)

    report.update({
        "database_present": True,
        "db_bytes": T.META_DB.stat().st_size,
        "tables": tables,
        **check,
        "migrations_applied": applied,
        "migrations_pending": pending,
        "baseline_v3_present": baseline_v3_present(),
        "disk_free_bytes": disk.free,
        "disk_free_mb": round(disk.free / (1024 ** 2), 1),
        "backups": _backup_status(),
        "provider": _provider_status(),
        "job_lock_path": str(T.BASE / ".job.lock"),
    })

    problems: List[str] = []
    if not check["integrity_ok"]:
        problems.append(f"integrity_check: {check['integrity']}")
    if check["foreign_key_violations"]:
        problems.append(f"{check['foreign_key_violations']} foreign-key violations")
    if pending:
        problems.append("pending migrations: " + ", ".join(pending))
    if disk.free < 500 * 1024 ** 2:
        problems.append(f"low disk: {report['disk_free_mb']} MB free")
    if report["backups"]["count"] == 0:
        problems.append("no backups present")
    elif report["backups"]["newest_verified"] is False:
        problems.append("newest backup failed verification")
    if not report["provider"]["alive"]:
        problems.append("model provider not reachable")

    report["problems"] = problems
    report["healthy"] = not problems
    return report


__all__ = [
    "DOCTOR_VERSION",
    "EXPECTED_MIGRATIONS",
    "diagnose",
    "verify_backup",
    "restore_backup",
    "repair",
    "applied_migrations",
    "pending_migrations",
]
