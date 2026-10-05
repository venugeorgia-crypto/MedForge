"""P6 recency-weighted learner model tests (offline, deterministic timestamps).

Covers the directive's 30-item list: first observation, repeated success/failure,
mixed performance, recency weighting + half-life configuration, numerical
stability, insufficient evidence, uncertainty, confidence tracking and
mismatch, weakness creation/repetition/recovery, prerequisite relationships,
explicit session completion, spaced-repetition integration, curriculum
progress, priority scoring, legacy preservation, migration idempotence,
malformed events, deterministic recalculation and model versioning.

No live Ollama anywhere: sessions/reviews are logged directly, and every
attempt carries an explicit ISO timestamp so decay math is reproducible.
"""

from __future__ import annotations

import csv
import json
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
from medforge import curriculum as C
from medforge import learner as L
from medforge import learner_model as LM

from core.database.schema import LEGACY_SCHEMA_DDL, V3_SCHEMA_DDL
from core.database.migrate_v7 import ensure_learner_model_v7, is_v7_applied

D1 = "2026-01-01T09:00:00Z"
D7 = "2026-01-07T09:00:00Z"
D14 = "2026-01-14T09:00:00Z"
D60 = "2026-02-28T09:00:00Z"   # 60 days after D1
D61 = "2026-03-01T09:00:00Z"
D68 = "2026-03-08T09:00:00Z"

GH = "Growth Hormone Physiology"


@pytest.fixture()
def lm_env():
    """Fresh temp META_DB + PRODUCTS; restore after the test."""
    orig_db, orig_products = T.META_DB, T.PRODUCTS
    fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(db_path)
    products = Path(tempfile.mkdtemp())
    T.META_DB, T.PRODUCTS = Path(db_path), products
    try:
        yield Path(db_path), products
    finally:
        T.META_DB, T.PRODUCTS = orig_db, orig_products
        Path(db_path).unlink(missing_ok=True)
        shutil.rmtree(products, ignore_errors=True)
        # The schema cache is keyed by DB path; drop this test's entry so a
        # later test reusing the path would re-ensure the schema.
        LM._ENSURED_DATABASES.discard(str(db_path))


def _fetch(db: Path, sql: str, params=()):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def add(topic: str, frac: float, when: str, confidence: float = None,
        source: str = "session", item_type: str = "session", item_id: str = "",
        correct: bool = None) -> dict:
    return LM.record_learning_event(
        topic, score_fraction=frac, answered_at=when, confidence=confidence,
        source=source, item_type=item_type, item_id=item_id, correct=correct,
        now=when,
    )


def seq(topic: str, pairs, **kwargs) -> dict:
    out = None
    for when, frac in pairs:
        out = add(topic, frac, when, **kwargs)
    return out or {}


# ─── 1-4. Basic update behavior ───


def test_first_observation_is_not_mastery(lm_env):
    r = add(GH, 0.90, D1)
    state = r["state"]
    assert state["evidence_count"] == 1
    assert 0.60 <= state["mastery_score"] <= 0.70   # shrunk toward the 0.5 prior
    assert state["uncertainty"] > 0.6               # one observation is weak
    assert LM.get_mastery(GH)["mastery_percent"] == round(100 * state["mastery_score"], 1)


def test_repeated_success_raises_mastery_and_shrinks_uncertainty(lm_env):
    first = add(GH, 0.90, D1)["state"]
    for when in [D7, D14, "2026-01-21T09:00:00Z", "2026-01-28T09:00:00Z"]:
        last = add(GH, 0.90, when)["state"]
    # The estimate rises but does not jump: five successes reach ~0.78, not 1.0.
    assert last["mastery_score"] > first["mastery_score"] > 0.6
    assert 0.75 < last["mastery_score"] < 0.85
    assert last["uncertainty"] < first["uncertainty"] < 1.0
    assert last["consistency"] is not None and last["consistency"] > 0.9
    # Sustained daily evidence does cross 0.85 (the 0.9 asymptote is reached
    # only after enough non-decayed mass accumulates).
    days = [f"2026-02-{d:02d}" for d in range(1, 11)] + [f"2026-03-{d:02d}" for d in range(1, 11)]
    for day in days:
        steady = add(GH, 0.90, f"{day}T09:00:00Z")["state"]
    assert steady["mastery_score"] > 0.85
    assert steady["uncertainty"] < 0.30


