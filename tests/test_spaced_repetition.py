"""Unit tests for the spaced repetition queue (offline-safe).

Uses a fresh temp META_DB plus a temp PRODUCTS tree, following the dynamic
config pattern of tests/test_learner.py (learner reads T.META_DB and
T.PRODUCTS at call time).
"""

from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

import medforge.types as T
from medforge import learner


@pytest.fixture()
def sr_env():
    """Fresh temp META_DB and PRODUCTS dir for the duration of a test."""
    orig_db, orig_products = T.META_DB, T.PRODUCTS
    fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(db_path)  # let sqlite create it fresh
    products = Path(tempfile.mkdtemp())
    T.META_DB = Path(db_path)
    T.PRODUCTS = products
    try:
        yield Path(db_path), products
    finally:
        T.META_DB, T.PRODUCTS = orig_db, orig_products
        Path(db_path).unlink(missing_ok=True)
        shutil.rmtree(products, ignore_errors=True)


def write_pack(products, folder, cards, version="v001", topic=None):
    """Write a minimal pack flashcards.csv (+ state.json); return the csv path."""
    pack = products / folder / version
    pack.mkdir(parents=True, exist_ok=True)
    path = pack / "flashcards.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Question", "Answer", "Sources"])
        for question, answer in cards:
            w.writerow([question, answer, "[S1]"])
    if topic:
        (pack / "state.json").write_text(
            '{"topic": "%s", "steps": {}}' % topic, encoding="utf-8")
    return path


def test_import_flashcards_idempotent_and_due_list(sr_env):
    _db, products = sr_env
    csv_path = write_pack(products, "cardiac-cycle", [
        ("What is EDV?", "End-diastolic volume"),
        ("What is ESV?", "End-systolic volume"),
    ])
    first = learner.import_flashcards("Cardiac Cycle", csv_path)
    assert first["topic_id"] == "cardiac-cycle"
    assert first["cards_in_pack"] == 2
    assert first["imported"] == 2 and first["scheduled"] == 2

    second = learner.import_flashcards("Cardiac Cycle", csv_path)
    assert second["imported"] == 0 and second["scheduled"] == 2

    due = learner.due_items()
    assert {d["question"] for d in due} == {"What is EDV?", "What is ESV?"}
    assert all(d["state"] == "new" for d in due)
    assert all(d["ease_factor"] == pytest.approx(2.50) for d in due)
    by_question = {d["question"]: d for d in due}
    assert by_question["What is EDV?"]["answer"] == "End-diastolic volume"
    assert by_question["What is EDV?"]["sources"] == "[S1]"
    assert by_question["What is EDV?"]["topic_id"] == "cardiac-cycle"
    assert by_question["What is EDV?"]["item_id"] == \
        "cardiac-cycle:" + learner.card_item_id("What is EDV?")
    assert learner.learner_snapshot()["summary"]["cards_due"] == 2


def test_import_flashcards_uses_newest_pack_and_state_topic(sr_env):
    _db, products = sr_env
    write_pack(products, "cardiac-cycle", [("Old card?", "old answer")], version="v001")
    write_pack(products, "cardiac-cycle", [
        ("New card 1?", "a1"), ("New card 2?", "a2"),
    ], version="v002")

    # No explicit path: the newest version for the topic is used.
    res = learner.import_flashcards("Cardiac Cycle")
    assert res["cards_in_pack"] == 2 and res["imported"] == 2

    # Folder names may carry a hash suffix; state.json's topic wins.
    hashed = write_pack(products, "renal-physiology-deadbeef", [
        ("What is GFR?", "Glomerular filtration rate"),
    ])
    (hashed.parent / "state.json").write_text(
        '{"topic": "Renal Physiology"}', encoding="utf-8")
    res = learner.import_flashcards("renal-physiology-deadbeef", hashed)
    assert res["topic_id"] == "renal-physiology"

    # Topic filter only returns that topic's due cards.
    due = learner.due_items(topic="Renal Physiology")
    assert [d["question"] for d in due] == ["What is GFR?"]
    assert len(learner.due_items(limit=1)) == 1


