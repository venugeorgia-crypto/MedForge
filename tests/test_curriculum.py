"""Phase 2 curriculum engine tests (offline-safe, fixture DB).

Covers: syllabus parsing (weekly + seminar-level), idempotent import,
normalization/duplicate detection, week ordering, prerequisite graph,
topic→curriculum traceability, progress join with learner_mastery, and the
data-preserving V4 CHECK rebuild migration.
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
from medforge import curriculum as C
from medforge import learner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.database.schema import V3_SCHEMA_DDL
from core.database.migrate_v4 import ensure_curriculum_v4, is_v4_applied


WEEKLY = """Endocrinology Block
Week 1: Hypothalamus & Pituitary
Seminar: Pituitary hormones
- Anterior pituitary hormones
  - GH and IGF-1 axis
  - Prolactin axis
LO: Explain the GH axis with feedback control
- Posterior pituitary: ADH and oxytocin
Week 2
Seminar: Thyroid
- Thyroid hormone synthesis
- Thyroid dysfunction
"""

SEMINAR_LEVEL = """Seminar: Adrenal cortex
- Cortisol synthesis
- Aldosterone regulation
LO: Describe the renin-angiotensin-aldosterone axis
"""


@pytest.fixture()
def curr_env():
    """Fresh temp META_DB + PRODUCTS dir; restore after the test."""
    orig_db, orig_products = T.META_DB, T.PRODUCTS
    fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(db_path)
    products = Path(tempfile.mkdtemp())
    T.META_DB = Path(db_path)
    T.PRODUCTS = products
    try:
        yield Path(db_path), products
    finally:
        T.META_DB, T.PRODUCTS = orig_db, orig_products
        Path(db_path).unlink(missing_ok=True)
        shutil.rmtree(products, ignore_errors=True)


# ─── Parsing ───


def test_parse_weekly_syllabus_structure(curr_env):
    parsed = C.parse_syllabus(WEEKLY)
    subjects = parsed["subjects"]
    assert len(subjects) == 1
    subj = subjects[0]
    # The leading heading names the subject.
    assert subj["title"] == "Endocrinology Block"
    assert [w["number"] for w in subj["weeks"]] == [1, 2]
    w1, w2 = subj["weeks"]
    assert w1["title"] == "Hypothalamus & Pituitary"
    assert len(w1["seminars"]) == 1
    sem = w1["seminars"][0]
    assert sem["title"] == "Pituitary hormones"
    assert [t["title"] for t in sem["topics"]] == [
        "Anterior pituitary hormones", "Posterior pituitary: ADH and oxytocin",
    ]
    assert sem["topics"][0]["subtopics"] == ["GH and IGF-1 axis", "Prolactin axis"]
    assert sem["topics"][0]["objectives"] == [
        "Explain the GH axis with feedback control",
    ]
    # Week 2 has no theme: the bare "Week 2" line still yields a week.
    assert w2["seminars"][0]["topics"][0]["title"] == "Thyroid hormone synthesis"


def test_parse_one_line_week_theme_becomes_topic(curr_env):
    parsed = C.parse_syllabus("Week 3: Adrenal medulla\n")
    week = parsed["subjects"][0]["weeks"][0]
    assert week["title"] == "Adrenal medulla"
    # Theme is promoted to a topic so minimal syllabi still produce targets.
    assert week["topics"][0]["title"] == "Adrenal medulla"


def test_parse_seminar_level_without_week(curr_env):
    parsed = C.parse_syllabus(SEMINAR_LEVEL)
    subj = parsed["subjects"][0]
    assert subj["weeks"] == []
    assert subj["loose_seminars"][0]["title"] == "Adrenal cortex"
    assert subj["loose_seminars"][0]["topics"][0]["title"] == "Cortisol synthesis"


def test_parse_empty_syllabus_raises(curr_env):
    with pytest.raises(ValueError):
        C.parse_syllabus("   \n  ")


# ─── Import: idempotence, ordering, normalization ───


def test_import_weekly_and_idempotent_repeat(curr_env):
    first = C.import_syllabus(WEEKLY)
    assert first["created_total"] > 0
    assert first["weeks"] == [1, 2]
    assert first["created"]["Semester"] == 1
    assert first["created"]["Subject"] == 1
    assert first["created"]["Week"] == 2
    assert first["created"]["Seminar"] == 2

    second = C.import_syllabus(WEEKLY)
    assert second["created_total"] == 0
    assert second["idempotent_repeat"] is True

    con = sqlite3.connect(T.META_DB)
    try:
        n = con.execute("SELECT count(*) FROM curriculum_nodes").fetchone()[0]
    finally:
        con.close()
    # Node count must not grow across the rerun.
    third = C.import_syllabus(WEEKLY)
    con = sqlite3.connect(T.META_DB)
    try:
        assert con.execute("SELECT count(*) FROM curriculum_nodes").fetchone()[0] == n
    finally:
        con.close()


def test_import_seminar_level_with_week_number(curr_env):
    res = C.import_syllabus(SEMINAR_LEVEL, subject_title="Endocrinology",
                            week_number=4)
    assert res["created"]["Seminar"] == 1
    con = sqlite3.connect(T.META_DB)
    try:
        row = con.execute(
            "SELECT w.order_index, w.code FROM curriculum_nodes s"
            " JOIN curriculum_nodes w ON w.parent_id=s.id"
            " WHERE s.node_type='Subject' AND w.node_type='Week'"
        ).fetchone()
    finally:
        con.close()
    assert row == (4, "W04")


def test_week_ordering_follows_numbers(curr_env):
    C.import_syllabus("Week 12: Reproductive endocrinology\nWeek 2: Thyroid\n")
    con = sqlite3.connect(T.META_DB)
    try:
        rows = con.execute(
            "SELECT order_index, code FROM curriculum_nodes"
            " WHERE node_type='Week' ORDER BY order_index"
        ).fetchall()
    finally:
        con.close()
    assert rows == [(2, "W02"), (12, "W12")]


def test_normalization_dedup_same_sibling(curr_env):
    C.ensure_curriculum_tables()
    a = C.ensure_node("Topic", "GH axis")
    b = C.ensure_node("Topic", "gh   axis!")  # case/spacing/punctuation variants
    c = C.ensure_node("Topic", "GH Axis")
    assert a["id"] == b["id"] == c["id"]
    assert C.detect_duplicates() == []


def test_detect_duplicates_reports_manual_variants(curr_env):
    C.ensure_curriculum_tables()
    parent = C.ensure_node("Week", "Week 1")
    C.ensure_node("Topic", "GH axis", parent_id=parent["id"])
    con = sqlite3.connect(T.META_DB)
    try:
        # The unique (parent_id, title) index blocks exact dupes; a manually
        # inserted case-variant slips past the index but not detection.
        con.execute(
            "INSERT INTO curriculum_nodes (id, parent_id, node_type, title,"
            " created_at, updated_at) VALUES('manual1', ?, 'Topic', 'GH  AXIS',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (parent["id"],),
        )
        con.commit()
    finally:
        con.close()
    dups = C.detect_duplicates()
    assert len(dups) == 1
    assert dups[0]["node_type"] == "Topic"
    assert set(dups[0]["ids"]) == {"manual1"} | {a["id"] for a in [C.find_node("Topic", "GH axis", parent["id"])]}


# ─── Prerequisites ───


def test_prerequisite_graph(curr_env):
    C.import_syllabus(
        "Week 1: Pituitary\n- GH axis\n- IGF-1 mediation\n"
        "Week 2: Growth\n- Growth plate physiology\n",
        subject_title="Endocrinology",
    )
    edge = C.add_prerequisite("Growth plate physiology", "GH axis", "strict")
    assert edge["relationship_type"] == "strict"
    # Idempotent: same edge twice stays one row.
    C.add_prerequisite("Growth plate physiology", "GH axis", "strict")
    con = sqlite3.connect(T.META_DB)
    try:
        n = con.execute("SELECT count(*) FROM prerequisites").fetchone()[0]
    finally:
        con.close()
    assert n == 1
    with pytest.raises(ValueError):
        C.add_prerequisite("GH axis", "GH axis")  # self-prerequisite
    with pytest.raises(ValueError):
        C.add_prerequisite("GH axis", "Nonexistent topic")
    with pytest.raises(ValueError):
        C.add_prerequisite("GH axis", "Growth plate physiology", "best-friend")
    assert C.remove_prerequisite("Growth plate physiology", "GH axis") == 1


# ─── Traceability + progress ───


def test_topic_path_traceability(curr_env):
    C.import_syllabus(WEEKLY)
    by_title = C.topic_path("Anterior pituitary hormones")
    assert by_title is not None
    labels = [(n["node_type"], n["title"]) for n in by_title["chain"]]
    assert labels[0][0] == "Semester" and labels[1][0] == "Subject"
    assert labels[-1] == ("Topic", "Anterior pituitary hormones")
    assert "Subject: Endocrinology Block" in by_title["position"]
    # Slug lookup (the learner_mastery.topic_id namespace).
    by_slug = C.topic_path("anterior-pituitary-hormones")
    assert by_slug is not None and by_slug["node"]["id"] == by_title["node"]["id"]
    assert C.topic_path("totally unmapped topic") is None


def test_progress_joins_learner_mastery(curr_env):
    C.import_syllabus(WEEKLY)
    # Real flows store mastery keys slugged (learner.log_study_result slugs
    # the topic before update_mastery), so join via the slug namespace.
    learner.update_mastery("anterior-pituitary-hormones", 85)
    learner.update_mastery("thyroid-hormone-synthesis", 40)
    learner.update_mastery("random-unstructured-topic", 90)
    prog = C.curriculum_progress()
    s = prog["summary"]
    assert s["topics"] >= 3
    assert s["topics_studied"] == 2
    assert s["topics_mastered"] == 1
    assert "random-unstructured-topic" in s["studied_unmapped"]
    subj = prog["subjects"][0]
    assert subj["title"] == "Endocrinology Block"
    assert subj["topics_mastered"] == 1
    flat = {t["title"]: t["mastery"] for w in subj["weeks"] for t in w["topics"]}
    flat.update({t["title"]: t["mastery"]
                 for w in subj["weeks"] for sem in w["seminars"]
                 for t in sem["topics"]})
    assert flat["Anterior pituitary hormones"] == 85.0
    assert flat["Thyroid hormone synthesis"] == 40.0
    assert flat["Posterior pituitary: ADH and oxytocin"] is None


def test_curriculum_tree_nesting(curr_env):
    C.import_syllabus(WEEKLY)
    tree = C.curriculum_tree()
    assert len(tree) == 1 and tree[0]["node_type"] == "Semester"
    subj = tree[0]["children"][0]
    assert subj["node_type"] == "Subject"
    weeks = [c for c in subj["children"] if c["node_type"] == "Week"]
    assert len(weeks) == 2 and weeks[0]["children"][0]["node_type"] == "Seminar"


# ─── V4 migration ───


def test_v4_rebuild_preserves_rows_and_allows_new_types(tmp_path):
    db = tmp_path / "old.sqlite3"
    con = sqlite3.connect(db)
    con.executescript(V3_SCHEMA_DDL)
    con.execute(
        "INSERT INTO curriculum_nodes (id, node_type, title, created_at, updated_at)"
        " VALUES('keep1', 'Year', 'Legacy Year', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z')"
    )
    con.commit()
    con.close()

    result = ensure_curriculum_v4(db)
    assert result["status"] == "success" and result["rows_copied"] == 1
    assert is_v4_applied(db)

    con = sqlite3.connect(db)
    try:
        assert con.execute(
            "SELECT count(*) FROM curriculum_nodes WHERE id='keep1'"
        ).fetchone()[0] == 1
        # New node types accepted post-rebuild…
        con.execute(
            "INSERT INTO curriculum_nodes (id, node_type, title, created_at,"
            " updated_at) VALUES('s1', 'Subject', 'Endocrinology',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        # …and unique (parent_id, title) enforcement active.
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO curriculum_nodes (id, parent_id, node_type, title,"
                " created_at, updated_at) VALUES('s2', NULL, 'Subject',"
                " 'Endocrinology', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
    finally:
        con.close()

    # Idempotent second run.
    again = ensure_curriculum_v4(db)
    assert again["status"] == "success" and again["rows_copied"] == 0


def test_fresh_database_self_heal_end_to_end(curr_env):
    """An empty DB file reaches full curriculum use with no manual migration."""
    res = C.import_syllabus(WEEKLY)
    assert res["created_total"] > 0
    assert is_v4_applied(T.META_DB)