def test_repeated_failure_lowers_mastery(lm_env):
    seq(GH, [(D1, 0.9), (D7, 0.9)])
    before = LM.get_mastery(GH)["mastery"]
    for when in [D14, "2026-01-21T09:00:00Z", "2026-01-28T09:00:00Z"]:
        last = add(GH, 0.25, when)["state"]
    assert last["mastery_score"] < before - 0.15
    assert last["last_failure_at"] == "2026-01-28T09:00:00Z"
    assert last["recent_failures"] == 3


def test_mixed_performance_consistency_and_uncertainty(lm_env):
    state = seq(GH, [(D1, 0.9), (D7, 0.3), (D14, 0.9), ("2026-01-21T09:00:00Z", 0.3)])["state"]
    assert 0.45 < state["mastery_score"] < 0.75
    assert state["consistency"] is not None and state["consistency"] < 0.5
    assert state["uncertainty"] < 0.5  # four events = decent evidence mass


# ─── 5-10. Recency, half-life, stability ───


def test_decay_weight_is_explicit_and_documented():
    assert LM.decay_weight(0.0) == pytest.approx(1.0)
    assert LM.decay_weight(T.LEARNER_HALF_LIFE_DAYS) == pytest.approx(0.5)
    assert LM.decay_weight(2 * T.LEARNER_HALF_LIFE_DAYS) == pytest.approx(0.25)
    assert LM.decay_weight(-5.0) == 1.0  # no weight above 1
    assert LM.decay_weight(30.0, half_life_days=10) == pytest.approx(0.125)
    with pytest.raises(ValueError):
        LM.decay_weight(1.0, half_life_days=0)


def test_old_success_vs_recent_failure(lm_env):
    seq(GH, [(D1, 0.90), (D7, 0.85), (D14, 0.90)])
    before = LM.get_mastery(GH)["mastery"]
    after = add(GH, 0.45, D60)["state"]
    assert after["mastery_score"] < 0.60
    assert after["mastery_score"] < before - 0.10
    assert LM.get_recent_performance(GH)["recent_performance"] == pytest.approx(0.45)


def test_many_old_successes_do_not_outweigh_recent_failures(lm_env):
    old = [(f"2026-01-{d:02d}T09:00:00Z", 0.95) for d in range(1, 11)]
    seq(GH, old)  # ten consistent old successes
    for when in ["2026-03-05T09:00:00Z", "2026-03-06T09:00:00Z", "2026-03-07T09:00:00Z"]:
        last = add(GH, 0.30, when)["state"]
    assert last["evidence_count"] == 13
    assert last["mastery_score"] < 0.60  # recent failures dominate the stale sample


def test_old_failure_vs_recent_success(lm_env):
    seq(GH, [("2026-01-01T09:00:00Z", 0.30), ("2026-01-02T09:00:00Z", 0.30),
             ("2026-01-03T09:00:00Z", 0.30)])
    old = LM.get_mastery(GH)["mastery"]
    for when in ["2026-03-05T09:00:00Z", "2026-03-06T09:00:00Z", "2026-03-07T09:00:00Z"]:
        last = add(GH, 0.90, when)["state"]
    assert last["mastery_score"] > old + 0.25 and last["mastery_score"] > 0.70
    assert last["historical_performance"] < 0.4 <= last["recent_performance"]


def test_half_life_configuration_changes_recency_sensitivity(lm_env):
    seq(GH, [(D1, 0.90), (D7, 0.90), (D14, 0.90), (D60, 0.45)])
    attempts = LM.learner_history(GH, limit=10)
    attempts = list(reversed(attempts))
    from datetime import datetime, timezone

    now = datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc)
    slow = LM.compute_model_state(attempts, now, half_life_days=84.0)
    fast = LM.compute_model_state(attempts, now, half_life_days=7.0)
    # A short half-life forgets old successes faster → recent failure weighs more.
    assert fast["mastery_score"] < slow["mastery_score"]
    assert fast["weighted_evidence"] < slow["weighted_evidence"]
    stored = LM.get_mastery(GH)
    assert stored["model_version"] == T.LEARNER_MODEL_VERSION


