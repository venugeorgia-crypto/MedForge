"""Unit tests for medforge.learner (offline-safe, temp META_DB).

The V3 learner tables are applied idempotently by the module itself, so a
fresh temp database (or even a legacy-only one) needs no explicit migration.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

import medforge.types as T
from medforge import learner
from medforge.storage import init_db


@pytest.fixture()
def learner_db():
    """Redirect META_DB to a fresh temp database for the duration of a test."""
    original = T.META_DB
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)  # let sqlite create it fresh
    T.META_DB = Path(path)
    try:
        yield Path(path)
    finally:
        T.META_DB = original
        Path(path).unlink(missing_ok=True)


def test_ensure_v3_tables_self_heal_legacy_db(learner_db):
    """A legacy-only V2 database gains the V3 learner tables on first use."""
    init_db()  # creates only chunks/study_sessions/weaknesses
    con = sqlite3.connect(learner_db)
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "learner_mastery" not in names

    learner.ensure_v3_tables()
    con = sqlite3.connect(learner_db)
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"learner_mastery", "learner_weaknesses",
            "interactive_sessions"} <= names


def test_record_session_validates_inputs(learner_db):
    with pytest.raises(ValueError):
        learner.record_session("cardiac cycle", session_type="RandomLecture")
    with pytest.raises(ValueError):
        learner.record_session("cardiac cycle", score=150.0)
    with pytest.raises(ValueError):
        learner.record_session("   ", session_type="Learn")
    row = learner.record_session(
        "Cardiac Cycle", session_type="Viva", score=8, duration_seconds=600,
        notes="oral practice", metrics={"questions": 5})
    assert row["session_id"] > 0
    assert row["topic_id"] == "cardiac-cycle"
    assert row["score"] == 80.0  # 8 on the 0-10 rubric
    con = sqlite3.connect(learner_db)
    try:
        stored = con.execute(
            "SELECT session_type, topic_id, score, completed_at FROM interactive_sessions"
            " WHERE id=?", (row["session_id"],)).fetchone()
    finally:
        con.close()
    assert stored == ("Viva", "cardiac-cycle", 80.0, stored[3])
    assert stored[3] is not None


def test_start_session_opens_and_log_study_result_closes(learner_db):
    opened = learner.start_session("Cardiac Cycle")
    assert opened["completed_at"] is None
    con = sqlite3.connect(learner_db)
    try:
        open_rows = con.execute(
            "SELECT count(*) FROM interactive_sessions WHERE completed_at IS NULL"
        ).fetchone()[0]
    finally:
        con.close()
    assert open_rows == 1

    result = learner.log_study_result("Cardiac Cycle", 7)
    assert result["session_id"] == opened["session_id"]
    assert result["score"] == 70.0
    # the open Learn row was completed in place with a measured duration
    con = sqlite3.connect(learner_db)
    try:
        row = con.execute(
            "SELECT completed_at, score, duration_seconds FROM interactive_sessions"
            " WHERE id=?", (opened["session_id"],)).fetchone()
        open_rows = con.execute(
            "SELECT count(*) FROM interactive_sessions WHERE completed_at IS NULL"
        ).fetchone()[0]
    finally:
        con.close()
    assert row[0] is not None and row[1] == 70.0 and row[2] >= 0
    assert open_rows == 0


def test_mastery_incremental_mean_and_success_ratio(learner_db):
    m = learner.update_mastery("cardiac-cycle", 8)
    assert m["mastery_score"] == 80.0
    assert m["total_attempts"] == 1 and m["successful_attempts"] == 1
    assert m["confidence_score"] == 100.0

    m = learner.update_mastery("cardiac-cycle", 6)  # 60 < PASS_SCORE(70)
    # incremental mean: 80 + (60-80)/2 = 70
    assert m["mastery_score"] == 70.0
    assert m["total_attempts"] == 2 and m["successful_attempts"] == 1
    assert m["confidence_score"] == 50.0

    with pytest.raises(ValueError):
        learner.update_mastery("cardiac-cycle", 150)
    with pytest.raises(ValueError):
        learner.update_mastery("cardiac-cycle", -1)


def test_weakness_escalation_and_resolution(learner_db):
    w = learner.record_weakness("Cardiac Cycle", "Isovolumetric Contraction",
                                "valve opens too early", severity="low")
    assert w["error_count"] == 1 and w["severity"] == "low" and w["is_resolved"] == 0

    w = learner.record_weakness("Cardiac Cycle", "isovolumetric contraction",
                                severity="high")  # same concept, different case
    assert w["error_count"] == 2
    assert w["severity"] == "high"  # escalated from low

    learner.record_weakness("Cardiac Cycle", "Isovolumetric Contraction",
                            severity="critical")
    w = learner.record_weakness("Cardiac Cycle", "Isovolumetric Contraction",
                                severity="low")
    # severity never degrades: stays at the max ever observed
    assert w["severity"] == "critical" and w["error_count"] == 4

    with pytest.raises(ValueError):
        learner.record_weakness("Cardiac Cycle", "x", severity="extreme")
    with pytest.raises(ValueError):
        learner.record_weakness("Cardiac Cycle", "  ")

    assert learner.resolve_weakness(topic="Cardiac Cycle") == 1
    snap = learner.learner_snapshot()
    assert snap["weaknesses"] == []
    assert snap["summary"]["open_weaknesses"] == 0


def test_log_study_result_records_weaknesses(learner_db):
    res = learner.log_study_result(
        "Cardiac Cycle", 5,
        weaknesses=[{"concept": "Stroke volume",
                     "misconception": "thought it equals EDV",
                     "severity": "medium"}])
    assert len(res["weaknesses"]) == 1
    assert res["mastery"]["mastery_score"] == 50.0
    snap = learner.learner_snapshot()
    assert snap["summary"]["open_weaknesses"] == 1
    assert snap["weaknesses"][0]["concept"] == "Stroke volume"


def test_snapshot_orders_weakest_first_and_counts(learner_db):
    learner.record_session("cardiac cycle", score=9)
    learner.record_session("renal physiology", score=4)
    snap = learner.learner_snapshot()
    assert snap["summary"]["topics_studied"] == 2
    assert snap["summary"]["sessions_total"] == 2
    topics = [m["topic_id"] for m in snap["mastery"]]
    assert topics[0] == "renal-physiology"  # weakest first
    assert len(snap["recent_sessions"]) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
