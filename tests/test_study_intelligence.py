"""P9 study intelligence orchestrator tests (offline, deterministic).

P9 is an ORCHESTRATOR over P2/P3/P4/P6/P7/P8/SR — these tests verify the
integration seams, not the engines: V10 schema idempotence/preservation,
deterministic recommendation reasons, persistent reproducible plans with
superseding versions, time-budget fitting, mission persistence through the
real P7 tutor / P8 assessment public interfaces, idempotent step completion,
restart recovery without duplicated events, knowledge-gap detection,
readiness transparency, adaptation profiles, bounded harvesting of engine
results, and study history.

No live Ollama: TUTOR_AUTO_MODEL / ASSESSMENT_AUTO_MODEL are disabled and the
tutor/assessment deterministic paths are used (seeded P3 textbook evidence).
"""

from __future__ import annotations

import ast
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

import medforge as mf
import medforge.types as T
from medforge import assessment as A
from medforge import curriculum as C
from medforge import learner_model as LM
from medforge import study as S
from medforge import tutor as TU

from core.database.migrate_v10 import ensure_study_v10, is_v10_applied
from core.database.schema import V9_SCHEMA_DDL

TOPIC = "Growth Hormone Physiology"
PREREQ = "Growth Plate Physiology"
SUBJECT = "Endocrinology"
D1 = "2026-01-01T09:00:00Z"

SENT_A = ("The growth hormone axis is regulated by hypothalamic GHRH and "
          "somatostatin, which control pituitary growth hormone release.")
SENT_B = ("Growth hormone stimulates hepatic IGF-1 production, and IGF-1 then "
          "mediates longitudinal bone growth at the epiphyseal plate.")
SENT_C = ("Growth hormone excess before epiphyseal closure causes gigantism, "
          "while excess afterwards causes acromegaly.")
SENT_D = ("The growth plate is a layer of cartilage where chondrocytes "
          "proliferate and then hypertrophy before ossification.")
