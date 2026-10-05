"""MedForge P6 recency-weighted learner model.

Deterministic, local, versioned learner-state estimate built from append-only
performance events:

- ``learning_attempts``   — every learning event (session score, flashcard
  review, question result, manual note) with score/correctness/confidence/
  response time/session/curriculum links.
- ``learner_model_state`` — the materialized estimate per mastery key:
  recency-weighted mastery, evidence strength, uncertainty, consistency,
  recent vs historical performance, confidence + calibration, timestamps and
  the model version that produced it.

Design rules:

- No LLM arithmetic: every number here is deterministic float math.
- One explicit decay model: ``w = exp(-ln2 · age_days / half_life)`` with a
  configurable half-life (``T.LEARNER_HALF_LIFE_DAYS``, default 21 days).
- Mastery is a *model estimate* with a documented uninformed prior
  (``T.LEARNER_PRIOR_MASTERY``/``T.LEARNER_PRIOR_STRENGTH``), never presented
  as an objective measurement.
- Uncertainty comes from effective evidence mass (Kish ESS), never invented.
- Confidence is tracked separately from mastery; calibration is ``None`` when
  no confidence observations exist.
- Learner state is fully reproducible: the stored state is recomputed from the
  stored attempts by the same pure function (``compute_model_state``), using
  the stored evaluation time, so recalculation matches the materialized rows.
- Never mutates P2/P3/P4 data; prerequisite links are read-only.
"""

from __future__ import annotations

import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import medforge.types as T
from medforge.utils import mkdirs, slugify, utcnow

__all__ = [
    "ensure_learner_model_tables", "decay_weight", "compute_model_state",
    "record_learning_event", "recalculate_mastery", "recalculate_all",
    "get_mastery", "get_confidence", "get_recent_performance",
    "get_weaknesses", "get_prerequisite_risks", "get_review_priority",
    "study_priority", "detect_weaknesses", "learner_history", "learner_summary",
    "curriculum_report", "parse_attempt_time", "model_state_rows",
    "WEAKNESS_CONCEPT",
]

# Stable concept label for learner-model-detected weaknesses.
WEAKNESS_CONCEPT = "recency-weighted mastery"

# Derived values are rounded to this many decimals before storing/comparing so
# materialized state and recalculation agree exactly.
ROUND = 4


# ─── Schema self-healing (V7, idempotent) ───


def _core_import():
    """Import core.database.migrate_v7, walking candidate bases like P2–P4."""
    here = Path(__file__).resolve()
    candidates = [str(T.BASE)] + [str(p) for p in here.parents]
    for base_str in candidates:
        if base_str not in sys.path:
            sys.path.insert(0, base_str)
        try:
            from core.database.migrate_v7 import ensure_learner_model_v7

            return ensure_learner_model_v7
        except ImportError:
            continue
    raise ImportError(
        "core.database.migrate_v7 not importable from: " + ", ".join(candidates)
    )


# Schema ensured once per process per database path: the V7 runner is
# idempotent but also runs integrity/foreign-key checks, and learner events are
# recorded far more often than the schema changes.
_ENSURED_DATABASES: set = set()


def ensure_learner_model_tables() -> Dict[str, Any]:
    """Bring the active META_DB to the V7 learner-model schema (idempotent)."""
    key = str(T.META_DB)
    if key in _ENSURED_DATABASES:
        return {"status": "already_ensured", "database": key}
    mkdirs()
    ensure_v7 = _core_import()
    result = ensure_v7(T.META_DB)
    _ENSURED_DATABASES.add(key)
    return result


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    return con


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return None if row is None else {k: row[k] for k in row.keys()}


def parse_attempt_time(value: str) -> datetime:
    """Parse an ISO-8601 attempt timestamp (Z or offset) into aware UTC."""
    if not value or not str(value).strip():
        raise ValueError("Timestamp must not be empty.")
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Timestamp must be ISO-8601, got {value!r}.") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _round(value: Optional[float], digits: int = ROUND) -> Optional[float]:
    return None if value is None else round(float(value), digits)


# ─── The decay model ───


def decay_weight(age_days: float, half_life_days: Optional[float] = None) -> float:
    """Exponential recency weight: ``0.5 ** (age / half_life)``.

    Explicit and configurable: age 0 → 1.0, age == half-life → 0.5,
    age == 2×half-life → 0.25. Negative ages clamp to 0 (a timestamp slightly
    in the future must not yield a weight above 1).
    """
    hl = float(half_life_days if half_life_days is not None else T.LEARNER_HALF_LIFE_DAYS)
    if hl <= 0:
        raise ValueError(f"half_life_days must be > 0, got {half_life_days!r}.")
    age = max(0.0, float(age_days))
    return math.exp(-math.log(2.0) * age / hl)


def _weighted_mean(points: List[tuple]) -> Optional[float]:
    """Weighted mean of [(weight, value), ...]; None when weights are zero."""
    total = sum(w for w, _v in points)
    if total <= 0:
        return None
    return sum(w * v for w, v in points) / total