def test_numerical_stability_with_many_attempts(lm_env):
    for i in range(300):
        add(GH, 0.9 if i % 5 else 0.4, f"2026-01-{1 + i % 28:02d}T09:00:00Z")
    state = LM.get_mastery(GH)
    assert 0.0 <= state["mastery"] <= 1.0
    assert 0.0 < state["uncertainty"] <= 1.0
    assert state["uncertainty"] == state["uncertainty"]  # not NaN
    assert LM.recalculate_mastery(GH)["matches_stored"] is True


# ─── 11-12. Evidence sufficiency + uncertainty ───


def test_insufficient_evidence_keeps_uncertainty_high(lm_env):
    one = add(GH, 0.9, D1)["state"]
    two = add(GH, 0.9, D7)["state"]
    assert two["evidence_count"] == 2
    assert two["uncertainty"] > 0.5 and two["uncertainty"] < one["uncertainty"]


def test_uncertainty_decreases_monotonically_with_evidence(lm_env):
    values = [add(GH, 0.9, f"2026-01-{d:02d}T09:00:00Z")["state"]["uncertainty"]
              for d in range(1, 9)]
    assert all(b <= a for a, b in zip(values, values[1:]))
    assert values[-1] < values[0]


# ─── 13-14. Confidence (separate from mastery) ───


def test_confidence_is_tracked_separately_from_mastery(lm_env):
    seq(GH, [(D1, 0.8), (D7, 0.8), (D14, 0.8)], confidence=0.6)
    state = LM.get_mastery(GH)
    conf = LM.get_confidence(GH)
    assert conf["has_confidence_data"] is True
    assert conf["confidence_estimate"] == pytest.approx(0.6)
    assert conf["mastery"] == pytest.approx(state["mastery"])
    assert conf["confidence_estimate"] != conf["mastery"]  # distinct variables
    assert conf["direction"] in ("overconfident", "underconfident", "aligned")


def test_confidence_mismatch_states(lm_env):
    # HIGH CONFIDENCE + LOW PERFORMANCE → overconfident.
    seq("Renal Physiology", [(D1, 0.30), (D7, 0.30), (D14, 0.30)], confidence=0.95)
    over = LM.get_confidence("Renal Physiology")
    assert over["calibration_gap"] > 0.2 and over["mismatch"] is True
    assert over["direction"] == "overconfident"
    # LOW CONFIDENCE + HIGH PERFORMANCE → underconfident.
    seq("Cardiac Cycle", [(D1, 0.95), (D7, 0.95), (D14, 0.95)], confidence=0.15)
    under = LM.get_confidence("Cardiac Cycle")
    assert under["calibration_gap"] < -0.2 and under["mismatch"] is True
    assert under["direction"] == "underconfident"
    # Aligned case is not a mismatch.
    seq("Respiratory Physiology", [(D1, 0.9), (D7, 0.9)], confidence=0.9)
    aligned = LM.get_confidence("Respiratory Physiology")
    assert aligned["mismatch"] is False
    # No confidence observations → no fabricated calibration.
    seq("Endocrine Pathology", [(D1, 0.9), (D7, 0.9)])
    none = LM.get_confidence("Endocrine Pathology")
    assert none["has_confidence_data"] is False
    assert none["calibration_gap"] is None and none["direction"] is None


# ─── 15-19. Weakness detection, escalation, recovery ───