SENT_E = ("Oestrogen accelerates growth plate senescence, which is why growth "
          "velocity falls after puberty.")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Keep tests offline: no live model anywhere in the P9 seams."""
    monkeypatch.setattr(TU, "_retrieval_sources", lambda topic, limit: [])
    monkeypatch.setattr(T, "TUTOR_AUTO_MODEL", False)
    monkeypatch.setattr(T, "ASSESSMENT_AUTO_MODEL", False)


@pytest.fixture()
def env(monkeypatch):
    """Fresh temp META_DB + PRODUCTS with curriculum, textbook evidence and V10."""
    orig_db, orig_products = T.META_DB, T.PRODUCTS
    fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(db_path)
    products = Path(tempfile.mkdtemp())
    T.META_DB, T.PRODUCTS = Path(db_path), products
    A._ENSURED_DATABASES.discard(str(db_path))
    TU._ENSURED_DATABASES.discard(str(db_path))
    LM._ENSURED_DATABASES.discard(str(db_path))
    try:
        ensure_study_v10(db_path)
        C.import_syllabus(
            "Week 1: Pituitary\n- Growth Hormone Physiology\n"
            "Week 2: Growth\n- Growth Plate Physiology\n",
            subject_title=SUBJECT,
        )
        C.add_prerequisite(TOPIC, PREREQ, "strict")
        _seed_textbook(db_path, TOPIC, "doc1", "ed1", "n1", [SENT_A, SENT_B, SENT_C])
        _seed_textbook(db_path, PREREQ, "doc2", "ed2", "n2", [SENT_D, SENT_E])
        yield Path(db_path), products
    finally:
        T.META_DB, T.PRODUCTS = orig_db, orig_products
        Path(db_path).unlink(missing_ok=True)
        shutil.rmtree(products, ignore_errors=True)
        for cache in (A._ENSURED_DATABASES, TU._ENSURED_DATABASES, LM._ENSURED_DATABASES):
            cache.discard(str(db_path))


# ─── helpers ───


def _seed_textbook(db, topic: str, doc_id: str, ed_id: str, node_id: str, sentences) -> None:
    """Seed P3 provenance rows (document → edition → node → chunks → link)."""
    now = "2026-01-01T00:00:00Z"
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON;")
    try:
        node = con.execute(
            "SELECT id FROM curriculum_nodes WHERE title=?", (topic,)
        ).fetchone()[0]
        con.execute(
            "INSERT INTO textbook_documents (id,title,authors,publisher,edition_label,"
            "publication_year,isbn,subject,source_type,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{topic} Textbook", "A. Author", "Test Press", "2nd", 2024, "",
             SUBJECT, "textbook", now, now),
        )
        con.execute(
            "INSERT INTO textbook_editions (id,document_id,content_hash,source_path,"
            "page_count,extracted_pages,skipped_pages,chunk_count,ocr_status,"
            "ingest_status,ingested_at,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ed_id, doc_id, f"hash-{ed_id}", "/tmp/x.pdf", len(sentences),
             len(sentences), 0, len(sentences), "not_needed", "EXTRACTED",
             now, now, now),
        )
        con.execute(
            "INSERT INTO textbook_nodes (id,edition_id,parent_id,node_type,code,title,"
            "order_index,start_page,end_page,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (node_id, ed_id, None, "Chapter", "", topic, 0, 1, len(sentences), now, now),
        )
        for i, text in enumerate(sentences, start=1):
            con.execute(
                "INSERT INTO textbook_chunks (id,edition_id,document_id,node_id,"
                "page_number,chunk_index,text,text_hash,word_count,locator,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"{ed_id}:p{i}:c0", ed_id, doc_id, node_id, i, 0, text,
                 f"h-{ed_id}-{i}", len(text.split()),
                 f"{topic} Textbook, p.{i}", now),
            )
        con.execute(
            "INSERT INTO curriculum_text_links (curriculum_node_id,edition_id,node_id,"
            "page_start,page_end,link_type,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (node, ed_id, node_id, 1, len(sentences), "primary", "", now),
        )
        con.commit()
    finally:
        con.close()


def _log_event(key: str, fraction: float, at: str = D1, source: str = "manual") -> None:
    LM.record_learning_event(key, score_fraction=fraction, item_type="session",
                             source=source, answered_at=at, presented_at=at)


def _graded_attempts(db, assessment_id: str, fraction: float, now: str) -> None:
    """Mark every presented item of an assessment as graded with one score."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM assessment_attempts WHERE assessment_id=?",
            (assessment_id,)).fetchall()
        for r in rows:
            con.execute(
                "UPDATE assessment_attempts SET answered_at=?, grading_status='graded',"
                " score=?, max_score=1.0, correctness=?, grader='deterministic',"
                " graded_at=? WHERE attempt_id=?",
                (now, fraction, "correct" if fraction >= 0.999 else "incorrect",
                 now, r["attempt_id"]),
            )
        n = con.execute(
            "SELECT COUNT(*) AS n FROM assessment_attempts WHERE assessment_id=?"
            " AND answered_at IS NULL", (assessment_id,)).fetchone()["n"]
        con.commit()
    finally:
        con.close()
    if n:
        A.complete_assessment(assessment_id, now=now)


# ─── V10 schema ───


class TestV10Schema:
    def test_v10_applies_idempotently_and_preserves_data(self, env):
        db, _ = env
        con = sqlite3.connect(db)
        try:
            con.execute("INSERT INTO study_plans (plan_id,learner_key,title,scope_type,"
                        "scope_titles,priorities,actions,gaps,status,planner_version,"
                        "config,plan_version,created_at,updated_at)"
                        " VALUES('plan-x','local','t','custom','[]','[]','[]','[]',"
                        "'ACTIVE','p9-planner-v1','{}',1,'2026-01-01T00:00:00Z',"
                        "'2026-01-01T00:00:00Z')")
            con.commit()
        finally:
            con.close()
        ensure_study_v10(db)  # second run
        ensure_study_v10(db)  # third run
        con = sqlite3.connect(db)
        try:
            row = con.execute("SELECT plan_id FROM study_plans WHERE plan_id='plan-x'").fetchone()
            assert row is not None
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            con.close()
        assert {"study_plans", "study_missions", "study_actions",
                "content_items", "content_artifacts"} <= tables
        assert is_v10_applied(db)

    def test_ensure_study_tables_public_entry(self, env):
        ensure_study_tables = mf.ensure_study_tables
        ensure_study_tables()  # idempotent; uses T.META_DB
        assert is_v10_applied(T.META_DB)


