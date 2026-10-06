"""MedForge P9 study intelligence: orchestration over the existing engines.

P9 is an ORCHESTRATOR, not a replacement. Mastery math stays in P6
(`learner_model`), evidence verdicts in P4 (`evidence`), grading in P7/P8,
scheduling mathematics in `learner.py`, curriculum in P2. This module reads
their public interfaces, explains WHAT to study next and WHY, persists
reproducible plans and resumable missions, and detects knowledge gaps —
deterministically, with the LLM involved only in canonical content
generation (see `medforge/content.py`).

Study target selection (deterministic, explainable)
---------------------------------------------------
``recommend_next_action()`` scores every curriculum topic with P6
``study_priority`` components plus P9 gap/assessment signals, and returns
machine-readable reasons (``[{code, detail, weight}]``), never a bare score.
Action selection maps learner state to one of the STUDY_ACTION_TYPES using
an explicit table (new→teach, weak+failures→drill, prerequisite→repair,
strong+due→spaced review, studied-unassessed→assess, open remediation→
remediation).

Plans are persistent and reproducible: ``build_study_plan()`` derives the
plan from (curriculum, learner, review, assessment, config) state with a
planner version and stores it; re-planning supersedes rather than rewrites.
Missions persist every step result at completion time, so a new process
resumes exactly where the learner stopped, with no duplicated events.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import medforge.types as T
from medforge.utils import slugify, utcnow

STUDY_VERSION = "p9-study-v1"
PLANNER_VERSION = "p9-planner-v1"

# Reason weight model (P9 layer; P6 weights untouched — these combine the
# already-normalized P6 signals with P9 gap signals into one ranking).
REASON_WEIGHTS = {
    "weakness": 0.30,
    "recent_failure": 0.15,
    "overdue_review": 0.15,
    "prerequisite_risk": 0.10,
    "uncertainty": 0.05,
    "assessment_gap": 0.10,
    "assessed_poorly": 0.10,
    "no_evidence": 0.05,
    "not_started": 0.20,
}

# Curriculum state thresholds (documented, deterministic; never "read once =
# mastered").
MASTERED_PCT = 85.0
STUDIED_PCT = 40.0
REVIEW_DUE_CUTOFF_DAYS = 7

# Realistic per-action study cost (minutes). A fixed 15 for every action made
# short budgets impossible to satisfy at all; the planner now estimates the
# work honestly and shortens the top action when the budget is smaller.
ACTION_MINUTES: Dict[str, int] = {
    "NEW_TEACHING": 25,
    "PREREQUISITE_REPAIR": 25,
    "TUTOR": 15,
    "DRILL": 12,
    "REMEDIATION": 15,
    "ASSESS": 12,
    "RECALL": 10,
    "REVIEW": 15,
    "SPACED_REVIEW": 10,
}
# A day is never empty when the learner has at least this much time: the top
# priority is shortened to the budget instead of being dropped entirely.
SHORTENED_MIN_MINUTES = 8

# Mirrors core.database.schema STUDY_ACTION_TYPES (V10 CHECK constraint).
STUDY_ACTION_TYPES = (
    "NEW_TEACHING", "REVIEW", "DRILL", "PREREQUISITE_REPAIR", "TUTOR",
    "ASSESS", "REMEDIATION", "RECALL", "SPACED_REVIEW",
)

# A topic counts as declining when P6's recent-vs-historical trend drops by
# at least this much (P6 computes both windows; P9 only reads the verdict).
DECLINING_TREND_THRESHOLD = -0.15

__all__ = [
    "STUDY_VERSION", "PLANNER_VERSION",
    "ensure_study_tables", "gather_learner_context", "get_study_status",
    "recommend_next_action", "get_knowledge_gaps", "get_readiness",
    "build_study_plan", "get_plan", "list_plans", "get_today_plan",
    "start_study_mission", "get_mission_state", "list_missions",
    "complete_mission_action", "complete_mission", "get_study_history",
    "adaptation_profile", "launch_mission_engines", "harvest_mission_results",
]


# ─── Storage ───


def ensure_study_tables() -> None:
    """Idempotently apply the V10 study-intelligence schema."""
    here = Path(__file__).resolve()
    candidates = [str(T.BASE)] + [str(p) for p in here.parents]
    for base_str in candidates:
        if base_str not in sys.path:
            sys.path.insert(0, base_str)
        try:
            from core.database.migrate_v10 import ensure_study_v10

            ensure_study_v10(T.META_DB)
            return
        except ImportError:
            continue
    raise ImportError(
        "core.database.migrate_v10 not importable from: " + ", ".join(candidates)
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    return con


def _now() -> str:
    return utcnow()


def _uid(prefix: str) -> str:
    return f"{prefix}-{hashlib.sha1(f'{prefix}{_now()}{datetime.now(timezone.utc).timestamp()}'.encode()).hexdigest()[:12]}"


def _json_load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# ─── Learner + curriculum context (read-only aggregation) ───


def gather_learner_context(now: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate read-only context from P2/P3/P4/P6/SR/P8 for planning."""
    now = now or _now()
    from medforge import learner_model as LM
    from medforge.learner import due_items

    ctx: Dict[str, Any] = {"now": now, "topics": {}, "due_cards": 0, "due_by_topic": {}}
    try:
        pri = LM.study_priority(limit=100, now=now) or {}
        ctx["priorities"] = {
            p.get("topic"): p for p in (pri.get("priorities") or pri.get("items") or [])
        }
    except Exception:
        ctx["priorities"] = {}
    try:
        due = due_items(limit=200, now=now) or []
        ctx["due_cards"] = len(due)
        for card in due:
            item_id = str(card.get("item_id") or "")
            topic_key = item_id.split(":", 1)[0] if ":" in item_id else ""
            if topic_key:
                ctx["due_by_topic"][topic_key] = ctx["due_by_topic"].get(topic_key, 0) + 1
    except Exception:
        pass
    try:
        weaknesses = LM.get_weaknesses(limit=100) or []
        for w in weaknesses:
            key = w.get("topic_id") or ""
            entry = ctx["topics"].setdefault(key, {})
            entry["weakness"] = max(entry.get("weakness", 0.0), float(w.get("weakness_score") or 0.0))
            entry["misconceptions"] = entry.get("misconceptions", 0) + 1
    except Exception:
        pass
    # P8 assessment history per mastery key (private aggregates only).
    try:
        con = _connect()
        rows = con.execute(
            "SELECT mastery_key, COUNT(*) AS n, AVG(score) AS avg_score,"
            " MAX(answered_at) AS last_at FROM assessment_attempts"
            " WHERE grading_status='graded' AND mastery_key != ''"
            " GROUP BY mastery_key"
        ).fetchall()
        con.close()
        for r in rows:
            entry = ctx["topics"].setdefault(r["mastery_key"], {})
            entry["assessments"] = r["n"]
            entry["assessment_avg"] = round(float(r["avg_score"]), 3) if r["avg_score"] is not None else None
            entry["last_assessed_at"] = r["last_at"]
    except Exception:
        pass
    return ctx