def test_weakness_created_from_recent_failure(lm_env):
    seq(GH, [(D1, 0.90), (D7, 0.85), (D14, 0.90)])
    add(GH, 0.45, D60)
    result = LM.detect_weaknesses(GH, now=D60)
    assert result["created"] == 1
    mastery = LM.get_mastery(GH)["mastery"]
    assert 0.50 < mastery < 0.60  # the recent failure moved the estimate
    rows = LM.get_weaknesses()
    assert len(rows) == 1
    w = rows[0]
    assert w["origin"] == "learner_model"
    assert w["concept"] == LM.WEAKNESS_CONCEPT
    assert w["severity"] == "medium"          # mastery ~0.55 stays 'medium'
    assert w["low_confidence"] == 0           # 4 events = known weakness
    assert w["failure_count"] == 1
    assert w["recent_failure_rate"] > 0
    assert w["weakness_score"] == pytest.approx(
        round(max(0.0, (T.LEARNER_WEAK_THRESHOLD - mastery)) / T.LEARNER_WEAK_THRESHOLD, 4)
    )
    assert w["last_failure_at"] == D60
    assert "recent failure" in w["misconception"]


def test_single_failure_is_possible_weakness_not_a_verdict(lm_env):
    add(GH, 0.30, D1)
    LM.detect_weaknesses(GH, now=D1)
    w = LM.get_weaknesses()[0]
    assert w["low_confidence"] == 1
    assert w["severity"] == "low"
    assert "limited evidence" in w["misconception"]