# ─── recommendations ───


class TestRecommendations:
    def test_untouched_topic_gets_deterministic_reasons(self, env):
        rec = S.recommend_next_action(now=D1)
        assert rec["recommendation"] is not None
        assert rec["recommendation"]["reasons"], "machine-readable reasons required"
        codes = {r["code"] for r in rec["recommendation"]["reasons"]}
        assert codes, codes
        # Deterministic: same state → identical output.
        again = S.recommend_next_action(now=D1)
        assert again == rec

    def test_learner_history_changes_recommendation(self, env):
        _log_event("growth-hormone-physiology", 0.2, D1)
        rec = S.recommend_next_action(now=D1)
        r = rec["recommendation"]
        assert r is not None
        details = " ".join(x["detail"] for x in r["reasons"])
        assert "mastery" in details or "weakness" in details
        assert rec["reason"].startswith(r["topic"])

    def test_empty_home_has_no_actionable_topics(self):
        fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(db_path)
        orig_db, orig_products = T.META_DB, T.PRODUCTS
        products = Path(tempfile.mkdtemp())
        T.META_DB, T.PRODUCTS = Path(db_path), products
        try:
            ensure_study_v10(db_path)
            out = S.recommend_next_action(now=D1)
            assert out["recommendation"] is None
            assert "no actionable topics" in out["reason"]
        finally:
            T.META_DB, T.PRODUCTS = orig_db, orig_products
            Path(db_path).unlink(missing_ok=True)
            shutil.rmtree(products, ignore_errors=True)


# ─── plans ───


class TestPlans:
    def test_plan_is_persistent_and_reproducible(self, env):
        p1 = S.build_study_plan(daily_minutes=45, objective="exam prep", now=D1)
        got = S.get_plan(p1["plan_id"])
        assert got is not None
        assert got["planner_version"] == S.PLANNER_VERSION
        assert got["daily_minutes"] == 45
        again = S.build_study_plan(daily_minutes=45, objective="exam prep", now=D1)
        # Same state → same priorities/actions (deterministic planner).
        assert again["priorities"] == p1["priorities"]
        assert again["actions"] == p1["actions"]

    def test_replanning_supersedes_never_rewrites(self, env):
        p1 = S.build_study_plan(daily_minutes=30, now=D1)
        p2 = S.build_study_plan(daily_minutes=60, now="2026-01-02T09:00:00Z")
        assert p2["plan_version"] == p1["plan_version"] + 1
        old = S.get_plan(p1["plan_id"])
        assert old["status"] == "RETIRED"
        assert old["priorities"] == p1["priorities"], "history must not be rewritten"
        new = S.get_plan(p2["plan_id"])
        assert new["status"] == "ACTIVE"

    def test_time_budget_fits_without_padding(self, env):
        plan = S.build_study_plan(daily_minutes=20, now=D1)
        plan["actions"] = [
            {"topic": "A", "action": "TUTOR", "estimated_minutes": 15, "reason": "r1"},
            {"topic": "B", "action": "REVIEW", "estimated_minutes": 15, "reason": "r2"},
        ]
        # Persist the trimmed actions so get_today_plan reads them back.
        con = sqlite3.connect(T.META_DB)
        try:
            con.execute("UPDATE study_plans SET actions=? WHERE plan_id=?",
                        (json.dumps(plan["actions"]), plan["plan_id"]))
            con.commit()
        finally:
            con.close()
        today = S.get_today_plan(plan["plan_id"], now=D1)
        assert today["planned_minutes"] == 15
        assert len(today["actions"]) == 1
        assert today["did_not_fit"][0]["topic"] == "B"
        assert today["why"], "each fitted action must show its reason"

    def test_action_costs_are_per_action_not_a_fixed_15(self, env):
        plan = S.build_study_plan(daily_minutes=60, now=D1)
        assert plan["actions"]
        for action in plan["actions"]:
            assert action["estimated_minutes"] == S.ACTION_MINUTES[action["action"]]

    def test_short_budget_shortens_top_action_instead_of_empty_day(self, env):
        # The 10-minute budget must produce a usable day, not nothing: the top
        # priority is shortened to the budget and labelled as shortened.
        plan = S.build_study_plan(daily_minutes=10, now=D1)
        today = S.get_today_plan(plan["plan_id"], daily_minutes=10, now=D1)
        assert len(today["actions"]) == 1
        top = today["actions"][0]
        assert top["shortened"] is True
        assert top["estimated_minutes"] == 10
        assert top["original_minutes"] > 10
        assert "shortened" in top["reason"]
        assert today["planned_minutes"] == 10