def _evidence_coverage(topic: str, node_id: Optional[str]) -> Dict[str, Any]:
    """Evidence availability via P3 textbook links (bounded, no excerpts)."""
    try:
        from medforge.textbook import textbook_evidence_for_topic

        data = textbook_evidence_for_topic(node_id or topic, preview_chars=0)
        records = data.get("links") or data.get("evidence") or data.get("records") or []
        return {
            "evidence_records": len(records),
            "editions": len({r.get("edition_id") for r in records if r.get("edition_id")}),
            "state": "SUPPORTED" if len(records) >= 2 else ("PARTIAL" if records else "NONE"),
        }
    except Exception:
        return {"evidence_records": 0, "editions": 0, "state": "NONE"}


def _classify_topic(mastery_pct: Optional[float], evidence_count: int,
                    attempts: int, due: int) -> str:
    """Deterministic curriculum state (never claims real mastery)."""
    if due > 0 and mastery_pct is not None and mastery_pct >= STUDIED_PCT:
        return "REVIEW_DUE"
    if mastery_pct is None:
        return "IN_PROGRESS" if attempts else "NOT_STARTED"
    if mastery_pct >= MASTERED_PCT:
        return "MASTERED_ESTIMATE"
    if mastery_pct >= STUDIED_PCT:
        return "STUDIED" if attempts >= 3 else "IN_PROGRESS"
    return "IN_PROGRESS"


# ─── Study status ───


def get_study_status(scope: Optional[str] = None, now: Optional[str] = None) -> Dict[str, Any]:
    """Unified curriculum progress view with P9 states and next actions."""
    ensure_study_tables()
    now = now or _now()
    from medforge.curriculum import curriculum_progress
    from medforge import learner_model as LM

    ctx = gather_learner_context(now)
    progress = curriculum_progress()
    subjects = []
    for subject in progress.get("subjects", []):
        weeks_out = []
        for week in subject.get("weeks", []):
            topics_out = []
            for t in week.get("topics", []):
                title = t.get("title", "")
                key = slugify(title)
                rwm = t.get("rwm_mastery")
                mastery_pct = None if rwm is None else round(100.0 * float(rwm), 1)
                extra = ctx["topics"].get(key, {})
                due = ctx["due_by_topic"].get(key, 0)
                coverage = _evidence_coverage(title, t.get("id"))
                state = _classify_topic(mastery_pct, coverage["evidence_records"],
                                        int(t.get("attempts") or 0), due)
                topics_out.append({
                    "topic": title, "mastery_key": key, "node_id": t.get("id"),
                    "mastery_percent": mastery_pct,
                    "uncertainty": t.get("rwm_uncertainty"),
                    "attempts": t.get("attempts") or 0,
                    "assessments": extra.get("assessments", 0),
                    "assessment_avg": extra.get("assessment_avg"),
                    "due_cards": due,
                    "evidence_state": coverage["state"],
                    "evidence_records": coverage["evidence_records"],
                    "state": state,
                })
            weeks_out.append({"week": week.get("week") or week.get("title"), "topics": topics_out})
        subjects.append({"subject": subject.get("subject") or subject.get("title"), "weeks": weeks_out})
    weak = [
        {"topic": w.get("topic_id"), "weakness": w.get("weakness_score"),
         "concept": w.get("concept"), "misconception": w.get("misconception")}
        for w in (LM.get_weaknesses(limit=10) or [])
    ]
    return {
        "generated_at": now, "study_version": STUDY_VERSION,
        "subjects": subjects, "weak_topics": weak,
        "due_cards_total": ctx["due_cards"],
        "due_by_topic": ctx["due_by_topic"],
        "declining": _declining_topics(ctx),
    }


