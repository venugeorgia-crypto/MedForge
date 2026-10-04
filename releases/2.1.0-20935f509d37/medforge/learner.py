"""MedForge V3 learner tracking: sessions, mastery, weaknesses, scheduling.

Backs the STUDY flow with the V3 curriculum tables (interactive_sessions,
learner_mastery, learner_weaknesses) and the REVIEW flow with the spaced
repetition queue (spaced_repetition_queue, SM-2 scheduling) at the active
T.META_DB. The V3 DDL is applied idempotently on every call, so a legacy V2
database self-heals on first use without an explicit migration.

Depends only on medforge.types + medforge.utils (top of the module DAG).
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import medforge.types as T
from medforge.utils import mkdirs, slugify, utcnow

# Session score at or above this counts as a successful attempt.
PASS_SCORE: float = 70.0

# SM-2 scheduling constants (mirrors the schema CHECK on ease_factor).
SM2_MIN_EASE: float = 1.30
DEFAULT_EASE: float = 2.50
SM2_PASS_GRADE: int = 3

# Spaced repetition schedulers: classic SM-2 (default) or FSRS-4.5.
SCHEDULERS: Tuple[str, ...] = ("sm2", "fsrs")
DEFAULT_SCHEDULER: str = "sm2"
# Failed cards come back after this short learning step instead of tomorrow.
LAPSE_STEP_MINUTES: int = 10
# Leech threshold: cards with this many lapses get flagged for rewriting.
LEECH_THRESHOLD: int = 3
# FSRS-4.5 default parameters (open-spaced-repetition/fsrs4anki wiki).
FSRS_WEIGHTS: Tuple[float, ...] = (
    0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031, 1.6474,
    0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755,
)
FSRS_DECAY: float = -0.5
FSRS_FACTOR: float = 19 / 81
FSRS_DESIRED_RETENTION: float = 0.9

_SEVERITY_RANK = {name: i for i, name in enumerate(T.WEAKNESS_SEVERITY)}

__all__ = [
    "PASS_SCORE", "SM2_MIN_EASE", "DEFAULT_EASE", "SM2_PASS_GRADE",
    "SCHEDULERS", "DEFAULT_SCHEDULER", "LAPSE_STEP_MINUTES", "LEECH_THRESHOLD",
    "ensure_v3_tables", "start_session", "record_session", "update_mastery",
    "record_weakness", "resolve_weakness", "log_study_result", "learner_snapshot",
    "card_item_id", "import_flashcards", "due_items", "review_card",
    "review_analytics", "spaced_repetition_snapshot",
]


def _v3_schema_ddl() -> str:
    """Import the canonical V3 DDL from core.database.schema.

    Tries T.BASE first (tests may point it elsewhere), then walks up from
    this file (symlinks like current/ -> releases/<tag>/ resolve away) until
    a core/database/schema.py is found.
    """
    here = Path(__file__).resolve()
    candidates = [str(T.BASE)] + [str(p) for p in here.parents]
    for base_str in candidates:
        if base_str not in sys.path:
            sys.path.insert(0, base_str)
        try:
            from core.database.schema import V3_SCHEMA_DDL

            return V3_SCHEMA_DDL
        except ImportError:
            continue
    raise ImportError(
        "core.database.schema not importable from: " + ", ".join(candidates)
    )


def ensure_v3_tables() -> None:
    """Create any missing V3 tables on the active META_DB (idempotent)."""
    mkdirs()
    con = sqlite3.connect(T.META_DB)
    try:
        con.executescript(_v3_schema_ddl())
        con.commit()
    finally:
        con.close()


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    return con


def _normalize_score(score: float) -> float:
    """Accept the 0-10 mission rubric or a raw 0-100 score; return 0-100."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        raise ValueError(f"Score must be a number, got {score!r}.") from None
    if 0.0 <= s <= 10.0:
        s *= 10.0  # mission self-score rubric is out of 10
    if not (0.0 <= s <= 100.0):
        raise ValueError(f"Score must be within 0-10 (rubric) or 0-100, got {score!r}.")
    return round(s, 1)


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return None if row is None else {k: row[k] for k in row.keys()}

def start_session(topic: str, session_type: str = "Learn", notes: str = "") -> Dict[str, Any]:
    """Open a new (not yet completed) interactive session for a topic."""
    if session_type not in T.SESSION_TYPES:
        raise ValueError(f"session_type must be one of {T.SESSION_TYPES}, got {session_type!r}.")
    if not topic.strip():
        raise ValueError("Topic must not be empty.")
    tid = slugify(topic)
    now = utcnow()
    ensure_v3_tables()
    con = _connect()
    try:
        cur = con.execute(
            """INSERT INTO interactive_sessions
               (session_type, topic_id, score, duration_seconds, metrics, notes, created_at, completed_at)
               VALUES(?,?,0.0,0,?,?,?,NULL)""",
            (session_type, tid, json.dumps({"title": topic.strip()}), notes, now),
        )
        con.commit()
        return {
            "session_id": cur.lastrowid, "topic_id": tid, "session_type": session_type,
            "created_at": now, "completed_at": None,
        }
    finally:
        con.close()