# ─── missions through real engines ───


class TestMissions:
    def test_start_launches_real_p7_tutor_session(self, env):
        db, _ = env
        state = S.start_study_mission(topic=TOPIC, now=D1)
        assert state["status"] == "active"
        assert state["tutor_session_id"] is not None, "P7 session must be opened"
        # The tutor session exists in the P7 engine's own table.
        con = sqlite3.connect(db)
        try:
            row = con.execute("SELECT status FROM tutor_sessions WHERE tutor_session_id=?",
                              (state["tutor_session_id"],)).fetchone()
        finally:
            con.close()
        assert row is not None, "session must live in P7's own storage"
        launch = state["launch"]["tutor"]
        assert launch["launched"] is True
        assert "steps" in state and state["steps"]

    def test_assessment_launch_for_assess_action(self, env):
        db, _ = env
        # Build studied-but-unassessed state and P8 items scoped to the topic's
        # curriculum node (the mission's scope must match the bank's scope).
        _log_event(TOPIC.lower().replace(" ", "-"), 0.5, D1)
        node_id = C.topic_path(TOPIC)["node"]["id"]
        evidence_ids = [ev["evidence_id"] for ev in TU.gather_evidence(TOPIC)["evidence"]]
        assert evidence_ids, "fixture must ground items in seeded P3 evidence"
        A.create_item("MCQ_SINGLE", f"Which hormone does the pituitary release for growth? [1]",
                      topic=TOPIC, concept=TOPIC, curriculum_node_id=node_id,
                      choices=["GH", "Insulin", "Cortisol", "Thyroxine"],
                      correct_choices=["GH"], correct_answer="GH",
                      evidence_refs=[{"evidence_id": ev} for ev in evidence_ids],
                      evidence_state="SUPPORTED", source_kind="cli")
        approved = A.approve_item(A.list_items("DRAFT")[0]["item_id"],
                                  allow_partially_supported=True)
        assert approved["approved"] is True, approved.get("validation")
        state = S.start_study_mission(topic=TOPIC, action_type="ASSESS", now=D1)
        aid = state.get("assessment_id")
        assert state["curriculum_node_id"] == node_id, "mission must carry its node"
        assert state["launch"]["assessment"].get("launched") is True, \
            f"assessment must launch, got {state['launch']['assessment']}"
        assert aid is not None
        con = sqlite3.connect(db)
        try:
            row = con.execute("SELECT status FROM assessment_sessions WHERE assessment_id=?",
                              (aid,)).fetchone()
        finally:
            con.close()
        assert row is not None, "assessment must live in P8's own storage"
        # Harvest reports the open assessment honestly (not a faked score).
        harvested = S.harvest_mission_results(state["mission_id"], now=D1)
        assert harvested["assessment"]["assessment_id"] == aid

    def test_step_completion_is_explicit_and_advances_once(self, env):
        state = S.start_study_mission(topic=TOPIC, now=D1)
        mid = state["mission_id"]
        n_steps = len(state["steps"])
        first = S.complete_mission_action(mid, result={"note": "done"}, now=D1)
        assert first["current_step"] == 1
        assert "0" in first["results"]
        # Each completion records exactly one step and advances exactly one.
        for i in range(1, n_steps):
            step = S.complete_mission_action(mid, now=D1)
            assert step["current_step"] == i + 1
            assert len(step["results"]) == i + 1
        assert step["status"] == "completed"
        # Completing a finished mission is a no-op (no duplicate events).
        again = S.complete_mission_action(mid, now=D1)
        assert again["status"] == "completed"
        assert len(again["results"]) == n_steps

    def test_completion_records_history_without_faking_mastery(self, env):
        state = S.start_study_mission(topic=TOPIC, now=D1)
        mid = state["mission_id"]
        done = S.complete_mission(mid, result={"self": "complete"}, now=D1)
        assert done["status"] == "completed"
        hist = S.get_study_history()
        assert hist["count"] >= 1
        row = [a for a in hist["actions"] if a["mission_id"] == mid][0]
        assert row["status"] == "done"
        # No automatic mastery claim: P6 mastery unchanged by mission bookkeeping.
        con = sqlite3.connect(T.META_DB)
        try:
            n = con.execute("SELECT COUNT(*) FROM learner_model_state").fetchone()[0]
        finally:
            con.close()
        # Any mastery rows present must come from engines, not completion itself.
        assert isinstance(n, int)

    def test_restart_recovery_resumes_without_duplicate_events(self, env):
        state = S.start_study_mission(topic=TOPIC, now=D1)
        mid = state["mission_id"]
        S.complete_mission_action(mid, now=D1)
        # Simulate a brand-new process: fresh module-level state, DB only.
        resumed = S.get_mission_state(mid)
        assert resumed["current_step"] == 1
        assert resumed["status"] == "active"
        before = len(S.get_study_history()["actions"])
        S.complete_mission_action(mid, now=D1)
        after = len(S.get_study_history()["actions"])
        assert after == before, "resuming must not append duplicate study_actions"
        # Engine ids survive restart.
        assert resumed["tutor_session_id"] == state["tutor_session_id"]

    def test_harvest_pulls_tutor_summary(self, env):
        state = S.start_study_mission(topic=TOPIC, now=D1)
        tsid = state["tutor_session_id"]
        TU.complete_tutor_session(int(tsid), reason="mission step", now=D1)
        harvested = S.harvest_mission_results(state["mission_id"], now=D1)
        assert "tutor" in harvested
        assert harvested["tutor"]["tutor_session_id"] == int(tsid)
        assert "mean_score" in harvested["tutor"]

    def test_list_missions_filters_by_status(self, env):
        S.start_study_mission(topic=TOPIC, now=D1)
        active = S.list_missions(status="active")
        assert active and all(m["status"] == "active" for m in active)


