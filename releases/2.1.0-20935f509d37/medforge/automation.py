"""P14 — Automation + Nightly Study Workflow (p14-automation-v1, migration 15.0.0).

One nightly job that does the housekeeping MedForge needs while the learner
sleeps, and never lies about what happened:

    maintenance → refresh (P13) → verified backup → study prep (P9) → audit row

Every step returns a structured result; a failing step is recorded with its
error and marks the run failed. ``dry_run=True`` plans the steps and writes
nothing. Scheduling writes a real launchd user agent (macOS) and loads it only
when ``launchctl`` is available; on other platforms it prints the exact cron
line instead of pretending to install.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import medforge.types as T
from medforge.utils import mkdirs, utcnow

AUTOMATION_VERSION = "p14-automation-v1"
NIGHTLY_JOB_NAME = "nightly"
DEFAULT_BACKUP_RETENTION = 7
PLIST_LABEL = "org.medforge.nightly"
NIGHTLY_STEPS = ("maintenance", "refresh", "backup", "study_prep")


# ─── Table ensure (candidate-walk import, mirrors publication.py) ───

def ensure_automation_tables() -> None:
    here = Path(__file__).resolve()
    candidates = [str(T.BASE)] + [str(p) for p in here.parents]
    for base_str in candidates:
        if base_str not in sys.path:
            sys.path.insert(0, base_str)
        try:
            from core.database.migrate_v15 import ensure_automation_v15

            ensure_automation_v15(T.META_DB, create_backup=False)
            return
        except ImportError:
            continue
    raise ImportError(
        "core.database.migrate_v15 not importable from: " + ", ".join(candidates)
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    return con


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ─── Steps ───

def maintenance_step() -> Dict[str, Any]:
    """Cheap health signals: db quick_check, db size, free disk space."""
    mkdirs()
    if not T.META_DB.is_file():
        return {"status": "ok", "db_present": False}
    con = sqlite3.connect(T.META_DB)
    try:
        quick = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    db_bytes = T.META_DB.stat().st_size
    free_bytes = shutil.disk_usage(T.BASE).free
    return {
        "status": "ok" if quick == "ok" else "failed",
        "db_present": True,
        "quick_check": quick,
        "db_bytes": db_bytes,
        "free_bytes": free_bytes,
        "free_mb": round(free_bytes / (1024 ** 2), 1),
    }


def refresh_step(
    pubmed_queries: Optional[List[Any]] = None,
    web_urls: Optional[List[Any]] = None,
    sources: Optional[List[str]] = None,
    verifier_fn: Optional[Callable[[str, str, Dict[str, Any]], str]] = None,
) -> Dict[str, Any]:
    """P13 source refresh (local textbooks always; network when asked/online)."""
    from medforge import knowledge_refresh as KR

    result = KR.run_refresh(
        triggered_by="scheduled",
        sources=sources,
        pubmed_queries=pubmed_queries,
        web_urls=web_urls,
        verifier_fn=verifier_fn,
    )
    return {
        "status": "ok" if result.get("status") == "completed" else "failed",
        "run_id": result.get("run_id"),
        "refresh_status": result.get("status"),
        "offline": result.get("offline"),
        "sources_checked": result.get("sources_checked"),
        "sources_changed": result.get("sources_changed"),
        "claims_reverified": result.get("claims_reverified"),
        "notifications_created": result.get("notifications_created"),
        "errors": result.get("errors", []),
    }


def backup_step(retention: int = DEFAULT_BACKUP_RETENTION) -> Dict[str, Any]:
    """Verified snapshot into backups/, then prune old nightly snapshots only."""
    mkdirs()
    backups = T.BASE / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    target = backups / f"medforge_nightly_{stamp}.db"

    src_conn = sqlite3.connect(f"file:{T.META_DB}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(target)
    try:
        src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()

    # Verify the COPY (a backup you have not verified is not a backup).
    con = sqlite3.connect(target)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()

    removed: List[str] = []
    if retention >= 0:
        candidates = sorted(backups.glob("medforge_nightly_*.db"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        for old in candidates[int(retention):]:
            try:
                old.unlink()
                removed.append(old.name)
            except OSError:
                pass

    return {
        "status": "ok" if integrity == "ok" else "failed",
        "path": str(target),
        "size_bytes": target.stat().st_size,
        "integrity": integrity,
        "retention": int(retention),
        "pruned": removed,
    }


def study_prep_step() -> Dict[str, Any]:
    """What to study next — read through P9's public API (no new algorithm)."""
    from medforge import study

    status = study.get_study_status()
    recommendation = study.recommend_next_action()
    open_notes = 0
    try:
        from medforge import knowledge_refresh as KR
        open_notes = len(KR.list_notifications(acknowledged=0, limit=50))
    except Exception:
        open_notes = 0
    return {
        "status": "ok",
        "recommendation": recommendation,
        "topics_tracked": status.get("topics_tracked") if isinstance(status, dict) else None,
        "open_notifications": open_notes,
    }