def _declining_topics(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Topics whose P6 trend (recent − historical) drops below the threshold."""
    out = []
    from medforge import learner_model as LM

    for key, pri in (ctx.get("priorities") or {}).items():
        try:
            view = LM.get_recent_performance(key) or {}
        except Exception:
            continue
        trend = view.get("trend")
        recent = view.get("recent_performance")
        mastery = pri.get("mastery_percent")
        if trend is None or recent is None:
            continue
        try:
            if float(trend) <= DECLINING_TREND_THRESHOLD:
                out.append({"topic": key, "mastery_percent": mastery,
                            "recent_performance": recent, "trend": trend})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x.get("trend") or 0.0)
    return out[:5]


# ─── Recommendations ───


def _assessment_state(key: str, ctx: Dict[str, Any]) -> Tuple[int, Optional[float]]:
    extra = ctx["topics"].get(key, {})
    return int(extra.get("assessments", 0)), extra.get("assessment_avg")


def _recommend_for_topic(topic: str, key: str, node_id: Optional[str],
                         ctx: Dict[str, Any], goal: Optional[str],
                         now: str) -> Optional[Dict[str, Any]]:
    from medforge import learner_model as LM

    pri = (ctx.get("priorities") or {}).get(key) or {}
    mastery_pct = pri.get("mastery_percent")
    mastery = None if mastery_pct is None else float(mastery_pct) / 100.0
    try:
        recent_view = LM.get_recent_performance(key) or {}
    except Exception:
        recent_view = {}
    recent = recent_view.get("recent_performance")
    uncertainty = float(pri.get("uncertainty") or 0.0)
    extra = ctx["topics"].get(key, {})
    weakness = float(extra.get("weakness", pri.get("weakness") or 0.0))
    due = ctx["due_by_topic"].get(key, 0)
    n_assess, avg_score = _assessment_state(key, ctx)
    coverage = _evidence_coverage(topic, node_id)

    prereq = LM.get_prerequisite_risks(topic) or {}
    prereq_impact = 0.0
    weak_prereqs: List[str] = []
    for risk in prereq.get("risks") or []:
        if risk.get("impact") in ("high", "medium") or risk.get("impact_score", 0) >= 0.4:
            prereq_impact = max(prereq_impact, float(risk.get("impact_score") or (0.5 if risk.get("impact") == "high" else 0.3)))
            weak_prereqs.append(risk.get("topic") or risk.get("mastery_key") or "?")

    if mastery is None and coverage["evidence_records"] == 0 and not weakness:
        return None  # nothing to act on (no learner history, no evidence)

    reasons: List[Dict[str, Any]] = []

    def add(code: str, detail: str, weight: float) -> None:
        if weight > 0:
            reasons.append({"code": code, "detail": detail, "weight": round(weight, 3)})

    if mastery is not None:
        add("weakness", f"mastery {mastery_pct:.1f}%",
            REASON_WEIGHTS["weakness"] * weakness if weakness else REASON_WEIGHTS["weakness"] * max(0.0, 1.0 - mastery) * 0.5)
        if recent is not None and recent < 0.6:
            add("recent_failure", f"recent performance {recent:.2f}",
                REASON_WEIGHTS["recent_failure"] * max(0.0, 0.6 - recent) / 0.6)
        if uncertainty > 0.3:
            add("uncertainty", f"uncertainty {uncertainty:.2f}",
                REASON_WEIGHTS["uncertainty"] * uncertainty)
    if weakness:
        add("weakness", f"unresolved weakness {weakness:.2f}", REASON_WEIGHTS["weakness"] * weakness)
    if due:
        add("overdue_review", f"{due} card(s) overdue", REASON_WEIGHTS["overdue_review"])
    if weak_prereqs:
        add("prerequisite_risk", f"weak prerequisite(s): {', '.join(sorted(set(weak_prereqs)))}",
            REASON_WEIGHTS["prerequisite_risk"] * prereq_impact)
    if mastery_pct is not None and n_assess == 0:
        add("assessment_gap", "studied but never assessed", REASON_WEIGHTS["assessment_gap"])
    if n_assess and avg_score is not None and avg_score < 0.6:
        add("assessed_poorly", f"assessment average {avg_score:.2f}", REASON_WEIGHTS["assessed_poorly"])
    if coverage["evidence_records"] == 0:
        add("no_evidence", "no curriculum-linked textbook evidence",
            REASON_WEIGHTS["no_evidence"])
    if goal == "exam" and n_assess == 0:
        add("assessment_gap", "exam prep: no assessment on record", REASON_WEIGHTS["assessment_gap"])
    if mastery is None and coverage["evidence_records"] > 0:
        add("not_started", "not studied yet; evidence available for teaching",
            REASON_WEIGHTS["not_started"])
    if not reasons:
        return None

    priority = sum(r["weight"] for r in reasons)
    # Action selection — explicit deterministic table.
    if weak_prereqs and (mastery is None or mastery < 0.6):
        action = "PREREQUISITE_REPAIR"
    elif n_assess == 0 and mastery_pct is not None and mastery_pct >= STUDIED_PCT:
        action = "ASSESS"
    elif n_assess and avg_score is not None and avg_score < 0.6:
        action = "REMEDIATION"
    elif weakness >= 0.25 and recent is not None and recent < 0.6:
        action = "DRILL"
    elif due >= 3 and (mastery is None or mastery >= 0.75):
        action = "SPACED_REVIEW"
    elif due:
        action = "REVIEW"
    elif mastery is None:
        action = "NEW_TEACHING"
    elif mastery < 0.6:
        action = "TUTOR"
    else:
        action = "REVIEW"
    return {
        "topic": topic, "mastery_key": key, "node_id": node_id,
        "priority": round(priority, 3), "action": action,
        "mastery_percent": mastery_pct, "due_cards": due,
        "assessments": n_assess, "assessment_avg": avg_score,
        "evidence_state": coverage["state"],
        "reasons": sorted(reasons, key=lambda r: -r["weight"]),
    }


def recommend_next_action(topic: Optional[str] = None, goal: Optional[str] = None,
                          now: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic study-target + action selection with inspectable reasons."""
    ensure_study_tables()
    now = now or _now()
    from medforge.curriculum import curriculum_progress

    ctx = gather_learner_context(now)
    if topic:
        node = _node_for_topic(topic)
        rec = _recommend_for_topic(topic, slugify(topic), node and node.get("id"),
                                   ctx, goal, now)
        return {"generated_at": now, "study_version": STUDY_VERSION,
                "recommendation": rec, "reason": _reason_text(rec)}

    progress = curriculum_progress()
    candidates: List[Dict[str, Any]] = []
    for subject in progress.get("subjects", []):
        for week in subject.get("weeks", []):
            for t in week.get("topics", []):
                rec = _recommend_for_topic(t.get("title", ""), slugify(t.get("title", "")),
                                           t.get("id"), ctx, goal, now)
                if rec:
                    candidates.append(rec)
    candidates.sort(key=lambda r: (-r["priority"], r["mastery_key"]))
    top = candidates[:5]
    return {
        "generated_at": now, "study_version": STUDY_VERSION,
        "recommendation": top[0] if top else None,
        "alternatives": top[1:],
        "reason": _reason_text(top[0]) if top else "no actionable topics (empty curriculum or no evidence of gaps)",
    }


def _node_for_topic(topic: str) -> Optional[Dict[str, Any]]:
    """Resolve a topic to its curriculum node (tree-wide, P2 read-only).

    ``find_node`` is sibling-scoped by design, so nested Topic nodes (subject →
    week → topic) are resolved through ``topic_path`` first; the scoped lookup
    stays as a fallback for root-level topics.
    """
    try:
        from medforge.curriculum import topic_path

        path = topic_path(topic)
        if path and path.get("node"):
            return path["node"]
    except Exception:
        pass
    try:
        from medforge.curriculum import find_node

        return find_node("Topic", topic)
    except Exception:
        return None


def _reason_text(rec: Optional[Dict[str, Any]]) -> str:
    if not rec:
        return "no actionable topic"
    why = "; ".join(f"{r['detail']}" for r in rec["reasons"][:4])
    return (f"{rec['topic']} selected for {rec['action']} because: {why}")


# ─── Knowledge gaps & readiness ───


def get_knowledge_gaps(now: Optional[str] = None) -> Dict[str, Any]:
    """Actionable gap states across curriculum/learner/evidence/assessment."""
    ensure_study_tables()
    now = now or _now()
    from medforge.curriculum import curriculum_progress

    ctx = gather_learner_context(now)
    gaps: List[Dict[str, Any]] = []
    progress = curriculum_progress()
    for subject in progress.get("subjects", []):
        for week in subject.get("weeks", []):
            for t in week.get("topics", []):
                title = t.get("title", "")
                key = slugify(title)
                rwm = t.get("rwm_mastery")
                mastery_pct = None if rwm is None else round(100.0 * float(rwm), 1)
                coverage = _evidence_coverage(title, t.get("id"))
                n_assess, avg = _assessment_state(key, ctx)
                if coverage["evidence_records"] == 0:
                    gaps.append({"topic": title, "gap": "NO_EVIDENCE",
                                 "detail": "no curriculum-linked textbook evidence",
                                 "next_action": "Add/link a textbook source, or abstain from generated content"})
                    continue
                if mastery_pct is None:
                    gaps.append({"topic": title, "gap": "NOT_STUDIED",
                                 "detail": "curriculum topic with no learner history",
                                 "next_action": "NEW_TEACHING via tutor"})
                    continue
                if n_assess == 0:
                    gaps.append({"topic": title, "gap": "STUDIED_NOT_ASSESSED",
                                 "detail": f"mastery estimate {mastery_pct:.1f}% but no assessment",
                                 "next_action": "ASSESS via P8 blueprint"})
                elif avg is not None and avg < 0.6:
                    gaps.append({"topic": title, "gap": "ASSESSED_POORLY",
                                 "detail": f"assessment average {avg:.2f}",
                                 "next_action": "REMEDIATION then reassess"})
                if ctx["due_by_topic"].get(key, 0) >= 3:
                    gaps.append({"topic": title, "gap": "REVIEWS_OVERDUE",
                                 "detail": f"{ctx['due_by_topic'][key]} cards overdue",
                                 "next_action": "SPACED_REVIEW"})
    prereq_gaps = []
    for key, pri in (ctx.get("priorities") or {}).items():
        for reason in pri.get("reasons") or []:
            if str(reason).startswith("prerequisite"):
                prereq_gaps.append({"topic": key, "gap": "PREREQUISITE_WEAK",
                                    "detail": reason, "next_action": "PREREQUISITE_REPAIR"})
    return {"generated_at": now, "study_version": STUDY_VERSION,
            "gaps": gaps, "prerequisite_gaps": prereq_gaps,
            "gap_count": len(gaps)}


def get_readiness(topic: str, now: Optional[str] = None) -> Dict[str, Any]:
    """Transparent readiness-for-assessment estimate (NOT medical competence)."""
    ensure_study_tables()
    now = now or _now()
    from medforge import learner_model as LM

    key = slugify(topic)
    node = _node_for_topic(topic)
    ctx = gather_learner_context(now)
    coverage = _evidence_coverage(topic, node and node.get("id"))
    pri = (ctx.get("priorities") or {}).get(key) or {}
    mastery_pct = pri.get("mastery_percent")
    n_assess, avg = _assessment_state(key, ctx)
    prereq = LM.get_prerequisite_risks(topic) or {}
    high_risk = [r for r in prereq.get("risks") or []
                 if (r.get("impact") == "high" or (r.get("impact_score") or 0) >= 0.5)]

    criteria = {
        "evidence_backed": coverage["state"] in ("SUPPORTED", "PARTIAL"),
        "taught": mastery_pct is not None and mastery_pct >= STUDIED_PCT,
        "tutor_or_assessment_history": n_assess > 0 or int(pri.get("evidence_count") or 0) > 0,
        "no_high_risk_prerequisite": not high_risk,
        "not_overdue_for_review": ctx["due_by_topic"].get(key, 0) < 3,
        "uncertainty_bounded": float(pri.get("uncertainty") or 1.0) <= 0.5,
    }
    ready = all(criteria.values())
    components = {
        "mastery_percent": mastery_pct,
        "uncertainty": pri.get("uncertainty"),
        "recent_performance": pri.get("recent_performance"),
        "assessments": n_assess, "assessment_avg": avg,
        "evidence_state": coverage["state"],
        "high_risk_prerequisites": [r.get("topic") or r.get("mastery_key") for r in high_risk],
        "due_cards": ctx["due_by_topic"].get(key, 0),
    }
    return {"topic": topic, "mastery_key": key, "generated_at": now,
            "study_version": STUDY_VERSION, "criteria": criteria,
            "ready_for_assessment": ready, "components": components,
            "note": "readiness-for-assessment heuristic; NOT a competence claim"}


def adaptation_profile(topic: str, now: Optional[str] = None) -> Dict[str, Any]:
    """Learner adaptation profile from P6 (weak/developing/strong/under/over)."""
    from medforge import learner_model as LM

    key = slugify(topic)
    mastery = LM.get_mastery(topic) or {}
    confidence = LM.get_confidence(topic) or {}
    m = mastery.get("mastery")
    conf = confidence.get("confidence_estimate")
    calibration = confidence.get("calibration_gap")
    if m is None:
        profile = "weak"          # no history: scaffold from the start
    elif m >= 0.8 and (calibration is not None and calibration >= 0.15):
        profile = "overconfident"
    elif m >= 0.8 and (calibration is not None and calibration <= -0.15):
        profile = "underconfident"
    elif m >= 0.8:
        profile = "strong"
    elif m >= 0.4:
        profile = "developing"
    else:
        profile = "weak"
    return {"topic": topic, "mastery_key": key, "profile": profile,
            "mastery": m, "confidence": conf, "calibration_gap": calibration,
            "study_version": STUDY_VERSION}


# ─── Plans ───


def build_study_plan(daily_minutes: int = 30, days: int = 1, objective: str = "",
                     goal: Optional[str] = None, scope_titles: Optional[List[str]] = None,
                     now: Optional[str] = None, seed: str = "") -> Dict[str, Any]:
    """Deterministic, persistent study plan (replanning supersedes, never rewrites)."""
    ensure_study_tables()
    now = now or _now()
    daily_minutes = max(5, min(480, int(daily_minutes)))
    rec = recommend_next_action(goal=goal, now=now)
    alternatives = rec.get("alternatives") or []
    ordered = ([rec["recommendation"]] if rec.get("recommendation") else []) + alternatives
    if scope_titles:
        wanted = {slugify(s) for s in scope_titles}
        ordered = [r for r in ordered if r["mastery_key"] in wanted] or ordered
    priorities = [{
        "rank": i + 1, "topic": r["topic"], "mastery_key": r["mastery_key"],
        "action": r["action"], "priority": r["priority"], "reasons": r["reasons"],
    } for i, r in enumerate(ordered)]

    actions = []
    for p in priorities:
        actions.append({
            "topic": p["topic"], "action": p["action"],
            "estimated_minutes": ACTION_MINUTES.get(p["action"], 15),
            "reason": _reason_text(p),
        })
    gaps = get_knowledge_gaps(now)["gaps"]
    con = _connect()
    try:
        plan_id = _uid("plan")
        row = con.execute(
            "SELECT plan_id, plan_version FROM study_plans WHERE status='ACTIVE'"
            " ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        version, supersedes = 1, None
        if row:
            version = int(row["plan_version"]) + 1
            supersedes = row["plan_id"]
            con.execute("UPDATE study_plans SET status='RETIRED', updated_at=? WHERE plan_id=?",
                        (now, row["plan_id"]))
        con.execute(
            "INSERT INTO study_plans (plan_id, learner_key, title, scope_type, scope_node_id,"
            " scope_titles, objective, target_date, daily_minutes, priorities, actions, gaps,"
            " status, planner_version, config, seed, plan_version, supersedes_plan_id,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, "local", objective or "Daily adaptive study plan", "custom", None,
             _json_dump(scope_titles or []), objective, None, daily_minutes,
             _json_dump(priorities), _json_dump(actions), _json_dump(gaps[:20]),
             "ACTIVE", PLANNER_VERSION,
             _json_dump({"daily_minutes": daily_minutes, "days": days, "goal": goal or "",
                         "scope_titles": scope_titles or []}),
             seed or "", version, supersedes, now, now),
        )
        con.commit()
    finally:
        con.close()
    return {"plan_id": plan_id, "plan_version": version, "status": "ACTIVE",
            "planner_version": PLANNER_VERSION, "daily_minutes": daily_minutes,
            "priorities": priorities, "actions": actions, "generated_at": now}


def get_plan(plan_id: str) -> Optional[Dict[str, Any]]:
    ensure_study_tables()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM study_plans WHERE plan_id=?", (plan_id,)).fetchone()
        return _plan_dict(row) if row else None
    finally:
        con.close()


def list_plans(limit: int = 10) -> List[Dict[str, Any]]:
    ensure_study_tables()
    con = _connect()
    try:
        rows = con.execute("SELECT * FROM study_plans ORDER BY created_at DESC LIMIT ?",
                           (max(1, limit),)).fetchall()
        return [_plan_dict(r) for r in rows]
    finally:
        con.close()


def _plan_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for col in ("scope_titles", "priorities", "actions", "gaps", "config"):
        d[col] = _json_load(d.get(col), [] if col != "config" else {})
    return d


def get_today_plan(plan_id: Optional[str] = None, daily_minutes: int = 30,
                   now: Optional[str] = None) -> Dict[str, Any]:
    """Fit today's actions to the time budget (greedy by priority; no padding)."""
    ensure_study_tables()
    now = now or _now()
    if plan_id:
        plan = get_plan(plan_id)
        if not plan:
            raise ValueError(f"Unknown plan_id {plan_id!r}")
    else:
        con = _connect()
        try:
            row = con.execute("SELECT plan_id FROM study_plans WHERE status='ACTIVE'"
                              " ORDER BY created_at DESC LIMIT 1").fetchone()
        finally:
            con.close()
        if not row:
            plan = build_study_plan(daily_minutes=daily_minutes, now=now)
        else:
            plan = get_plan(row["plan_id"])
    budget = int(plan.get("daily_minutes") or daily_minutes)
    fitted, used, skipped = [], 0, []
    pending = list(plan.get("actions") or [])
    for action in pending:
        cost = int(action.get("estimated_minutes") or 15)
        if used + cost <= budget:
            fitted.append({**action, "fits": True})
            used += cost
        else:
            skipped.append({"topic": action.get("topic"), "action": action.get("action"),
                            "estimated_minutes": cost})
    # A smaller-than-any-action budget must still produce a studyable day:
    # shorten the top priority to the budget instead of showing an empty plan.
    if not fitted and budget >= SHORTENED_MIN_MINUTES and skipped:
        top = skipped.pop(0)
        original = next((a for a in pending
                         if a.get("topic") == top["topic"]
                         and a.get("action") == top["action"]), {})
        fitted.append({
            "topic": top["topic"], "action": top["action"],
            "estimated_minutes": budget, "fits": True, "shortened": True,
            "original_minutes": top["estimated_minutes"],
            "reason": (original.get("reason", "") +
                       f"; shortened to {budget} min to fit today's budget"),
        })
        used = budget
    sr = _sr_snapshot()
    return {
        "plan_id": plan["plan_id"], "plan_version": plan.get("plan_version"),
        "planner_version": plan.get("planner_version"), "date": now[:10],
        "budget_minutes": budget, "planned_minutes": used,
        "actions": fitted, "did_not_fit": skipped,
        "spaced_repetition": sr,
        "why": [a["reason"] for a in fitted],
    }


def _sr_snapshot() -> Dict[str, Any]:
    try:
        from medforge.learner import due_items

        due = due_items(limit=500) or []
        overdue = [c for c in due if str(c.get("due_date", "")) < _now()[:10]]
        return {"due_now": len(due), "overdue": len(overdue),
                "upcoming_note": "scheduler math stays in learner.py"}
    except Exception:
        return {"due_now": 0, "overdue": 0}


# ─── Missions ───


# Steps are metadata + launch intent: ``engine`` names the authoritative P7/P8/SR
# engine; P9 launches it through its PUBLIC interface only (start_tutor_session,
# create_assessment, due_items) and never duplicates its logic.
_ACTION_STEPS: Dict[str, List[Dict[str, Any]]] = {
    "NEW_TEACHING": [
        {"step": "teach", "engine": "tutor", "mode": "explain", "minutes": 10},
        {"step": "recall", "engine": "tutor", "mode": "drill", "minutes": 5},
        {"step": "review", "engine": "sr", "minutes": 3},
    ],
    "TUTOR": [
        {"step": "tutor", "engine": "tutor", "mode": "explain", "minutes": 12},
        {"step": "recall", "engine": "tutor", "mode": "drill", "minutes": 5},
    ],
    "DRILL": [
        {"step": "drill", "engine": "tutor", "mode": "drill", "minutes": 10},
        {"step": "review", "engine": "sr", "minutes": 5},
    ],
    "PREREQUISITE_REPAIR": [
        {"step": "prerequisite_tutor", "engine": "tutor", "mode": "prerequisite_repair", "minutes": 15},
        {"step": "retry_topic", "engine": "tutor", "mode": "explain", "minutes": 10},
    ],
    "ASSESS": [
        {"step": "assessment", "engine": "assessment", "minutes": 15},
        {"step": "review_result", "engine": "assessment", "minutes": 5},
    ],
    "REMEDIATION": [
        {"step": "remediation_tutor", "engine": "tutor", "mode": "correct", "minutes": 12},
        {"step": "reassess", "engine": "assessment", "minutes": 10},
    ],
    "RECALL": [
        {"step": "recall", "engine": "tutor", "mode": "drill", "minutes": 5},
        {"step": "review", "engine": "sr", "minutes": 5},
    ],
    "REVIEW": [
        {"step": "review", "engine": "sr", "minutes": 10},
    ],
    "SPACED_REVIEW": [
        {"step": "review", "engine": "sr", "minutes": 12},
        {"step": "quick_quiz", "engine": "assessment", "minutes": 8},
    ],
}

_TUTOR_MODE_FOR_STEP = {
    "teach": "explain", "tutor": "explain", "recall": "drill", "drill": "drill",
    "prerequisite_tutor": "prerequisite_repair", "retry_topic": "explain",
    "remediation_tutor": "correct",
}


def _tutor_mode_for_action(action: str) -> str:
    return {
        "NEW_TEACHING": "explain", "TUTOR": "explain", "DRILL": "drill",
        "PREREQUISITE_REPAIR": "prerequisite_repair", "REMEDIATION": "correct",
        "RECALL": "drill", "REVIEW": "review", "SPACED_REVIEW": "review",
        "ASSESS": "explain",
    }.get(action, "explain")


def _launch_tutor(topic: str, mode: str, steps: int) -> Dict[str, Any]:
    """Launch a P7 tutor session through its public interface (bounded goal)."""
    from medforge.tutor import start_tutor_session

    # P7 accepts named goals or an interaction count; missions stay bounded to
    # a short session so one step cannot balloon into an unbounded drill.
    goal = "10min" if steps <= 10 else "20min"
    out = start_tutor_session(topic=topic, mode=mode, goal=goal)
    summary = out.get("summary") or {}
    session = (out.get("state") or {}).get("session") or {}
    evidence = out.get("evidence") or []
    return {
        "launched": True, "engine": "tutor (P7)",
        "tutor_session_id": out.get("session_id"),
        "abstained": bool(out.get("abstained")),
        "mode": mode,
        "objective": session.get("session_objective") or summary.get("objective"),
        "evidence_count": (summary.get("evidence_count") if out.get("abstained")
                           else len(evidence)),
        "assessment_status": (out.get("assessment") or {}).get("status"),
        "note": ("P7 abstained (insufficient evidence) — link a textbook source first"
                 if out.get("abstained") else None),
    }


def _launch_assessment(topic: str, node_id: Optional[str], item_count: int = 5,
                       mode: str = "PRACTICE") -> Dict[str, Any]:
    """Create+start a P8 assessment through its public interface (topic scope)."""
    from medforge.assessment import create_assessment

    if not node_id:
        return {"launched": False, "engine": "assessment (P8)",
                "reason": "no curriculum node id for this topic; link curriculum first"}
    out = create_assessment(scope_type="topic", scope_node_id=node_id,
                            item_count=item_count, mode=mode,
                            title=f"P9 mission assessment: {topic}")
    if not out.get("created"):
        return {"launched": False, "engine": "assessment (P8)",
                "reason": out.get("error", "no eligible ACTIVE items for this topic")}
    return {"launched": True, "engine": "assessment (P8)",
            "assessment_id": out.get("assessment_id"),
            "item_count": (out.get("selection") or {}).get("count"),
            "mode": mode}


def _resume_engine(mission: Dict[str, Any]) -> Dict[str, Any]:
    """Re-attach to the engines a launched mission already references."""
    out: Dict[str, Any] = {}
    tsid, aid = mission.get("tutor_session_id"), mission.get("assessment_id")
    if tsid:
        try:
            from medforge.tutor import get_tutor_state

            state = get_tutor_state(int(tsid))
            out["tutor"] = {"tutor_session_id": int(tsid),
                            "status": state.get("status"),
                            "stage": state.get("stage"),
                            "abstained": state.get("status") == "blocked"}
        except Exception as exc:  # engine state unreadable → surface honestly
            out["tutor"] = {"tutor_session_id": int(tsid), "error": str(exc)}
    if aid:
        try:
            from medforge.assessment import get_assessment_state

            state = get_assessment_state(str(aid))
            out["assessment"] = {"assessment_id": str(aid),
                                 "status": state.get("status"),
                                 "current_index": state.get("current_index"),
                                 "item_count": state.get("item_count")}
        except Exception as exc:
            out["assessment"] = {"assessment_id": str(aid), "error": str(exc)}
    return out


def launch_mission_engines(mission_id: str, tutor_steps: int = 5,
                           assessment_items: int = 5) -> Dict[str, Any]:
    """Launch the P7/P8 engines a resumed mission still needs (idempotent)."""
    ensure_study_tables()
    mission = get_mission_state(mission_id)
    if mission["status"] != "active":
        return {"mission_id": mission_id, "launched": {}, "reason": "mission not active"}
    engines = _resume_engine(mission)
    launched: Dict[str, Any] = {}
    updates: Dict[str, Any] = {}
    need_tutor = any(s.get("engine") == "tutor" for s in mission["steps"])
    need_assess = any(s.get("engine") == "assessment" for s in mission["steps"])
    if need_tutor and "tutor" not in engines:
        mode = mission["steps"][mission["current_step"]].get("mode") \
            if mission.get("next_step") else _tutor_mode_for_action(mission["action_type"])
        launched["tutor"] = _launch_tutor(mission["topic"],
                                          mode or _tutor_mode_for_action(mission["action_type"]),
                                          tutor_steps)
        if launched["tutor"].get("tutor_session_id"):
            updates["tutor_session_id"] = launched["tutor"]["tutor_session_id"]
    if need_assess and "assessment" not in engines:
        launched["assessment"] = _launch_assessment(
            mission["topic"], mission.get("curriculum_node_id"), assessment_items)
        if launched["assessment"].get("assessment_id"):
            updates["assessment_id"] = launched["assessment"]["assessment_id"]
    if updates:
        con = _connect()
        try:
            sets = ", ".join(f"{k}=?" for k in updates)
            con.execute(f"UPDATE study_missions SET {sets}, updated_at=? WHERE mission_id=?",
                        (*updates.values(), _now(), mission_id))
            con.commit()
        finally:
            con.close()
    return {"mission_id": mission_id, "launched": launched,
            "engines": {**engines, **{k: v for k, v in launched.items()
                                      if v.get("tutor_session_id") or v.get("assessment_id")}}}


def start_study_mission(topic: Optional[str] = None, plan_id: Optional[str] = None,
                        action_type: Optional[str] = None,
                        now: Optional[str] = None) -> Dict[str, Any]:
    """Materialize a persistent mission and LAUNCH its P7/P8 engine sessions.

    The tutor session (P7) is opened immediately; an assessment session (P8) is
    created when the action needs one. Engine ids are stored on the mission so
    the loop resumes through the engines' own public state after restart.
    """
    ensure_study_tables()
    now = now or _now()
    rec = recommend_next_action(topic=topic, now=now)
    target = rec.get("recommendation")
    if not target:
        raise ValueError("No actionable study target for this topic/learner state.")
    action = action_type or target["action"]
    if action not in STUDY_ACTION_TYPES:
        raise ValueError(f"Unknown action {action!r}")
    steps = [dict(s) for s in _ACTION_STEPS.get(action, _ACTION_STEPS["TUTOR"])]
    profile = adaptation_profile(target["topic"], now)
    coverage = _evidence_coverage(target["topic"], target.get("node_id"))
    mission_id = _uid("mission")

    # Launch real engine sessions through public interfaces. Engine failures are
    # recorded on the mission (honest abstention) instead of aborting it.
    tutor_launch: Dict[str, Any] = {}
    assess_launch: Dict[str, Any] = {}
    if any(s.get("engine") == "tutor" for s in steps):
        try:
            tutor_launch = _launch_tutor(
                target["topic"], _tutor_mode_for_action(action), steps=5)
        except Exception as exc:
            tutor_launch = {"launched": False, "engine": "tutor (P7)",
                            "error": str(exc)}
    if any(s.get("engine") == "assessment" for s in steps):
        try:
            assess_launch = _launch_assessment(
                target["topic"], target.get("node_id"), item_count=5)
        except Exception as exc:
            assess_launch = {"launched": False, "engine": "assessment (P8)",
                             "error": str(exc)}

    completion = {"all_steps_done": True}
    if tutor_launch.get("tutor_session_id"):
        completion["tutor_session_complete"] = True
    if assess_launch.get("assessment_id"):
        completion["assessment_complete"] = True

    con = _connect()
    try:
        con.execute(
            "INSERT INTO study_missions (mission_id, plan_id, learner_key, topic, mastery_key,"
            " curriculum_node_id, objective, action_type, adaptation_profile, steps,"
            " estimated_minutes, evidence_refs, expected_outcome, completion_criteria,"
            " status, tutor_session_id, assessment_id, current_step, results, started_at,"
            " completed_at, study_version, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mission_id, plan_id, "local", target["topic"], target["mastery_key"],
             target.get("node_id"),
             f"{action} on {target['topic']}", action, profile["profile"],
             _json_dump(steps), sum(int(s.get("minutes", 5)) for s in steps),
             _json_dump(["P3/P4 evidence via tutor pack" if tutor_launch else ""]),
             f"complete {action.lower()} for {target['topic']}",
             _json_dump(completion),
             "active",
             tutor_launch.get("tutor_session_id"),
             assess_launch.get("assessment_id"),
             0, _json_dump({}), now, None, STUDY_VERSION, now, now),
        )
        con.execute(
            "INSERT INTO study_actions (mission_id, plan_id, topic, mastery_key, action_type,"
            " status, reason, engine_ref, outcome, occurred_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (mission_id, plan_id, target["topic"], target["mastery_key"], action,
             "in_progress", _reason_text(target),
             _json_dump({"tutor_session_id": tutor_launch.get("tutor_session_id"),
                         "assessment_id": assess_launch.get("assessment_id"),
                         "tutor_abstained": tutor_launch.get("abstained", False)}),
             _json_dump({"launch": {"tutor": tutor_launch, "assessment": assess_launch}}),
             now, now),
        )
        con.commit()
    finally:
        con.close()
    state = get_mission_state(mission_id)
    state["engines"] = {
        "tutor": tutor_launch or None,
        "assessment": assess_launch or None,
    }
    state["launch"] = {"tutor": tutor_launch, "assessment": assess_launch}
    return state