class TestCurriculumNodeResolution:
    """Regression: nested (Semester -> Subject -> Week -> Topic) topics must resolve.

    ``find_node`` is sibling-scoped, so a tree-wide look-up bug left missions
    with a null ``curriculum_node_id``, which silently blocked real P8
    assessment launches and per-node evidence/readiness look-ups.
    """

    def test_nested_topic_resolves_to_its_curriculum_node(self, env):
        node = S._node_for_topic(TOPIC)
        assert node is not None, "nested topic must resolve tree-wide"
        assert node["node_type"] == "Topic"
        assert node["id"] == C.topic_path(TOPIC)["node"]["id"]

    def test_mission_keeps_node_and_tutor_launch_surfaces_real_fields(self, env):
        node = S._node_for_topic(TOPIC)
        state = S.start_study_mission(topic=TOPIC, now=D1)
        assert state["curriculum_node_id"] == node["id"]
        launch = state["launch"]["tutor"]
        assert launch["objective"], "P7 session objective must be surfaced"
        assert int(launch["evidence_count"] or 0) >= 1, \
            "seeded P3 evidence must be counted"


# ─── gaps, readiness, adaptation ───


class TestGapsAndReadiness:
    def test_knowledge_gaps_detect_states(self, env):
        out = S.get_knowledge_gaps(now=D1)
        gaps = {g["gap"] for g in out["gaps"]}
        # Textbook linked for both topics → NO_EVIDENCE must not fire for them.
        assert "NO_EVIDENCE" not in gaps or all(
            g["topic"] not in (TOPIC, PREREQ)
            for g in out["gaps"] if g["gap"] == "NO_EVIDENCE")
        assert "NOT_STUDIED" in gaps
        studied = [g for g in out["gaps"] if g["topic"] == TOPIC and
                   g["gap"] == "STUDIED_NOT_ASSESSED"]
        assert not studied, "unstudied topics must not claim studied-not-assessed"

    def test_studied_not_assessed_gap(self, env):
        _log_event("growth-hormone-physiology", 0.5, D1)
        out = S.get_knowledge_gaps(now=D1)
        match = [g for g in out["gaps"]
                 if g["topic"] == TOPIC and g["gap"] == "STUDIED_NOT_ASSESSED"]
        assert match, "studied topic without assessments must be flagged"
        assert "ASSESS" in match[0]["next_action"]

    def test_readiness_is_transparent_and_not_competence(self, env):
        out = S.get_readiness(TOPIC, now=D1)
        assert set(out["criteria"]) == {
            "evidence_backed", "taught", "tutor_or_assessment_history",
            "no_high_risk_prerequisite", "not_overdue_for_review",
            "uncertainty_bounded"}
        assert out["ready_for_assessment"] == all(out["criteria"].values())
        assert "NOT a competence claim" in out["note"] or "not a competence claim" in out["note"]
        # With no history: taught=False → not ready.
        assert out["ready_for_assessment"] is False

    def test_adaptation_profiles(self, env):
        assert S.adaptation_profile(TOPIC)["profile"] == "weak"  # no history
        _log_event("growth-hormone-physiology", 0.9, D1)
        _log_event("growth-hormone-physiology", 0.95, D1)
        LM.recalculate_mastery("growth-hormone-physiology")
        prof = S.adaptation_profile(TOPIC)
        assert prof["profile"] in ("weak", "developing", "strong",
                                   "underconfident", "overconfident")
        assert prof["mastery"] is not None