def test_repeated_failure_updates_the_same_weakness(lm_env):
    add(GH, 0.30, D1)
    first = LM.detect_weaknesses(GH, now=D1)
    assert first["created"] == 1 and first["updated"] == 0
    add(GH, 0.25, D7)
    second = LM.detect_weaknesses(GH, now=D7)
    assert second["created"] == 0 and second["updated"] == 1
    con = sqlite3.connect(T.META_DB)
    try:
        n = con.execute(
            "SELECT count(*) FROM learner_weaknesses WHERE topic_id=? AND origin='learner_model'",
            ("growth-hormone-physiology",),
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 1  # one row, enriched — not duplicates
    w = LM.get_weaknesses()[0]
    assert w["failure_count"] == 2
    assert w["last_observed_at"] == D7


def test_weakness_recovers_after_sustained_success(lm_env):
    seq(GH, [(D1, 0.90), (D7, 0.85), (D14, 0.90)])
    add(GH, 0.45, D60)
    LM.detect_weaknesses(GH, now=D60)
    assert len(LM.get_weaknesses()) == 1
    # Recovery: two consecutive strong passes lift the estimate back over the
    # recovery floor; the weakness is auto-resolved but its history is kept.
    add(GH, 0.80, D61)
    add(GH, 0.90, D68)
    state = LM.get_mastery(GH)
    assert state["mastery"] > 0.60
    result = LM.detect_weaknesses(GH, now=D68)
    assert result["recovered"] == 1
    assert LM.get_weaknesses() == []          # no unresolved model weakness
    resolved = LM.get_weaknesses(include_resolved=True)
    assert len(resolved) == 1
    assert resolved[0]["is_resolved"] == 1
    assert resolved[0]["recovered_at"] == D68
    assert resolved[0]["review_priority"] == 0.0
    assert state["evidence_count"] == 6       # all six attempts preserved
    assert len(LM.learner_history(GH, limit=10)) == 6


def test_manual_weaknesses_are_never_touched_by_recovery(lm_env):
    manual = L.record_weakness(GH, "Feedback loops", "Confuses long-loop feedback", "high")
    seq(GH, [(D1, 0.95), (D7, 0.95), (D14, 0.95)])
    result = LM.detect_weaknesses(GH, now=D14)
    assert result["recovered"] == 0
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM learner_weaknesses WHERE id=?", (manual["id"],)
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    assert row["is_resolved"] == 0             # manual row stays unresolved
    assert row["origin"] == "manual"           # never rewritten by P6
    assert row["severity"] == "high"


# ─── 20-21. Prerequisite awareness ───


def _prereq_env():
    """Curriculum with a prerequisite edge: GH axis → Growth plate physiology."""
    C.import_syllabus(
        "Week 1: Pituitary\n- GH axis\n- IGF-1 mediation\n"
        "Week 2: Growth\n- Growth plate physiology\n",
        subject_title="Endocrinology",
    )
    C.add_prerequisite("Growth plate physiology", "GH axis", "strict", 1.0)


def test_prerequisite_risks_are_reported_without_penalizing_the_topic(lm_env):
    _prereq_env()
    seq("GH axis", [(D1, 0.30), (D7, 0.30), (D14, 0.25)])   # weak prerequisite
    seq("Growth plate physiology", [(D1, 0.80), (D7, 0.80)])  # topic itself ok
    topic_before = LM.get_mastery("Growth plate physiology")["mastery"]
    risks = LM.get_prerequisite_risks("Growth plate physiology")
    assert risks["node_id"] is not None
    assert risks["weak_count"] == 1
    entry = risks["risks"][0]
    assert entry["title"] == "GH axis"
    assert entry["weak"] is True and entry["has_learner_data"] is True
    assert 0.0 < entry["impact"] <= 1.0
    assert entry["mastery"] < T.LEARNER_WEAK_THRESHOLD
    # Reading prerequisite risks never mutates the topic's own estimate.
    assert LM.get_mastery("Growth plate physiology")["mastery"] == topic_before
    # A topic outside the curriculum reports no node instead of inventing one.
    unmapped = LM.get_prerequisite_risks("Totally Unstructured Topic")
    assert unmapped["node_id"] is None and unmapped["risks"] == []


def test_prerequisite_weakness_feeds_priority_but_not_mastery(lm_env):
    _prereq_env()
    seq("GH axis", [(D1, 0.30), (D7, 0.30), (D14, 0.25)])
    # Two topics with identical own evidence; only one has a weak prerequisite.
    seq("Growth plate physiology", [(D1, 0.30), (D7, 0.30), (D14, 0.25)])
    seq("Standalone Weak Topic", [(D1, 0.30), (D7, 0.30), (D14, 0.25)])
    a = LM.get_mastery("GH axis")["mastery"]
    b_before = LM.get_mastery("Growth plate physiology")["mastery"]
    prio = LM.study_priority(now=D14)
    by_topic = {i["topic"]: i for i in prio["items"]}
    dependent = by_topic["growth-plate-physiology"]
    standalone = by_topic["standalone-weak-topic"]
    assert dependent["components"]["prerequisite_impact"] > 0
    assert standalone["components"]["prerequisite_impact"] == 0
    assert dependent["priority"] > standalone["priority"]
    # Priority still never rewrites the topic's own mastery.
    assert LM.get_mastery("Growth plate physiology")["mastery"] == b_before
    assert LM.get_mastery("GH axis")["mastery"] == a


# ─── 22. Deterministic priority scoring ───


def test_priority_components_are_explicit_and_reproducible(lm_env):
    seq(GH, [(D1, 0.9), (D7, 0.9)])
    seq("Renal Physiology", [(D1, 0.4), (D7, 0.35), (D14, 0.30)])
    L.ensure_v3_tables()
    con = sqlite3.connect(T.META_DB)
    try:
        con.execute(
            "INSERT INTO spaced_repetition_queue (item_type, item_id, due_date,"
            " created_at, updated_at) VALUES('card', 'growth-hormone-physiology:abc',"
            " '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z')"
        )
        con.commit()
    finally:
        con.close()
    now = "2026-03-10T00:00:00Z"
    prio = LM.study_priority(now=now)
    assert prio["weights"] == dict(T.LEARNER_PRIORITY_WEIGHTS)
    assert prio["model_version"] == T.LEARNER_MODEL_VERSION
    items = prio["items"]
    assert [i["priority"] for i in items] == sorted(
        (i["priority"] for i in items), reverse=True
    )
    for i in items:
        expected = round(sum(
            prio["weights"][k] * i["components"][k] for k in prio["weights"]
        ), 4)
        assert i["priority"] == expected
    overdue = next(i for i in items if i["topic"] == "growth-hormone-physiology")
    assert overdue["components"]["overdue"] == 1.0   # 9 days overdue caps at 1
    assert overdue["overdue_days"] == pytest.approx(9.0)
    weak = next(i for i in items if i["topic"] == "renal-physiology")
    assert weak["components"]["recent_failure"] > 0
    # Identical inputs → identical ranking (determinism), and the named P7
    # alias returns the same signal.
    again = LM.study_priority(now=now)
    assert [i["topic"] for i in again["items"]] == [i["topic"] for i in items]
    assert LM.get_review_priority(now=now) == prio


# ─── 23-24. Explicit session completion (P6 session-id safety) ───


def _sessions(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT id, topic_id, score, completed_at FROM interactive_sessions ORDER BY id"
        )]
    finally:
        con.close()