def compute_model_state(
    attempts: List[Dict[str, Any]],
    now: datetime,
    half_life_days: Optional[float] = None,
    model_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure deterministic learner state from stored attempts.

    Mastery (0..1) shrinks a recency-weighted mean toward the documented
    uninformed prior: ``(Σ w·s + k0·p0) / (Σ w + k0)`` — so one success is not
    mastery and old successes do not outvote recent failures.
    """
    hl = float(half_life_days if half_life_days is not None else T.LEARNER_HALF_LIFE_DAYS)
    version = model_version or T.LEARNER_MODEL_VERSION
    prior = float(T.LEARNER_PRIOR_MASTERY)
    prior_strength = float(T.LEARNER_PRIOR_STRENGTH)
    recent_points: List[tuple] = []
    historical_points: List[tuple] = []
    all_points: List[tuple] = []
    confidences: List[float] = []
    failures = successes = 0
    last_attempt = last_success = last_failure = None
    ordered: List[tuple] = []
    for a in attempts:
        answered = parse_attempt_time(a["answered_at"])
        age = max(0.0, (now - answered).total_seconds() / 86400.0)
        w = decay_weight(age, hl)
        s = float(a["score"])
        all_points.append((w, s))
        ordered.append((answered, s, a.get("learner_confidence")))
        if age <= float(T.LEARNER_RECENT_WINDOW_DAYS):
            recent_points.append((w, s))
        else:
            historical_points.append((w, s))
        if a.get("learner_confidence") is not None:
            confidences.append(float(a["learner_confidence"]))
        ts = a["answered_at"]
        if last_attempt is None or ts > last_attempt:
            last_attempt = ts
        if s >= float(T.LEARNER_PASS_THRESHOLD):
            successes += 1
            if last_success is None or ts > last_success:
                last_success = ts
        else:
            failures += 1
            if last_failure is None or ts > last_failure:
                last_failure = ts
    ordered.sort(key=lambda x: x[0])
    sum_w = sum(w for w, _s in all_points)
    sum_ws = sum(w * s for w, s in all_points)
    sum_w2 = sum(w * w for w, _s in all_points)
    evidence_count = len(all_points)
    mastery = (sum_ws + prior_strength * prior) / (sum_w + prior_strength) if sum_w > 0 else prior
    ess = (sum_w * sum_w) / sum_w2 if sum_w2 > 0 else 0.0
    uncertainty = 1.0 / math.sqrt(1.0 + ess)
    wmean = (sum_ws / sum_w) if sum_w > 0 else None
    if wmean is not None and sum_w > 0:
        variance = sum(w * (s - wmean) ** 2 for w, s in all_points) / sum_w
        std = math.sqrt(max(0.0, variance))
        consistency = max(0.0, min(1.0, 1.0 - 2.0 * std))
    else:
        consistency = None
    confidence_estimate = (
        sum(confidences) / len(confidences) if confidences else None
    )
    calibration = (
        confidence_estimate - mastery if confidence_estimate is not None else None
    )
    last3 = [s for _t, s, _c in ordered[-3:]]
    recent_failures = sum(1 for s in last3 if s < float(T.LEARNER_PASS_THRESHOLD))
    return {
        "model_version": version,
        "half_life_days": _round(hl),
        "mastery_score": _round(mastery),
        "weighted_evidence": _round(sum_w),
        "evidence_count": evidence_count,
        "recent_performance": _round(_weighted_mean(recent_points)),
        "historical_performance": _round(_weighted_mean(historical_points)),
        "consistency": _round(consistency),
        "uncertainty": _round(uncertainty),
        "confidence_estimate": _round(confidence_estimate),
        "confidence_calibration": _round(calibration),
        "last_attempt_at": last_attempt,
        "last_success_at": last_success,
        "last_failure_at": last_failure,
        "successes": successes,
        "failures": failures,
        "recent_failures": recent_failures,
        "evaluated_at": _fmt(now),
    }


# ─── Attempt events ───


def _resolve_curriculum_node(topic: str) -> Optional[str]:
    """Best-effort topic → existing curriculum node id (read-only, never creates)."""
    if not (topic or "").strip():
        return None
    try:
        from medforge.curriculum import topic_path

        path = topic_path(topic)
        return (path or {}).get("node", {}).get("id")
    except Exception:
        return None


def _load_attempts(con: sqlite3.Connection, mastery_key: str) -> List[Dict[str, Any]]:
    rows = con.execute(
        "SELECT score, answered_at, learner_confidence, correct, source, item_type,"
        " item_id, session_id, curriculum_node_id, presented_at, response_time_seconds,"
        " content_version, created_at FROM learning_attempts"
        " WHERE mastery_key=? ORDER BY answered_at, attempt_id",
        (mastery_key,),
    ).fetchall()
    return [_row_dict(r) or {} for r in rows]


def _materialize(con: sqlite3.Connection, mastery_key: str, now: datetime,
                 curriculum_node_id: Optional[str] = None,
                 half_life_days: Optional[float] = None) -> Dict[str, Any]:
    """Recompute and upsert the materialized state for one key from history."""
    state = compute_model_state(_load_attempts(con, mastery_key), now,
                                half_life_days)
    existing = con.execute(
        "SELECT created_at, curriculum_node_id FROM learner_model_state"
        " WHERE mastery_key=?", (mastery_key,),
    ).fetchone()
    created = existing["created_at"] if existing else _fmt(now)
    node = curriculum_node_id or (existing["curriculum_node_id"] if existing else None)
    con.execute(
        """INSERT INTO learner_model_state
           (mastery_key, curriculum_node_id, model_version, half_life_days,
            mastery_score, weighted_evidence, evidence_count, recent_performance,
            historical_performance, consistency, uncertainty, confidence_estimate,
            confidence_calibration, last_attempt_at, last_success_at, last_failure_at,
            created_at, mastery_updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(mastery_key) DO UPDATE SET
             curriculum_node_id=COALESCE(excluded.curriculum_node_id, learner_model_state.curriculum_node_id),
             model_version=excluded.model_version,
             half_life_days=excluded.half_life_days,
             mastery_score=excluded.mastery_score,
             weighted_evidence=excluded.weighted_evidence,
             evidence_count=excluded.evidence_count,
             recent_performance=excluded.recent_performance,
             historical_performance=excluded.historical_performance,
             consistency=excluded.consistency,
             uncertainty=excluded.uncertainty,
             confidence_estimate=excluded.confidence_estimate,
             confidence_calibration=excluded.confidence_calibration,
             last_attempt_at=excluded.last_attempt_at,
             last_success_at=excluded.last_success_at,
             last_failure_at=excluded.last_failure_at,
             mastery_updated_at=excluded.mastery_updated_at""",
        (mastery_key, node, state["model_version"], state["half_life_days"],
         state["mastery_score"], state["weighted_evidence"],
         state["evidence_count"], state["recent_performance"],
         state["historical_performance"], state["consistency"],
         state["uncertainty"], state["confidence_estimate"],
         state["confidence_calibration"], state["last_attempt_at"],
         state["last_success_at"], state["last_failure_at"], created,
         state["evaluated_at"]),
    )
    state["mastery_key"] = mastery_key
    state["curriculum_node_id"] = node
    return state


def record_learning_event(
    topic: str,
    score: Optional[float] = None,
    score_fraction: Optional[float] = None,
    session_id: Optional[int] = None,
    item_type: str = "session",
    item_id: str = "",
    correct: Optional[bool] = None,
    confidence: Optional[float] = None,
    response_time_seconds: Optional[float] = None,
    source: str = "manual",
    presented_at: Optional[str] = None,
    answered_at: Optional[str] = None,
    curriculum_node_id: Optional[str] = None,
    content_version: str = "",
    mastery_key: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one learning attempt and refresh the materialized learner state.

    ``score`` uses the MedForge convention (0-10 rubric or 0-100 raw) or pass an
    explicit ``score_fraction`` in 0..1. ``answered_at`` (ISO-8601) makes the
    decay math deterministic for tests and backfills; it defaults to now.
    Returns ``{"attempt": {...}, "state": {...}}``.
    """
    if not (topic or "").strip():
        raise ValueError("topic must not be empty.")
    if score is None and score_fraction is None:
        raise ValueError("Provide score (0-10 rubric / 0-100) or score_fraction (0..1).")
    if score_fraction is not None:
        try:
            frac = float(score_fraction)
        except (TypeError, ValueError):
            raise ValueError(f"score_fraction must be a number, got {score_fraction!r}.") from None
    else:
        from medforge.learner import normalize_score

        frac = normalize_score(score) / 100.0
    if not (0.0 <= frac <= 1.0):
        raise ValueError(f"score must resolve to 0..1, got {frac!r}.")
    if item_type not in T.LEARNING_ATTEMPT_ITEM_TYPES:
        raise ValueError(
            f"item_type must be one of {T.LEARNING_ATTEMPT_ITEM_TYPES}, got {item_type!r}."
        )
    if source not in T.LEARNING_ATTEMPT_SOURCES:
        raise ValueError(
            f"source must be one of {T.LEARNING_ATTEMPT_SOURCES}, got {source!r}."
        )
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            raise ValueError(f"confidence must be a number 0..1, got {confidence!r}.") from None
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(f"confidence must be within 0..1, got {confidence!r}.")
    if response_time_seconds is not None:
        response_time_seconds = float(response_time_seconds)
        if response_time_seconds < 0:
            raise ValueError("response_time_seconds must be >= 0.")
    answered_dt = parse_attempt_time(answered_at) if answered_at else (
        parse_attempt_time(now) if now else datetime.now(timezone.utc)
    )
    presented = None
    if presented_at:
        presented = _fmt(parse_attempt_time(presented_at))
    key = mastery_key or slugify(topic)
    ensure_learner_model_tables()
    con = _connect()
    try:
        if curriculum_node_id is not None:
            row = con.execute(
                "SELECT count(*) FROM curriculum_nodes WHERE id=?", (curriculum_node_id,)
            ).fetchone()
            if not row or row[0] == 0:
                raise ValueError(f"Unknown curriculum_node_id: {curriculum_node_id!r}.")
        if session_id is not None:
            row = con.execute(
                "SELECT id FROM interactive_sessions WHERE id=?", (int(session_id),)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown session_id: {session_id!r}.")
        node = curriculum_node_id or _resolve_curriculum_node(topic)
        cur = con.execute(
            """INSERT INTO learning_attempts
               (session_id, curriculum_node_id, mastery_key, item_type, item_id,
                presented_at, answered_at, score, correct, learner_confidence,
                response_time_seconds, source, content_version, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(session_id) if session_id is not None else None, node, key, item_type,
             item_id or "", presented, _fmt(answered_dt), _round(frac),
             None if correct is None else int(bool(correct)), confidence,
             response_time_seconds, source, content_version, utcnow()),
        )
        attempt_id = cur.lastrowid
        state = _materialize(con, key, answered_dt, node)
        con.commit()
        attempt = _row_dict(con.execute(
            "SELECT * FROM learning_attempts WHERE attempt_id=?", (attempt_id,),
        ).fetchone()) or {}
        return {"attempt": attempt, "state": state}
    finally:
        con.close()


def model_state_rows(curriculum_node_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Materialized states, optionally filtered by curriculum node."""
    ensure_learner_model_tables()
    con = _connect()
    try:
        if curriculum_node_id:
            rows = con.execute(
                "SELECT * FROM learner_model_state WHERE curriculum_node_id=?"
                " ORDER BY mastery_score", (curriculum_node_id,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM learner_model_state ORDER BY mastery_score"
            ).fetchall()
        return [_row_dict(r) or {} for r in rows]
    finally:
        con.close()


# ─── Recalculation (reproducibility) ───


def recalculate_mastery(topic: str, now: Optional[str] = None) -> Dict[str, Any]:
    """Rebuild one mastery key's state from stored attempts.

    With no ``now`` the stored ``mastery_updated_at`` is reused as the
    evaluation time, so the rebuilt state must equal the materialized row
    exactly (the core reproducibility guarantee). Passing ``now`` explicitly
    re-evaluates the decay as of that time.
    """
    if not (topic or "").strip():
        raise ValueError("topic must not be empty.")
    key = topic.strip()
    ensure_learner_model_tables()
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM learner_model_state WHERE mastery_key=?", (key,)
        ).fetchone()
        if row is None:  # fall back to the topic-slug namespace
            key = slugify(topic)
            row = con.execute(
                "SELECT * FROM learner_model_state WHERE mastery_key=?", (key,)
            ).fetchone()
        stored = _row_dict(row)
        if stored is None:
            raise ValueError(f"No learner-model state for topic {topic!r} (key {key!r}).")
        evaluation_time = parse_attempt_time(now) if now else parse_attempt_time(
            stored["mastery_updated_at"]
        )
        rebuilt = compute_model_state(_load_attempts(con, key), evaluation_time,
                                      stored["half_life_days"],
                                      stored["model_version"])
        fields = ("mastery_score", "weighted_evidence", "evidence_count",
                  "recent_performance", "historical_performance", "consistency",
                  "uncertainty", "confidence_estimate", "confidence_calibration",
                  "last_attempt_at", "last_success_at", "last_failure_at")
        matches = all(stored.get(f) == rebuilt.get(f) for f in fields)
        if now is not None:
            _materialize(con, key, evaluation_time, stored.get("curriculum_node_id"))
            con.commit()
        return {
            "mastery_key": key, "matches_stored": matches,
            "model_version": stored["model_version"],
            "evaluated_at": rebuilt["evaluated_at"],
            "rebuilt": {f: rebuilt.get(f) for f in fields},
            "stored": {f: stored.get(f) for f in fields},
        }
    finally:
        con.close()


def recalculate_all(now: Optional[str] = None) -> Dict[str, Any]:
    """Rebuild every materialized state; reports mismatches (expected: none)."""
    ensure_learner_model_tables()
    con = _connect()
    try:
        keys = [r[0] for r in con.execute(
            "SELECT mastery_key FROM learner_model_state ORDER BY mastery_key"
        ).fetchall()]
    finally:
        con.close()
    results = [recalculate_mastery(k, now) for k in keys]
    mismatches = [r["mastery_key"] for r in results if not r["matches_stored"]]
    return {"keys": len(results), "mismatches": mismatches,
            "model_version": T.LEARNER_MODEL_VERSION, "results": results}


# ─── Read interfaces (P7-consumable) ───


def _state_for(topic: str) -> Optional[Dict[str, Any]]:
    ensure_learner_model_tables()
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM learner_model_state WHERE mastery_key=?", (topic.strip(),)
        ).fetchone()
        if row is None:
            row = con.execute(
                "SELECT * FROM learner_model_state WHERE mastery_key=?",
                (slugify(topic),),
            ).fetchone()
        return _row_dict(row)
    finally:
        con.close()


def get_mastery(topic: str) -> Optional[Dict[str, Any]]:
    """Current recency-weighted mastery estimate for a topic (or None)."""
    state = _state_for(topic)
    if state is None:
        return None
    return {
        "topic": topic, "mastery_key": state["mastery_key"],
        "mastery": state["mastery_score"],
        "mastery_percent": round(100.0 * state["mastery_score"], 1),
        "evidence_count": state["evidence_count"],
        "uncertainty": state["uncertainty"],
        "consistency": state["consistency"],
        "recent_performance": state["recent_performance"],
        "historical_performance": state["historical_performance"],
        "model_version": state["model_version"],
        "evaluated_at": state["mastery_updated_at"],
        "last_attempt_at": state["last_attempt_at"],
    }


def get_confidence(topic: str) -> Optional[Dict[str, Any]]:
    """Separate confidence track: estimate, calibration gap and mismatch flag."""
    state = _state_for(topic)
    if state is None:
        return None
    estimate = state["confidence_estimate"]
    calibration = state["confidence_calibration"]
    return {
        "topic": topic, "mastery_key": state["mastery_key"],
        "confidence_estimate": estimate,
        "mastery": state["mastery_score"],
        "calibration_gap": calibration,
        "mismatch": (abs(calibration) >= 0.2) if calibration is not None else False,
        "direction": (
            None if calibration is None else
            ("overconfident" if calibration > 0 else "underconfident" if calibration < 0 else "aligned")
        ),
        "has_confidence_data": estimate is not None,
    }


def get_recent_performance(topic: str) -> Optional[Dict[str, Any]]:
    """Weighted recent vs historical performance for a topic (or None)."""
    state = _state_for(topic)
    if state is None:
        return None
    return {
        "topic": topic, "mastery_key": state["mastery_key"],
        "recent_window_days": float(T.LEARNER_RECENT_WINDOW_DAYS),
        "recent_performance": state["recent_performance"],
        "historical_performance": state["historical_performance"],
        "trend": (
            None if state["recent_performance"] is None
            or state["historical_performance"] is None
            else round(state["recent_performance"] - state["historical_performance"], 4)
        ),
    }


def learner_history(topic: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Most recent attempts for a topic (newest first)."""
    ensure_learner_model_tables()
    key = None
    state = _state_for(topic)
    if state is not None:
        key = state["mastery_key"]
    else:
        key = slugify(topic)
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM learning_attempts WHERE mastery_key=?"
            " ORDER BY answered_at DESC, attempt_id DESC LIMIT ?",
            (key, max(1, int(limit))),
        ).fetchall()
        return [_row_dict(r) or {} for r in rows]
    finally:
        con.close()


# ─── Weakness detection (known vs possible/low-confidence) ───


def _severity_for(mastery: float) -> str:
    if mastery < 0.35:
        return "critical"
    if mastery < 0.50:
        return "high"
    return "medium"


def _prerequisite_map(con: sqlite3.Connection) -> Dict[str, List[str]]:
    """topic-slug → prerequisite topic-slugs (P2 graph, read-only)."""
    rows = con.execute(
        """SELECT child.title AS topic_title, parent.title AS prereq_title
           FROM prerequisites p
           JOIN curriculum_nodes child ON child.id = p.topic_id
           JOIN curriculum_nodes parent ON parent.id = p.prerequisite_id"""
    ).fetchall()
    out: Dict[str, List[str]] = {}
    for r in rows:
        out.setdefault(slugify(r["topic_title"]), []).append(slugify(r["prereq_title"]))
    return out


def _prerequisite_magnitude(key: str, prereq_map: Dict[str, List[str]],
                            states: Dict[str, Dict[str, Any]]) -> float:
    """How weak the weakest prerequisite of this topic is (0..1)."""
    worst = 0.0
    for prereq_key in prereq_map.get(key, []):
        state = states.get(prereq_key)
        if state is None:
            continue
        worst = max(worst, max(0.0, (float(T.LEARNER_WEAK_THRESHOLD)
                                     - state["mastery_score"])) / float(T.LEARNER_WEAK_THRESHOLD))
    return round(min(1.0, worst), ROUND)


def _attempt_failure_stats(con: sqlite3.Connection, key: str) -> tuple:
    """(total failures, last-3 failure share, two newest scores)."""
    rows = con.execute(
        "SELECT score FROM learning_attempts WHERE mastery_key=?"
        " ORDER BY answered_at DESC, attempt_id DESC", (key,),
    ).fetchall()
    if not rows:
        return 0, 0.0, []
    threshold = float(T.LEARNER_PASS_THRESHOLD)
    failures = sum(1 for r in rows if float(r[0]) < threshold)
    recent = rows[:3]
    recent_rate = sum(1 for r in recent if float(r[0]) < threshold) / len(recent)
    return failures, round(recent_rate, ROUND), [float(r[0]) for r in rows[:2]]


def detect_weaknesses(topic: Optional[str] = None,
                      now: Optional[str] = None) -> Dict[str, Any]:
    """Derive known/possible weaknesses from the materialized learner states.

    Rules (documented in docs/LEARNER_MODEL.md):
    - mastery < 0.60 and evidence_count < 3 → POSSIBLE weakness
      (``low_confidence=1``, severity low) — one mistake is never a verdict.
    - mastery < 0.60, evidence_count ≥ 3 and a recent failure → KNOWN weakness
      (severity from the estimate: <0.35 critical, <0.50 high, else medium).
    - mastery < 0.60, evidence ≥ 3, no recent failure → POSSIBLE/
      low-confidence again (old failures only).
    - mastery ≥ 0.75 and recent performance ≥ 0.70 → auto-recovery: the
      learner-model weakness row is resolved; manual/review rows are untouched.
    """
    ensure_learner_model_tables()
    now_dt = parse_attempt_time(now) if now else datetime.now(timezone.utc)
    states = {s["mastery_key"]: s for s in model_state_rows()}
    if topic:
        wanted = _state_for(topic)
        states = {wanted["mastery_key"]: wanted} if wanted else {}
    con = _connect()
    created = updated = recovered = 0
    try:
        prereq_map = _prerequisite_map(con)
        for key, state in states.items():
            if not state.get("evidence_count"):
                continue
            mastery = float(state["mastery_score"])
            total_failures, recent_rate, last_two = _attempt_failure_stats(con, key)
            recent_failures = int(round(recent_rate * min(3, int(state["evidence_count"]))))
            weak_score = round(min(1.0, max(0.0, (
                float(T.LEARNER_WEAK_THRESHOLD) - mastery)) / float(T.LEARNER_WEAK_THRESHOLD)), ROUND)
            row = con.execute(
                "SELECT id FROM learner_weaknesses WHERE topic_id=? AND concept=?"
                " COLLATE NOCASE AND origin='learner_model' AND is_resolved=0",
                (key, WEAKNESS_CONCEPT),
            ).fetchone()
            recent_perf = state["recent_performance"]
            recent_perf_value = float(recent_perf) if recent_perf is not None else 0.0
            recovered_path = (
                (mastery >= float(T.LEARNER_RECOVERY_THRESHOLD)
                 and recent_perf_value >= 0.70)
                or (
                    mastery >= float(T.LEARNER_RECOVERY_MASTERY_FLOOR)
                    and len(last_two) >= 2
                    and all(s >= float(T.LEARNER_PASS_THRESHOLD) for s in last_two)
                    and (sum(last_two) / len(last_two)) >= float(T.LEARNER_RECOVERY_RECENT_MIN)
                )
            )
            if recovered_path:
                if row is not None:
                    con.execute(
                        "UPDATE learner_weaknesses SET is_resolved=1, recovered_at=?,"
                        " review_priority=0.0, last_observed_at=? WHERE id=?",
                        (_fmt(now_dt), _fmt(now_dt), row["id"]),
                    )
                    recovered += 1
                continue
            if mastery >= float(T.LEARNER_WEAK_THRESHOLD):
                continue
            evidence_count = int(state["evidence_count"])
            failure_count = max(1, total_failures)
            known = evidence_count >= int(T.LEARNER_MIN_EVIDENCE_KNOWN_WEAKNESS) \
                and recent_rate > 0
            severity = _severity_for(mastery) if known else "low"
            prereq_impact = 1 if _prerequisite_magnitude(key, prereq_map, states) > 0 else 0
            misconception = (
                f"Recency-weighted mastery estimate is {mastery:.0%}"
                + (" with a recent failure" if recent_failures else "")
                + (" (limited evidence)" if not known else "")
            )
            if row is None:
                con.execute(
                    """INSERT INTO learner_weaknesses
                       (topic_id, concept, misconception, error_count, severity,
                        is_resolved, first_observed_at, last_observed_at,
                        curriculum_node_id, origin, weakness_score, failure_count,
                        recent_failure_rate, prerequisite_impact, review_priority,
                        low_confidence, last_failure_at, recovered_at)
                       VALUES(?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                    (key, WEAKNESS_CONCEPT, misconception, failure_count, severity,
                     _fmt(now_dt), _fmt(now_dt), state.get("curriculum_node_id"),
                     "learner_model", weak_score, failure_count, recent_rate,
                     prereq_impact, None, 0 if known else 1,
                     state.get("last_failure_at")),
                )
                created += 1
            else:
                con.execute(
                    """UPDATE learner_weaknesses SET misconception=?, error_count=?,
                       severity=?, last_observed_at=?, curriculum_node_id=?,
                       weakness_score=?, failure_count=?, recent_failure_rate=?,
                       prerequisite_impact=?, low_confidence=?, last_failure_at=?
                       WHERE id=?""",
                    (misconception, failure_count, severity, _fmt(now_dt),
                     state.get("curriculum_node_id"), weak_score, failure_count,
                     recent_rate, prereq_impact, 0 if known else 1,
                     state.get("last_failure_at"), row["id"]),
                )
                updated += 1
        con.commit()
        return {"created": created, "updated": updated, "recovered": recovered,
                "evaluated_at": _fmt(now_dt)}
    finally:
        con.close()


def get_weaknesses(include_resolved: bool = False, limit: int = 200) -> List[Dict[str, Any]]:
    """Unresolved weaknesses with their P6 analysis fields."""
    ensure_learner_model_tables()
    con = _connect()
    try:
        where = "" if include_resolved else "WHERE is_resolved=0"
        rows = con.execute(
            f"SELECT * FROM learner_weaknesses {where}"
            " ORDER BY COALESCE(weakness_score, 0) DESC, error_count DESC, id DESC"
            " LIMIT ?", (max(1, int(limit)),),
        ).fetchall()
        return [_row_dict(r) or {} for r in rows]
    finally:
        con.close()


# ─── Prerequisite awareness (read-only) ───


def get_prerequisite_risks(topic: str) -> Dict[str, Any]:
    """Expose weak prerequisites for a topic without punishing the topic.

    Returns ``{"topic", "mastery_key", "node_id", "risks": [...]}`` where each
    risk carries the prerequisite's own mastery/evidence and an impact flag.
    The topic's mastery is never modified by this call.
    """
    key = slugify(topic)
    state = _state_for(topic)
    node_id = None
    try:
        from medforge.curriculum import topic_path

        path = topic_path(topic)
        node_id = (path or {}).get("node", {}).get("id")
    except Exception:
        node_id = None
    if node_id is None:
        return {"topic": topic, "mastery_key": key, "node_id": None,
                "risks": [], "note": "topic is not mapped to a curriculum node"}
    ensure_learner_model_tables()
    con = _connect()
    try:
        rows = con.execute(
            """SELECT p.prerequisite_id, p.relationship_type, p.dependency_weight,
                      cn.title AS title, cn.node_type
               FROM prerequisites p JOIN curriculum_nodes cn ON cn.id=p.prerequisite_id
               WHERE p.topic_id=? ORDER BY p.dependency_weight DESC""",
            (node_id,),
        ).fetchall()
        states = {s["mastery_key"]: s for s in model_state_rows()}
        risks: List[Dict[str, Any]] = []
        for r in rows:
            prereq_key = slugify(r["title"])
            p_state = states.get(prereq_key)
            mastery = float(p_state["mastery_score"]) if p_state else None
            weak = (mastery is not None and mastery < float(T.LEARNER_WEAK_THRESHOLD))
            impact = 0.0
            if mastery is not None and weak:
                impact = round(min(1.0, (float(T.LEARNER_WEAK_THRESHOLD) - mastery)
                                   / float(T.LEARNER_WEAK_THRESHOLD)), ROUND)
            risks.append({
                "prerequisite_id": r["prerequisite_id"], "title": r["title"],
                "node_type": r["node_type"],
                "relationship_type": r["relationship_type"],
                "mastery": None if mastery is None else round(mastery, ROUND),
                "mastery_percent": None if mastery is None else round(100.0 * mastery, 1),
                "evidence_count": int(p_state["evidence_count"]) if p_state else 0,
                "weak": bool(weak), "impact": impact,
                "has_learner_data": bool(p_state),
            })
        return {"topic": topic, "mastery_key": key, "node_id": node_id,
                "risks": risks, "weak_count": sum(1 for r in risks if r["weak"])}
    finally:
        con.close()


# ─── Deterministic study priority ───


def study_priority(limit: int = 20, now: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic recommendation signal over the learner states.

    ``priority = 0.35·weakness + 0.20·uncertainty + 0.20·overdue
                  + 0.15·recent_failure + 0.10·prerequisite_impact``
    with every component normalized to 0..1 and returned for inspection.
    This is a signal for the future planner — P6 does not build the planner.
    """
    ensure_learner_model_tables()
    now_dt = parse_attempt_time(now) if now else datetime.now(timezone.utc)
    now_str = _fmt(now_dt)
    states = model_state_rows()
    con = _connect()
    try:
        prereq_map = _prerequisite_map(con)
        state_map = {s["mastery_key"]: s for s in states}
        weakness_rows = con.execute(
            "SELECT topic_id, max(COALESCE(weakness_score, 0)) AS score"
            " FROM learner_weaknesses WHERE is_resolved=0 GROUP BY topic_id"
        ).fetchall()
        weakness_map = {r["topic_id"]: float(r["score"] or 0.0) for r in weakness_rows}
        due_rows = con.execute(
            "SELECT substr(item_id, 1, instr(item_id, ':') - 1) AS topic,"
            " min(due_date) AS due FROM spaced_repetition_queue"
            " WHERE item_type='card' AND instr(item_id, ':') > 1"
            " GROUP BY topic"
        ).fetchall()
        due_map = {r["topic"]: r["due"] for r in due_rows}
        recent_failure_map = {
            s["mastery_key"]: _attempt_failure_stats(con, s["mastery_key"])[1]
            for s in states
        }
    finally:
        con.close()
    weights = dict(T.LEARNER_PRIORITY_WEIGHTS)
    items: List[Dict[str, Any]] = []
    for state in states:
        key = state["mastery_key"]
        mastery = float(state["mastery_score"])
        weakness_component = max(
            min(1.0, max(0.0, (float(T.LEARNER_RECOVERY_THRESHOLD) - mastery)
                         / float(T.LEARNER_RECOVERY_THRESHOLD))),
            weakness_map.get(key, 0.0),
        )
        uncertainty_component = float(state["uncertainty"])
        due = due_map.get(key)
        overdue_days = 0.0
        if due:
            try:
                overdue_days = max(0.0, (now_dt - parse_attempt_time(due)).total_seconds() / 86400.0)
            except ValueError:
                overdue_days = 0.0
        overdue_component = min(1.0, overdue_days / 7.0)
        recent_failure_component = recent_failure_map.get(key, 0.0)
        prereq_component = _prerequisite_magnitude(key, prereq_map, state_map)
        components = {
            "weakness": round(weakness_component, ROUND),
            "uncertainty": round(uncertainty_component, ROUND),
            "overdue": round(overdue_component, ROUND),
            "recent_failure": round(recent_failure_component, ROUND),
            "prerequisite_impact": round(prereq_component, ROUND),
        }
        score = round(sum(weights[k] * components[k] for k in weights), ROUND)
        reasons = [f"{k}={components[k]:.2f}" for k in weights if components[k] > 0]
        items.append({
            "topic": key, "mastery": round(mastery, ROUND),
            "mastery_percent": round(100.0 * mastery, 1),
            "evidence_count": int(state["evidence_count"]),
            "overdue_days": round(overdue_days, 2),
            "next_due_at": due,
            "components": components, "priority": score,
            "reasons": reasons or ["no outstanding risk signals"],
        })
    items.sort(key=lambda x: (-x["priority"], x["topic"]))
    return {"model_version": T.LEARNER_MODEL_VERSION, "generated_at": now_str,
            "weights": weights, "count": len(items), "items": items[: max(1, int(limit))]}


def get_review_priority(limit: int = 20, now: Optional[str] = None) -> Dict[str, Any]:
    """Named P7 interface: deterministic review/study priority signal."""
    return study_priority(limit, now)


# ─── Summary + curriculum report ───


def learner_summary(limit: int = 8) -> Dict[str, Any]:
    """Model-level dashboard summary: strengths, declines, mismatches, priorities."""
    ensure_learner_model_tables()
    con = _connect()
    try:
        events = con.execute("SELECT count(*) FROM learning_attempts").fetchone()[0]
        weakness_counts = {
            r[0]: r[1] for r in con.execute(
                "SELECT low_confidence, count(*) FROM learner_weaknesses"
                " WHERE is_resolved=0 GROUP BY low_confidence"
            ).fetchall()
        }
        overdue = con.execute(
            "SELECT count(*) FROM spaced_repetition_queue"
            " WHERE item_type='card' AND due_date <= ?", (utcnow(),),
        ).fetchone()[0]
    finally:
        con.close()
    states = model_state_rows()
    tracked = [s for s in states if s["evidence_count"] > 0]
    strongest = sorted(tracked, key=lambda s: -s["mastery_score"])[: int(limit)]
    weakest = sorted(tracked, key=lambda s: s["mastery_score"])[: int(limit)]
    improving, declining = [], []
    for s in tracked:
        if s["recent_performance"] is None or s["historical_performance"] is None:
            continue
        delta = float(s["recent_performance"]) - float(s["historical_performance"])
        entry = {"topic": s["mastery_key"], "delta": round(delta, ROUND),
                 "recent_performance": s["recent_performance"],
                 "historical_performance": s["historical_performance"]}
        if delta >= 0.05:
            improving.append(entry)
        elif delta <= -0.05:
            declining.append(entry)
    improving.sort(key=lambda x: -x["delta"])
    declining.sort(key=lambda x: x["delta"])
    mismatches = [
        {"topic": s["mastery_key"], "confidence_estimate": s["confidence_estimate"],
         "mastery": s["mastery_score"], "calibration_gap": s["confidence_calibration"]}
        for s in tracked if s["confidence_calibration"] is not None
        and abs(float(s["confidence_calibration"])) >= 0.2
    ]
    avg = (sum(float(s["mastery_score"]) for s in tracked) / len(tracked)) if tracked else 0.0
    return {
        "model_version": T.LEARNER_MODEL_VERSION,
        "half_life_days": float(T.LEARNER_HALF_LIFE_DAYS),
        "summary": {
            "topics_tracked": len(tracked),
            "events_total": int(events),
            "avg_mastery": round(100.0 * avg, 1),
            "known_weaknesses": int(weakness_counts.get(0, 0)),
            "possible_weaknesses": int(weakness_counts.get(1, 0)),
            "calibration_mismatches": len(mismatches),
            "overdue_cards": int(overdue),
        },
        "strongest": [
            {"topic": s["mastery_key"], "mastery_percent": round(100.0 * s["mastery_score"], 1),
             "evidence_count": s["evidence_count"], "uncertainty": s["uncertainty"]}
            for s in strongest
        ],
        "weakest": [
            {"topic": s["mastery_key"], "mastery_percent": round(100.0 * s["mastery_score"], 1),
             "evidence_count": s["evidence_count"], "uncertainty": s["uncertainty"]}
            for s in weakest
        ],
        "improving": improving[: int(limit)],
        "declining": declining[: int(limit)],
        "confidence_mismatches": mismatches[: int(limit)],
        "priorities": study_priority(limit=int(limit))["items"],
    }


def curriculum_report() -> Dict[str, Any]:
    """P2 curriculum nodes joined with the P6 learner state (additive)."""
    ensure_learner_model_tables()
    con = _connect()
    try:
        nodes = con.execute(
            "SELECT id, parent_id, node_type, title FROM curriculum_nodes"
            " WHERE node_type IN ('Topic', 'Subtopic')"
            " ORDER BY order_index, title COLLATE NOCASE"
        ).fetchall()
        weakness_rows = con.execute(
            "SELECT topic_id, count(*) AS n FROM learner_weaknesses"
            " WHERE is_resolved=0 GROUP BY topic_id"
        ).fetchall()
        weakness_map = {r["topic_id"]: int(r["n"]) for r in weakness_rows}
        due_rows = con.execute(
            "SELECT substr(item_id, 1, instr(item_id, ':') - 1) AS topic,"
            " min(due_date) AS due FROM spaced_repetition_queue"
            " WHERE item_type='card' AND instr(item_id, ':') > 1 GROUP BY topic"
        ).fetchall()
        due_map = {r["topic"]: r["due"] for r in due_rows}
        states = {s["mastery_key"]: s for s in model_state_rows()}
    finally:
        con.close()
    topics: List[Dict[str, Any]] = []
    for node in nodes:
        key = slugify(node["title"])
        state = states.get(key)
        risks = get_prerequisite_risks(node["title"]) if state else None
        topics.append({
            "node_id": node["id"], "title": node["title"],
            "node_type": node["node_type"], "mastery_key": key,
            "mastery": state["mastery_score"] if state else None,
            "mastery_percent": round(100.0 * state["mastery_score"], 1) if state else None,
            "evidence_count": state["evidence_count"] if state else 0,
            "evidence_strength": state["uncertainty"] if state else None,
            "recent_performance": state["recent_performance"] if state else None,
            "weaknesses": weakness_map.get(key, 0),
            "last_studied": state["last_attempt_at"] if state else None,
            "next_review": due_map.get(key),
            "prerequisite_warnings": (risks or {}).get("weak_count", 0),
        })
    return {"model_version": T.LEARNER_MODEL_VERSION, "count": len(topics),
            "topics": topics}