_STEP_FUNCS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "maintenance": maintenance_step,
    "refresh": refresh_step,
    "backup": backup_step,
    "study_prep": study_prep_step,
}


# ─── Nightly run ───

def _ensure_job(con: sqlite3.Connection, name: str) -> str:
    row = con.execute("SELECT job_id FROM automation_jobs WHERE name=?", (name,)).fetchone()
    if row:
        return row["job_id"]
    job_id = _uid("job")
    now = utcnow()
    con.execute(
        """INSERT INTO automation_jobs
           (job_id, name, schedule, enabled, created_at, updated_at)
           VALUES (?,?,?,1,?,?)""",
        (job_id, name, "daily 03:00", now, now),
    )
    return job_id


def run_nightly(
    triggered_by: str = "manual",
    dry_run: bool = False,
    retention: int = DEFAULT_BACKUP_RETENTION,
    pubmed_queries: Optional[List[Any]] = None,
    web_urls: Optional[List[Any]] = None,
    refresh_sources: Optional[List[str]] = None,
    verifier_fn: Optional[Callable[[str, str, Dict[str, Any]], str]] = None,
    steps: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run the nightly workflow. Returns per-step results and the run id."""
    if triggered_by not in ("scheduled", "manual", "backfill"):
        raise ValueError("triggered_by must be scheduled|manual|backfill")
    wanted = list(steps or NIGHTLY_STEPS)
    unknown = [s for s in wanted if s not in _STEP_FUNCS]
    if unknown:
        raise ValueError(f"unknown steps: {unknown}")

    if dry_run:
        return {
            "automation_version": AUTOMATION_VERSION,
            "dry_run": True,
            "triggered_by": triggered_by,
            "steps": [{"step": name, "status": "planned"} for name in wanted],
            "recorded": False,
        }

    ensure_automation_tables()
    started = utcnow()
    run_id = _uid("arun")
    con = _connect()
    try:
        job_id = _ensure_job(con, NIGHTLY_JOB_NAME)
        con.execute(
            """INSERT INTO automation_runs
               (run_id, job_id, triggered_by, dry_run, steps, status, started_at, created_at)
               VALUES (?,?,?,0,'[]','running',?,?)""",
            (run_id, job_id, triggered_by, started, started),
        )
        con.commit()
    finally:
        con.close()

    results: List[Dict[str, Any]] = []
    for name in wanted:
        entry: Dict[str, Any] = {"step": name}
        try:
            if name == "refresh":
                detail = refresh_step(pubmed_queries=pubmed_queries, web_urls=web_urls,
                                      sources=refresh_sources, verifier_fn=verifier_fn)
            elif name == "backup":
                detail = backup_step(retention=retention)
            else:
                detail = _STEP_FUNCS[name]()
            entry.update(detail)
        except Exception as exc:
            entry.update({"status": "failed", "error": str(exc)})
        results.append(entry)

    failed = [r for r in results if r.get("status") == "failed"]
    status = "failed" if failed else "completed"
    completed = utcnow()
    error_msg = "; ".join(f"{r['step']}: {r.get('error') or r.get('quick_check') or ''}"
                          for r in failed) or None

    con = _connect()
    try:
        con.execute(
            "UPDATE automation_runs SET steps=?, status=?, completed_at=?, error=?"
            " WHERE run_id=?",
            (json.dumps(results, sort_keys=True), status, completed, error_msg, run_id),
        )
        con.execute(
            "UPDATE automation_jobs SET last_run_id=?, last_status=?, last_run_at=?,"
            " updated_at=? WHERE job_id=?",
            (run_id, status, completed, completed, job_id),
        )
        con.commit()
    finally:
        con.close()

    return {
        "automation_version": AUTOMATION_VERSION,
        "run_id": run_id,
        "job_id": job_id,
        "triggered_by": triggered_by,
        "dry_run": False,
        "status": status,
        "steps": results,
        "started_at": started,
        "completed_at": completed,
        "error": error_msg,
        "recorded": True,
    }


def list_runs(limit: int = 20) -> List[Dict[str, Any]]:
    ensure_automation_tables()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM automation_runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_automation_status() -> Dict[str, Any]:
    ensure_automation_tables()
    con = _connect()
    try:
        job = con.execute("SELECT * FROM automation_jobs WHERE name=?",
                          (NIGHTLY_JOB_NAME,)).fetchone()
        last = con.execute(
            "SELECT * FROM automation_runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        total = con.execute("SELECT count(*) FROM automation_runs").fetchone()[0]
        return {
            "automation_version": AUTOMATION_VERSION,
            "job": dict(job) if job else None,
            "last_run": dict(last) if last else None,
            "runs_recorded": total,
            "schedule": schedule_status(),
        }
    finally:
        con.close()


# ─── Scheduling (launchd on macOS; cron guidance elsewhere) ───

def launch_agents_dir() -> Path:
    override = os.environ.get("MEDFORGE_LAUNCH_AGENTS")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return launch_agents_dir() / f"{PLIST_LABEL}.plist"


def nightly_command(python_executable: Optional[str] = None) -> List[str]:
    """The exact command the scheduler must run (no shell interpolation)."""
    python = python_executable or sys.executable
    core = Path(__file__).resolve().parent.parent / "medforge_core.py"
    return [str(python), str(core), "nightly"]


def build_plist_payload(program: List[str], hour: int = 3, minute: int = 0) -> str:
    args = "".join(f"        <string>{a}</string>\n" for a in program)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"    <key>Label</key>\n    <string>{PLIST_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args}"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n    <false/>\n"
        "    <key>StartCalendarInterval</key>\n"
        "    <dict>\n"
        f"        <key>Hour</key>\n        <integer>{int(hour)}</integer>\n"
        f"        <key>Minute</key>\n        <integer>{int(minute)}</integer>\n"
        "    </dict>\n"
        "    <key>StandardOutPath</key>\n"
        f"    <string>{T.LOGS / 'nightly.log'}</string>\n"
        "    <key>StandardErrorPath</key>\n"
        f"    <string>{T.LOGS / 'nightly.err.log'}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def install_schedule(
    hour: int = 3,
    minute: int = 0,
    write_only: bool = False,
    python_executable: Optional[str] = None,
) -> Dict[str, Any]:
    """Write (and, on macOS, load) the nightly launchd user agent."""
    if not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59:
        raise ValueError("hour must be 0-23 and minute 0-59")
    if sys.platform != "darwin":
        core = Path(__file__).resolve().parent.parent / "medforge_core.py"
        python = python_executable or sys.executable
        return {
            "platform": sys.platform,
            "installed": False,
            "reason": "launchd is macOS-only; add this cron line instead",
            "cron_line": f"{int(minute)} {int(hour)} * * * {python} {core} nightly",
        }

    mkdirs()
    launch_agents_dir().mkdir(parents=True, exist_ok=True)
    path = plist_path()
    program = nightly_command(python_executable)
    path.write_text(build_plist_payload(program, hour=hour, minute=minute),
                    encoding="utf-8")

    loaded = False
    load_error = None
    if not write_only and shutil.which("launchctl"):
        proc = subprocess.run(["launchctl", "load", str(path)],
                              capture_output=True, text=True, check=False)
        loaded = proc.returncode == 0
        if not loaded:
            load_error = (proc.stderr or proc.stdout or "").strip()[:300]

    return {
        "platform": sys.platform,
        "installed": True,
        "path": str(path),
        "program": program,
        "hour": int(hour),
        "minute": int(minute),
        "loaded": loaded,
        "load_error": load_error,
        "write_only": bool(write_only),
    }


def uninstall_schedule() -> Dict[str, Any]:
    path = plist_path()
    unloaded = False
    if sys.platform == "darwin" and path.is_file() and shutil.which("launchctl"):
        proc = subprocess.run(["launchctl", "unload", str(path)],
                              capture_output=True, text=True, check=False)
        unloaded = proc.returncode == 0
    removed = False
    if path.is_file():
        path.unlink()
        removed = True
    return {"platform": sys.platform, "path": str(path),
            "removed": removed, "unloaded": unloaded}


def schedule_status() -> Dict[str, Any]:
    path = plist_path()
    installed = path.is_file()
    loaded: Optional[bool] = None
    if sys.platform == "darwin" and shutil.which("launchctl"):
        proc = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                              check=False)
        loaded = PLIST_LABEL in (proc.stdout or "")
    return {
        "platform": sys.platform,
        "label": PLIST_LABEL,
        "path": str(path),
        "installed": installed,
        "loaded": loaded,
    }


__all__ = [
    "AUTOMATION_VERSION",
    "NIGHTLY_JOB_NAME",
    "NIGHTLY_STEPS",
    "DEFAULT_BACKUP_RETENTION",
    "ensure_automation_tables",
    "maintenance_step",
    "refresh_step",
    "backup_step",
    "study_prep_step",
    "run_nightly",
    "list_runs",
    "get_automation_status",
    "launch_agents_dir",
    "plist_path",
    "nightly_command",
    "build_plist_payload",
    "install_schedule",
    "uninstall_schedule",
    "schedule_status",
]