def record_session(
    topic: str,
    session_type: str = "Active Recall",
    score: Optional[float] = None,
    duration_seconds: int = 0,
    notes: str = "",
    metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Insert a completed session row (primitive; use log_study_result for scoring)."""
    if session_type not in T.SESSION_TYPES:
        raise ValueError(f"session_type must be one of {T.SESSION_TYPES}, got {session_type!r}.")
    if not topic.strip():
        raise ValueError("Topic must not be empty.")
    s = None if score is None else _normalize_score(score)
    duration_seconds = max(0, int(duration_seconds or 0))
    tid = slugify(topic)
    now = utcnow()
    ensure_v3_tables()
    result: Dict[str, Any] = {
        "topic_id": tid, "session_type": session_type,
        "score": s, "duration_seconds": duration_seconds, "completed_at": now,
    }
    con = _connect()
    try:
        cur = con.execute(
            """INSERT INTO interactive_sessions
               (session_type, topic_id, score, duration_seconds, metrics, notes, created_at, completed_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (session_type, tid, s, duration_seconds,
             json.dumps(metrics or {}), notes, now, now),
        )
        con.commit()
        result["session_id"] = cur.lastrowid
    finally:
        con.close()
    if s is not None:
        result["mastery"] = update_mastery(tid, s)
    return result


def update_mastery(topic_id: str, score: float) -> Dict[str, Any]:
    """Fold a normalized 0-100 session score into the topic's mastery row.

    mastery_score is an incremental mean over attempts; confidence_score is the
    share of attempts that reached PASS_SCORE.
    """
    s = _normalize_score(score)
    now = utcnow()
    ensure_v3_tables()
    con = _connect()
    try:
        row = con.execute(
            "SELECT mastery_score, total_attempts, successful_attempts, created_at"
            " FROM learner_mastery WHERE topic_id=?", (topic_id,),
        ).fetchone()
        if row is None:
            mastery, attempts, successful, created = s, 1, int(s >= PASS_SCORE), now
        else:
            attempts = row["total_attempts"] + 1
            mastery = row["mastery_score"] + (s - row["mastery_score"]) / attempts
            successful = row["successful_attempts"] + int(s >= PASS_SCORE)
            created = row["created_at"]
        con.execute(
            """INSERT INTO learner_mastery
               (topic_id, mastery_score, confidence_score, total_attempts,
                successful_attempts, last_attempt_at, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(topic_id) DO UPDATE SET
                 mastery_score=excluded.mastery_score,
                 confidence_score=excluded.confidence_score,
                 total_attempts=excluded.total_attempts,
                 successful_attempts=excluded.successful_attempts,
                 last_attempt_at=excluded.last_attempt_at,
                 updated_at=excluded.updated_at""",
            (topic_id, round(mastery, 1), round(100.0 * successful / attempts, 1),
             attempts, successful, now, created, now),
        )
        con.commit()
        return _row_dict(con.execute(
            "SELECT * FROM learner_mastery WHERE topic_id=?", (topic_id,),
        ).fetchone()) or {}
    finally:
        con.close()

def record_weakness(
    topic: str,
    concept: str,
    misconception: str = "",
    severity: str = "medium",
) -> Dict[str, Any]:
    """Record a diagnostic weakness, or escalate the matching unresolved one."""
    if severity not in T.WEAKNESS_SEVERITY:
        raise ValueError(f"severity must be one of {T.WEAKNESS_SEVERITY}, got {severity!r}.")
    if not concept.strip():
        raise ValueError("Concept must not be empty.")
    tid = slugify(topic)
    now = utcnow()
    ensure_v3_tables()
    con = _connect()
    try:
        row = con.execute(
            "SELECT id, error_count, severity, misconception FROM learner_weaknesses"
            " WHERE topic_id=? AND concept=? COLLATE NOCASE AND is_resolved=0"
            " ORDER BY id DESC LIMIT 1", (tid, concept.strip()),
        ).fetchone()
        if row is None:
            cur = con.execute(
                """INSERT INTO learner_weaknesses
                   (topic_id, concept, misconception, error_count, severity,
                    is_resolved, first_observed_at, last_observed_at)
                   VALUES(?,?,?,?,?,0,?,?)""",
                (tid, concept.strip(), misconception.strip(), 1, severity, now, now),
            )
            wid = cur.lastrowid
        else:
            wid = row["id"]
            new_rank = max(_SEVERITY_RANK[severity], _SEVERITY_RANK[row["severity"]])
            con.execute(
                """UPDATE learner_weaknesses SET error_count=?, severity=?,
                   misconception=?, last_observed_at=? WHERE id=?""",
                (row["error_count"] + 1, T.WEAKNESS_SEVERITY[new_rank],
                 (misconception.strip() or row["misconception"]), now, wid),
            )
        con.commit()
        return _row_dict(con.execute(
            "SELECT * FROM learner_weaknesses WHERE id=?", (wid,),
        ).fetchone()) or {}
    finally:
        con.close()


def resolve_weakness(topic: Optional[str] = None, weakness_id: Optional[int] = None) -> int:
    """Mark weakness rows resolved (by id, or every unresolved row of a topic)."""
    if weakness_id is None and not (topic or "").strip():
        raise ValueError("Provide weakness_id or a topic.")
    now = utcnow()
    ensure_v3_tables()
    con = _connect()
    try:
        if weakness_id is not None:
            cur = con.execute(
                "UPDATE learner_weaknesses SET is_resolved=1, resolved_at=? WHERE id=?",
                (now, int(weakness_id)),
            )
        else:
            cur = con.execute(
                """UPDATE learner_weaknesses SET is_resolved=1, resolved_at=?
                   WHERE topic_id=? AND is_resolved=0""",
                (now, slugify(topic)),
            )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def log_study_result(
    topic: str,
    score: float,
    session_type: Optional[str] = None,
    duration_seconds: int = 0,
    notes: str = "",
    weaknesses: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Log a finished study session: close any open session, score it,
    update mastery, and record reported weaknesses.

    The score may use the 0-10 mission rubric or a raw 0-100 value. If an open
    session exists for the topic (created by start_session/study()), it is
    completed in place with the score and measured duration; otherwise a new
    completed session row is inserted.
    """
    if session_type is not None and session_type not in T.SESSION_TYPES:
        raise ValueError(f"session_type must be one of {T.SESSION_TYPES}, got {session_type!r}.")
    s = _normalize_score(score)
    tid = slugify(topic)
    now = utcnow()
    ensure_v3_tables()
    con = _connect()
    try:
        row = con.execute(
            "SELECT id, session_type, created_at FROM interactive_sessions"
            " WHERE topic_id=? AND completed_at IS NULL ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        if row is not None:
            stype = session_type or row["session_type"]
            duration = int(duration_seconds or 0)
            if duration <= 0:
                try:
                    started = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                    duration = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
                except (TypeError, ValueError):
                    duration = 0
            con.execute(
                """UPDATE interactive_sessions SET session_type=?, score=?,
                   duration_seconds=?, notes=?, completed_at=? WHERE id=?""",
                (stype, s, duration, notes, now, row["id"]),
            )
            session_id = row["id"]
        else:
            stype = session_type or "Active Recall"
            cur = con.execute(
                """INSERT INTO interactive_sessions
                   (session_type, topic_id, score, duration_seconds, metrics, notes, created_at, completed_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (stype, tid, s, max(0, int(duration_seconds or 0)), "{}", notes, now, now),
            )
            session_id = cur.lastrowid
        con.commit()
    finally:
        con.close()
    mastery = update_mastery(tid, s)
    recorded: List[Dict[str, Any]] = []
    for w in weaknesses or []:
        recorded.append(record_weakness(
            tid, w.get("concept", ""), w.get("misconception", ""),
            w.get("severity", "medium"),
        ))
    return {
        "session_id": session_id, "topic_id": tid, "session_type": stype,
        "score": s, "mastery": mastery, "weaknesses": recorded,
    }


def learner_snapshot(
    limit_topics: int = 50,
    limit_sessions: int = 15,
) -> Dict[str, Any]:
    """Aggregate mastery, unresolved weaknesses, and recent sessions for the dashboard."""
    ensure_v3_tables()
    con = _connect()
    try:
        mastery = [_row_dict(r) for r in con.execute(
            "SELECT topic_id, mastery_score, confidence_score, total_attempts,"
            " successful_attempts, last_attempt_at FROM learner_mastery"
            " ORDER BY mastery_score ASC LIMIT ?", (int(limit_topics),),
        )]
        unresolved = [_row_dict(r) for r in con.execute(
            "SELECT id, topic_id, concept, misconception, error_count, severity,"
            " last_observed_at FROM learner_weaknesses WHERE is_resolved=0"
            " ORDER BY CASE severity" + "" +
            "".join(f" WHEN '{s}' THEN {i}" for s, i in _SEVERITY_RANK.items()) +
            " END DESC, error_count DESC LIMIT ?", (int(limit_topics),),
        )]
        sessions = [_row_dict(r) for r in con.execute(
            "SELECT id, session_type, topic_id, score, duration_seconds, notes,"
            " created_at, completed_at FROM interactive_sessions"
            " ORDER BY id DESC LIMIT ?", (int(limit_sessions),),
        )]
        open_count = con.execute(
            "SELECT count(*) FROM interactive_sessions WHERE completed_at IS NULL"
        ).fetchone()[0]
    finally:
        con.close()
    by_severity = {s: 0 for s in T.WEAKNESS_SEVERITY}
    for w in unresolved:
        by_severity[w["severity"]] += 1
    avg = round(sum(m["mastery_score"] for m in mastery) / len(mastery), 1) if mastery else 0.0
    con = _connect()
    try:
        cards_due = con.execute(
            "SELECT count(*) FROM spaced_repetition_queue"
            " WHERE item_type='card' AND due_date <= ?", (utcnow(),),
        ).fetchone()[0]
    finally:
        con.close()
    return {
        "summary": {
            "topics_studied": len(mastery),
            "avg_mastery": avg,
            "open_weaknesses": len(unresolved),
            "weaknesses_by_severity": by_severity,
            "sessions_total": sum(m["total_attempts"] for m in mastery),
            "open_sessions": open_count,
            "cards_due": cards_due,
        },
        "mastery": mastery,
        "weaknesses": unresolved,
        "recent_sessions": sessions,
    }


# ─── Spaced repetition (V3 spaced_repetition_queue, SM-2) ───


def card_item_id(question: str) -> str:
    """Content-addressed id for a flashcard question (stable across reruns)."""
    normalized = " ".join((question or "").strip().casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _schedule_item_id(topic_id: str, question: str) -> str:
    return f"{topic_id}:{card_item_id(question)}"


def _pack_version_key(path: Path) -> Tuple[float, str]:
    """Order pack files oldest-first (newest packs win content lookups)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, str(path))


def _newest_flashcards_csv(topic: str) -> Optional[Path]:
    """Newest flashcards.csv under PRODUCTS for a topic, or None."""
    slug = slugify(topic)
    candidates = [
        p for pattern in (f"{slug}/v*/flashcards.csv", f"{slug}-*/v*/flashcards.csv")
        for p in T.PRODUCTS.glob(pattern)
    ]
    if not candidates:
        return None

    def sort_key(p: Path) -> Tuple[int, float]:
        try:
            version = int(p.parent.name[1:])
        except ValueError:
            version = -1
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (version, mtime)

    return max(candidates, key=sort_key)


def _read_card_rows(csv_path: Path) -> List[Tuple[str, str, str]]:
    """Question/answer/sources triples from a pack flashcards.csv (header tolerated)."""
    rows: List[Tuple[str, str, str]] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.reader(f)):
            if i == 0 and row and row[0].strip().lower() == "question":
                continue
            if len(row) < 2 or not row[0].strip():
                continue
            sources = row[2].strip() if len(row) > 2 else ""
            rows.append((row[0].strip(), row[1].strip(), sources))
    return rows


def _content_index() -> Dict[str, Tuple[str, str, str]]:
    """card_item_id → (question, answer, sources) across packs (newest wins)."""
    index: Dict[str, Tuple[str, str, str]] = {}
    for csv_path in sorted(T.PRODUCTS.glob("*/v*/flashcards.csv"), key=_pack_version_key):
        try:
            for question, answer, sources in _read_card_rows(csv_path):
                index[card_item_id(question)] = (question, answer, sources)
        except OSError:
            continue
    return index


def import_flashcards(
    topic: str,
    flashcards: Optional[Path] = None,
) -> Dict[str, Any]:
    """Schedule a pack's flashcards in spaced_repetition_queue (idempotent).

    Reads Question/Answer rows from flashcards.csv — an explicit path or the
    newest pack for the topic under PRODUCTS — and inserts each card as a new
    queue entry due immediately, with a stable content-addressed item_id, so
    rerunning/rebuilding a pack never duplicates or resets existing schedules.
    """
    csv_path: Optional[Path] = Path(flashcards) if flashcards is not None else _newest_flashcards_csv(topic)
    if csv_path is None or not csv_path.is_file():
        raise FileNotFoundError(
            f"No flashcards.csv found for {topic!r}; generate a product pack first."
        )
    # Prefer the topic recorded in the pack's state.json: resumed products can
    # live in slug-hash folders, and the schedule namespace should not split.
    state_path = csv_path.parent / "state.json"
    if state_path.is_file():
        try:
            topic = json.loads(state_path.read_text(encoding="utf-8")).get("topic") or topic
        except (OSError, ValueError):
            pass
    cards = _read_card_rows(csv_path)
    if not cards:
        raise ValueError(f"No Question/Answer rows parsed from {csv_path}.")
    tid = slugify(topic)
    now = utcnow()
    ensure_v3_tables()
    con = _connect()
    try:
        imported = 0
        for question, _answer, _sources in cards:
            cur = con.execute(
                """INSERT INTO spaced_repetition_queue
                   (item_type, item_id, repetition_count, interval_days, ease_factor,
                    due_date, state, created_at, updated_at)
                   VALUES('card', ?, 0, 0.0, ?, ?, 'new', ?, ?)
                   ON CONFLICT(item_type, item_id) DO NOTHING""",
                (_schedule_item_id(tid, question), DEFAULT_EASE, now, now, now),
            )
            imported += cur.rowcount
        con.commit()
        scheduled = con.execute(
            "SELECT count(*) FROM spaced_repetition_queue"
            " WHERE item_type='card' AND item_id LIKE ?", (tid + ":%",),
        ).fetchone()[0]
    finally:
        con.close()
    return {
        "topic_id": tid, "source": str(csv_path), "cards_in_pack": len(cards),
        "imported": imported, "scheduled": scheduled,
    }


def _weak_topic_ids() -> set:
    """Topics with weak mastery or unresolved weaknesses (surface their cards first)."""
    con = _connect()
    try:
        weak = {r[0] for r in con.execute(
            "SELECT topic_id FROM learner_mastery WHERE mastery_score < ?", (PASS_SCORE,))}
        weak |= {r[0] for r in con.execute(
            "SELECT DISTINCT topic_id FROM learner_weaknesses WHERE is_resolved=0")}
    finally:
        con.close()
    return weak


def due_items(
    limit: int = 50,
    topic: Optional[str] = None,
    now: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Cards due at or before now, soonest first; weak topics surface first.

    Question/answer/source text is resolved from the saved pack flashcards.csv
    (the queue stores only scheduling state). Cards belong to topics with weak
    mastery or unresolved weaknesses sort ahead of the rest, then by due date.
    """
    limit = max(1, int(limit))
    now = now or utcnow()
    ensure_v3_tables()
    con = _connect()
    try:
        rows = [_row_dict(r) for r in con.execute(
            "SELECT id, item_id, repetition_count, interval_days, ease_factor,"
            " stability, difficulty, due_date, last_reviewed_at, last_grade, state"
            " FROM spaced_repetition_queue"
            " WHERE item_type='card' AND due_date <= ?", (now,),
        )]
    finally:
        con.close()
    weak_topics = _weak_topic_ids()
    wanted = slugify(topic) if (topic or "").strip() else None
    if wanted:
        rows = [r for r in rows if (r["item_id"] or "").split(":", 1)[0] == wanted]
    rows.sort(key=lambda r: (
        0 if (r["item_id"] or "").split(":", 1)[0] in weak_topics else 1,
        r["due_date"], r["id"],
    ))
    index = _content_index()
    out: List[Dict[str, Any]] = []
    for row in rows[:limit]:
        item_topic = (row["item_id"] or "").split(":", 1)[0]
        question, answer, sources = index.get(
            (row["item_id"] or "").split(":", 1)[-1], ("", "", ""))
        out.append({**row, "topic_id": item_topic, "question": question,
                    "answer": answer, "sources": sources,
                    "topic_weak": item_topic in weak_topics})
    return out


def _fsrs_rating(grade: int) -> int:
    """Map a 0-5 grade to an FSRS rating (1 again, 2 hard, 3 good, 4 easy)."""
    return 1 if grade <= 2 else grade - 1


def _fsrs_retrievability(elapsed_days: float, stability: float) -> float:
    return float((1 + FSRS_FACTOR * max(elapsed_days, 0.0) / stability) ** FSRS_DECAY)


def _fsrs_difficulty_after(stability: float, difficulty: float, rating: int) -> float:
    if stability <= 0:  # first review: D0(G) = w4 - (G-3)*w5
        return min(10.0, max(1.0, FSRS_WEIGHTS[4] - (rating - 3) * FSRS_WEIGHTS[5]))
    d = difficulty - FSRS_WEIGHTS[6] * (rating - 3)
    d = FSRS_WEIGHTS[7] * FSRS_WEIGHTS[4] + (1 - FSRS_WEIGHTS[7]) * d
    return min(10.0, max(1.0, d))


def _fsrs_stability_after(stability: float, difficulty: float, retrievability: float, rating: int) -> float:
    if stability <= 0:  # first review: S0(G) = w[G-1]
        return float(FSRS_WEIGHTS[rating - 1])
    if rating == 1:  # forgetting: post-lapse stability
        return max(0.01, FSRS_WEIGHTS[11] * difficulty ** (-FSRS_WEIGHTS[12])
                   * ((stability + 1) ** FSRS_WEIGHTS[13] - 1)
                   * math.exp(FSRS_WEIGHTS[14] * (1 - retrievability)))
    inc = (math.exp(FSRS_WEIGHTS[8]) * (11 - difficulty)
           * stability ** (-FSRS_WEIGHTS[9])
           * (math.exp(FSRS_WEIGHTS[10] * (1 - retrievability)) - 1))
    if rating == 2:  # hard penalty
        inc *= FSRS_WEIGHTS[15]
    elif rating == 4:  # easy bonus
        inc *= FSRS_WEIGHTS[16]
    return max(stability, stability * (inc + 1))


def _fsrs_interval(stability: float) -> float:
    """Next interval for the desired retention (0.9 means interval == stability)."""
    return max(0.01, round(stability / FSRS_FACTOR
                           * (FSRS_DESIRED_RETENTION ** (1 / FSRS_DECAY) - 1), 2))


def _schedule_sm2(row: sqlite3.Row, grade: int, elapsed_days: float) -> Dict[str, Any]:
    """Classic SM-2: interval 1 day → 6 days → previous interval × ease."""
    ease = max(SM2_MIN_EASE, float(row["ease_factor"])
               + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02)))
    if grade < SM2_PASS_GRADE:
        return {
            "repetitions": 0, "interval": 0.0, "ease": ease,
            "stability": float(row["stability"] or 0.0),
            "difficulty": float(row["difficulty"] or 0.0),
            "state": "relearning", "due_in_days": LAPSE_STEP_MINUTES / 1440.0,
        }
    repetitions = int(row["repetition_count"]) + 1
    if repetitions == 1:
        interval = 1.0
    elif repetitions == 2:
        interval = 6.0
    else:
        interval = round(max(float(row["interval_days"]), 1.0) * ease, 2)
    return {
        "repetitions": repetitions, "interval": interval, "ease": ease,
        "stability": float(row["stability"] or 0.0),
        "difficulty": float(row["difficulty"] or 0.0),
        "state": "review" if repetitions >= 2 else "learning",
        "due_in_days": interval,
    }


def _schedule_fsrs(row: sqlite3.Row, grade: int, elapsed_days: float) -> Dict[str, Any]:
    """FSRS-4.5 scheduler; fills the stability/difficulty columns.

    Next interval is the stability at the desired retention (0.9), so it matches
    the published FSRS behavior while grades still map from the local 0-5 scale.
    """
    rating = _fsrs_rating(grade)
    stability = float(row["stability"] or 0.0)
    difficulty = float(row["difficulty"] or 0.0)
    retrievability = _fsrs_retrievability(elapsed_days, stability) if stability > 0 else 0.9
    new_difficulty = _fsrs_difficulty_after(stability, difficulty, rating)
    new_stability = _fsrs_stability_after(stability, new_difficulty, retrievability, rating)
    repetitions = int(row["repetition_count"]) + 1
    ease = max(SM2_MIN_EASE, float(row["ease_factor"]))
    if rating == 1:
        return {
            "repetitions": 0, "interval": 0.0, "ease": ease,
            "stability": new_stability, "difficulty": new_difficulty,
            "state": "relearning", "due_in_days": LAPSE_STEP_MINUTES / 1440.0,
        }
    interval = _fsrs_interval(new_stability)
    return {
        "repetitions": repetitions, "interval": interval, "ease": ease,
        "stability": new_stability, "difficulty": new_difficulty,
        "state": "review" if repetitions >= 2 else "learning",
        "due_in_days": interval,
    }


SCHEDULER_FUNCS = {"sm2": _schedule_sm2, "fsrs": _schedule_fsrs}


def review_card(
    item_id: str,
    grade: int,
    item_type: str = "card",
    scheduler: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Grade a scheduled item 0-5 and reschedule it (SM-2 or FSRS-4.5).

    Grades below 3 are lapses: repetitions reset, the item enters
    `relearning`, and it returns after a short step (LAPSE_STEP_MINUTES).
    Grades 3+ advance the interval (SM-2 or FSRS), and every review is
    appended to `review_log` with the before/after state. A lapse of a
    graduated card records a learner weakness; a card graduating back to
    `review` resolves the matching weakness.
    """
    if item_type not in T.SPACED_REPETITION_ITEM_TYPES:
        raise ValueError(
            f"item_type must be one of {T.SPACED_REPETITION_ITEM_TYPES}, got {item_type!r}."
        )
    try:
        q = int(grade)
    except (TypeError, ValueError):
        raise ValueError(f"Grade must be an integer 0-5, got {grade!r}.") from None
    if not 0 <= q <= 5:
        raise ValueError(f"Grade must be within 0-5, got {grade!r}.")
    scheduler = scheduler or DEFAULT_SCHEDULER
    if scheduler not in SCHEDULERS:
        raise ValueError(f"scheduler must be one of {SCHEDULERS}, got {scheduler!r}.")
    now_dt = datetime.now(timezone.utc)
    if now:
        try:
            now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"now must be ISO-8601, got {now!r}.") from None
    now_str = now_dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    ensure_v3_tables()
    con = _connect()
    try:
        row = con.execute(
            "SELECT id, repetition_count, interval_days, ease_factor, stability,"
            " difficulty, due_date, last_reviewed_at, state FROM spaced_repetition_queue"
            " WHERE item_type=? AND item_id=?",
            (item_type, item_id),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"No scheduled {item_type} with item_id {item_id!r}; import its pack first."
            )
        elapsed_days = 0.0
        if row["last_reviewed_at"]:
            try:
                last = datetime.fromisoformat(row["last_reviewed_at"].replace("Z", "+00:00"))
                elapsed_days = max(0.0, (now_dt - last).total_seconds() / 86400.0)
            except ValueError:
                elapsed_days = 0.0
        plan = SCHEDULER_FUNCS[scheduler](row, q, elapsed_days)
        due = (now_dt + timedelta(days=plan["due_in_days"])).astimezone(timezone.utc)\
            .isoformat(timespec="seconds").replace("+00:00", "Z")
        state_before = row["state"]
        con.execute(
            """UPDATE spaced_repetition_queue SET repetition_count=?, interval_days=?,
               ease_factor=?, stability=?, difficulty=?, due_date=?, last_reviewed_at=?,
               last_grade=?, state=?, updated_at=? WHERE id=?""",
            (plan["repetitions"], plan["interval"], round(plan["ease"], 4),
             round(plan["stability"], 4), round(plan["difficulty"], 4), due, now_str,
             q, plan["state"], now_str, row["id"]),
        )
        con.execute(
            """INSERT INTO review_log
               (item_type, item_id, grade, scheduler, interval_before, interval_after,
                ease_before, ease_after, state_before, state_after, reviewed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (item_type, item_id, q, scheduler, float(row["interval_days"]), plan["interval"],
             float(row["ease_factor"]), round(plan["ease"], 4), state_before,
             plan["state"], now_str),
        )
        con.commit()
        updated = _row_dict(con.execute(
            "SELECT * FROM spaced_repetition_queue WHERE id=?", (row["id"],),
        ).fetchone()) or {}
    finally:
        con.close()
    topic_id = (item_id or "").split(":", 1)[0]
    updated["topic_id"] = topic_id
    updated["scheduler"] = scheduler
    updated["due_in_days"] = plan["due_in_days"]
    updated["weakness_recorded"] = False
    updated["weakness_resolved"] = False
    # Lapse of a graduated card = a real knowledge gap; graduating back to
    # review (two passes after the lapse) resolves it.
    lapse = q < SM2_PASS_GRADE and state_before == "review"
    graduation = (q >= SM2_PASS_GRADE and plan["state"] == "review"
                  and state_before in ("learning", "relearning"))
    if lapse or graduation:
        digest = (item_id or "").split(":", 1)[-1]
        question, _answer, _sources = _content_index().get(digest, ("", "", ""))
        concept = (question or item_id)[:200]
        if lapse:
            record_weakness(topic_id, concept, "Lapsed during flashcard review", "medium")
            updated["weakness_recorded"] = True
        else:
            updated["weakness_resolved"] = _resolve_weakness_concept(topic_id, concept) > 0
    return updated


def _resolve_weakness_concept(topic_id: str, concept: str) -> int:
    """Resolve the unresolved weakness matching topic + concept (if any)."""
    now = utcnow()
    con = _connect()
    try:
        cur = con.execute(
            "UPDATE learner_weaknesses SET is_resolved=1, resolved_at=?"
            " WHERE topic_id=? AND concept=? COLLATE NOCASE AND is_resolved=0",
            (now, topic_id, concept),
        )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def review_analytics(days: int = 30, leech_threshold: int = LEECH_THRESHOLD) -> Dict[str, Any]:
    """Retention, streak, per-day review counts, and leeches from review_log."""
    days = max(1, int(days))
    leech_threshold = max(1, int(leech_threshold))
    now_dt = datetime.now(timezone.utc)
    today = now_dt.date()
    cutoff = (now_dt - timedelta(days=days - 1)).astimezone(timezone.utc)\
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    today_start = f"{today.isoformat()}T00:00:00Z"
    ensure_v3_tables()
    con = _connect()
    try:
        total = con.execute("SELECT count(*) FROM review_log").fetchone()[0]
        reviews_today = con.execute(
            "SELECT count(*) FROM review_log WHERE reviewed_at >= ?", (today_start,),
        ).fetchone()[0]
        window = con.execute(
            "SELECT count(*) FROM review_log WHERE reviewed_at >= ?", (cutoff,),
        ).fetchone()[0]
        passed = con.execute(
            "SELECT count(*) FROM review_log WHERE reviewed_at >= ? AND grade >= ?",
            (cutoff, SM2_PASS_GRADE),
        ).fetchone()[0]
        by_day = {r[0]: r[1] for r in con.execute(
            "SELECT substr(reviewed_at, 1, 10) AS day, count(*) FROM review_log"
            " WHERE reviewed_at >= ? GROUP BY day", (cutoff,))}
        leeches = [_row_dict(r) for r in con.execute(
            """SELECT item_id,
                      sum(CASE WHEN grade < ? THEN 1 ELSE 0 END) AS lapses,
                      count(*) AS reviews, max(reviewed_at) AS last_reviewed_at
               FROM review_log GROUP BY item_id
               HAVING lapses >= ? ORDER BY lapses DESC, reviews DESC LIMIT 50""",
            (SM2_PASS_GRADE, leech_threshold))]
    finally:
        con.close()
    streak = 0
    cursor = today
    if cursor.isoformat() not in by_day:  # a streak may end yesterday
        cursor -= timedelta(days=1)
    while cursor.isoformat() in by_day:
        streak += 1
        cursor -= timedelta(days=1)
    index = _content_index()
    for row in leeches:
        digest = (row["item_id"] or "").split(":", 1)[-1]
        question, _answer, _sources = index.get(digest, ("", "", ""))
        row["topic_id"] = (row["item_id"] or "").split(":", 1)[0]
        row["question"] = question
    series = [{"date": (today - timedelta(days=d)).isoformat(),
               "reviews": by_day.get((today - timedelta(days=d)).isoformat(), 0)}
              for d in range(days - 1, -1, -1)]
    return {
        "days": days,
        "reviews_total": total,
        "reviews_today": reviews_today,
        "reviews_window": window,
        "retention": round(100.0 * passed / window, 1) if window else None,
        "streak_days": streak,
        "reviews_by_day": series,
        "leech_threshold": leech_threshold,
        "leeches": leeches,
    }


def spaced_repetition_snapshot(
    limit: int = 100,
    days: int = 30,
    leech_threshold: int = LEECH_THRESHOLD,
) -> Dict[str, Any]:
    """Due cards, queue state counts, and review analytics for the dashboard."""
    limit = max(1, int(limit))
    now = utcnow()
    ensure_v3_tables()
    con = _connect()
    try:
        counts = {r["state"]: r["n"] for r in con.execute(
            "SELECT state, count(*) AS n FROM spaced_repetition_queue"
            " WHERE item_type='card' GROUP BY state")}
        due_count = con.execute(
            "SELECT count(*) FROM spaced_repetition_queue"
            " WHERE item_type='card' AND due_date <= ?", (now,),
        ).fetchone()[0]
        next_due = con.execute(
            "SELECT min(due_date) FROM spaced_repetition_queue"
            " WHERE item_type='card' AND due_date > ?", (now,),
        ).fetchone()[0]
        item_ids = [r[0] for r in con.execute(
            "SELECT item_id FROM spaced_repetition_queue WHERE item_type='card'")]
    finally:
        con.close()
    analytics = review_analytics(days, leech_threshold)
    return {
        "summary": {
            "scheduled_cards": sum(counts.values()),
            "due_cards": due_count,
            "new_cards": counts.get("new", 0),
            "learning_cards": counts.get("learning", 0),
            "review_cards": counts.get("review", 0),
            "relearning_cards": counts.get("relearning", 0),
            "topics": len({i.split(":", 1)[0] for i in item_ids}),
            "next_due_at": next_due,
            "reviews_today": analytics["reviews_today"],
            "retention": analytics["retention"],
            "streak_days": analytics["streak_days"],
            "leeches": len(analytics["leeches"]),
            "scheduler": DEFAULT_SCHEDULER,
        },
        "due": due_items(limit, now=now),
        "analytics": analytics,
    }