def get_mission_state(mission_id: str) -> Dict[str, Any]:
    ensure_study_tables()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM study_missions WHERE mission_id=?",
                          (mission_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown mission_id {mission_id!r}")
    finally:
        con.close()
    d = dict(row)
    d["steps"] = _json_load(d.get("steps"), [])
    d["results"] = _json_load(d.get("results"), {})
    d["evidence_refs"] = _json_load(d.get("evidence_refs"), [])
    d["completion_criteria"] = _json_load(d.get("completion_criteria"), {})
    d["next_step"] = (d["steps"][d["current_step"]]
                      if d["current_step"] < len(d["steps"]) and d["status"] == "active"
                      else None)
    return d


def list_missions(limit: int = 10, status: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_study_tables()
    con = _connect()
    try:
        if status:
            rows = con.execute("SELECT mission_id FROM study_missions WHERE status=?"
                               " ORDER BY created_at DESC LIMIT ?", (status, max(1, limit))).fetchall()
        else:
            rows = con.execute("SELECT mission_id FROM study_missions"
                               " ORDER BY created_at DESC LIMIT ?", (max(1, limit),)).fetchall()
        con.close()
        return [get_mission_state(r["mission_id"]) for r in rows]
    finally:
        pass


def complete_mission(mission_id: str, result: Optional[Dict[str, Any]] = None,
                     now: Optional[str] = None) -> Dict[str, Any]:
    """Complete the whole mission: harvest engine outcomes, mark remaining steps."""
    ensure_study_tables()
    now = now or _now()
    mission = get_mission_state(mission_id)
    harvested = harvest_mission_results(mission_id, now=now)
    results = dict(mission["results"])
    for i in range(int(mission["current_step"]), len(mission["steps"])):
        results.setdefault(str(i), {"step": mission["steps"][i]["step"],
                                    "completed_at": now, "result": result or {},
                                    "engine_results": harvested})
    con = _connect()
    try:
        con.execute("UPDATE study_missions SET results=?, status='completed',"
                    " current_step=?, completed_at=?, updated_at=? WHERE mission_id=?",
                    (_json_dump(results), len(mission["steps"]), now, now, mission_id))
        con.execute("UPDATE study_actions SET status='done', outcome=?, occurred_at=?"
                    " WHERE mission_id=? AND status IN ('pending','in_progress')",
                    (_json_dump({"completed": True, "results": results}), now, mission_id))
        con.commit()
    finally:
        con.close()
    state = get_mission_state(mission_id)
    state["harvested"] = harvested
    return state


def harvest_mission_results(mission_id: str, now: Optional[str] = None) -> Dict[str, Any]:
    """Pull outcomes from the P7/P8 engines this mission launched (read-only).

    Bounded private aggregates only: tutor mean score / question counts and
    assessment percentage / passed. Full transcripts stay inside the engines.
    """
    ensure_study_tables()
    now = now or _now()
    mission = get_mission_state(mission_id)
    out: Dict[str, Dict[str, Any]] = {}
    tsid = mission.get("tutor_session_id")
    if tsid:
        try:
            from medforge.tutor import get_tutor_summary

            summary = get_tutor_summary(int(tsid))
            out["tutor"] = {
                "tutor_session_id": int(tsid), "topic": summary.get("topic"),
                "questions_attempted": summary.get("questions_attempted", 0),
                "correct": summary.get("correct", 0),
                "incorrect": summary.get("incorrect", 0),
                "mean_score": summary.get("mean_score"),
            }
        except Exception as exc:
            out["tutor"] = {"tutor_session_id": int(tsid), "error": str(exc)}
    aid = mission.get("assessment_id")
    if aid:
        try:
            from medforge.assessment import get_assessment_result

            res = get_assessment_result(str(aid))
            if res.get("available"):
                score = (res.get("result") or {}).get("score") or {}
                out["assessment"] = {
                    "assessment_id": str(aid),
                    "percentage": score.get("percentage"),
                    "passed": score.get("passed"),
                    "graded_items": score.get("graded_items"),
                    "pending_items": score.get("pending_items"),
                }
            else:
                out["assessment"] = {"assessment_id": str(aid), "status": res.get("status"),
                                     "reason": res.get("reason")}
        except Exception as exc:
            out["assessment"] = {"assessment_id": str(aid), "error": str(exc)}
    return out


def complete_mission_action(mission_id: str, result: Optional[Dict[str, Any]] = None,
                            now: Optional[str] = None) -> Dict[str, Any]:
    """Record one completed step (idempotent per step index), harvest engines, advance."""
    ensure_study_tables()
    now = now or _now()
    mission = get_mission_state(mission_id)
    if mission["status"] != "active":
        return mission
    step_index = int(mission["current_step"])
    engine_results = harvest_mission_results(mission_id, now=now)
    results = dict(mission["results"])
    key = str(step_index)
    if key not in results:
        results[key] = {"step": (mission["steps"][step_index]["step"]
                                 if step_index < len(mission["steps"]) else "?"),
                        "completed_at": now, "result": result or {},
                        "engine_results": engine_results}
    next_index = step_index + 1
    status = "active"
    completed_at = None
    if next_index >= len(mission["steps"]):
        status, completed_at = "completed", now
        con = _connect()
        try:
            con.execute("UPDATE study_actions SET status='done', outcome=?, occurred_at=?"
                        " WHERE mission_id=? AND status IN ('pending','in_progress')",
                        (_json_dump({"completed": True, "engine_results": engine_results}),
                         now, mission_id))
            con.commit()
        finally:
            con.close()
    con = _connect()
    try:
        con.execute("UPDATE study_missions SET current_step=?, results=?, status=?,"
                    " completed_at=COALESCE(?, completed_at), updated_at=? WHERE mission_id=?",
                    (next_index, _json_dump(results), status, completed_at, now, mission_id))
        con.commit()
    finally:
        con.close()
    state = get_mission_state(mission_id)
    state["harvested"] = engine_results
    return state


def get_study_history(limit: int = 50) -> Dict[str, Any]:
    """Auditable action history (append-only)."""
    ensure_study_tables()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT action_id, mission_id, plan_id, topic, mastery_key, action_type,"
            " status, reason, engine_ref, outcome, occurred_at FROM study_actions"
            " ORDER BY occurred_at DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
        actions = []
        for r in rows:
            d = dict(r)
            d["outcome"] = _json_load(d.get("outcome"), {})
            actions.append(d)
    finally:
        con.close()
    return {"actions": actions, "count": len(actions), "study_version": STUDY_VERSION}