def test_review_card_sm2_progression(sr_env):
    _db, products = sr_env
    csv_path = write_pack(products, "cardiac-cycle", [("What is EDV?", "EDV")])
    learner.import_flashcards("cardiac cycle", csv_path)
    item_id = "cardiac-cycle:" + learner.card_item_id("What is EDV?")

    r1 = learner.review_card(item_id, 5)
    assert (r1["repetition_count"], r1["interval_days"], r1["state"]) == (1, 1.0, "learning")
    assert r1["ease_factor"] == pytest.approx(2.6)
    assert r1["last_grade"] == 5
    assert r1["due_date"] > r1["last_reviewed_at"]

    r2 = learner.review_card(item_id, 4)
    assert (r2["repetition_count"], r2["interval_days"], r2["state"]) == (2, 6.0, "review")
    assert r2["ease_factor"] == pytest.approx(2.6)  # grade 4 leaves ease unchanged

    r3 = learner.review_card(item_id, 5)
    assert r3["repetition_count"] == 3
    assert r3["ease_factor"] == pytest.approx(2.7)
    assert r3["interval_days"] == pytest.approx(round(6.0 * 2.7, 2))

    # Completed for today: no longer due.
    assert item_id not in {d["item_id"] for d in learner.due_items()}
    snap = learner.spaced_repetition_snapshot()
    assert snap["summary"]["due_cards"] == 0
    assert snap["summary"]["review_cards"] == 1
    assert snap["summary"]["next_due_at"] == r3["due_date"]
    assert snap["summary"]["reviews_today"] == 3
    assert snap["summary"]["scheduler"] == "sm2"
    assert snap["analytics"]["retention"] == 100.0
    assert snap["analytics"]["streak_days"] == 1

    # Every review is appended to review_log with before/after scheduling state.
    con = sqlite3.connect(T.META_DB)
    try:
        log = con.execute(
            "SELECT grade, scheduler, interval_before, interval_after, state_before, state_after"
            " FROM review_log WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
    finally:
        con.close()
    assert [row[0] for row in log] == [5, 4, 5]
    assert {row[1] for row in log} == {"sm2"}
    assert log[0][2] == 0.0 and log[0][3] == 1.0 and log[0][4] == "new"
    assert log[1][2] == 1.0 and log[1][3] == 6.0
    assert log[2][3] == pytest.approx(16.2)


def test_review_card_failure_resets_and_ease_floors(sr_env):
    _db, products = sr_env
    csv_path = write_pack(products, "cardiac-cycle", [("What is EDV?", "EDV")])
    learner.import_flashcards("cardiac cycle", csv_path)
    item_id = "cardiac-cycle:" + learner.card_item_id("What is EDV?")

    learner.review_card(item_id, 5)
    r = learner.review_card(item_id, 1)  # fail → short relearning step
    assert (r["repetition_count"], r["interval_days"], r["state"]) == (0, 0.0, "relearning")
    assert r["ease_factor"] == pytest.approx(2.06)
    # Sub-day step: back after LAPSE_STEP_MINUTES, not tomorrow.
    assert item_id not in {d["item_id"] for d in learner.due_items()}
    due_at = r["due_date"]
    assert item_id in {d["item_id"] for d in learner.due_items(now=due_at)}
    started = datetime.fromisoformat(r["last_reviewed_at"].replace("Z", "+00:00"))
    due_dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    assert (due_dt - started).total_seconds() == pytest.approx(learner.LAPSE_STEP_MINUTES * 60)

    for _ in range(6):  # repeated blackouts floor ease at the schema minimum
        learner.review_card(item_id, 0)
    con = sqlite3.connect(T.META_DB)
    try:
        ease = con.execute(
            "SELECT ease_factor FROM spaced_repetition_queue WHERE item_id=?",
            (item_id,)).fetchone()[0]
    finally:
        con.close()
    assert ease == pytest.approx(learner.SM2_MIN_EASE)


def test_review_card_validates_inputs(sr_env):
    _db, products = sr_env
    csv_path = write_pack(products, "cardiac-cycle", [("What is EDV?", "EDV")])
    learner.import_flashcards("cardiac cycle", csv_path)
    item_id = "cardiac-cycle:" + learner.card_item_id("What is EDV?")

    with pytest.raises(ValueError):
        learner.review_card("cardiac-cycle:missing", 5)
    with pytest.raises(ValueError):
        learner.review_card(item_id, 6)
    with pytest.raises(ValueError):
        learner.review_card(item_id, "great")
    with pytest.raises(ValueError):
        learner.review_card(item_id, 5, item_type="lecture")
    with pytest.raises(ValueError):
        learner.review_card(item_id, 5, scheduler="anki")
    with pytest.raises(ValueError):
        learner.review_card(item_id, 5, now="not-a-date")


def test_lapse_records_weakness_and_graduation_resolves(sr_env):
    _db, products = sr_env
    csv_path = write_pack(products, "cardiac-cycle", [("What is EDV?", "EDV")])
    learner.import_flashcards("cardiac cycle", csv_path)
    item_id = "cardiac-cycle:" + learner.card_item_id("What is EDV?")

    # new → learning → review: an ordinary learning path records no weakness
    assert learner.review_card(item_id, 5)["weakness_recorded"] is False
    assert learner.review_card(item_id, 5)["state"] == "review"
    assert learner.learner_snapshot()["summary"]["open_weaknesses"] == 0

    # a lapse after graduation is a real knowledge gap → weakness recorded
    r = learner.review_card(item_id, 1)
    assert r["weakness_recorded"] is True and r["state"] == "relearning"
    weak = learner.learner_snapshot()["weaknesses"]
    assert len(weak) == 1
    assert weak[0]["topic_id"] == "cardiac-cycle"
    assert weak[0]["concept"] == "What is EDV?"
    assert "Lapsed" in weak[0]["misconception"]

    # graduating back to review resolves it
    learner.review_card(item_id, 4)  # relearning → learning
    assert learner.review_card(item_id, 4)["weakness_resolved"] is True
    assert learner.learner_snapshot()["summary"]["open_weaknesses"] == 0


def test_review_analytics_counts_retention_and_leeches(sr_env):
    _db, products = sr_env
    csv_path = write_pack(products, "cardiac-cycle", [
        ("Card A?", "a"), ("Card B?", "b"),
    ])
    learner.import_flashcards("cardiac cycle", csv_path)
    a = "cardiac-cycle:" + learner.card_item_id("Card A?")
    b = "cardiac-cycle:" + learner.card_item_id("Card B?")

    learner.review_card(a, 5)
    for _ in range(3):  # Card B keeps failing → leech
        learner.review_card(b, 1)

    analytics = learner.review_analytics()
    assert analytics["reviews_total"] == 4
    assert analytics["reviews_today"] == 4
    assert analytics["retention"] == 25.0  # one pass of four reviews
    assert analytics["streak_days"] == 1
    assert len(analytics["reviews_by_day"]) == 30
    assert analytics["reviews_by_day"][-1]["reviews"] == 4
    leeches = {x["item_id"]: x for x in analytics["leeches"]}
    assert b in leeches and a not in leeches
    assert leeches[b]["lapses"] == 3 and leeches[b]["reviews"] == 3
    assert leeches[b]["question"] == "Card B?"
    assert leeches[b]["topic_id"] == "cardiac-cycle"


def test_fsrs_scheduler_uses_stability_and_difficulty(sr_env):
    _db, products = sr_env
    csv_path = write_pack(products, "cardiac-cycle", [
        ("Hard card?", "h"), ("Good card?", "g"), ("Easy card?", "e"),
    ])
    learner.import_flashcards("cardiac cycle", csv_path)
    hard = "cardiac-cycle:" + learner.card_item_id("Hard card?")
    good = "cardiac-cycle:" + learner.card_item_id("Good card?")
    easy = "cardiac-cycle:" + learner.card_item_id("Easy card?")

    rh = learner.review_card(hard, 3, scheduler="fsrs")
    rg = learner.review_card(good, 4, scheduler="fsrs")
    re_ = learner.review_card(easy, 5, scheduler="fsrs")
    assert rh["scheduler"] == "fsrs"
    assert rh["stability"] == pytest.approx(learner.FSRS_WEIGHTS[1], abs=1e-3)
    assert rg["stability"] == pytest.approx(learner.FSRS_WEIGHTS[2], abs=1e-3)
    assert re_["stability"] == pytest.approx(learner.FSRS_WEIGHTS[3], abs=1e-3)
    assert rh["interval_days"] < rg["interval_days"] < re_["interval_days"]
    assert 1.0 <= rh["difficulty"] <= 10.0

    # immediate repeat: retrievability ~1 means no stability gain, difficulty drops
    again = learner.review_card(easy, 5, scheduler="fsrs")
    assert again["interval_days"] == pytest.approx(re_["interval_days"], abs=0.02)
    assert again["difficulty"] < re_["difficulty"]

    # a lapse uses the post-lapse stability and the short step
    lapse = learner.review_card(easy, 0, scheduler="fsrs")
    assert lapse["state"] == "relearning"
    assert 0.0 < lapse["stability"] < re_["stability"]
    assert lapse["due_in_days"] == pytest.approx(learner.LAPSE_STEP_MINUTES / 1440.0)
    con = sqlite3.connect(T.META_DB)
    try:
        schedulers = {row[0] for row in con.execute("SELECT DISTINCT scheduler FROM review_log")}
    finally:
        con.close()
    assert schedulers == {"fsrs"}


def test_weak_topics_surface_their_cards_first(sr_env):
    _db, products = sr_env
    a_csv = write_pack(products, "topic-a", [("Card A?", "a")])
    b_csv = write_pack(products, "topic-b", [("Card B?", "b")])
    learner.import_flashcards("topic a", a_csv)
    learner.import_flashcards("topic b", b_csv)

    order = [d["topic_id"] for d in learner.due_items()]
    assert order == ["topic-a", "topic-b"]  # stable by due date/id while both equal
    assert not any(d["topic_weak"] for d in learner.due_items())

    learner.update_mastery("topic-b", 40)  # weak mastery → B first
    due = learner.due_items()
    assert [d["topic_id"] for d in due][0] == "topic-b"
    assert due[0]["topic_weak"] is True and due[1]["topic_weak"] is False
    assert [d["topic_id"] for d in learner.due_items(topic="Topic A")] == ["topic-a"]

    learner.record_weakness("Topic A", "Card A?", severity="high")
    due = learner.due_items()
    assert {d["topic_id"] for d in due if d["topic_weak"]} == {"topic-a", "topic-b"}


def test_study_task_anchors_on_due_questions_only(sr_env):
    import medforge.product as product

    cards = [{"topic_id": "cardiac-cycle", "question": "What is EDV?",
              "answer": "End-diastolic volume"}]
    task = product._study_task("Cardiac Cycle", cards)
    assert "What is EDV?" in task
    assert "End-diastolic volume" not in task
    assert "Due card recall" in task
    plain = product._study_task("Cardiac Cycle", [])
    assert "Due card recall" not in plain
    assert "20-minute" in plain


def test_import_flashcards_missing_or_empty_pack(sr_env):
    _db, products = sr_env
    with pytest.raises(FileNotFoundError):
        learner.import_flashcards("nothing here")

    empty = write_pack(products, "empty-topic", [])
    with pytest.raises(ValueError):
        learner.import_flashcards("empty topic", empty)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