def test_explicit_session_completion_is_isolated(lm_env):
    db, _products = lm_env
    first = L.start_session(GH, "Learn")
    second = L.start_session(GH, "Active Recall")
    done = L.log_study_result(GH, 85, session_id=first["session_id"])
    assert done["session_id"] == first["session_id"]
    assert done["learner_model"]["evidence_count"] == 1
    rows = {r["id"]: r for r in _sessions(db)}
    assert rows[first["session_id"]]["completed_at"] is not None
    assert rows[second["session_id"]]["completed_at"] is None  # untouched
    done2 = L.complete_session(second["session_id"], 40)
    assert done2["session_id"] == second["session_id"]
    rows = {r["id"]: r for r in _sessions(db)}
    assert rows[second["session_id"]]["completed_at"] is not None
    assert rows[first["session_id"]]["score"] == 85.0
    assert rows[second["session_id"]]["score"] == 40.0
    assert LM.get_mastery(GH)["evidence_count"] == 2


def test_session_completion_validation_blocks_wrong_targets(lm_env):
    first = L.start_session(GH, "Learn")
    second = L.start_session(GH, "Learn")
    L.log_study_result(GH, 85, session_id=first["session_id"])
    with pytest.raises(ValueError):   # already completed
        L.log_study_result(GH, 85, session_id=first["session_id"])
    with pytest.raises(ValueError):   # another topic's open session
        L.log_study_result("Renal Physiology", 85, session_id=second["session_id"])
    with pytest.raises(ValueError):   # unknown session
        L.log_study_result(GH, 85, session_id=999999)
    with pytest.raises(ValueError):
        L.complete_session(999999, 85)
    L.complete_session(second["session_id"], 85)
    with pytest.raises(ValueError):   # double completion
        L.complete_session(second["session_id"], 85)
    # Rejected calls left no orphan evidence anywhere.
    assert LM.get_mastery("Renal Physiology") is None
    assert LM.get_mastery(GH)["evidence_count"] == 2


# ─── 25. Spaced-repetition integration ───


def _write_pack(products, folder, cards):
    pack = products / folder
    pack.mkdir(parents=True, exist_ok=True)
    path = pack / "flashcards.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Question", "Answer", "Sources"])
        for question, answer in cards:
            w.writerow([question, answer, "[S1]"])
    return path


def test_spaced_repetition_reviews_feed_the_learner_model(lm_env):
    _db, products = lm_env
    csv_path = _write_pack(
        products, "growth-hormone-physiology",
        [("What is the GH axis?", "GHRH → GH → IGF-1")],
    )
    L.import_flashcards(GH, csv_path)
    item_id = L.due_items(topic=GH)[0]["item_id"]
    fail = L.review_card(item_id, 1, now=D1)
    assert fail["learner_model"]["evidence_count"] == 1
    assert fail["learner_model"]["last_failure_at"] == D1
    ok = L.review_card(item_id, 5, now=D7)
    state = ok["learner_model"]
    assert state["evidence_count"] == 2
    assert state["last_success_at"] == D7
    history = LM.learner_history(GH, limit=10)
    assert history[0]["source"] == "review" and history[0]["item_type"] == "card"
    assert history[0]["score"] == pytest.approx(1.0)
    assert history[0]["correct"] == 1
    assert history[1]["score"] == pytest.approx(0.2)
    assert history[1]["correct"] == 0
    assert LM.recalculate_mastery(GH)["matches_stored"] is True


# ─── 26-27. Curriculum + dashboard integration ───


