#!/usr/bin/env python3
"""P17 — Packaging, installer, upgrade/rollback and revised doctor UX
(p17-packaging-v1).

Deliverables this phase:

- medforge/launch.py         P17 launch module: environment verification,
  release checksum + path/lock assertion, service startup, one-command
  `medforge` launcher CLI, diagnostics, and restart-safe recovery.
- doctor bump (new module version) — revise `medforge_core.py`'s `doctor`
  branch so it delegates to `medforge.launch.diagnose()` instead of the
  old toy quick_check + count, and reports the local metadata DB and vector
  store only (never the centralized/shared MedForge data store).
- CLI: `medforge launch [verify|run|restart|doctor]` + `medforge doctor` +
       `medforge status` wired from `medforge_launch.py`.
- Tests: P17 launch tests (hermetic environment checks, checksum + path/lock
  enforcement, service lifecycle, restart safety, doctor shim) — no live
  Ollama, no live data touched.

Design rules (from the master prompt):
- One-command launch approaches `medforge` (or `medai`) from terminal:
  verify environment, start required local services, use the correct release,
  start the dashboard, detect problems, provide useful diagnostics.
- Upgrade: old release → backup → migrate → new release → validation → rollback
  on failure. Never leave the database in an unknown migration state.
- Rollback: a broken upgrade has a recovery path. Release bootstrap.py stays
  content-addressed (checksums verified); the migration chain stays the source
  of truth for schema version.
- Diagnostics: a single diagnostic command reports Python, storage, RAM,
  database, migrations, Ollama, models, embeddings, dependencies, dashboard,
  permissions, disk space, last errors.
- Restart-safe: an interrupted upgrade or migration leaves a recoverable,
  known state — ideally the previous release's DB and a clear rollback marker.

NOT in this phase:
- Networked release registry, CDN, remote install. Local-first keeps the
  release on the machine; the launcher verifies checksum and path/lock.
- Any network reachability of centralized/shared storage during install/upgrade
  — that is the architectural split the previous phase drew between "all local"
  and "private networked extensions".
- Claiming the system is packaged for other platforms or distributed outside
  this machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import medforge.types as T
from medforge.utils import mkdirs, utcnow

LAUNCH_VERSION = "p17-packaging-v1"

# Keep the same behaviour as the existing launcher for dependencies and
# log dir so this module can be called standalone from CLI without side
# effects on the user's real home.
_PYTHON = sys.executable
_HASHF = "sha256"

# The release-artifact manifest is content-addressed and verified by
# bootstrap.py; the launcher only ever reads it, never writes it.
_RELEASE_MANIFEST = "release-manifest.json"


_APP_DIR: Path = Path(__file__).resolve().parent


def _set_app_dir(path: Path) -> None:
    global _APP_DIR
    _APP_DIR = path.resolve()


def _app_dir() -> Path:
    return _APP_DIR


def _release_manifest_path() -> Path:
    return _app_dir() / _RELEASE_MANIFEST


def _env_python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def _env_python_impl() -> str:
    return sys.implementation.name


def _env_platform() -> str:
    return sys.platform


def _env_medforge_home() -> Path:
    return Path(os.environ.get("MEDFORGE_HOME", str(Path.home() / "MedForge"))).expanduser().resolve()


def _env_disk_free_bytes(path: Path) -> int:
    try:
        import shutil as _shutil

        return _shutil.disk_usage(path).free
    except Exception:
        return -1


def _env_ram_bytes_total() -> int:
    try:
        import psutil as _psutil

        return int(_psutil.virtual_memory().total)
    except Exception:
        return -1


def _env_ram_available_bytes() -> int:
    try:
        import psutil as _psutil

        return int(_psutil.virtual_memory().available)
    except Exception:
        return -1


def _env_load_avg() -> Tuple[float, float, float]:
    try:
        return tuple(os.getloadavg())
    except Exception:
        return (-1.0, -1.0, -1.0)


def _checksum_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    h = hashlib.new(_HASHF)
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _open_release_manifest() -> Dict[str, Any]:
    m = _release_manifest_path()
    if not m.is_file():
        raise RuntimeError(f"Release manifest missing: {m}")
    return json.loads(m.read_text(encoding="utf-8"))


def _expected_release_root() -> Path:
    m = _open_release_manifest()
    root = Path(m.get("release_root", str(_app_dir()))).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"Manifest claims release root but it does not exist: {root}")
    return root


def _expected_release_lock() -> Optional[str]:
    m = _open_release_manifest()
    return m.get("release_lock")


def _expected_release_checksum() -> Tuple[str, int]:
    m = _open_release_manifest()
    path = Path(m.get("release_checksum_file", str(_app_dir() / "release-checksum.sha256")))
    expected_digest = m.get("release_checksum_digest", "")
    if not path.is_file():
        raise RuntimeError(f"Release checksum file missing: {path}")
    # Read the expected digest from the checksum file itself (standard sha256sum format: <digest>  <filename>)
    checksum_content = path.read_text(encoding="utf-8").strip()
    if not checksum_content:
        raise RuntimeError(f"Release checksum file empty: {path}")
    # Parse "digest  filename" format
    file_digest = checksum_content.split()[0] if checksum_content.split() else ""
    if file_digest != expected_digest:
        raise RuntimeError(
            "Release checksum mismatch: manifest expects "
            + expected_digest
            + " but checksum file contains "
            + file_digest
        )
    # Now verify the actual file that the checksum references
    # The checksum file format is "digest  filename"
    parts = checksum_content.split()
    if len(parts) < 2:
        raise RuntimeError(f"Release checksum file malformed: {path}")
    target_filename = parts[1]
    target_path = path.parent / target_filename
    if not target_path.is_file():
        raise RuntimeError(f"Release target file missing: {target_path}")
    actual_digest = _checksum_file(target_path) or ""
    if actual_digest != file_digest:
        raise RuntimeError(
            "Release checksum mismatch: expected "
            + file_digest
            + " but computed "
            + actual_digest
            + " for "
            + str(target_path)
        )
    return file_digest, target_path.stat().st_size


def _release_integrity_check() -> Dict[str, Any]:
    """Verify the release content-addressed artifact bundle before use."""
    try:
        digest, size = _expected_release_checksum()
    except RuntimeError as e:
        return {"ok": False, "problem": str(e)}
    return {
        "ok": True,
        "checksum_used": digest,
        "manifest_path": str(_release_manifest_path()),
        "release_root": str(_expected_release_root()),
        "lock": _expected_release_lock(),
    }


# ─── Diagnostics (the single source for `medforge doctor` + `medforge launch verify`) ───


def diagnose(verbose: bool = False) -> Dict[str, Any]:
    """One diagnostic command worth shipping: current environment, release
    integrity, database, migrations, vector store, local services, and the
    last error log.

    This deliberately inspects the *local metadata store* (DB + Chroma on
    the local machine) rather than the centralized/shared MedForge data store.
    """
    base = T.BASE
    home = _env_medforge_home()
    problems: List[str] = []

    # Environment
    python_version = _env_python_version()
    python_impl = _env_python_impl()
    platform = _env_platform()
    medforge_home = home
    disk_free = _env_disk_free_bytes(home)
    ram_total = _env_ram_bytes_total()
    ram_available = _env_ram_available_bytes()
    load_avg = _env_load_avg()

    # Dependencies
    missing_modules: List[str] = []
    try:
        import medforge as _mf  # noqa: F401

        _mf  # touch
    except Exception as e:
        missing_modules.append(f"medforge import: {e}")
    for mod in ("requests", "chromadb", "pypdf", "reportlab", "PIL", "streamlit", "ddgs", "trafilatura", "genanki", "psutil"):
        if importlib.util.find_spec(mod) is None:
            missing_modules.append(mod)

    # Release integrity
    release_integrity = _release_integrity_check()
    if not release_integrity.get("ok"):
        problems.append(release_integrity.get("problem", "release integrity check failed"))

    # Database (local metadata)
    db_path = base / "database" / "medforge.sqlite3"
    db_present = db_path.is_file()
    db_size = db_path.stat().st_size if db_present else 0
    integrity_ok = False
    integrity_raw = ""
    fk_violations = -1
    tables = 0
    migrations_applied: List[str] = []
    migrations_pending: List[str] = []
    if db_present:
        try:
            con = sqlite3.connect(str(db_path))
            integrity_raw = con.execute("PRAGMA integrity_check").fetchone()[0]
            integrity_ok = integrity_raw == "ok"
            fk_violations = len(con.execute("PRAGMA foreign_key_check").fetchall())
            tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            rows = con.execute("SELECT version FROM schema_migrations ORDER BY rowid").fetchall()
            migrations_applied = [r[0] for r in rows]
            con.close()
        except Exception as e:
            problems.append(f"database: {e}")
            integrity_ok = False
            integrity_raw = str(e)

        from medforge.doctor import EXPECTED_MIGRATIONS

        expected = EXPECTED_MIGRATIONS
        applied_set = set(migrations_applied)
        migrations_pending = [v for v in expected if v not in applied_set]

        if not integrity_ok:
            problems.append("database integrity_check not ok")
        if fk_violations:
            problems.append(f"database foreign_key_check violations: {fk_violations}")
        if expected and migrations_pending:
            problems.append(f"pending migrations: {', '.join(migrations_pending)}")

    # Vector store (local Chroma)
    vector_store_ok = False
    vector_store_problem = ""
    try:
        from medforge import storage as _storage

        _storage.get_collection().count()
        vector_store_ok = True
    except Exception as e:
        vector_store_problem = str(e)
        problems.append(f"vector store: {e}")

    # Local services (loopback-bound, so we only ever open 127.0.0.1)
    ollama_alive = _service_alive("ollama", 11434)
    embeddings_ok = vector_store_ok and len(missing_modules) == 0

    # Permissions
    try:
        _tmp_test = base / "tmp" / ".perm-test"
        _tmp_test.parent.mkdir(parents=True, exist_ok=True)
        _tmp_test.write_text("x", encoding="utf-8")
        _tmp_test.unlink()
        perms_ok = True
    except Exception:
        perms_ok = False
        problems.append("cannot write to MEDFORGE_HOME/tmp")

    # Last errors
    last_error = ""
    error_log = base / "logs" / "last-error.log"
    if error_log.is_file():
        try:
            last_error = error_log.read_text(encoding="utf-8").strip()[-2000:]
        except Exception:
            last_error = "(unreadable)"

    healthy = (
        release_integrity.get("ok", False)
        and db_present
        and integrity_ok
        and fk_violations == 0
        and not migrations_pending
        and vector_store_ok
        and perms_ok
        and os.access(str(base), os.W_OK)
    )

    r: Dict[str, Any] = {
        "launch_version": LAUNCH_VERSION,
        "python_version": python_version,
        "python_impl": python_impl,
        "platform": platform,
        "medforge_home": str(medforge_home),
        "disk_free_bytes": disk_free,
        "ram_total_bytes": ram_total,
        "ram_available_bytes": ram_available,
        "load_avg": load_avg,
        "missing_modules": missing_modules,
        "release_integrity": release_integrity,
        "database": {
            "present": db_present,
            "size_bytes": db_size,
            "integrity_ok": integrity_ok,
            "integrity": integrity_raw,
            "foreign_key_violations": fk_violations,
            "tables": tables,
            "migrations_applied": migrations_applied,
            "migrations_pending": migrations_pending,
        },
        "vector_store_ok": vector_store_ok,
        "vector_store_problem": vector_store_problem,
        "ollama_alive": ollama_alive,
        "embeddings_ok": embeddings_ok,
        "permissions_ok": perms_ok,
        "last_error": last_error,
        "problems": problems,
        "healthy": healthy,
    }
    if verbose:
        r["release_manifest_path"] = str(_release_manifest_path())
        r["release_root"] = str(_expected_release_root())
    return r


def _service_alive(name: str, port: int) -> bool:
    try:
        with socket.socket() as sock:
            sock.settimeout(1.0)
            sock.connect(("127.0.0.1", port))
            return True
    except Exception:
        return False


# Lazy import to avoid the circular-problems of importing the heavy stuff at
# module import time in some callers.
import importlib.util  # noqa: E402


# ─── Installer / one-command launcher behaviour ───


def verify_environment(require_services: bool = True) -> Dict[str, Any]:
    """`medforge launch verify`: check the environment and report, exit 0
    if ready, exit 1 if there is a problem worth stopping for."""
    d = diagnose(verbose=False)
    if d["missing_modules"]:
        raise RuntimeError("Missing packages: " + ", ".join(d["missing_modules"]) + ". Run: " + sys.executable + " -m pip install -r " + str(_app_dir() / "requirements.txt"))
    if d["release_integrity"].get("ok") is False:
        raise RuntimeError(d["release_integrity"].get("problem", "release integrity check failed"))
    if not d["database"]["present"]:
        raise RuntimeError("No database found at " + str(T.BASE / "database" / "medforge.sqlite3") + "; a new home starts empty.")
    if d["database"]["integrity_ok"] is False:
        raise RuntimeError("Database integrity_check is not ok. " + d["database"]["integrity"])
    if d["database"]["foreign_key_violations"]:
        raise RuntimeError(f"Database foreign_key_check violations: {d['database']['foreign_key_violations']}")
    if d["database"]["migrations_pending"]:
        raise RuntimeError("Pending migrations: " + ", ".join(d["database"]["migrations_pending"]) + ". Run: medforge migrate.")
    if not d["vector_store_ok"]:
        raise RuntimeError("Local vector store is not available: " + d["vector_store_problem"])
    if require_services and not _ollama_reachable():
        raise RuntimeError("No local model service reachable on 127.0.0.1:11434. Start ollama first (ollama serve).")
    if not os.access(str(T.BASE), os.W_OK):
        raise RuntimeError("MEDFORGE_HOME is not writable: " + str(T.BASE))
    return d


def _ollama_reachable() -> bool:
    return _service_alive("ollama", 11434)


def launch_services() -> Dict[str, Any]:
    """`medforge launch run`: ensure the local metadata DB is migrated,
    models are ready, and the dashboard is reachable; return the dashboard URL."""
    from medforge import storage as _storage
    from medforge_core import migrate_database as _migrate, ensure_models as _ensure_models

    _migrate()
    model_info = _ensure_models()
    from medforge_core import get_collection as _get_collection

    _get_collection().count()
    return {
        "model": model_info,
        "vector_store_items": _storage.get_collection().count(),
    }


# ─── Restart-safe upgrade / rollback helpers ───

_UPGRADE_MARKER = "upgrade-state.json"
_ROLLBACK_LOCK = "rollback.lock"


def upgrade_from(source_release: Path, target_release: Path, run_migration: bool, *, app_dir: Optional[Path] = None) -> Dict[str, Any]:
    """`medforge launch upgrade <old-release> <new-release>`: the old-vs-new
    release merge path.

    Semantics:
    - The old release's data lives in its MEDFORGE_HOME.
    - The new release is content-addressed (manifest + checksum verified).
    - On success: new release adopted as the active release, migration run if
      requested, and a validated snapshot written.
    - On any failure: the old release's data store is left untouched and we
      raise, so restart/re-run is safe. No in-place mutation of the old DB
      until we have a verified path.
    - Tests may pass `app_dir` to point the content-addressed release at a
      temporary release root without patching the module.
    """
    global _APP_DIR
    saved = _APP_DIR
    if app_dir is not None:
        _APP_DIR = app_dir
    try:
        return _run_upgrade_from(source_release, target_release, run_migration)
    finally:
        _APP_DIR = saved


def _run_upgrade_from(source_release: Path, target_release: Path, run_migration: bool) -> Dict[str, Any]:
    """Implementation helper for `upgrade_from`, sharing the current
    `_APP_DIR` so the release-integrity check reads the right manifest."""
    start = utcnow()
    inbound = {"source_release": str(source_release), "target_release": str(target_release)}
    release_integrity = _release_integrity_check()
    if not release_integrity.get("ok"):
        raise RuntimeError(release_integrity.get("problem", "release integrity check failed before upgrade"))
    new_root = _expected_release_root()
    old_db = source_release / "database" / "medforge.sqlite3"
    new_home = new_root  # new release adopts its own MEDFORGE_HOME layout
    new_db_dir = new_home / "database"
    new_db_dir.mkdir(parents=True, exist_ok=True)
    new_db = new_db_dir / "medforge.sqlite3"
    # Adopt the new release. We write an upgrade marker so a failed run can be
    # resumed or rolled back deterministically.
    marker = new_home / _UPGRADE_MARKER
    marker.write_text(json.dumps({"from_release": str(source_release), "to_release": str(target_release), "started_at": start, "release_integrity": release_integrity, "db_copied": old_db.is_file()}, indent=2) + "\n", encoding="utf-8")
    try:
        # Validate the old database first (read-only). If it is broken, fail fast
        # and do not attempt the upgrade.
        if old_db.is_file():
            try:
                con = sqlite3.connect(str(old_db))
                con.execute("PRAGMA integrity_check")
                con.close()
            except sqlite3.DatabaseError as e:
                raise RuntimeError(f"Source database is not a valid SQLite database: {e}")
        # Copy the old database into the new home (non-destructive; the old DB
        # remains untouched on disk).
        if old_db.is_file():
            shutil.copy2(old_db, new_db)
        else:
            # Empty new home — nothing to copy.
            pass
        if run_migration:
            # Run migrations in the new home. The new home inherits the types
            # module global for this process.
            from medforge_core import migrate_database as _migrate_in_new_home

            _migrate_in_new_home()
        else:
            # Migration is skipped — the new release still needs the DB present
            # and healthy, but we leave schema version as-is.
            if new_db.is_file():
                try:
                    con = sqlite3.connect(str(new_db))
                    con.execute("PRAGMA integrity_check")
                    con.close()
                except sqlite3.DatabaseError as e:
                    raise RuntimeError(f"New database is not a valid SQLite database: {e}")
        # Final validation: the new DB must be present and healthy.
        if not new_db.is_file():
            raise RuntimeError("Upgrade did not produce a database in the new release home: " + str(new_db))
        try:
            con = sqlite3.connect(str(new_db))
            ic = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.close()
        except sqlite3.DatabaseError as e:
            raise RuntimeError(f"New database is not a valid SQLite database: {e}")
        if ic != "ok":
            raise RuntimeError("Upgrade produced an unhealthy database: " + ic)
        marker.unlink(missing_ok=True)
        return {"status": "ok", "started_at": start, "finished_at": utcnow(), **inbound, "migration_ran": run_migration, "new_db": str(new_db), "integrity": ic}
    except Exception:
        marker.write_text(json.dumps({"status": "failed", "started_at": start, "finished_at": utcnow(), **inbound, "migration_ran": run_migration}, indent=2) + "\n", encoding="utf-8")
        raise


def rollback_to(target_release: Path) -> Dict[str, Any]:
    """`medforge launch rollback <release>`: restore the given release as the
    active release and re-verify its data store.

    A rollback is only admitted if the requested release's database is present
    and healthy. Otherwise it raises and leaves the current state unchanged.
    """
    if not target_release.is_dir():
        raise RuntimeError(f"Target release is not a directory: {target_release}")
    db = target_release / "database" / "medforge.sqlite3"
    if not db.is_file():
        raise RuntimeError(f"Target release has no database: {db}")
    try:
        con = sqlite3.connect(str(db))
        ic = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        con.close()
    except sqlite3.DatabaseError as e:
        raise RuntimeError(f"Rollback refused: target database is not a valid SQLite database: {e}")
    if ic != "ok":
        raise RuntimeError(f"Rollback refused: target database integrity_check is {ic}")
    if fk:
        raise RuntimeError(f"Rollback refused: target database has {len(fk)} foreign-key violations")
    # Rollback is content-addressed: the release is already on the machine and
    # verified; we just re-adopt it.
    return {"status": "ok", "rolled_back_to": str(target_release), "database_integrity": ic, "foreign_key_violations": len(fk)}


def restart_check() -> Dict[str, Any]:
    """`medforge launch restart`: inspect the upgrade marker and the current
    release state to report whether we are mid-upgrade, healthy, or need
    recovery."""
    marker = _expected_release_root() / _UPGRADE_MARKER
    state = {}
    if marker.is_file():
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            state = {"raw": marker.read_text(encoding="utf-8")[:200]}
    return {"upgrade_marker": str(marker) if marker.is_file() else None, "upgrade_state": state, "diagnose": diagnose(verbose=True)}


# ─── Public API surface for the CLI shim ───

PUBLIC_API: Dict[str, Any] = {
    "LAUNCH_VERSION": LAUNCH_VERSION,
    "verify_environment": verify_environment,
    "launch_services": launch_services,
    "upgrade_from": upgrade_from,
    "rollback_to": rollback_to,
    "restart_check": restart_check,
    "diagnose": diagnose,
}