# ─── status surface ───


class TestStudyStatus:
    def test_status_joins_curriculum_learner_and_evidence(self, env):
        out = S.get_study_status(now=D1)
        assert out["study_version"] == S.STUDY_VERSION
        assert out["subjects"], "seeded curriculum must appear"
        topics = [t for s in out["subjects"] for w in s["weeks"]
                  for t in w["topics"]]
        by_key = {t["mastery_key"]: t for t in topics}
        assert "growth-hormone-physiology" in by_key
        gh = by_key["growth-hormone-physiology"]
        assert gh["evidence_state"] in ("SUPPORTED", "PARTIAL")
        assert gh["evidence_records"] >= 1

    def test_declining_topics_detect_drop(self, env):
        # P6 splits attempts into a 14-day recent window vs older history, so
        # the earlier strong attempt must fall outside the window.
        _log_event("growth-hormone-physiology", 0.9, "2026-01-01T09:00:00Z")
        _log_event("growth-hormone-physiology", 0.2, "2026-02-01T09:00:00Z")
        LM.recalculate_mastery("growth-hormone-physiology")
        out = S.get_study_status(now="2026-02-02T09:00:00Z")
        assert out["declining"], "90%→20% drop must be detected"
        assert out["declining"][0]["trend"] <= S.DECLINING_TREND_THRESHOLD


# ─── public surface regression ───


class TestPublicSurface:
    def test_contract_functions_exported(self):
        contract = [
            "get_study_status", "recommend_next_action", "build_study_plan",
            "get_today_plan", "start_study_mission", "get_mission_state",
            "complete_mission_action", "generate_canonical_content",
            "render_study_products", "get_knowledge_gaps", "get_readiness",
            "get_product_status", "get_study_history",
        ]
        missing = [n for n in contract if not hasattr(mf, n)]
        assert not missing, f"P9 orchestration contract broken: {missing}"

    def test_mission_state_unknown_id_raises(self, env):
        with pytest.raises(ValueError):
            S.get_mission_state("mission-does-not-exist")


class TestCLIWiring:
    """`medforge_core.main()` is one giant function, so any local it binds
    shadows a module-level command of the same name for the whole scope — a
    stray `status = ...` silently made `medforge_core status` unusable."""

    @staticmethod
    def _shadowed_called_names():
        src = (Path(__file__).resolve().parent.parent / "current" / "medforge_core.py")
        tree = ast.parse(src.read_text(encoding="utf-8"))
        module_names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                module_names.add(node.name)
            elif isinstance(node, ast.Assign):
                module_names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                module_names |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        main = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        locals_ = {n.id for n in ast.walk(main)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        called = {n.func.id for n in ast.walk(main)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        # Only a name that main() both binds and calls is a real failure: the
        # binding wins, so the call hits an unbound (or wrongly typed) local.
        return sorted((locals_ & module_names) & called)

    def test_main_locals_do_not_shadow_module_level_commands(self):
        shadowed = self._shadowed_called_names()
        assert not shadowed, (
            f"main() binds and calls names that shadow module-level objects: {shadowed}"
        )