def _all_topic_entries(prog):
    out = []
    for subject in prog["subjects"]:
        for week in subject["weeks"]:
            out.extend(week["topics"])
            for seminar in week["seminars"]:
                out.extend(seminar["topics"])
    return out


def test_curriculum_progress_exposes_learner_model_fields(lm_env):
    C.import_syllabus(
        "Week 1: Hypothalamus & Pituitary\n"
        "Seminar: Pituitary hormones\n"
        "- Anterior pituitary hormones\n"
    )
    seq("Anterior pituitary hormones", [(D1, 0.9), (D7, 0.85)])
    prog = C.curriculum_progress()
    assert prog["summary"]["learner_model_tracked"] == 1
    entry = next(e for e in _all_topic_entries(prog)
                 if e["title"] == "Anterior pituitary hormones")
    state = LM.get_mastery("Anterior pituitary hormones")
    assert entry["rwm_mastery"] == state["mastery"]
    assert entry["rwm_evidence_count"] == 2
    assert entry["rwm_uncertainty"] == state["uncertainty"]
    # P2's own join is untouched: no legacy mastery rows were written.
    assert entry["mastery"] is None and entry["attempts"] == 0


def test_learner_snapshot_includes_model_summary(lm_env):
    result = L.record_session(GH, score=85)
    assert "learner_model_error" not in result
    snap = L.learner_snapshot()
    assert snap["summary"]["topics_studied"] == 1
    assert snap["mastery"][0]["mastery_score"] == 85.0  # legacy mean intact
    model = snap["model"]
    assert model["model_version"] == T.LEARNER_MODEL_VERSION
    assert model["half_life_days"] == T.LEARNER_HALF_LIFE_DAYS
    assert model["summary"]["topics_tracked"] == 1
    assert model["summary"]["events_total"] == 1


# ─── 28-31. Migration, legacy preservation, malformed events ───


def test_legacy_flows_keep_producing_legacy_results(lm_env):
    first = L.record_session(GH, score=85)
    assert first["mastery"]["mastery_score"] == 85.0
    assert first["learner_model"]["evidence_count"] == 1
    second = L.record_session(GH, score=55)
    assert second["mastery"]["mastery_score"] == 70.0   # incremental mean
    assert second["mastery"]["confidence_score"] == 50.0
    manual = L.record_weakness(GH, "Feedback loops", "mix-up", "high")
    assert manual["origin"] == "manual"
    snap = L.learner_snapshot()
    assert snap["summary"]["open_weaknesses"] == 1
    assert snap["summary"]["avg_mastery"] == 70.0


def test_v7_migration_preserves_pre_existing_legacy_rows(lm_env):
    db, _products = lm_env
    con = sqlite3.connect(db)
    try:
        con.executescript(LEGACY_SCHEMA_DDL)
        con.executescript(V3_SCHEMA_DDL)
        con.execute(
            "INSERT INTO learner_mastery (topic_id, mastery_score, confidence_score,"
            " total_attempts, successful_attempts, last_attempt_at, created_at, updated_at)"
            " VALUES('gh-axis', 82.5, 100.0, 3, 3, '2026-01-01T00:00:00Z',"
            " '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')"
        )
        con.execute(
            "INSERT INTO learner_weaknesses (topic_id, concept, misconception,"
            " error_count, severity, is_resolved, first_observed_at, last_observed_at)"
            " VALUES('gh-axis', 'Feedback loops', 'mix-up', 4, 'high', 0,"
            " '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')"
        )
        con.commit()
    finally:
        con.close()
    result = ensure_learner_model_v7(db)
    assert result["status"] == "success" and result["integrity"] == "ok"
    assert len(result["weakness_columns_added"]) == 10
    assert is_v7_applied(db)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        m = con.execute("SELECT * FROM learner_mastery WHERE topic_id='gh-axis'").fetchone()
        w = con.execute(
            "SELECT * FROM learner_weaknesses WHERE concept='Feedback loops'"
        ).fetchone()
        n = con.execute(
            "SELECT count(*) FROM schema_migrations WHERE version='7.0.0'"
        ).fetchone()[0]
    finally:
        con.close()
    assert m["mastery_score"] == 82.5 and m["total_attempts"] == 3
    assert w["severity"] == "high" and w["error_count"] == 4  # legacy data intact
    assert w["origin"] == "manual"                            # documented default
    assert w["weakness_score"] is None and w["low_confidence"] == 0
    assert n == 1


