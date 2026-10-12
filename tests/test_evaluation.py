"""P16 evaluation tests (hermetic: sandboxed cases, stub verdicts, no models).

Covers: V16 schema (fresh + idempotent), golden-set validation/rejection,
builtin suite end-to-end on a fixture home, stub-verdict drives on the real P4
aggregation (support / partial / contradiction / abstention), canonical
grounding + product consistency on the real P9 renderer, run persistence and
reads, thresholds, skipped cases, regression detection between runs, sandbox
protection of the live home, and read-only snapshot metrics.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

import medforge.types as T
from medforge import evaluation as EV

from core.database.schema import LEGACY_SCHEMA_DDL, V3_SCHEMA_DDL
from core.database.migrate_v16 import ensure_evaluation_v16, is_v16_applied


@pytest.fixture()
def eval_env(monkeypatch):
    """Fresh temp home (proper database/ layout) + restored paths afterwards."""
    orig = {name: getattr(T, name) for name in
            ("BASE", "META_DB", "PRODUCTS", "DOCS", "LOGS", "TMP")}
    base = Path(tempfile.mkdtemp())
    dbdir = base / "database"
    dbdir.mkdir(parents=True, exist_ok=True)
    for name in ("products", "docs", "logs", "tmp"):
        (base / name).mkdir(exist_ok=True)
    db = dbdir / "medforge.sqlite3"

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


# ─── V16 schema ───

class TestV16Schema:
    def test_migration_fresh_and_idempotent(self, eval_env):
        _base, db = eval_env
        _apply_core_schema(db)
        first = ensure_evaluation_v16(db)
        assert first["success"] is True
        assert first["integrity"] == "ok"
        assert first["foreign_key_errors"] == 0
        assert is_v16_applied(db) is True
        names = {r[0] for r in _fetch(
            db, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"evaluation_runs", "evaluation_cases"} <= names
        versions = {r[0] for r in _fetch(db, "SELECT version FROM schema_migrations")}
        assert {"15.0.0", "16.0.0"} <= versions
        second = ensure_evaluation_v16(db)
        assert second["success"] is True
        rows = _fetch(db, "SELECT count(*) FROM schema_migrations WHERE version='16.0.0'")
        assert rows[0][0] == 1

    def test_backup_written_when_requested(self, eval_env):
        _base, db = eval_env
        _apply_core_schema(db)
        result = ensure_evaluation_v16(db, create_backup=True)
        assert result["success"] is True
        assert result["backup"] and Path(result["backup"]).is_file()


# ─── Golden set validation ───

class TestGoldenSet:
    def test_builtin_set_is_valid(self):
        cases = EV.builtin_golden_set()
        assert cases and len({c["id"] for c in cases}) == len(cases)
        for case in cases:
            assert case["kind"] in EV.CASE_KINDS
            assert case["expect"]

    def test_loader_rejects_unknown_kind(self, eval_env, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps([{"id": "x", "kind": "wishful", "expect": {"a": 1}}]))
        with pytest.raises(ValueError, match="unknown kind"):
            EV.load_golden_set(bad)

    def test_loader_rejects_duplicate_ids(self, eval_env, tmp_path):
        case = {"id": "dup", "kind": "canonical", "expect": {"min_facts": 1},
                "sources": [{"text": "x" * 60}]}
        bad = tmp_path / "dup.json"
        bad.write_text(json.dumps([case, dict(case)]))
        with pytest.raises(ValueError, match="duplicate"):
            EV.load_golden_set(bad)

    def test_loader_rejects_missing_claim(self, eval_env, tmp_path):
        bad = tmp_path / "noclaim.json"
        bad.write_text(json.dumps([{"id": "x", "kind": "abstention", "expect": {"a": 1}}]))
        with pytest.raises(ValueError, match="claim"):
            EV.load_golden_set(bad)

    def test_loader_rejects_bad_json(self, eval_env, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            EV.load_golden_set(bad)

    def test_sample_export_round_trips(self, eval_env, tmp_path):
        target = tmp_path / "golden.json"
        path = EV.write_sample_golden_set(target)
        assert Path(path).is_file()
        assert len(EV.load_golden_set(target)) == len(EV.builtin_golden_set())


# ─── Builtin suite end-to-end ───

class TestBuiltinRun:
    def test_builtin_suite_passes_with_clean_metrics(self, eval_env):
        base, _db = eval_env
        report = EV.run_evaluation(triggered_by="manual")
        assert report["status"] == "passed", report["cases"]
        assert report["cases_failed"] == 0
        assert report["cases_passed"] == len(EV.builtin_golden_set())
        assert report["cases_skipped"] == 0
        assert report["pass_rate"] == 1.0
        metrics = report["metrics"]
        assert metrics["abstention_accuracy"] == 1.0
        assert metrics["abstention_accuracy_cases"] == 4  # 4 cases test abstention
        assert metrics["support_rate"] == 1.0
        assert metrics["support_rate_cases"] == 3  # claim-supported, claim-evidence-type-priority, claim-human-reviewed-override
        assert metrics["contradiction_recall"] == 1.0
        assert metrics["contradiction_recall_cases"] == 2  # claim-contradicted, claim-multiple-evidence-mixed
        assert metrics["grounding_rate"] == 1.0
        assert metrics["grounding_rate_cases"] == 5  # 5 canonical cases
        assert metrics["citation_continuity_rate"] == 1.0
        assert metrics["citation_continuity_rate_cases"] == 9  # 5 canonical + 2 product + 2 claim_status
        assert metrics["consistency_rate"] == 1.0
        assert metrics["consistency_rate_cases"] == 4  # 4 product cases

    def test_abstention_case_reports_zero_model_calls(self, eval_env):
        report = EV.run_evaluation()
        by_id = {c["case_id"]: c for c in report["cases"]}
        abstain = by_id["abstain-without-evidence"]
        assert abstain["status"] == "passed"
        assert abstain["metrics"]["model_calls"] == 0
        assert abstain["metrics"]["latest_method"] == "deterministic-gate"
        assert abstain["metrics"]["abstained"] == 1.0

    def test_contradiction_case_is_flagged(self, eval_env):
        report = EV.run_evaluation()
        by_id = {c["case_id"]: c for c in report["cases"]}
        contradicted = by_id["claim-contradicted"]
        assert contradicted["status"] == "passed"
        assert contradicted["metrics"]["status"] == "CONTRADICTED"

    def test_no_verifier_abstains(self, eval_env):
        report = EV.run_evaluation()
        by_id = {c["case_id"]: c for c in report["cases"]}
        case = by_id["claim-no-verifier-abstains"]
        assert case["status"] == "passed"
        assert case["metrics"]["status"] == "INSUFFICIENT_EVIDENCE"
        assert case["metrics"]["model_calls"] == 0

    def test_product_case_checks_checksums(self, eval_env):
        report = EV.run_evaluation()
        by_id = {c["case_id"]: c for c in report["cases"]}
        product = by_id["product-consistency"]
        assert product["status"] == "passed"
        assert product["metrics"]["artifacts"] >= 2
        assert product["metrics"]["consistent"] is True

    def test_run_is_persisted_and_readable(self, eval_env):
        report = EV.run_evaluation(triggered_by="manual")
        stored = EV.get_evaluation_run(report["run_id"])
        assert stored is not None
        assert stored["status"] == report["status"]
        assert stored["cases_passed"] == report["cases_passed"]
        assert len(stored["cases"]) == report["cases_total"]
        assert stored["golden_digest"] == report["golden_digest"]
        listed = EV.list_evaluation_runs()
        assert listed and listed[0]["run_id"] == report["run_id"]
        status = EV.evaluation_status()
        assert status["runs"] >= 1
        assert status["latest"]["run_id"] == report["run_id"]


# ─── Failures, thresholds, skipped ───

class TestFailureAndThresholds:
    def _failing_case(self):
        return {"id": "expects-support-without-evidence", "kind": "abstention",
                "claim": "The cardiac cycle begins with ventricular filling.",
                "expect": {"status": "SUPPORTED"}}

    def test_failing_case_fails_run_with_reason(self, eval_env):
        report = EV.run_evaluation(cases=[self._failing_case()], persist=False)
        assert report["status"] == "failed"
        assert report["cases_failed"] == 1
        case = report["cases"][0]
        assert case["status"] == "failed"
        assert any("INSUFFICIENT_EVIDENCE" in f for f in case["failures"])

    def test_threshold_violation_is_reported(self, eval_env):
        cases = [
            {"id": "ok", "kind": "abstention",
             "claim": "The cardiac cycle begins with ventricular filling.",
             "expect": {"status": "INSUFFICIENT_EVIDENCE"}},
            {"id": "bad", "kind": "abstention",
             "claim": "Stroke volume is the volume of blood ejected in one contraction.",
             "expect": {"status": "SUPPORTED"}},
        ]
        report = EV.run_evaluation(cases=cases, thresholds={"pass_rate": 0.75},
                                   persist=False)
        assert report["status"] == "failed"
        assert report["pass_rate"] == 0.5
        assert any("pass_rate" in f for f in report["threshold_failures"])

    def test_unmeasured_metric_is_not_enforced(self, eval_env):
        cases = [{"id": "only-claims", "kind": "abstention",
                  "claim": "The cardiac cycle begins with ventricular filling.",
                  "expect": {"status": "INSUFFICIENT_EVIDENCE"}}]
        report = EV.run_evaluation(cases=cases,
                                   thresholds={"grounding_rate": 1.0},
                                   persist=False)
        assert report["status"] == "passed"
        assert report["threshold_failures"] == []
        assert "grounding_rate" not in report["metrics"]

    def test_skipped_cases_do_not_count_as_pass(self, eval_env):
        cases = [{"id": "disabled", "kind": "abstention", "enabled": False,
                  "claim": "The cardiac cycle begins with ventricular filling.",
                  "expect": {"status": "INSUFFICIENT_EVIDENCE"}}]
        report = EV.run_evaluation(cases=cases, persist=False)
        assert report["cases_skipped"] == 1
        assert report["status"] == "failed"
        assert any("no cases executed" in f for f in report["threshold_failures"])

    def test_mixed_skip_and_execute(self, eval_env):
        cases = [
            {"id": "disabled", "kind": "abstention", "enabled": False,
             "claim": "The cardiac cycle begins with ventricular filling.",
             "expect": {"status": "INSUFFICIENT_EVIDENCE"}},
            {"id": "runs", "kind": "abstention",
             "claim": "The cardiac cycle begins with ventricular filling.",
             "expect": {"status": "INSUFFICIENT_EVIDENCE"}},
        ]
        report = EV.run_evaluation(cases=cases, persist=False)
        assert report["status"] == "passed"
        assert report["cases_skipped"] == 1
        assert report["cases_passed"] == 1
        assert report["pass_rate"] == 1.0


# ─── Regression detection ───

class TestRegressionDetection:
    def test_auto_regression_fails_and_is_recorded(self, eval_env):
        good = [{"id": "sensor", "kind": "abstention",
                 "claim": "The cardiac cycle begins with ventricular filling.",
                 "expect": {"status": "INSUFFICIENT_EVIDENCE"}}]
        first = EV.run_evaluation(cases=good, suite="regression-suite",
                                  triggered_by="manual")
        assert first["status"] == "passed"
        bad = [{"id": "sensor", "kind": "abstention",
                "claim": "The cardiac cycle begins with ventricular filling.",
                "expect": {"status": "SUPPORTED"}}]
        second = EV.run_evaluation(cases=bad, suite="regression-suite",
                                   triggered_by="manual")
        assert second["status"] == "failed"
        assert second["baseline_run_id"] == first["run_id"]
        assert len(second["regressions"]) == 1
        assert second["regressions"][0]["case_id"] == "sensor"
        stored = EV.get_evaluation_run(second["run_id"])
        assert stored["regressions"] == 1
        assert stored["baseline_run_id"] == first["run_id"]

    def test_compare_evaluations_explicit(self, eval_env):
        good = [{"id": "sensor", "kind": "abstention",
                 "claim": "The cardiac cycle begins with ventricular filling.",
                 "expect": {"status": "INSUFFICIENT_EVIDENCE"}}]
        first = EV.run_evaluation(cases=good, suite="compare-suite")
        fixed = [{"id": "sensor", "kind": "abstention",
                  "claim": "The cardiac cycle begins with ventricular filling.",
                  "expect": {"status": "INSUFFICIENT_EVIDENCE"}},
                 {"id": "new-one", "kind": "abstention",
                  "claim": "Stroke volume is the volume of blood ejected in one contraction.",
                  "expect": {"status": "INSUFFICIENT_EVIDENCE"}}]
        second = EV.run_evaluation(cases=fixed, suite="compare-suite")
        diff = EV.compare_evaluations(second["run_id"], first["run_id"])
        assert diff["baseline_run_id"] == first["run_id"]
        assert diff["regressions"] == []
        assert diff["added"] == ["new-one"]
        assert diff["pass_rate_delta"] == 0.0

    def test_compare_without_baseline(self, eval_env):
        report = EV.run_evaluation(cases=[{
            "id": "only", "kind": "abstention",
            "claim": "The cardiac cycle begins with ventricular filling.",
            "expect": {"status": "INSUFFICIENT_EVIDENCE"}}], suite="lonely-suite")
        diff = EV.compare_evaluations(report["run_id"])
        assert diff["baseline_run_id"] is None
        assert diff["regressions"] == []


# ─── Sandbox + snapshot ───

class TestSandboxAndSnapshot:
    def test_cases_do_not_pollute_live_home(self, eval_env):
        _base, db = eval_env
        _apply_core_schema(db)
        EV.ensure_evaluation_tables()
        from medforge import evidence as E

        seeded = E.extract_claims("The heart has four chambers and two atria.",
                                  topic="anatomy")
        E.store_claims(seeded, topic="anatomy")
        before = _fetch(db, "SELECT count(*) FROM claims")[0][0]
        assert before >= 1
        EV.run_evaluation(persist=False)
        after = _fetch(db, "SELECT count(*) FROM claims")[0][0]
        assert after == before, "fixture claims leaked into the live evidence graph"

    def test_evaluate_case_primitive_is_sandboxed(self, eval_env):
        _base, db = eval_env
        _apply_core_schema(db)
        EV.ensure_evaluation_tables()
        before = _fetch(db, "SELECT count(*) FROM claims")[0][0]
        result = EV.evaluate_case({
            "id": "single", "kind": "abstention",
            "claim": "The cardiac cycle begins with ventricular filling.",
            "expect": {"status": "INSUFFICIENT_EVIDENCE"}})
        assert result["status"] == "passed"
        after = _fetch(db, "SELECT count(*) FROM claims")[0][0]
        assert after == before

    def test_snapshot_empty_home(self, eval_env):
        snap = EV.snapshot_metrics()
        assert snap["db_present"] is True
        assert snap["claims"]["total"] == 0
        assert snap["content"]["items"] == 0
        assert snap["evaluations"]["runs"] == 0

    def test_snapshot_after_run_and_seed(self, eval_env):
        _base, db = eval_env
        _apply_core_schema(db)
        EV.ensure_evaluation_tables()
        from medforge import evidence as E

        claims = E.extract_claims("The heart has four chambers and two atria.",
                                  topic="anatomy")
        E.store_claims(claims, topic="anatomy")
        report = EV.run_evaluation()
        snap = EV.snapshot_metrics()
        assert snap["claims"]["total"] == 1
        assert snap["claims"]["by_status"].get("PENDING") == 1
        assert snap["claims"]["grounded"] == 0
        assert snap["evaluations"]["runs"] == 1
        assert snap["evaluations"]["latest"]["status"] == "passed"
        assert snap["evaluations"]["latest"]["run_id"] == report["run_id"]
        # read-only: snapshot never adds rows
        assert _fetch(db, "SELECT count(*) FROM claims")[0][0] == 1


# ─── CLI (regression: Path shadowing broke `evaluate sample`) ───

class TestEvaluationCli:
    """The CLI branch was dead on arrival: a redundant `from pathlib import
    Path` inside the P11 `video probe` branch made `Path` local to the whole
    of ``main()``, so every earlier branch touching Path raised
    UnboundLocalError. These tests drive the real entry point in a subprocess
    with an isolated home, which is the only way that defect is visible."""

    REPO = Path(__file__).resolve().parent.parent
    CORE = REPO / "current" / "medforge_core.py"

    def _run(self, home: Path, *args: str):
        env = dict(os.environ, MEDFORGE_HOME=str(home))
        return subprocess.run(
            [sys.executable, str(self.CORE), *args],
            cwd=str(self.CORE.parent), env=env,
            capture_output=True, text=True, timeout=300,
        )

    @pytest.fixture()
    def home(self, tmp_path):
        return tmp_path / "home"

    def test_sample_writes_a_loadable_golden_file(self, home):
        target = home / "golden_sample.json"
        result = self._run(home, "evaluate", "sample", str(target))
        assert "UnboundLocalError" not in result.stderr
        assert result.returncode == 0, result.stderr
        assert target.exists()
        cases = json.loads(target.read_text(encoding="utf-8"))
        assert isinstance(cases, list) and cases
        assert {c["kind"] for c in cases} == set(EV.CASE_KINDS)

    def test_run_accepts_a_golden_file_path(self, home, tmp_path):
        baseline = self._run(home, "evaluate", "run")
        assert baseline.returncode == 0, baseline.stderr
        golden = tmp_path / "golden.json"
        assert self._run(home, "evaluate", "sample", str(golden)).returncode == 0
        result = self._run(home, "evaluate", "run", str(golden))
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["status"] == "passed"
        assert report["cases_total"] >= 1
        # same suite twice: the second run is compared against the first
        assert report["regressions"] == []

    def test_unknown_run_id_fails_loudly(self, home):
        assert self._run(home, "evaluate", "run").returncode == 0
        result = self._run(home, "evaluate", "show", "eval-missing")
        assert result.returncode == 1
        assert "Unknown evaluation run" in (result.stderr + result.stdout)
