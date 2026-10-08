"""P14 automation tests (offline-safe, fixture home + no real scheduler calls).

Covers: V15 schema (fresh + idempotent), maintenance/backup/study-prep/refresh
steps, verified backup + retention pruning (never touching non-matching files),
dry runs that write nothing, recorded runs with per-step results, a failing step
surfacing as a failed run, and launchd plist generation/status/removal for the
nightly job (written into an override directory, never the real LaunchAgents).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

import medforge.types as T
from medforge import automation as A

from core.database.schema import LEGACY_SCHEMA_DDL, V3_SCHEMA_DDL
from core.database.migrate_v15 import ensure_automation_v15, is_v15_applied


@pytest.fixture()
def auto_env(monkeypatch):
    """Fresh temp home: META_DB/PRODUCTS/DOCS/LOGS/BASE + LaunchAgents override."""
    orig = {name: getattr(T, name) for name in
            ("BASE", "META_DB", "PRODUCTS", "DOCS", "LOGS", "TMP")}
    base = Path(tempfile.mkdtemp())
    dbdir = base / "database"
    dbdir.mkdir(parents=True, exist_ok=True)
    (base / "products").mkdir(exist_ok=True)
    (base / "docs").mkdir(exist_ok=True)
    (base / "logs").mkdir(exist_ok=True)
    (base / "tmp").mkdir(exist_ok=True)
    db = dbdir / "medforge.sqlite3"

    T.BASE = base
    T.META_DB = db
    T.PRODUCTS = base / "products"
    T.DOCS = base / "docs"
    T.LOGS = base / "logs"
    T.TMP = base / "tmp"
    monkeypatch.setenv("MEDFORGE_LAUNCH_AGENTS", str(base / "LaunchAgents"))
    try:
        yield base, db
    finally:
        for name, value in orig.items():
            setattr(T, name, value)
        shutil.rmtree(base, ignore_errors=True)


def _apply_core_schema(db: Path) -> None:
    con = sqlite3.connect(db)
    con.executescript(LEGACY_SCHEMA_DDL)
    con.executescript(V3_SCHEMA_DDL)
    con.commit()
    con.close()


def _fetch(db: Path, sql: str, params=()):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


# ─── V15 schema ───

class TestV15Schema:
    def test_migration_fresh_and_idempotent(self, auto_env):
        _base, db = auto_env
        _apply_core_schema(db)
        first = ensure_automation_v15(db, create_backup=False)
        assert first["success"] is True and first["integrity"] == "ok"
        assert first["foreign_key_errors"] == 0
        names = {r[0] for r in _fetch(db, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"automation_jobs", "automation_runs"} <= names
        assert is_v15_applied(db)
        second = ensure_automation_v15(db, create_backup=False)
        assert second["success"] is True
        assert _fetch(db, "SELECT count(*) FROM schema_migrations"
                          " WHERE version='15.0.0'")[0][0] == 1

    def test_module_ensure(self, auto_env):
        _base, db = auto_env
        _apply_core_schema(db)
        A.ensure_automation_tables()
        assert is_v15_applied(db)


# ─── Steps ───

class TestSteps:
    def test_maintenance_reports_health(self, auto_env):
        _base, db = auto_env
        _apply_core_schema(db)
        result = A.maintenance_step()
        assert result["status"] == "ok"
        assert result["quick_check"] == "ok"
        assert result["db_bytes"] > 0 and result["free_mb"] > 0

    def test_backup_creates_verified_snapshot(self, auto_env):
        base, db = auto_env
        _apply_core_schema(db)
        result = A.backup_step(retention=7)
        assert result["status"] == "ok"
        path = Path(result["path"])
        assert path.is_file() and path.parent == base / "backups"
        con = sqlite3.connect(path)
        try:
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            con.close()

    def test_backup_retention_prunes_only_its_own_files(self, auto_env):
        base, db = auto_env
        _apply_core_schema(db)
        backups = base / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        kept_decoy = backups / "medforge_pre_v5_backup.db"
        kept_decoy.write_bytes(b"do not delete")
        for i in range(9):
            f = backups / f"medforge_nightly_2025010{i}.db"
            f.write_bytes(b"old")
            os.utime(f, (1_600_000_000 + i, 1_600_000_000 + i))

        result = A.backup_step(retention=7)
        assert result["status"] == "ok"
        nightly = sorted(backups.glob("medforge_nightly_*.db"))
        assert len(nightly) == 7, "retention keeps the newest N nightly snapshots"
        assert len(result["pruned"]) == 3
        assert kept_decoy.is_file(), "non-nightly backups must never be pruned"

    def test_study_prep_returns_recommendation(self, auto_env):
        _base, db = auto_env
        _apply_core_schema(db)
        result = A.study_prep_step()
        assert result["status"] == "ok"
        assert "recommendation" in result

    def test_refresh_step_offline_is_honest(self, auto_env, monkeypatch):
        _base, db = auto_env
        _apply_core_schema(db)
        monkeypatch.setattr(T, "OFFLINE", True)
        result = A.refresh_step()
        assert result["status"] == "ok"
        assert result["offline"] is True


# ─── Nightly run ───

class TestRunNightly:
    def test_dry_run_writes_nothing(self, auto_env):
        _base, db = auto_env
        _apply_core_schema(db)
        result = A.run_nightly(dry_run=True)
        assert result["dry_run"] is True and result["recorded"] is False
        assert [s["step"] for s in result["steps"]] == list(A.NIGHTLY_STEPS)
        assert all(s["status"] == "planned" for s in result["steps"])
        names = {r[0] for r in _fetch(db, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "automation_runs" not in names, "dry run must not create tables"

    def test_full_run_records_steps_and_backup(self, auto_env):
        base, db = auto_env
        _apply_core_schema(db)
        result = A.run_nightly(triggered_by="scheduled")
        assert result["status"] == "completed", result
        assert result["recorded"] is True
        statuses = {s["step"]: s["status"] for s in result["steps"]}
        assert statuses == {"maintenance": "ok", "refresh": "ok",
                            "backup": "ok", "study_prep": "ok"}
        backup = next(s for s in result["steps"] if s["step"] == "backup")
        assert Path(backup["path"]).is_file() and backup["integrity"] == "ok"
        prep = next(s for s in result["steps"] if s["step"] == "study_prep")
        assert prep["recommendation"]

        row = _fetch(db, "SELECT status, steps, completed_at FROM automation_runs"
                         " WHERE run_id=?", (result["run_id"],))[0]
        assert row[0] == "completed" and row[2]
        import json
        assert len(json.loads(row[1])) == 4

        job = _fetch(db, "SELECT last_run_id, last_status FROM automation_jobs"
                         " WHERE name=?", (A.NIGHTLY_JOB_NAME,))[0]
        assert job == (result["run_id"], "completed")

        runs = A.list_runs()
        assert runs and runs[0]["run_id"] == result["run_id"]
        status = A.get_automation_status()
        assert status["job"]["name"] == A.NIGHTLY_JOB_NAME
        assert status["last_run"]["run_id"] == result["run_id"]

    def test_failing_step_is_recorded_not_hidden(self, auto_env, monkeypatch):
        _base, db = auto_env
        _apply_core_schema(db)

        def boom():
            raise RuntimeError("prep exploded")

        monkeypatch.setitem(A._STEP_FUNCS, "study_prep", boom)
        result = A.run_nightly(steps=["study_prep", "maintenance"])
        assert result["status"] == "failed"
        steps = {s["step"]: s for s in result["steps"]}
        assert steps["study_prep"]["status"] == "failed"
        assert "prep exploded" in steps["study_prep"]["error"]
        assert steps["maintenance"]["status"] == "ok"
        row = _fetch(db, "SELECT status, error FROM automation_runs WHERE run_id=?",
                     (result["run_id"],))[0]
        assert row[0] == "failed" and "prep exploded" in row[1]

    def test_rejects_unknown_step_and_trigger(self, auto_env):
        _base, db = auto_env
        _apply_core_schema(db)
        with pytest.raises(ValueError):
            A.run_nightly(steps=["teleport"])
        with pytest.raises(ValueError):
            A.run_nightly(triggered_by="whenever")


# ─── Scheduling ───

class TestSchedule:
    def test_plist_payload_shape(self):
        program = A.nightly_command("/usr/bin/python3")
        assert program[-1] == "nightly"
        assert program[1].endswith("medforge_core.py")
        payload = A.build_plist_payload(program, hour=4, minute=15)
        assert "<string>org.medforge.nightly</string>" in payload
        assert "<integer>4</integer>" in payload and "<integer>15</integer>" in payload
        assert "<string>nightly</string>" in payload
        assert "<false/>" in payload  # RunAtLoad

    def test_install_status_uninstall(self, auto_env):
        base, _db = auto_env
        result = A.install_schedule(hour=2, minute=30, write_only=True,
                                    python_executable="/usr/bin/python3")
        if sys.platform != "darwin":
            pytest.skip("launchd scheduling is macOS-only")
        assert result["installed"] is True and result["write_only"] is True
        path = Path(result["path"])
        assert path.is_file() and path.parent == base / "LaunchAgents"
        text = path.read_text(encoding="utf-8")
        assert "<integer>2</integer>" in text and "<integer>30</integer>" in text

        status = A.schedule_status()
        assert status["installed"] is True and status["label"] == A.PLIST_LABEL

        removed = A.uninstall_schedule()
        assert removed["removed"] is True and not path.exists()
        assert A.schedule_status()["installed"] is False

    def test_invalid_schedule_time_rejected(self, auto_env):
        with pytest.raises(ValueError):
            A.install_schedule(hour=25, write_only=True)
        with pytest.raises(ValueError):
            A.install_schedule(minute=99, write_only=True)

    def test_non_macos_reports_cron_line(self, auto_env, monkeypatch):
        monkeypatch.setattr(A.sys, "platform", "linux")
        result = A.install_schedule(hour=3, minute=0, write_only=True)
        assert result["installed"] is False
        assert "nightly" in result["cron_line"]
        assert result["cron_line"].split()[0] == "0"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