def test_v7_migration_is_idempotent_and_keeps_learner_data(lm_env):
    db, _products = lm_env
    first = ensure_learner_model_v7(db)
    assert len(first["weakness_columns_added"]) == 10
    add(GH, 0.90, D1)
    second = ensure_learner_model_v7(db)
    assert second["weakness_columns_added"] == []
    assert second["integrity"] == "ok"
    assert is_v7_applied(db)
    assert LM.get_mastery(GH)["evidence_count"] == 1
    con = sqlite3.connect(db)
    try:
        n = con.execute(
            "SELECT count(*) FROM schema_migrations WHERE version='7.0.0'"
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 1


def test_malformed_learning_events_are_rejected(lm_env):
    with pytest.raises(ValueError):
        LM.record_learning_event("   ", score_fraction=0.5)
    with pytest.raises(ValueError):
        LM.record_learning_event(GH)                       # no score at all
    with pytest.raises(ValueError):
        LM.record_learning_event(GH, score_fraction="nope")
    with pytest.raises(ValueError):
        LM.record_learning_event(GH, score_fraction=1.5)
    with pytest.raises(ValueError):
        LM.record_learning_event(GH, score_fraction=0.5, item_type="blog")
    with pytest.raises(ValueError):
        LM.record_learning_event(GH, score_fraction=0.5, source="telepathy")
    with pytest.raises(ValueError):
        LM.record_learning_event(GH, score_fraction=0.5, confidence=1.2)
    with pytest.raises(ValueError):
        LM.record_learning_event(GH, score_fraction=0.5, response_time_seconds=-1)
    with pytest.raises(ValueError):
        LM.record_learning_event(GH, score_fraction=0.5, session_id=12345)
    with pytest.raises(ValueError):
        LM.record_learning_event(GH, score_fraction=0.5, curriculum_node_id="nope")
    with pytest.raises(ValueError):
        LM.record_learning_event(GH, score_fraction=0.5, answered_at="not-a-date")
    assert LM.get_mastery(GH) is None    # nothing partial was persisted
    ok = LM.record_learning_event(
        GH, score_fraction=0.9, answered_at=D1, content_version="pack-v2"
    )
    assert ok["attempt"]["content_version"] == "pack-v2"


# ─── 32. Deterministic recalculation + model version ───


def test_recalculation_and_model_version_are_stable(lm_env):
    seq(GH, [(D1, 0.9), (D7, 0.4), (D14, 0.85)])
    seq("Renal Physiology", [(D1, 0.7)])
    before = LM.get_mastery(GH)
    assert before["model_version"] == T.LEARNER_MODEL_VERSION
    one = LM.recalculate_mastery(GH)
    assert one["matches_stored"] is True
    assert one["rebuilt"]["mastery_score"] == before["mastery"]
    everything = LM.recalculate_all()
    assert everything["keys"] == 2 and everything["mismatches"] == []
    assert everything["model_version"] == T.LEARNER_MODEL_VERSION
    # Explicit re-evaluation at a later date legitimately moves the estimate…
    later = LM.recalculate_mastery(GH, now="2026-06-01T00:00:00Z")
    assert later["matches_stored"] is False    # stored was evaluated earlier
    after = LM.get_mastery(GH)
    assert after["mastery"] == later["rebuilt"]["mastery_score"]
    assert after["evaluated_at"] == "2026-06-01T00:00:00Z"
    assert after["evidence_count"] == 3        # history length never changes
    # …and the rebuilt state is again exactly reproducible.
    assert LM.recalculate_mastery(GH)["matches_stored"] is True
    assert LM.get_mastery(GH)["model_version"] == T.LEARNER_MODEL_VERSION
