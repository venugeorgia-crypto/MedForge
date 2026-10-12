#!/usr/bin/env python3
"""P17 launch tests (hermetic: environment checks, release-integrity
enforcement, upgrade + rollback + restart state, doctor-behaviour contract).

No live Ollama, no live data touched. The tests run against a fixture home
and a synthetic release root + manifest + checksum so they exercise the
content-addressed release path without any network or real package bundle.

Fixture strategy:
- We never patch module globals in `medforge.launch` (which caused the
  UnboundLocalError and import-time coupling bugs). Instead, `medforge.launch`
  exposes `_set_app_dir` so tests can point the content-addressed release at
  a temporary `release-root` without mutating any module state and without
  racing any import-time behaviour.
- `tests.test_launch` keeps its own helper `_make_manifest_for` that builds a
  minimal release artifact hierarchy (manifest + checksum file + placeholder
  dashboard/medforge_core/requirements + medforge dir) and returns the
  metadata dict so tests can mutate the digest to simulate tampering.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "current"))

import medforge.types as T
from core.database.schema import LEGACY_SCHEMA_DDL, V3_SCHEMA_DDL
from core.database.migrate_v15 import ensure_automation_v15
from core.database.migrate_v16 import ensure_evaluation_v16


# ─── test-local helpers ───


def _make_checksum_file(root: Path, path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    root.joinpath("release-checksum.sha256").write_text(digest + "  " + path.name + "\n", encoding="utf-8")
    return digest


def _make_manifest_for(root: Path, release_lock: str = "local-only-p17-demo-lock", run_checksum: bool = True) -> dict:
    """Build a minimal release-artifact hierarchy rooted at `root` and return
    the metadata dict so tests can mutate the digest to simulate tampering.
    Creates the manifest exactly once per root so upgrade/rollback tests can
    reuse a stable temporary release directory."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "dashboard.py").write_text("# placeholder\n", encoding="utf-8")
    (root / "medforge_core.py").write_text("# placeholder\n", encoding="utf-8")
    (root / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (root / "medforge").mkdir(exist_ok=True)
    payload = root / "requirements.txt"
    manifest = {
        "release_root": str(root),
        "release_lock": release_lock,
        "release_checksum_file": str(root / "release-checksum.sha256"),
        "release_checksum_digest": _make_checksum_file(root, payload) if run_checksum else "",
        "version": "17.0.0-demo",
    }
    mpath = root / "release-manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _seed_home(base: Path) -> Path:
    db = base / "database" / "medforge.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.executescript(LEGACY_SCHEMA_DDL)
    con.executescript(V3_SCHEMA_DDL)
    con.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES ('3.0.0','v3','now')")
    con.commit()
    con.close()
    ensure_automation_v15(db, create_backup=False)
    ensure_evaluation_v16(db, create_backup=False)
    return db


# ─── fixtures ───


@pytest.fixture()
def home():
    base = Path(tempfile.mkdtemp())
    for sub in ("database", "products", "docs", "logs", "tmp", "backups"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    db = _seed_home(base)
    orig = {name: getattr(T, name) for name in ("BASE", "META_DB", "PRODUCTS", "DOCS", "LOGS", "TMP")}
    T.BASE = base
    T.META_DB = db
    T.PRODUCTS = base / "products"
    T.DOCS = base / "docs"
    T.LOGS = base / "logs"
    T.TMP = base / "tmp"
    try:
        yield base, db
    finally:
        for name, value in orig.items():
            setattr(T, name, value)
        shutil.rmtree(base, ignore_errors=True)


# ─── Environment / diagnose ───


class TestDiagnose:
    def test_reports_missing_database(self, tmp_path):
        from medforge.launch import diagnose
        import medforge.types as T

        base = tmp_path / "home"
        for sub in ("database", "products", "docs", "logs", "tmp", "backups"):
            (base / sub).mkdir(parents=True, exist_ok=True)
        orig = {name: getattr(T, name) for name in ("BASE", "META_DB", "PRODUCTS", "DOCS", "LOGS", "TMP")}
        T.BASE = base
        T.META_DB = base / "database" / "medforge.sqlite3"
        T.PRODUCTS = base / "products"
        T.DOCS = base / "docs"
        T.LOGS = base / "logs"
        T.TMP = base / "tmp"
        try:
            d = diagnose()
            assert d["database"]["present"] is False
            assert d["healthy"] is False
            assert any("missing" in p.lower() or "no database" in p.lower() for p in d["problems"])
        finally:
            for name, value in orig.items():
                setattr(T, name, value)

    def test_full_diagnosis_on_seeded_home(self, tmp_path):
        from medforge.launch import diagnose, _APP_DIR as _launch_app, _set_app_dir
        from tests.test_launch import _make_manifest_for as _mk_man

        root = tmp_path / "release"
        _mk_man(root)
        orig_app = _launch_app
        try:
            _set_app_dir(root)
            base, db = None, None
            try:
                base = Path(tempfile.mkdtemp())
                for sub in ("database", "products", "docs", "logs", "tmp", "backups"):
                    (base / sub).mkdir(parents=True, exist_ok=True)
                db = _seed_home(base)
                orig_T = {name: getattr(T, name) for name in ("BASE", "META_DB", "PRODUCTS", "DOCS", "LOGS", "TMP")}
                T.BASE = base
                T.META_DB = db
                T.PRODUCTS = base / "products"
                T.DOCS = base / "docs"
                T.LOGS = base / "logs"
                T.TMP = base / "tmp"
                try:
                    d = diagnose()
                    assert d["database"]["present"] is True
                    assert d["database"]["integrity_ok"] is True
                    assert d["database"]["foreign_key_violations"] == 0
                    assert d["database"]["tables"] > 10
                    assert "16.0.0" in d["database"]["migrations_applied"]
                    assert d["database"]["migrations_pending"] == []
                    assert d["vector_store_ok"] is True
                    assert d["embeddings_ok"] is True
                    assert d["permissions_ok"] is True
                    assert d["healthy"] is True
                finally:
                    for name, value in orig_T.items():
                        setattr(T, name, value)
                    shutil.rmtree(base, ignore_errors=True)
            finally:
                _set_app_dir(orig_app)
        finally:
            pass

    def test_detects_missing_release_manifest(self, tmp_path):
        from medforge.launch import _release_integrity_check, _APP_DIR as _launch_app, _set_app_dir
        import medforge.types as T

        # Create a release directory WITHOUT a manifest
        release_root = tmp_path / "release"
        release_root.mkdir(parents=True, exist_ok=True)
        (release_root / "dashboard.py").write_text("# placeholder\n", encoding="utf-8")
        (release_root / "medforge_core.py").write_text("# placeholder\n", encoding="utf-8")
        (release_root / "requirements.txt").write_text("requests\n", encoding="utf-8")
        (release_root / "medforge").mkdir(exist_ok=True)
        # NO manifest file
        
        base = tmp_path / "home"
        for sub in ("database", "products", "docs", "logs", "tmp", "backups"):
            (base / sub).mkdir(parents=True, exist_ok=True)
        db = base / "database" / "medforge.sqlite3"
        con = sqlite3.connect(str(db))
        con.executescript(LEGACY_SCHEMA_DDL)
        con.executescript(V3_SCHEMA_DDL)
        con.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES ('3.0.0','v3','now')")
        con.commit()
        con.close()
        
        orig_app = _launch_app
        orig = {name: getattr(T, name) for name in ("BASE", "META_DB", "PRODUCTS", "DOCS", "LOGS", "TMP")}
        try:
            _set_app_dir(release_root)
            T.BASE = base
            T.META_DB = db
            T.PRODUCTS = base / "products"
            T.DOCS = base / "docs"
            T.LOGS = base / "logs"
            T.TMP = base / "tmp"
            result = _release_integrity_check()
            assert result["ok"] is False
            assert "missing" in result["problem"].lower()
        finally:
            _set_app_dir(orig_app)
            for name, value in orig.items():
                setattr(T, name, value)

    def test_detects_checksum_mismatch(self, tmp_path):
        from medforge.launch import _release_integrity_check, _APP_DIR as _launch_app, _set_app_dir
        from tests.test_launch import _make_manifest_for as _mk_man

        root = tmp_path / "release"
        _mk_man(root, run_checksum=False)
        orig_app = _launch_app
        try:
            _set_app_dir(root)
            m = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
            m["release_checksum_digest"] = "deadbeef" * 8
            (root / "release-manifest.json").write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
            result = _release_integrity_check()
            assert result["ok"] is False
            assert "mismatch" in result["problem"]
        finally:
            _set_app_dir(orig_app)


# ─── Verify environment ───


class TestVerifyEnvironment:
    def test_verifies_a_healthy_home(self, tmp_path):
        from medforge.launch import _APP_DIR as _launch_app, _set_app_dir, verify_environment
        from tests.test_launch import _make_manifest_for as _mk_man

        root = tmp_path / "release"
        _mk_man(root)
        orig_app = _launch_app
        try:
            _set_app_dir(root)
            base, db = None, None
            try:
                base = Path(tempfile.mkdtemp())
                for sub in ("database", "products", "docs", "logs", "tmp", "backups"):
                    (base / sub).mkdir(parents=True, exist_ok=True)
                db = _seed_home(base)
                orig_T = {name: getattr(T, name) for name in ("BASE", "META_DB", "PRODUCTS", "DOCS", "LOGS", "TMP")}
                T.BASE = base
                T.META_DB = db
                T.PRODUCTS = base / "products"
                T.DOCS = base / "docs"
                T.LOGS = base / "logs"
                T.TMP = base / "tmp"
                try:
                    d = verify_environment(require_services=False)
                    assert d["healthy"] is True
                finally:
                    for name, value in orig_T.items():
                        setattr(T, name, value)
                    shutil.rmtree(base, ignore_errors=True)
            finally:
                _set_app_dir(orig_app)
        finally:
            pass

    def test_fails_with_pending_migrations(self, tmp_path):
        from medforge.launch import verify_environment, _APP_DIR as _launch_app, _set_app_dir
        from tests.test_launch import _make_manifest_for as _mk_man
        import medforge.types as T

        base = tmp_path / "home"
        for sub in ("database", "products", "docs", "logs", "tmp", "backups"):
            (base / sub).mkdir(parents=True, exist_ok=True)
        db = base / "database" / "medforge.sqlite3"
        con = sqlite3.connect(str(db))
        con.executescript(LEGACY_SCHEMA_DDL)
        con.executescript(V3_SCHEMA_DDL)
        con.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES ('3.0.0','v3','now')")
        con.commit()
        con.close()
        
        # Create a proper release directory with manifest
        release_root = tmp_path / "release"
        _mk_man(release_root)
        
        orig_app = _launch_app
        orig = {name: getattr(T, name) for name in ("BASE", "META_DB", "PRODUCTS", "DOCS", "LOGS", "TMP")}
        try:
            _set_app_dir(release_root)
            T.BASE = base
            T.META_DB = db
            T.PRODUCTS = base / "products"
            T.DOCS = base / "docs"
            T.LOGS = base / "logs"
            T.TMP = base / "tmp"
            with pytest.raises(RuntimeError) as exc:
                verify_environment(require_services=False)
            assert "pending" in str(exc.value).lower()
        finally:
            _set_app_dir(orig_app)
            for name, value in orig.items():
                setattr(T, name, value)


# ─── Upgrade / rollback / restart ───


class TestUpgradeAndRollback:
    def test_upgrade_adopts_new_release_and_marks_success(self, home, tmp_path):
        from medforge.launch import _APP_DIR as _launch_app, _set_app_dir, upgrade_from
        from tests.test_launch import _make_manifest_for as _mk_man

        old_root = tmp_path / "old"
        old_root.mkdir()
        old_db = old_root / "database" / "medforge.sqlite3"
        old_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(home[1], old_db)
        new_root = tmp_path / "new"
        _mk_man(new_root)
        orig_app = _launch_app
        try:
            _set_app_dir(new_root)
            result = upgrade_from(old_root, new_root, run_migration=False)
            assert result["status"] == "ok"
            assert (new_root / "database" / "medforge.sqlite3").is_file()
            assert not (new_root / "upgrade-state.json").exists()
        finally:
            _set_app_dir(orig_app)

    def test_upgrade_leaves_old_db_untouched(self, home, tmp_path):
        from medforge.launch import _APP_DIR as _launch_app, _set_app_dir, upgrade_from
        from tests.test_launch import _make_manifest_for as _mk_man

        old_root = tmp_path / "old"
        old_root.mkdir()
        old_db = old_root / "database" / "medforge.sqlite3"
        old_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(home[1], old_db)
        before_size = old_db.stat().st_size
        new_root = tmp_path / "new"
        _mk_man(new_root)
        orig_app = _launch_app
        try:
            _set_app_dir(new_root)
            upgrade_from(old_root, new_root, run_migration=False)
            assert old_db.stat().st_size == before_size and old_db.is_file()
        finally:
            _set_app_dir(orig_app)

    def test_upgrade_marks_failure_on_unhealthy_source_db(self, tmp_path):
        from medforge.launch import _APP_DIR as _launch_app, _set_app_dir, upgrade_from
        from tests.test_launch import _make_manifest_for as _mk_man

        new_root = tmp_path / "new"
        new_root.mkdir()
        bad = new_root / "database" / "medforge.sqlite3"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"not a database")
        _mk_man(new_root)
        orig_app = _launch_app
        try:
            _set_app_dir(new_root)
            with pytest.raises(RuntimeError) as exc:
                upgrade_from(new_root, new_root, run_migration=False)
            assert "source database is not a valid" in str(exc.value).lower()
            assert (new_root / "upgrade-state.json").exists()
        finally:
            _set_app_dir(orig_app)

    def test_rollback_refuses_an_unhealthy_database(self, tmp_path):
        from medforge.launch import _APP_DIR as _launch_app, _set_app_dir, rollback_to
        from tests.test_launch import _make_manifest_for as _mk_man

        root = tmp_path / "release"
        db = root / "database" / "medforge.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"not a database")
        _mk_man(root)
        orig_app = _launch_app
        try:
            _set_app_dir(root)
            with pytest.raises(RuntimeError) as exc:
                rollback_to(root)
            assert "refused" in str(exc.value).lower()
        finally:
            _set_app_dir(orig_app)

    def test_rollback_accepts_a_healthy_database(self, home, tmp_path):
        from medforge.launch import _APP_DIR as _launch_app, _set_app_dir, rollback_to
        from tests.test_launch import _make_manifest_for as _mk_man

        root = tmp_path / "release"
        db = root / "database" / "medforge.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(home[1], db)
        _mk_man(root)
        orig_app = _launch_app
        try:
            _set_app_dir(root)
            result = rollback_to(root)
            assert result["status"] == "ok"
            assert result["database_integrity"] == "ok"
        finally:
            _set_app_dir(orig_app)

    def test_restart_reports_mid_upgrade_state(self, tmp_path):
        from medforge.launch import _APP_DIR as _launch_app, _set_app_dir, restart_check
        from tests.test_launch import _make_manifest_for as _mk_man

        new_root = tmp_path / "new"
        _mk_man(new_root)
        marker = new_root / "upgrade-state.json"
        marker.write_text(json.dumps({"from_release": str(new_root), "to_release": str(new_root), "started_at": "now", "status": "running"}, indent=2) + "\n", encoding="utf-8")
        orig_app = _launch_app
        try:
            _set_app_dir(new_root)
            res = restart_check()
            assert res["upgrade_marker"].endswith("upgrade-state.json")
            assert res["upgrade_state"]["status"] == "running"
        finally:
            _set_app_dir(orig_app)


# ─── CLI equivalence for the shim ───


class TestCliShim:
    def test_diagnose_returns_a_healthy_report_for_a_seeded_home(self, tmp_path):
        from medforge.launch import _APP_DIR as _launch_app, _set_app_dir, diagnose
        from tests.test_launch import _make_manifest_for as _mk_man

        root = tmp_path / "release"
        _mk_man(root)
        orig_app = _launch_app
        try:
            _set_app_dir(root)
            base, db = None, None
            try:
                base = Path(tempfile.mkdtemp())
                for sub in ("database", "products", "docs", "logs", "tmp", "backups"):
                    (base / sub).mkdir(parents=True, exist_ok=True)
                db = _seed_home(base)
                orig_T = {name: getattr(T, name) for name in ("BASE", "META_DB", "PRODUCTS", "DOCS", "LOGS", "TMP")}
                T.BASE = base
                T.META_DB = db
                T.PRODUCTS = base / "products"
                T.DOCS = base / "docs"
                T.LOGS = base / "logs"
                T.TMP = base / "tmp"
                try:
                    d = diagnose()
                    assert d["healthy"] is True
                    assert d["launch_version"] == "p17-packaging-v1"
                    assert d["database"]["present"] is True
                    assert d["database"]["integrity_ok"] is True
                    assert d["database"]["migrations_applied"] == [
                        "3.0.0", "4.0.0", "5.0.0", "6.0.0", "7.0.0",
                        "8.0.0", "9.0.0", "10.0.0", "11.0.0", "12.0.0",
                        "13.0.0", "14.0.0", "15.0.0", "16.0.0",
                    ]
                finally:
                    for name, value in orig_T.items():
                        setattr(T, name, value)
                    shutil.rmtree(base, ignore_errors=True)
            finally:
                _set_app_dir(orig_app)
        finally:
            pass

    def test_restart_check_reads_upgrade_marker(self, tmp_path):
        from medforge.launch import _APP_DIR as _launch_app, _set_app_dir, restart_check
        from tests.test_launch import _make_manifest_for as _mk_man

        new_root = tmp_path / "new"
        _mk_man(new_root)
        marker = new_root / "upgrade-state.json"
        marker.write_text(json.dumps({"from_release": str(new_root), "to_release": str(new_root), "started_at": "now", "status": "running"}, indent=2) + "\n", encoding="utf-8")
        orig_app = _launch_app
        try:
            _set_app_dir(new_root)
            res = restart_check()
            assert res["upgrade_marker"].endswith("upgrade-state.json")
            assert res["upgrade_state"]["status"] == "running"
        finally:
            _set_app_dir(orig_app)
