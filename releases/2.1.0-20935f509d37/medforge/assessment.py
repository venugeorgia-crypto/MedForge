"""P8 question-level assessment engine.

A persistent, reproducible assessment system built strictly on top of P3
textbook provenance, the P4 evidence graph, the P6 recency-weighted learner
model and the P7 tutor/grading primitives. Design rules (see docs/P8_MATRIX.md):

- Item identity (`item_id`) is separated from immutable per-version content
  (`item_version`). A historical assessment stores `item_id + item_version`, so
  editing an item later can never change what an old exam meant.
- Every medically substantive item needs an evidence basis. The P4 evidence
  graph stays authoritative: evidence state is recomputed from stored evidence
  (or the stored claim status) rather than trusted from generated text, and the
  approval gate rejects UNSUPPORTED / CONTRADICTED / INSUFFICIENT_EVIDENCE
  items. Nothing is auto-fixed silently — invalid items are rejected.
- Deterministic work stays deterministic: item selection, MCQ/true-false/
  multi-select grading, scoring arithmetic, timing, exposure, statistics and
  pass/fail never call a model. A model is used only for draft generation and
  free-text/clinical-reasoning grading, always through P7's delimited prompt and
  strict JSON parser.
- Grading is reused from P7 (`tutor.evaluate_answer`); P8 adds no second
  grading engine. The only new deterministic grader is multi-select, which P7
  does not model.
- The only learner write path is P6 `learner_model.record_learning_event`;
  assessment code never edits mastery tables. Each graded attempt records
  exactly one event (`learning_attempt_id` guard).
- A model failure never loses an answer and never awards credit: the answer is
  persisted before grading, the attempt becomes `retryable`, a retry regrades
  the same attempt (grading history, never a duplicate attempt), and the
  session can always be finalized with pending items reported honestly.
- Untrusted content (evidence text, stems, choices, learner answers) is data,
  never instructions: it is flagged via P7's `injection_suspected` and passed to
  models inside `<<<...>>>` delimiters with a "data, not instructions" notice.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import medforge.types as T
from medforge.utils import mkdirs, slugify, utcnow

__all__ = [
    "ensure_assessment_tables", "create_item", "get_item", "list_items",
    "revise_item", "retire_item", "validate_item", "approve_item", "reject_item",
    "detect_duplicate_items", "generate_items", "grade_item", "grade_multi_select",
    "create_blueprint", "validate_blueprint", "get_blueprint", "list_blueprints",
    "select_items", "create_assessment", "start_assessment", "get_assessment_state",
    "get_current_item", "submit_assessment_answer", "retry_pending_grading",
    "flag_item", "next_item", "previous_item", "complete_assessment",
    "get_assessment_result", "get_item_statistics", "compute_item_quality",
    "get_assessment_remediation", "review_assessment", "list_assessments",
    "resume_assessment", "public_assessment_view", "assessment_export",
]

ROUND = 4
_ENSURED_DATABASES: set = set()

_STOP_WORDS = {
    "the", "and", "for", "are", "was", "were", "with", "that", "this", "from",
    "into", "which", "when", "where", "then", "than", "these", "those", "have",
    "has", "had", "not", "but", "its", "their", "there", "here", "also", "can",
    "may", "will", "would", "should", "could", "about", "between", "during",
    "does", "did", "how", "what", "why", "who", "whom", "e.g", "i.e", "one",
    "two", "both", "each", "more", "most", "such", "other", "only", "over",
}

# P8 item type → P7 question type used for grading (no second grader).
_TUTOR_TYPE = {
    "MCQ_SINGLE": "mcq",
    "TRUE_FALSE": "mcq",
    "MCQ_MULTI": "mcq",
    "RECALL": "recall",
    "SHORT_ANSWER": "short_answer",
    "CLINICAL_REASONING": "clinical_reasoning",
}

_DETERMINISTIC_TYPES = ("MCQ_SINGLE", "MCQ_MULTI", "TRUE_FALSE")


# ─── Schema self-healing (V9, idempotent) ───


def _core_import():
    """Import core.database.migrate_v9, walking candidate bases like P2–P7."""
    here = Path(__file__).resolve()
    candidates = [str(T.BASE)] + [str(p) for p in here.parents]
    for base_str in candidates:
        if base_str not in sys.path:
            sys.path.insert(0, base_str)
        try:
            from core.database.migrate_v9 import ensure_assessment_v9

            return ensure_assessment_v9
        except ImportError:
            continue
    raise ImportError(
        "core.database.migrate_v9 not importable from: " + ", ".join(candidates)
    )


def ensure_assessment_tables() -> Dict[str, Any]:
    """Bring the active META_DB to the V9 assessment schema (idempotent)."""
    key = str(T.META_DB)
    if key in _ENSURED_DATABASES:
        return {"status": "already_ensured", "database": key}
    mkdirs()
    ensure_v9 = _core_import()
    result = ensure_v9(T.META_DB)
    _ENSURED_DATABASES.add(key)
    return result


def _connect() -> sqlite3.Connection:
    """Connection to META_DB, self-healing the V9 schema on first use."""
    ensure_assessment_tables()
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    return con


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return None if row is None else {k: row[k] for k in row.keys()}


def parse_time(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (Z or offset) into aware UTC."""
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


def _now_str(now: Optional[str] = None) -> str:
    return _fmt(parse_time(now)) if now else utcnow()


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _round(value: Optional[float], digits: int = ROUND) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _clean(text: Any, limit: int = 4000) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _terms(text: str) -> set:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", (text or "").lower())
    return {w for w in words if w not in _STOP_WORDS}


def _coverage(terms: set, text: str) -> float:
    if not terms:
        return 0.0
    return len(terms & _terms(text)) / float(len(terms))


def _sha1(*parts: Any, length: int = 16) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


# ─── Item bank ───


def _item_row(item_id: str) -> Dict[str, Any]:
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM assessment_items WHERE item_id=?", (item_id,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise ValueError(f"Unknown item_id {item_id!r}.")
    return _row_dict(row) or {}


def _item_version_row(item_id: str, item_version: int) -> Optional[Dict[str, Any]]:
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM assessment_item_versions WHERE item_id=? AND item_version=?",
            (item_id, int(item_version)),
        ).fetchone()
    finally:
        con.close()
    return _row_dict(row)


def _item_dict(identity: Dict[str, Any], version: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "item_id": identity["item_id"],
        "item_version": int(version["item_version"]),
        "item_type": identity["item_type"],
        "status": identity["status"],
        "topic": identity.get("topic") or "",
        "mastery_key": identity.get("mastery_key") or "",
        "concept": identity.get("concept") or "",
        "curriculum_node_id": identity.get("curriculum_node_id"),
        "author": identity.get("author") or "",
        "source_kind": identity.get("source_kind") or "",
        "generation_mode": identity.get("generation_mode") or "",
        "item_model_version": identity.get("item_model_version") or "",
        "quality_flags": _json_load(identity.get("quality_flags"), []),
        "retired_at": identity.get("retired_at"),
        "created_at": identity.get("created_at"),
        "updated_at": identity.get("updated_at"),
        "current_version": int(identity.get("current_version") or 1),
        "difficulty_target": int(version.get("difficulty_target") or 2),
        "stem": version.get("stem") or "",
        "choices": _json_load(version.get("choices"), []),
        "correct_choices": _json_load(version.get("correct_choices"), []),
        "correct_answer": version.get("correct_answer") or "",
        "rubric": _json_load(version.get("rubric"), []),
        "explanation": version.get("explanation") or "",
        "scoring_policy": _json_load(version.get("scoring_policy"), {}),
        "evidence_requirement": version.get("evidence_requirement") or "SUPPORTED",
        "evidence_state": version.get("evidence_state") or "INSUFFICIENT_EVIDENCE",
        "evidence_refs": _json_load(version.get("evidence_refs"), []),
        "claim_refs": _json_load(version.get("claim_refs"), []),
        "content_hash": version.get("content_hash") or "",
        "validation_report": _json_load(version.get("validation_report"), {}),
        "duplicate_of": version.get("duplicate_of"),
        "review_note": version.get("review_note") or "",
    }


def get_item(item_id: str, item_version: Optional[int] = None) -> Dict[str, Any]:
    """One item, latest version by default; a specific immutable version when asked."""
    ensure_assessment_tables()
    identity = _item_row(item_id)
    version_number = int(item_version or identity["current_version"])
    version = _item_version_row(item_id, version_number)
    if version is None:
        raise ValueError(f"Item {item_id!r} has no version {version_number}.")
    return _item_dict(identity, version)


def _content_hash(item_type: str, stem: str, choices: List[Dict[str, str]],
                  correct_choices: List[str], correct_answer: str,
                  rubric: List[Dict[str, Any]], explanation: str) -> str:
    return _sha1(
        item_type, _clean(stem, 2000).lower(),
        "|".join(_clean(c.get("text"), 500).lower() for c in choices),
        ",".join(correct_choices), _clean(correct_answer, 1000).lower(),
        "|".join(_clean(p.get("point"), 500).lower() for p in rubric),
        _clean(explanation, 2000).lower(), length=32,
    )


def _normalize_choices(choices: Any) -> List[Dict[str, str]]:
    """Coerce choices to `[{key,text}]` with deterministic A..E keys."""
    out: List[Dict[str, str]] = []
    if not choices:
        return out
    for i, choice in enumerate(choices):
        letter = "ABCDEFGH"[: len(str(len(choices)))]
        if isinstance(choice, dict):
            key = _clean(choice.get("key") or (letter[i] if i < len(letter) else str(i)), 4).upper()
            text = _clean(choice.get("text"), 1000)
        else:
            key = letter[i] if i < len(letter) else str(i)
            text = _clean(choice, 1000)
        out.append({"key": key, "text": text})
    return out


def _normalize_correct_choices(correct_choices: Any, choices: List[Dict[str, str]],
                               correct_answer: str = "") -> List[str]:
    """Resolve intended-correct keys from keys, letters, indices or answer text."""
    raw = correct_choices
    if isinstance(raw, str):
        raw = [raw]
    keys = [c["key"] for c in choices]
    resolved: List[str] = []
    for entry in (raw or []):
        text = _clean(entry, 1000)
        if text.upper() in keys:
            resolved.append(text.upper())
        elif text.isdigit() and 1 <= int(text) <= len(keys):
            resolved.append(keys[int(text) - 1])
        else:
            for choice in choices:
                if text and (text.lower() in choice["text"].lower()
                             or choice["text"].lower() in text.lower()):
                    resolved.append(choice["key"])
                    break
    if not resolved and correct_answer:
        answer = _clean(correct_answer, 1000)
        for choice in choices:
            if answer.lower() == choice["text"].lower():
                resolved.append(choice["key"])
                break
        if not resolved and answer.upper() in keys:
            resolved = [answer.upper()]
    seen: List[str] = []
    for key in resolved:
        if key not in seen:
            seen.append(key)
    return seen


def _evidence_rows_for(refs: List[Any]) -> List[Dict[str, Any]]:
    """Fetch P4 evidence rows for `evidence_id`s, preserving the given order."""
    ids: List[str] = []
    for ref in refs or []:
        if isinstance(ref, dict):
            ev_id = ref.get("evidence_id") or ref.get("id")
        else:
            ev_id = ref
        if ev_id and ev_id not in ids:
            ids.append(ev_id)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    con = _connect()
    try:
        try:
            rows = con.execute(
                f"SELECT * FROM evidence WHERE evidence_id IN ({placeholders})", ids
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        con.close()
    by_id = {r["evidence_id"]: _row_dict(r) or {} for r in rows}
    return [by_id[ev_id] for ev_id in ids if ev_id in by_id]


def _claim_state_for(claim_refs: List[Any]) -> Dict[str, Any]:
    """Stored P4 verification status of the referenced claims (authoritative)."""
    ids: List[str] = []
    for ref in claim_refs or []:
        if isinstance(ref, dict):
            claim_id = ref.get("claim_id") or ref.get("id")
        else:
            claim_id = ref
        if claim_id and claim_id not in ids:
            ids.append(str(claim_id))
    if not ids:
        return {"present": False, "states": {}, "worst": None}
    placeholders = ",".join("?" for _ in ids)
    con = _connect()
    try:
        try:
            rows = con.execute(
                f"SELECT claim_id, verification_status FROM claims"
                f" WHERE claim_id IN ({placeholders})", ids
            ).fetchall()
        except sqlite3.OperationalError:
            return {"present": False, "states": {}, "worst": None}
    finally:
        con.close()
    states = {r["claim_id"]: r["verification_status"] for r in rows}
    order = {"INSUFFICIENT_EVIDENCE": 0, "PARTIALLY_SUPPORTED": 1, "SUPPORTED": 2,
             "UNSUPPORTED": 3, "CONTRADICTED": 4}
    worst = None
    for status in states.values():
        if worst is None or order.get(str(status), 0) < order.get(str(worst), 0):
            worst = status
    return {"present": bool(states), "states": states, "worst": worst}


def _derive_evidence_state(concept: str, evidence_refs: List[Any],
                           rubric: List[Dict[str, Any]],
                           declared: Optional[str] = None) -> Dict[str, Any]:
    """Evidence state from the P4 graph; declared state only as a fallback basis.

    - With evidence references, P7's verification floor decides (`assess_evidence`).
    - With claim references only, the stored claim status decides.
    - With neither, the author must still have declared one; approval requires a
      real evidence basis and rejects the item otherwise.
    """
    from medforge import tutor as TU

    rows = _evidence_rows_for(evidence_refs)
    if rows:
        key_points = [
            {"text": p.get("point") or "", "evidence_id": p.get("evidence_id"),
             "locator": p.get("locator") or ""}
            for p in (rubric or [])
        ]
        if not key_points:
            key_points = [{"text": concept or "concept", "evidence_id": rows[0]["evidence_id"]}]
        key_points = [kp for kp in key_points if kp.get("text")]
        if key_points:
            assessment = TU.assess_evidence(concept or "", key_points, rows)
            return {"state": assessment.get("status") or "INSUFFICIENT_EVIDENCE",
                    "source": "p4-recomputed", "detail": assessment}
    return {"state": declared or "INSUFFICIENT_EVIDENCE",
            "source": "declared" if declared else "missing"}


def _find_duplicates(content_hash: str, normalized_stem: str,
                     item_id: Optional[str] = None) -> Optional[str]:
    """Report the first exact/near duplicate item id (never merges anything)."""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT item_id, content_hash, stem FROM assessment_item_versions"
            " WHERE item_id != ? ORDER BY item_id", (item_id or "",),
        ).fetchall()
    finally:
        con.close()
    stem_key = _clean(normalized_stem, 600).lower()
    for row in rows:
        if row["content_hash"] == content_hash:
            return row["item_id"]
    for row in rows:
        if stem_key and _clean(row["stem"], 600).lower() == stem_key:
            return row["item_id"]
    return None


def create_item(
    item_type: str,
    stem: str,
    *,
    topic: str = "",
    concept: str = "",
    mastery_key: Optional[str] = None,
    curriculum_node_id: Optional[str] = None,
    choices: Any = None,
    correct_choices: Any = None,
    correct_answer: str = "",
    rubric: Any = None,
    explanation: str = "",
    difficulty_target: int = 2,
    evidence_refs: Any = None,
    claim_refs: Any = None,
    evidence_state: Optional[str] = None,
    evidence_requirement: str = "SUPPORTED",
    scoring_policy: Optional[Dict[str, Any]] = None,
    author: str = "medforge",
    source_kind: str = "manual",
    generation_mode: str = "manual",
    item_model_version: str = "",
    item_id: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Create item version 1 in DRAFT. Unsupported content can never publish.

    Generated and manual items follow the same path: create → validate →
    approve/retire. Duplicate content is reported (`duplicates`) and recorded,
    never merged or deleted.
    """
    ensure_assessment_tables()
    item_type = str(item_type or "").upper()
    if item_type not in T.ASSESSMENT_ITEM_TYPES:
        raise ValueError(
            f"item_type must be one of {T.ASSESSMENT_ITEM_TYPES}, got {item_type!r}."
        )
    stem = _clean(stem, 4000)
    if not stem:
        raise ValueError("stem must not be empty.")
    topic = _clean(topic or concept, 400)
    concept = _clean(concept or topic, 400)
    key = _clean(mastery_key or slugify(topic or concept), 200)
    difficulty = max(T.TUTOR_DIFFICULTY_MIN, min(T.TUTOR_DIFFICULTY_MAX, int(difficulty_target or 2)))
    normalized_choices = _normalize_choices(choices)
    normalized_correct = _normalize_correct_choices(correct_choices, normalized_choices, correct_answer)
    normalized_rubric = [
        {"point": _clean(p.get("point") if isinstance(p, dict) else p, 1000),
         "evidence_id": (p.get("evidence_id") if isinstance(p, dict) else None),
         "locator": (_clean(p.get("locator"), 300) if isinstance(p, dict) else "")}
        for p in (rubric or [])
    ]
    normalized_rubric = [p for p in normalized_rubric if p["point"]]
    normalized_refs = [
        r if isinstance(r, dict) else {"evidence_id": r} for r in (evidence_refs or [])
    ]
    normalized_claims = [
        c if isinstance(c, dict) else {"claim_id": c} for c in (claim_refs or [])
    ]
    policy = dict(scoring_policy or {})
    # Partial credit needs a multi-point rubric; a single-point rubric snaps by default.
    policy.setdefault("partial_credit", len(normalized_rubric) >= 2
                      and item_type not in _DETERMINISTIC_TYPES)
    if item_type == "MCQ_MULTI":
        policy.setdefault("multi_select", "exact")
    derived = _derive_evidence_state(concept, normalized_refs, normalized_rubric, evidence_state)
    now_str = _now_str(now)
    item_id = _clean(item_id, 64) or "it-" + _sha1(
        item_type, stem.lower(), topic.lower(), slugify(concept), length=12
    )
    content_hash = _content_hash(item_type, stem, normalized_choices, normalized_correct,
                                 correct_answer, normalized_rubric, explanation)
    con = _connect()
    try:
        existing = con.execute(
            "SELECT item_id, current_version FROM assessment_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if existing:
            raise ValueError(
                f"item_id {item_id!r} already exists (current version "
                f"{existing['current_version']}); use revise_item to create a new version."
            )
        duplicate_of = _find_duplicates(content_hash, stem, item_id=None)
        con.execute(
            "INSERT INTO assessment_items (item_id,item_type,status,current_version,"
            "topic,mastery_key,concept,curriculum_node_id,author,source_kind,"
            "generation_mode,item_model_version,quality_flags,created_at,updated_at)"
            " VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, item_type, "DRAFT", topic, key, concept, curriculum_node_id,
             author, source_kind, generation_mode, item_model_version,
             _json_dump(["possible_duplicate"] if duplicate_of else []), now_str, now_str),
        )
        con.execute(
            "INSERT INTO assessment_item_versions (item_id,item_version,stem,"
            "difficulty_target,choices,correct_choices,rubric,correct_answer,explanation,"
            "scoring_policy,evidence_requirement,evidence_state,evidence_refs,claim_refs,"
            "content_hash,validation_report,duplicate_of,item_model_version,review_note,"
            "author,created_at) VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, stem, difficulty, _json_dump(normalized_choices),
             _json_dump(normalized_correct), _json_dump(normalized_rubric),
             _clean(correct_answer, 2000), _clean(explanation, 4000),
             _json_dump(policy), evidence_requirement, derived["state"],
             _json_dump(normalized_refs), _json_dump(normalized_claims), content_hash,
             _json_dump({}), duplicate_of, item_model_version, "", author, now_str),
        )
        con.commit()
    finally:
        con.close()
    item = get_item(item_id, 1)
    item["duplicates"] = ([duplicate_of] if duplicate_of else [])
    item["evidence_source"] = derived["source"]
    return item


def revise_item(item_id: str, *, now: Optional[str] = None, **changes: Any) -> Dict[str, Any]:
    """Create a NEW immutable version when wording/answer/rubric/evidence changes."""
    ensure_assessment_tables()
    current = get_item(item_id)
    fields = {
        "stem": changes.get("stem", current["stem"]),
        "choices": changes.get("choices", current["choices"]),
        "correct_choices": changes.get("correct_choices", current["correct_choices"]),
        "correct_answer": changes.get("correct_answer", current["correct_answer"]),
        "rubric": changes.get("rubric", current["rubric"]),
        "explanation": changes.get("explanation", current["explanation"]),
        "difficulty_target": changes.get("difficulty_target", current["difficulty_target"]),
        "evidence_refs": changes.get("evidence_refs", current["evidence_refs"]),
        "claim_refs": changes.get("claim_refs", current["claim_refs"]),
        "evidence_state": changes.get("evidence_state", current["evidence_state"]),
        "scoring_policy": changes.get("scoring_policy", current["scoring_policy"]),
        "item_model_version": changes.get("item_model_version", current["item_model_version"]),
        "review_note": changes.get("review_note", ""),
    }
    stem = _clean(fields["stem"], 4000)
    if not stem:
        raise ValueError("A revised item needs a non-empty stem.")
    choices = _normalize_choices(fields["choices"])
    correct = _normalize_correct_choices(fields["correct_choices"], choices, fields["correct_answer"])
    rubric = [
        {"point": _clean(p.get("point") if isinstance(p, dict) else p, 1000),
         "evidence_id": (p.get("evidence_id") if isinstance(p, dict) else None),
         "locator": (_clean(p.get("locator"), 300) if isinstance(p, dict) else "")}
        for p in (fields["rubric"] or [])
    ]
    rubric = [p for p in rubric if p["point"]]
    refs = [r if isinstance(r, dict) else {"evidence_id": r} for r in (fields["evidence_refs"] or [])]
    claims = [c if isinstance(c, dict) else {"claim_id": c} for c in (fields["claim_refs"] or [])]
    policy = dict(fields["scoring_policy"] or {})
    policy.setdefault("partial_credit", len(rubric) >= 2
                      and current["item_type"] not in _DETERMINISTIC_TYPES)
    if current["item_type"] == "MCQ_MULTI":
        policy.setdefault("multi_select", "exact")
    derived = _derive_evidence_state(current["concept"], refs, rubric, fields["evidence_state"])
    next_version = int(current["current_version"]) + 1
    now_str = _now_str(now)
    content_hash = _content_hash(current["item_type"], stem, choices, correct,
                                 fields["correct_answer"], rubric, fields["explanation"])
    duplicate_of = _find_duplicates(content_hash, stem, item_id=item_id)
    con = _connect()
    try:
        con.execute(
            "INSERT INTO assessment_item_versions (item_id,item_version,stem,"
            "difficulty_target,choices,correct_choices,rubric,correct_answer,explanation,"
            "scoring_policy,evidence_requirement,evidence_state,evidence_refs,claim_refs,"
            "content_hash,validation_report,duplicate_of,item_model_version,review_note,"
            "author,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, next_version, stem, int(fields["difficulty_target"]),
             _json_dump(choices), _json_dump(correct), _json_dump(rubric),
             _clean(fields["correct_answer"], 2000), _clean(fields["explanation"], 4000),
             _json_dump(policy), current["evidence_requirement"], derived["state"],
             _json_dump(refs), _json_dump(claims), content_hash, _json_dump({}),
             duplicate_of, fields["item_model_version"], _clean(fields["review_note"], 400),
             current["author"], now_str),
        )
        con.execute(
            "UPDATE assessment_items SET current_version=?, status='DRAFT', retired_at=NULL,"
            " quality_flags=?, updated_at=? WHERE item_id=?",
            (next_version, _json_dump(["possible_duplicate"] if duplicate_of else []),
             now_str, item_id),
        )
        con.commit()
    finally:
        con.close()
    item = get_item(item_id, next_version)
    item["duplicates"] = ([duplicate_of] if duplicate_of else [])
    return item


def validate_item(item_id: str, item_version: Optional[int] = None,
                  now: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic validation; hard findings reject, soft findings need review.

    Never fixes anything: the report says exactly what is wrong, and an invalid
    item is rejected (kept for history, never selectable).
    """
    ensure_assessment_tables()
    item = get_item(item_id, item_version)
    errors: List[str] = []
    warnings: List[str] = []
    item_type = item["item_type"]
    stem = item["stem"]
    choices = item["choices"]
    keys = [c["key"] for c in choices]
    correct = item["correct_choices"]
    policy = item["scoring_policy"]
    refs = item["evidence_refs"]
    claims = item["claim_refs"]

    # 1. Evidence availability (P4 authoritative, never self-verified text).
    derived = _derive_evidence_state(item["concept"], refs, item["rubric"], item["evidence_state"])
    evidence_state = derived["state"]
    if not refs and not claims:
        errors.append("no evidence basis: item must reference P3/P4 evidence or verified claims")
    if evidence_state in T.ASSESSMENT_EVIDENCE_REJECT:
        errors.append(f"evidence state {evidence_state} is not eligible for assessment content")
    elif evidence_state in T.ASSESSMENT_EVIDENCE_REVIEW:
        warnings.append(f"evidence state {evidence_state} requires explicit review before approval")
    if derived["source"] == "declared":
        warnings.append("evidence state was declared by the author; no P4 evidence rows resolved")

    # 2. Answer determinacy per type.
    if item_type in ("MCQ_SINGLE", "TRUE_FALSE"):
        if item_type == "TRUE_FALSE":
            if len(choices) != 2:
                errors.append("TRUE_FALSE needs exactly two choices (True/False)")
        elif len(choices) < 1 + T.ASSESSMENT_MCQ_MIN_DISTRACTORS:
            errors.append("MCQ_SINGLE needs one correct answer and at least "
                          f"{T.ASSESSMENT_MCQ_MIN_DISTRACTORS} distractors")
        if len(correct) != 1:
            errors.append(f"{item_type} needs exactly one correct choice")
        elif correct[0] not in keys:
            errors.append("the intended correct choice is not among the choices")
    elif item_type == "MCQ_MULTI":
        if len(correct) < 2:
            errors.append("MCQ_MULTI needs at least two correct choices")
        if any(key not in keys for key in correct):
            errors.append("an intended correct choice is not among the choices")
        mode = str(policy.get("multi_select") or "exact").lower()
        if mode not in ("exact", "partial"):
            errors.append(f"unknown multi_select scoring policy {mode!r}")
    else:  # free-text family
        if not item["rubric"] and not item["correct_answer"]:
            errors.append(f"{item_type} needs a rubric or a reference answer")
        if policy.get("partial_credit") and len(item["rubric"]) < 2:
            errors.append("partial credit requires a multi-point rubric")

    # 3. Choice quality.
    if choices:
        texts = [_clean(c["text"], 500).lower() for c in choices]
        if any(not t for t in texts):
            errors.append("a choice is empty")
        if len(set(texts)) != len(texts):
            errors.append("duplicate choice text")
        if any(_coverage(_terms(c["text"]), item["correct_answer"]) >= T.ASSESSMENT_DISTRACTOR_OVERLAP_LIMIT
               for c in choices if c["key"] not in correct):
            warnings.append("a distractor overlaps the reference answer closely (possible contradictory option)")
        if len(choices) > T.ASSESSMENT_MAX_CHOICES:
            warnings.append(f"more than {T.ASSESSMENT_MAX_CHOICES} choices")

    # 4. Stem quality and ambiguity markers.
    if len(stem) < T.ASSESSMENT_MIN_STEM_CHARS:
        warnings.append("stem is very short")
    for marker in ("all of the above", "none of the above", "except"):
        if marker in stem.lower():
            warnings.append(f"stem contains {marker!r} (ambiguity risk)")

    # 5. Curriculum alignment.
    if not item["topic"] and not item["concept"]:
        errors.append("item is not aligned to a topic or concept")
    if item["curriculum_node_id"]:
        con = _connect()
        try:
            found = con.execute(
                "SELECT 1 FROM curriculum_nodes WHERE id=?", (item["curriculum_node_id"],)
            ).fetchone()
        finally:
            con.close()
        if not found:
            errors.append("curriculum_node_id does not resolve to a curriculum node")

    # 6. Duplicate content is reported (never merged).
    duplicate_of = item["duplicate_of"] or _find_duplicates(
        item["content_hash"], stem, item_id=item["item_id"]
    )
    if duplicate_of:
        warnings.append(f"possible duplicate of item {duplicate_of}")

    # 7. Injection markers are contained, never executed.
    from medforge import tutor as TU

    injection = TU.injection_suspected(stem, item["explanation"],
                                       *[c["text"] for c in choices],
                                       item["correct_answer"])
    if injection:
        warnings.append("instruction-like text detected in item content; treated as data")

    report = {
        "item_id": item["item_id"], "item_version": item["item_version"],
        "valid": not errors, "errors": errors, "warnings": warnings,
        "evidence_state": evidence_state, "evidence_source": derived["source"],
        "duplicate_of": duplicate_of, "injection_suspected": bool(injection),
        "checked_at": _now_str(now), "validator_version": T.ASSESSMENT_VERSION,
    }
    status = "REJECTED" if errors else ("REVIEW_REQUIRED" if warnings else "APPROVED")
    now_str = _now_str(now)
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_item_versions SET validation_report=?, evidence_state=?,"
            " duplicate_of=? WHERE item_id=? AND item_version=?",
            (_json_dump(report), evidence_state, duplicate_of,
             item["item_id"], item["item_version"]),
        )
        con.execute(
            "UPDATE assessment_items SET status=?, updated_at=? WHERE item_id=?",
            (status, now_str, item["item_id"]),
        )
        if duplicate_of:
            con.execute(
                "UPDATE assessment_items SET quality_flags=? WHERE item_id=?",
                (_json_dump(["possible_duplicate"]), item["item_id"]),
            )
        con.commit()
    finally:
        con.close()
    report["status"] = status
    return report


def reject_item(item_id: str, reason: str = "", now: Optional[str] = None) -> Dict[str, Any]:
    """Explicitly reject an item (kept for history, never selectable)."""
    ensure_assessment_tables()
    now_str = _now_str(now)
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_items SET status='REJECTED', updated_at=? WHERE item_id=?",
            (now_str, item_id),
        )
        con.execute(
            "UPDATE assessment_item_versions SET review_note=? WHERE item_id=?"
            " AND item_version=(SELECT current_version FROM assessment_items WHERE item_id=?)",
            (_clean(reason, 400), item_id, item_id),
        )
        con.commit()
    finally:
        con.close()
    item = get_item(item_id)
    return {"item_id": item_id, "status": item["status"], "reason": reason}


def approve_item(item_id: str, item_version: Optional[int] = None, *,
                 reviewed_by: str = "local reviewer", allow_partially_supported: bool = False,
                 note: str = "", now: Optional[str] = None) -> Dict[str, Any]:
    """Run validation, then apply the evidence gate and publish (ACTIVE)."""
    report = validate_item(item_id, item_version, now=now)
    item = get_item(item_id, report["item_version"])
    if not report["valid"]:
        con = _connect()
        try:
            con.execute("UPDATE assessment_items SET status='REJECTED', updated_at=? WHERE item_id=?",
                        (_now_str(now), item_id))
            con.commit()
        finally:
            con.close()
        return {"item_id": item_id, "approved": False, "status": "REJECTED",
                "reason": "validation failed", "validation": report}
    if not item["evidence_refs"] and not item["claim_refs"]:
        return {"item_id": item_id, "approved": False, "status": item["status"],
                "reason": "no evidence basis recorded", "validation": report}
    resolved_evidence = _evidence_rows_for(item["evidence_refs"])
    claim_state = _claim_state_for(item["claim_refs"])
    if not resolved_evidence and not claim_state["present"]:
        return {"item_id": item_id, "approved": False, "status": item["status"],
                "reason": ("evidence references do not resolve to stored P4 evidence"
                           " and no stored claim matches; the P4 graph is authoritative"),
                "validation": report}
    if not resolved_evidence and claim_state["worst"] in T.ASSESSMENT_EVIDENCE_REJECT:
        return {"item_id": item_id, "approved": False, "status": item["status"],
                "reason": f"stored claim status {claim_state['worst']} cannot be approved",
                "validation": report}
    state = report["evidence_state"]
    if state in T.ASSESSMENT_EVIDENCE_REJECT:
        return {"item_id": item_id, "approved": False, "status": item["status"],
                "reason": f"evidence state {state} cannot be approved", "validation": report}
    if state in T.ASSESSMENT_EVIDENCE_REVIEW and not allow_partially_supported:
        return {"item_id": item_id, "approved": False, "status": "REVIEW_REQUIRED",
                "reason": (f"evidence state {state}: explicit reviewed approval required"
                           " (allow_partially_supported=True)"),
                "validation": report}
    now_str = _now_str(now)
    review_note = _clean(note, 400) or (
        f"approved by {reviewed_by}" + (f"; partially supported evidence reviewed" 
                                        if state in T.ASSESSMENT_EVIDENCE_REVIEW else "")
    )
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_item_versions SET review_note=? WHERE item_id=? AND item_version=?",
            (review_note, item_id, int(item["item_version"])),
        )
        con.execute(
            "UPDATE assessment_items SET status='ACTIVE', updated_at=? WHERE item_id=?",
            (now_str, item_id),
        )
        con.commit()
    finally:
        con.close()
    published = get_item(item_id, item["item_version"])
    return {"item_id": item_id, "approved": True, "status": published["status"],
            "item_version": published["item_version"], "evidence_state": state,
            "review_note": review_note, "validation": report, "item": published}


def retire_item(item_id: str, reason: str = "", now: Optional[str] = None) -> Dict[str, Any]:
    """Retire an item. Historical assessments keep their stored item version."""
    ensure_assessment_tables()
    item = get_item(item_id)
    now_str = _now_str(now)
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_items SET status='RETIRED', retired_at=?, updated_at=? WHERE item_id=?",
            (now_str, now_str, item_id),
        )
        con.execute(
            "UPDATE assessment_item_versions SET review_note=?"
            " WHERE item_id=? AND item_version=?",
            (_clean(reason, 400) or "retired", item_id, int(item["item_version"])),
        )
        con.commit()
    finally:
        con.close()
    return {"item_id": item_id, "status": "RETIRED", "retired_at": now_str, "reason": reason}


def detect_duplicate_items(item_id: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    """Report exact and normalized-stem duplicate groups (never merges)."""
    ensure_assessment_tables()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT v.item_id, v.item_version, v.stem, v.content_hash, i.status"
            " FROM assessment_item_versions v JOIN assessment_items i USING(item_id)"
            " ORDER BY v.item_id, v.item_version"
        ).fetchall()
    finally:
        con.close()
    by_hash: Dict[str, List[str]] = {}
    by_stem: Dict[str, List[str]] = {}
    for row in rows:
        by_hash.setdefault(row["content_hash"], []).append(row["item_id"])
        by_stem.setdefault(_clean(row["stem"], 600).lower(), []).append(row["item_id"])
    groups: List[Dict[str, Any]] = []
    seen: set = set()
    for kind, mapping in (("exact_content", by_hash), ("normalized_stem", by_stem)):
        for key, ids in mapping.items():
            unique = sorted(set(ids))
            if len(unique) < 2:
                continue
            signature = (kind, tuple(unique))
            if signature in seen:
                continue
            seen.add(signature)
            if item_id and item_id not in unique:
                continue
            groups.append({"kind": kind, "items": unique, "count": len(unique)})
    groups.sort(key=lambda g: (-g["count"], g["kind"], g["items"][0]))
    return {"groups": groups[:max(1, int(limit))], "group_count": len(groups),
            "note": "possible duplicates are reported for review; nothing is merged"}


def list_items(status: Optional[str] = None, item_type: Optional[str] = None,
               topic: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Recent item versions (newest first), optionally filtered."""
    ensure_assessment_tables()
    con = _connect()
    try:
        where, params = [], []
        if status:
            where.append("i.status=?")
            params.append(status)
        if item_type:
            where.append("i.item_type=?")
            params.append(item_type.upper())
        if topic:
            where.append("(i.topic LIKE ? OR i.mastery_key=?)")
            params.extend([f"%{topic}%", slugify(topic)])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = con.execute(
            f"SELECT i.item_id, i.current_version FROM assessment_items i{clause}"
            " ORDER BY i.updated_at DESC, i.item_id LIMIT ?", (*params, max(1, int(limit))),
        ).fetchall()
    finally:
        con.close()
    return [get_item(r["item_id"], r["current_version"]) for r in rows]


# ─── Evidence-grounded generation (DRAFT only) ───


def _evidence_sentences(evidence_rows: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for ev in evidence_rows:
        text = _clean(ev.get("excerpt") or "", 4000)
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = _clean(sentence, 500)
            if sentence and sentence not in out:
                out.append(sentence)
    return out


def _distractor_sentences(concept: str, evidence_rows: List[Dict[str, Any]],
                          exclude: List[str]) -> List[str]:
    """Real evidence sentences that do not discuss the concept (never invented)."""
    concept_terms = _terms(concept)
    pool: List[str] = []
    for sentence in _evidence_sentences(evidence_rows):
        if sentence in exclude:
            continue
        if concept_terms & _terms(sentence):
            continue
        pool.append(sentence)
    return pool


def _item_provenance(key_points: List[Dict[str, Any]],
                     evidence_rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """item → claim → evidence → source → edition/chapter/section/page refs."""
    refs: List[Dict[str, Any]] = []
    claims: List[Dict[str, Any]] = []
    by_id = {ev.get("evidence_id"): ev for ev in evidence_rows}
    evidence_ids = [ev_id for ev_id in by_id if ev_id]
    claim_map: Dict[str, List[str]] = {}
    if evidence_ids:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT claim_id, evidence_id FROM claim_evidence WHERE evidence_id IN ("
                + ",".join("?" for _ in evidence_ids) + ") ORDER BY claim_id",
                evidence_ids,
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            con.close()
        for row in rows:
            claim_map.setdefault(row["evidence_id"], []).append(row["claim_id"])
    for kp in key_points or []:
        ev = by_id.get(kp.get("evidence_id")) or {}
        linked = claim_map.get(ev.get("evidence_id"), [])
        ref = {
            "evidence_id": kp.get("evidence_id"),
            "claim_id": linked[0] if linked else None,
            "source_id": ev.get("source_id"),
            "evidence_type": ev.get("evidence_type"),
            "locator": kp.get("locator") or ev.get("locator") or "",
            "page_number": kp.get("page_number") or ev.get("page_number"),
            "edition_id": ev.get("edition_id"),
            "chapter_title": ev.get("chapter_title") or "",
            "section_title": ev.get("section_title") or "",
        }
        ref = {k: v for k, v in ref.items() if v not in (None, "")}
        if ref and ref not in refs:
            refs.append(ref)
        for claim_id in linked:
            if {"claim_id": claim_id} not in claims:
                claims.append({"claim_id": claim_id})
    return refs, claims


def generate_items(topic: str, count: int = 5, item_types: Optional[List[str]] = None, *,
                   concept: Optional[str] = None, difficulty: int = 2,
                   now: Optional[str] = None) -> Dict[str, Any]:
    """Generate DRAFT items from P3/P4 evidence. Never publishes anything.

    Abstains entirely when the evidence floor says the topic cannot be taught;
    falls back to another item type when safe distractors do not exist rather
    than inventing medicine.
    """
    from medforge import tutor as TU

    ensure_assessment_tables()
    topic = _clean(topic, 400)
    if not topic:
        raise ValueError("generate_items needs a topic.")
    concept = _clean(concept or topic, 400)
    item_types = [t.upper() for t in (item_types or
                                      ["MCQ_SINGLE", "TRUE_FALSE", "RECALL", "SHORT_ANSWER"])]
    for item_type in item_types:
        if item_type not in T.ASSESSMENT_ITEM_TYPES:
            raise ValueError(f"Unsupported item type {item_type!r}.")
    gathered = TU.gather_evidence(topic)
    key_points = TU.extract_key_points(topic, gathered["evidence"])
    assessment = TU.assess_evidence(topic, key_points, gathered["evidence"])
    status = assessment.get("status") or "INSUFFICIENT_EVIDENCE"
    if not TU.verification_policy(status)["teach"]:
        return {"topic": topic, "created": [], "abstained": True, "assessment": assessment,
                "evidence_count": gathered["count"],
                "reason": f"evidence state {status}: no items generated"}
    target = TU.select_tutor_target(topic)
    evidence_rows = list(gathered["evidence"])
    refs, claims = _item_provenance(key_points, evidence_rows)
    sentences = _evidence_sentences(evidence_rows)
    created: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for index in range(max(1, int(count))):
        anchor = key_points[index % len(key_points)]
        item_type = item_types[index % len(item_types)]
        used_sentences = [kp["text"] for kp in key_points]
        choices: List[str] = []
        correct_choices: List[str] = []
        correct_answer = anchor["text"]
        stem = ""
        if item_type == "MCQ_SINGLE":
            distractors = _distractor_sentences(concept, evidence_rows, exclude=used_sentences)
            if len(distractors) < T.ASSESSMENT_MCQ_MIN_DISTRACTORS:
                skipped.append({"index": index, "requested_type": item_type,
                                "reason": "fewer than 2 evidence-backed distractors; "
                                          "fell back to RECALL"})
                item_type = "RECALL"
                stem = f"From memory: state what you know about {concept}."
            else:
                options = [anchor["text"]] + distractors[:3]
                letters = "ABCD"[: len(options)]
                choices = list(zip(letters, options))
                correct_choices = ["A"]
                stem = f"Which statement about {concept} is supported by your sources?"
        elif item_type == "TRUE_FALSE":
            distractors = _distractor_sentences(concept, evidence_rows, exclude=used_sentences)
            if not distractors:
                skipped.append({"index": index, "requested_type": item_type,
                                "reason": "no evidence-backed false statement; fell back to RECALL"})
                item_type = "RECALL"
                stem = f"From memory: state what you know about {concept}."
            else:
                false_statement = distractors[0]
                flip = index % 2 == 0
                statement = anchor["text"] if flip else false_statement
                choices = [("A", "True"), ("B", "False")]
                correct_choices = ["A"] if flip else ["B"]
                correct_answer = "True" if flip else "False"
                stem = f"True or false: {statement}"
        elif item_type == "SHORT_ANSWER":
            stem = f"Explain in one or two sentences: what is {concept}?"
        elif item_type == "CLINICAL_REASONING":
            stem = (f"A patient's case turns on {concept}. Using your sources, reason "
                    f"through the key mechanism in two or three sentences.")
        elif item_type == "MCQ_MULTI":
            second = key_points[(index + 1) % len(key_points)]["text"]
            if second == anchor["text"]:
                skipped.append({"index": index, "requested_type": item_type,
                                "reason": "needs two distinct key points"})
                continue
            choices = [("A", anchor["text"]), ("B", second)]
            choices.append(("C", _distractor_sentences(concept, evidence_rows, exclude=used_sentences)
                            [0] if _distractor_sentences(concept, evidence_rows,
                                                         exclude=used_sentences) else "No other option"))
            correct_choices = ["A", "B"]
            stem = f"Select ALL statements about {concept} supported by your sources."
        else:  # RECALL
            stem = f"From memory: state what you know about {concept}."
        rubric = [{"point": kp["text"], "evidence_id": kp.get("evidence_id"),
                   "locator": kp.get("locator") or ""} for kp in key_points]
        try:
            item = create_item(
                item_type, stem, topic=target["topic"], concept=concept,
                mastery_key=target["mastery_key"], curriculum_node_id=target.get("node_id"),
                choices=[{"key": k, "text": t} for k, t in choices],
                correct_choices=correct_choices, correct_answer=correct_answer,
                rubric=rubric, explanation=(
                    f"Evidence-backed answer: {anchor['text']}"
                    + (f" ({anchor.get('locator')})" if anchor.get("locator") else "")
                ),
                difficulty_target=difficulty, evidence_refs=refs, claim_refs=claims,
                evidence_state=status, generation_mode="textbook",
                source_kind="textbook_evidence", author="medforge-generator",
                item_model_version=T.ASSESSMENT_VERSION, now=now,
            )
        except ValueError as e:
            skipped.append({"index": index, "requested_type": item_type, "reason": str(e)[:200]})
            continue
        created.append(item)
    return {
        "topic": topic, "concept": concept, "created": created,
        "created_count": len(created), "skipped": skipped, "abstained": False,
        "assessment": assessment, "evidence_count": gathered["count"],
        "next_step": "validate_item → approve_item (generation never publishes)",
    }


# ─── Blueprints ───


def _normalize_distribution(distribution: Any, total: int,
                            allowed: Optional[Tuple[str, ...]] = None) -> Dict[str, int]:
    """Deterministically convert fractions or counts into integer counts."""
    if not distribution:
        return {}
    if not isinstance(distribution, dict):
        raise ValueError("a distribution must be a mapping of name → fraction or count")
    values = {str(k): float(v) for k, v in distribution.items()}
    if allowed:
        unknown = [k for k in values if k not in allowed]
        if unknown:
            raise ValueError(f"unknown distribution keys {unknown}; allowed: {list(allowed)}")
    total_value = sum(values.values())
    if total_value <= 0:
        raise ValueError("distribution values must be positive")
    counts: Dict[str, int] = {}
    if total_value <= 1.5:  # fractions
        raw = {k: v * total for k, v in values.items()}
        counts = {k: int(v) for k, v in raw.items()}
        remainder = total - sum(counts.values())
        order = sorted(raw, key=lambda k: (-(raw[k] - int(raw[k])), k))
        for k in order[:max(0, remainder)]:
            counts[k] += 1
    else:  # explicit counts
        counts = {k: int(v) for k, v in values.items()}
    return {k: v for k, v in counts.items() if v > 0}


def _scope_nodes(scope_type: str, scope_node_id: Optional[str],
                 scope_node_ids: Any = None) -> List[str]:
    """Resolve a curriculum scope to the Topic node ids it covers."""
    from medforge import curriculum as C

    if scope_type == "custom":
        return [str(n) for n in (scope_node_ids or []) if n]
    if not scope_node_id:
        return []
    if scope_type == "topic":
        return [scope_node_id]
    collected: List[str] = []
    frontier = [scope_node_id]
    con = _connect()
    try:
        while frontier:
            current = frontier.pop(0)
            try:
                rows = con.execute(
                    "SELECT id, node_type FROM curriculum_nodes WHERE parent_id=?",
                    (current,),
                ).fetchall()
            except sqlite3.OperationalError:
                break
            for row in rows:
                if row["node_type"] == "Topic":
                    collected.append(row["id"])
                frontier.append(row["id"])
    finally:
        con.close()
    return collected


def create_blueprint(
    title: str,
    *,
    scope_type: str = "topic",
    scope_node_id: Optional[str] = None,
    scope_node_ids: Any = None,
    item_count: int = 10,
    type_distribution: Any = None,
    difficulty_distribution: Any = None,
    topic_distribution: Any = None,
    prerequisite_coverage: float = 0.0,
    time_limit_minutes: Optional[int] = None,
    pass_threshold: float = T.ASSESSMENT_DEFAULT_PASS_THRESHOLD,
    seed: str = "",
    status: str = "ACTIVE",
    blueprint_id: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Create (or update) a deterministic, versioned blueprint."""
    ensure_assessment_tables()
    scope_type = str(scope_type or "topic").lower()
    if scope_type not in T.ASSESSMENT_SCOPE_TYPES:
        raise ValueError(f"scope_type must be one of {T.ASSESSMENT_SCOPE_TYPES}, got {scope_type!r}.")
    status = str(status or "ACTIVE").upper()
    if status not in T.ASSESSMENT_BLUEPRINT_STATUSES:
        raise ValueError(f"status must be one of {T.ASSESSMENT_BLUEPRINT_STATUSES}.")
    item_count = max(1, min(500, int(item_count)))
    blueprint_id = _clean(blueprint_id, 80) or "bp-" + _sha1(
        title.lower(), scope_type, scope_node_id or "", item_count, seed, length=12
    )
    now_str = _now_str(now)
    con = _connect()
    try:
        existing = con.execute(
            "SELECT blueprint_version FROM assessment_blueprints WHERE blueprint_id=?",
            (blueprint_id,),
        ).fetchone()
        version = int(existing["blueprint_version"]) + 1 if existing else 1
        con.execute(
            "INSERT INTO assessment_blueprints (blueprint_id,blueprint_version,title,"
            "scope_type,scope_node_id,scope_node_ids,item_count,type_distribution,"
            "difficulty_distribution,topic_distribution,prerequisite_coverage,"
            "time_limit_minutes,pass_threshold,status,validation_report,seed,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(blueprint_id) DO UPDATE SET blueprint_version=excluded.blueprint_version,"
            " title=excluded.title, scope_type=excluded.scope_type,"
            " scope_node_id=excluded.scope_node_id, scope_node_ids=excluded.scope_node_ids,"
            " item_count=excluded.item_count, type_distribution=excluded.type_distribution,"
            " difficulty_distribution=excluded.difficulty_distribution,"
            " topic_distribution=excluded.topic_distribution,"
            " prerequisite_coverage=excluded.prerequisite_coverage,"
            " time_limit_minutes=excluded.time_limit_minutes,"
            " pass_threshold=excluded.pass_threshold, status=excluded.status,"
            " seed=excluded.seed, updated_at=excluded.updated_at",
            (blueprint_id, version, _clean(title, 300) or blueprint_id, scope_type,
             scope_node_id, _json_dump([str(n) for n in (scope_node_ids or [])]),
             item_count, _json_dump(type_distribution or {}),
             _json_dump(difficulty_distribution or {}),
             _json_dump(topic_distribution or {}), float(prerequisite_coverage or 0.0),
             (int(time_limit_minutes) if time_limit_minutes else None),
             float(pass_threshold or T.ASSESSMENT_DEFAULT_PASS_THRESHOLD), status,
             _json_dump({}), _clean(seed, 80), now_str, now_str),
        )
        con.commit()
    finally:
        con.close()
    blueprint = get_blueprint(blueprint_id)
    blueprint["validation"] = validate_blueprint(blueprint_id, now=now)
    return blueprint


def get_blueprint(blueprint_id: str) -> Dict[str, Any]:
    ensure_assessment_tables()
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM assessment_blueprints WHERE blueprint_id=?", (blueprint_id,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise ValueError(f"Unknown blueprint_id {blueprint_id!r}.")
    data = _row_dict(row) or {}
    for field in ("type_distribution", "difficulty_distribution", "topic_distribution",
                  "scope_node_ids", "validation_report"):
        data[field] = _json_load(data.get(field), {} if field.endswith("distribution") or field == "validation_report" else [])
    return data


def list_blueprints(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    ensure_assessment_tables()
    con = _connect()
    try:
        if status:
            rows = con.execute(
                "SELECT blueprint_id FROM assessment_blueprints WHERE status=?"
                " ORDER BY updated_at DESC LIMIT ?", (status.upper(), max(1, int(limit))),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT blueprint_id FROM assessment_blueprints"
                " ORDER BY updated_at DESC LIMIT ?", (max(1, int(limit)),),
            ).fetchall()
    finally:
        con.close()
    return [get_blueprint(r["blueprint_id"]) for r in rows]


def _bank_counts() -> Dict[str, Any]:
    """Available ACTIVE bank capacity (deterministic; no item content loaded)."""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT i.item_type, v.difficulty_target, i.mastery_key, count(*) AS n"
            " FROM assessment_items i JOIN assessment_item_versions v"
            "   ON v.item_id=i.item_id AND v.item_version=i.current_version"
            " WHERE i.status='ACTIVE' GROUP BY i.item_type, v.difficulty_target, i.mastery_key"
        ).fetchall()
    finally:
        con.close()
    by_type: Dict[str, int] = {}
    by_band: Dict[str, int] = {}
    by_topic: Dict[str, int] = {}
    total = 0
    for row in rows:
        n = int(row["n"])
        total += n
        by_type[row["item_type"]] = by_type.get(row["item_type"], 0) + n
        band = _difficulty_band(int(row["difficulty_target"])) 
        by_band[band] = by_band.get(band, 0) + n
        by_topic[row["mastery_key"]] = by_topic.get(row["mastery_key"], 0) + n
    return {"total": total, "by_type": by_type, "by_band": by_band, "by_topic": by_topic}


def _difficulty_band(difficulty: int) -> str:
    for band, (low, high) in T.ASSESSMENT_DIFFICULTY_BANDS.items():
        if low <= int(difficulty) <= high:
            return band
    return "medium"


def validate_blueprint(blueprint: Any, now: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic blueprint validation, including bank availability."""
    blueprint = get_blueprint(blueprint) if isinstance(blueprint, str) else dict(blueprint or {})
    errors: List[str] = []
    warnings: List[str] = []
    item_count = int(blueprint.get("item_count") or 0)
    if item_count < 1:
        errors.append("item_count must be >= 1")
    scope_type = str(blueprint.get("scope_type") or "").lower()
    if scope_type not in T.ASSESSMENT_SCOPE_TYPES:
        errors.append(f"scope_type must be one of {list(T.ASSESSMENT_SCOPE_TYPES)}")
    scope_nodes = _scope_nodes(scope_type, blueprint.get("scope_node_id"),
                               blueprint.get("scope_node_ids"))
    if scope_type != "custom" and not scope_nodes:
        errors.append("scope does not resolve to any curriculum topic node")
    normalized: Dict[str, Any] = {}
    try:
        normalized["types"] = _normalize_distribution(
            blueprint.get("type_distribution"), item_count,
            allowed=tuple(T.ASSESSMENT_ITEM_TYPES))
        normalized["bands"] = _normalize_distribution(
            blueprint.get("difficulty_distribution"), item_count,
            allowed=tuple(T.ASSESSMENT_DIFFICULTY_BANDS.keys()))
    except ValueError as e:
        errors.append(str(e))
        normalized.setdefault("types", {})
        normalized.setdefault("bands", {})
    topic_distribution = blueprint.get("topic_distribution") or {}
    normalized["topics"] = {str(k): int(v) for k, v in topic_distribution.items()} if topic_distribution else {}
    for name, dist in (("type_distribution", blueprint.get("type_distribution")),
                       ("difficulty_distribution", blueprint.get("difficulty_distribution"))):
        if not dist:
            continue
        total_value = sum(float(v) for v in dict(dist).values())
        if total_value > 1.5 and abs(total_value - item_count) > 0.01:
            warnings.append(f"{name} counts sum to {total_value:g}, not item_count {item_count}")
    time_limit = blueprint.get("time_limit_minutes")
    if time_limit is not None and int(time_limit) < 1:
        errors.append("time_limit_minutes must be >= 1 when set")
    threshold = float(blueprint.get("pass_threshold", T.ASSESSMENT_DEFAULT_PASS_THRESHOLD))
    if not (0.0 <= threshold <= 1.0):
        errors.append("pass_threshold must be within 0..1")
    bank = _bank_counts()
    shortfalls: List[Dict[str, Any]] = []
    if scope_nodes:
        pool = 0
        con = _connect()
        try:
            placeholders = ",".join("?" for _ in scope_nodes)
            pool = con.execute(
                f"SELECT count(*) FROM assessment_items i JOIN assessment_item_versions v"
                f" ON v.item_id=i.item_id AND v.item_version=i.current_version"
                f" WHERE i.status='ACTIVE' AND i.curriculum_node_id IN ({placeholders})",
                scope_nodes,
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pool = 0
        finally:
            con.close()
        if pool < item_count:
            shortfalls.append({"kind": "scope", "needed": item_count, "available": pool})
    for item_type, need in normalized["types"].items():
        available = bank["by_type"].get(item_type, 0)
        if available < need:
            shortfalls.append({"kind": "type", "name": item_type, "needed": need,
                               "available": available})
    for band, need in normalized["bands"].items():
        available = bank["by_band"].get(band, 0)
        if available < need:
            shortfalls.append({"kind": "difficulty", "name": band, "needed": need,
                               "available": available})
    for key, need in normalized["topics"].items():
        available = bank["by_topic"].get(slugify(key), 0) or bank["by_topic"].get(key, 0)
        if available < need:
            shortfalls.append({"kind": "topic", "name": key, "needed": int(need),
                               "available": available})
    report = {
        "blueprint_id": blueprint.get("blueprint_id"),
        "valid": not errors, "errors": errors, "warnings": warnings,
        "normalized": normalized, "scope_nodes": scope_nodes,
        "bank": {"active_items": bank["total"], "by_type": bank["by_type"],
                 "by_band": bank["by_band"]},
        "shortfalls": shortfalls, "checked_at": _now_str(now),
        "validator_version": T.ASSESSMENT_VERSION,
    }
    return report


# ─── Deterministic item selection ───


def _exposure_map(item_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    con = _connect()
    try:
        rows = con.execute(
            f"SELECT item_id, count(*) AS presented,"
            f" sum(CASE WHEN answered_at IS NOT NULL THEN 1 ELSE 0 END) AS answered,"
            f" max(COALESCE(answered_at, presented_at)) AS last_seen"
            f" FROM assessment_attempts WHERE item_id IN ({placeholders})"
            f" GROUP BY item_id", item_ids,
        ).fetchall()
    finally:
        con.close()
    return {
        row["item_id"]: {
            "times_presented": int(row["presented"] or 0),
            "times_answered": int(row["answered"] or 0),
            "last_exposure": row["last_seen"],
        }
        for row in rows
    }


def select_items(blueprint: Any, *, assessment_id: Optional[str] = None,
                 limit: Optional[int] = None, exclude_item_ids: Any = None,
                 prefer_unseen: bool = True, now: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic, exposure-aware selection of ACTIVE items for a blueprint."""
    from medforge import learner_model as LM

    ensure_assessment_tables()
    blueprint = get_blueprint(blueprint) if isinstance(blueprint, str) else dict(blueprint or {})
    blueprint_id = blueprint.get("blueprint_id") or "adhoc"
    blueprint_version = int(blueprint.get("blueprint_version") or 1)
    item_count = max(1, min(T.ASSESSMENT_MAX_ITEMS, int(limit or blueprint.get("item_count") or 10)))
    scope_nodes = _scope_nodes(str(blueprint.get("scope_type") or "topic").lower(),
                               blueprint.get("scope_node_id"), blueprint.get("scope_node_ids"))
    exclude = {str(x) for x in (exclude_item_ids or [])}
    con = _connect()
    try:
        sql = (
            "SELECT i.item_id, i.item_type, i.mastery_key, i.topic, i.curriculum_node_id,"
            " i.current_version, v.difficulty_target, v.evidence_state"
            " FROM assessment_items i JOIN assessment_item_versions v"
            " ON v.item_id=i.item_id AND v.item_version=i.current_version"
            " WHERE i.status='ACTIVE'"
        )
        params: List[Any] = []
        if scope_nodes:
            sql += f" AND i.curriculum_node_id IN ({','.join('?' for _ in scope_nodes)})"
            params.extend(scope_nodes)
        rows = [_row_dict(r) or {} for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()
    candidates = [r for r in rows if r["item_id"] not in exclude]
    exposure = _exposure_map([r["item_id"] for r in candidates])
    now_dt = parse_time(_now_str(now))
    window = timedelta(days=T.ASSESSMENT_EXPOSURE_WINDOW_DAYS)
    recent: set = set()
    for item_id, stats in exposure.items():
        last = stats.get("last_exposure")
        if not last:
            continue
        try:
            if now_dt - parse_time(last) <= window:
                recent.add(item_id)
        except ValueError:
            continue

    for cand in candidates:
        stats = exposure.get(cand["item_id"], {})
        cand["times_presented"] = stats.get("times_presented", 0)
        cand["times_answered"] = stats.get("times_answered", 0)
        cand["last_exposure"] = stats.get("last_exposure")
        cand["recent_exposure"] = cand["item_id"] in recent
        mastery = LM.get_mastery(cand["mastery_key"]) if cand["mastery_key"] else None
        cand["mastery"] = (float(mastery["mastery"])
                          if mastery and mastery.get("mastery") is not None else None)
        cand["in_scope"] = bool(scope_nodes) and cand["curriculum_node_id"] in scope_nodes

    seed = f"{blueprint_id}|{blueprint_version}|{assessment_id or ''}|{blueprint.get('seed') or ''}"
    rng = random.Random(int(hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12], 16))

    def priority(cand: Dict[str, Any]) -> Tuple:
        mastery = cand["mastery"] if cand["mastery"] is not None else 0.5
        # Exposure is an ordering preference, never a hard filter: a recently
        # presented item is still usable when the blueprint needs its type/band,
        # so exposure avoidance can never starve a constraint or empty a slot.
        return (
            1 if (prefer_unseen and cand["recent_exposure"]) else 0,
            round(1.0 - mastery, 6),
            cand["times_presented"],
            _sha1(seed, cand["item_id"], length=16),
        )

    def order(pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(pool, key=priority)

    type_needs = _normalize_distribution(blueprint.get("type_distribution"), item_count,
                                        allowed=tuple(T.ASSESSMENT_ITEM_TYPES)) \
        if blueprint.get("type_distribution") else {}
    band_needs = _normalize_distribution(blueprint.get("difficulty_distribution"), item_count,
                                        allowed=tuple(T.ASSESSMENT_DIFFICULTY_BANDS.keys())) \
        if blueprint.get("difficulty_distribution") else {}
    topic_needs = {slugify(k): int(v) for k, v in (blueprint.get("topic_distribution") or {}).items()}

    def expand(needs: Dict[str, int]) -> List[Optional[str]]:
        slots: List[Optional[str]] = []
        for name in sorted(needs):
            slots.extend([name] * int(needs[name]))
        while len(slots) < item_count:
            slots.append(None)
        return slots[:item_count]

    # Build slots so that type and difficulty demands are matched to the bands
    # where each type actually has items (a feasible assignment is honoured
    # exactly; more constrained types claim their bands first).
    capacity: Dict[str, Dict[str, int]] = {}
    for cand in candidates:
        band = _difficulty_band(cand["difficulty_target"])
        capacity.setdefault(cand["item_type"], {})
        capacity[cand["item_type"]][band] = capacity[cand["item_type"]].get(band, 0) + 1
    band_remaining = dict(band_needs)
    slot_specs: List[Dict[str, Optional[str]]] = []
    for item_type in sorted(type_needs, key=lambda t: (sum(capacity.get(t, {}).values()), t)):
        for _ in range(int(type_needs[item_type])):
            chosen_band = None
            for candidate_band in sorted(band_remaining,
                                         key=lambda b: (-band_remaining[b], b)):
                if (band_remaining.get(candidate_band, 0) > 0
                        and capacity.get(item_type, {}).get(candidate_band, 0) > 0):
                    chosen_band = candidate_band
                    band_remaining[candidate_band] -= 1
                    break
            if chosen_band is None and band_remaining:
                relaxations.append({
                    "type": item_type,
                    "reason": "no difficulty band with matching items; band left open"})
            slot_specs.append({"item_type": item_type, "band": chosen_band, "topic": None})
    for leftover_band in sorted(band_remaining):
        for _ in range(max(0, int(band_remaining[leftover_band]))):
            if len(slot_specs) >= item_count:
                break
            slot_specs.append({"item_type": None, "band": leftover_band, "topic": None})
    topic_slots = expand(topic_needs)
    while len(slot_specs) < item_count:
        slot_specs.append({"item_type": None, "band": None, "topic": None})
    for index, slot in enumerate(slot_specs[:item_count]):
        slot["topic"] = topic_slots[index] if index < len(topic_slots) else None
    # A blueprint with no type distribution keeps all six types eligible.
    chosen: List[Dict[str, Any]] = []
    pairs: List[Tuple[int, Dict[str, Any]]] = []
    used: set = set()
    shortfalls: List[Dict[str, Any]] = []
    relaxations: List[Dict[str, Any]] = []
    pending = list(range(item_count))
    # Most-constrained-slot-first: a loose slot can never consume the only item a
    # tighter slot needed, so a feasible blueprint assignment is honoured exactly
    # and only an infeasible one is relaxed (and reported).
    while pending:
        scored: List[Tuple[int, int, List[Dict[str, Any]]]] = []
        for slot in pending:
            want_type = slot_specs[slot]["item_type"]
            want_band = slot_specs[slot]["band"]
            want_topic = slot_specs[slot]["topic"]
            pool = [c for c in candidates if c["item_id"] not in used]
            strict = [c for c in pool
                      if (not want_type or c["item_type"] == want_type)
                      and (not want_band or _difficulty_band(c["difficulty_target"]) == want_band)
                      and (not want_topic or c["mastery_key"] == want_topic)]
            scored.append((len(strict), slot, strict))
        scored.sort(key=lambda entry: (entry[0] == 0, entry[0], entry[1]))
        count, slot, strict = scored[0]
        pending.remove(slot)
        want_type = slot_specs[slot]["item_type"]
        want_band = slot_specs[slot]["band"]
        want_topic = slot_specs[slot]["topic"]
        if not strict:
            pool = [c for c in candidates if c["item_id"] not in used]
            relaxed = [c for c in pool
                       if (want_type and c["item_type"] == want_type)
                       or (want_band and _difficulty_band(c["difficulty_target"]) == want_band)]
            if relaxed:
                relaxations.append({"slot": slot + 1, "wanted":
                                    {"item_type": want_type, "band": want_band,
                                     "mastery_key": want_topic},
                                    "reason": "cross-constraint infeasible; matched the closest available item"})
                strict = relaxed
            else:
                strict = pool
        if not strict:
            shortfalls.append({"slot": slot + 1, "needed": 1, "available": 0,
                               "wanted": {"item_type": want_type, "band": want_band,
                                          "mastery_key": want_topic}})
            continue
        pick = order(strict)[0]
        pairs.append((slot, pick))
        used.add(pick["item_id"])
    # Present questions interleaved deterministically across item types (a
    # stable round-robin over the canonical type order, then within-type picks).
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for _, pick in sorted(pairs, key=lambda entry: (entry[1]["item_type"], entry[0])):
        by_type.setdefault(pick["item_type"], []).append(pick)
    chosen = []
    depth = 0
    while True:
        added = False
        for item_type in T.ASSESSMENT_ITEM_TYPES:
            bucket = by_type.get(item_type) or []
            if depth < len(bucket):
                chosen.append(bucket[depth])
                added = True
        if not added:
            break
        depth += 1
    coverage = {
        "types": {t: sum(1 for c in chosen if c["item_type"] == t)
                  for t in sorted({c["item_type"] for c in chosen})},
        "bands": {b: sum(1 for c in chosen if _difficulty_band(c["difficulty_target"]) == b)
                  for b in sorted({_difficulty_band(c["difficulty_target"]) for c in chosen})},
        "topics": {k: sum(1 for c in chosen if c["mastery_key"] == k)
                   for k in sorted({c["mastery_key"] for c in chosen})},
    }
    items = [{
        "item_id": c["item_id"], "item_version": int(c["current_version"]),
        "item_type": c["item_type"], "difficulty": int(c["difficulty_target"]),
        "topic": c["topic"], "mastery_key": c["mastery_key"],
        "curriculum_node_id": c["curriculum_node_id"],
        "recent_exposure": bool(c["recent_exposure"]),
        "times_presented": int(c["times_presented"]),
    } for c in chosen]
    return {
        "blueprint_id": blueprint.get("blueprint_id"),
        "blueprint_version": blueprint_version,
        "items": items, "count": len(items), "requested": item_count,
        "shortfalls": shortfalls, "relaxations": relaxations, "coverage": coverage,
        "seed": seed, "candidates": len(candidates), "excluded_recent": sorted(recent),
        "selected_at": _now_str(now), "selector_version": T.ASSESSMENT_VERSION,
    }


# ─── Assessment sessions ───


def _session_row(assessment_id: str) -> Dict[str, Any]:
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM assessment_sessions WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise ValueError(f"Unknown assessment_id {assessment_id!r}.")
    return _row_dict(row) or {}


def _attempt_rows(assessment_id: str) -> List[Dict[str, Any]]:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM assessment_attempts WHERE assessment_id=? ORDER BY question_order",
            (assessment_id,),
        ).fetchall()
    finally:
        con.close()
    return [_row_dict(r) or {} for r in rows]


def _attempt_row(assessment_id: str, question_order: int) -> Optional[Dict[str, Any]]:
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM assessment_attempts WHERE assessment_id=? AND question_order=?",
            (assessment_id, int(question_order)),
        ).fetchone()
    finally:
        con.close()
    return _row_dict(row)


def _item_order(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _json_load(session.get("item_order"), [])


def _ensure_attempt(session: Dict[str, Any], question_order: int,
                    now_str: str) -> Dict[str, Any]:
    """Create the presented-item snapshot on first presentation (exposure ledger)."""
    existing = _attempt_row(session["assessment_id"], question_order)
    if existing:
        return existing
    order = _item_order(session)
    entry = order[int(question_order) - 1]
    item = get_item(entry["item_id"], entry.get("item_version"))
    con = _connect()
    try:
        con.execute(
            "INSERT INTO assessment_attempts (assessment_id,item_id,item_version,"
            "question_order,stem,item_type,mastery_key,topic,difficulty,choices,"
            "correct_choices,rubric,correct_answer,scoring_policy,evidence_refs,"
            "evidence_state,presented_at,grading_status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(assessment_id, question_order) DO NOTHING",
            (session["assessment_id"], item["item_id"], item["item_version"],
             int(question_order), item["stem"], item["item_type"], item["mastery_key"],
             item["topic"], int(item["difficulty_target"]), _json_dump(item["choices"]),
             _json_dump(item["correct_choices"]), _json_dump(item["rubric"]),
             item["correct_answer"], _json_dump(item["scoring_policy"]),
             _json_dump(item["evidence_refs"]), item["evidence_state"], now_str,
             "ungraded", now_str, now_str),
        )
        con.commit()
    finally:
        con.close()
    return _attempt_row(session["assessment_id"], question_order) or {}


def _progress(attempts: List[Dict[str, Any]]) -> Dict[str, Any]:
    answered = [a for a in attempts if a.get("answered_at")]
    graded = [a for a in answered if a.get("grading_status") == "graded"]
    return {
        "answered": len(answered),
        "unanswered": sum(1 for a in attempts if not a.get("answered_at")),
        "graded": len(graded),
        "pending": sum(1 for a in answered if a.get("grading_status") in ("retryable", "ungraded")),
        "flagged": sum(1 for a in attempts if int(a.get("flagged") or 0) == 1),
        "correct": sum(1 for a in graded if a.get("correctness") == "correct"),
        "partial": sum(1 for a in graded if a.get("correctness") == "partial"),
        "incorrect": sum(1 for a in graded if a.get("correctness") == "incorrect"),
    }


def _remaining_seconds(session: Dict[str, Any], now_dt: datetime) -> Optional[float]:
    if not session.get("expires_at"):
        return None
    try:
        expires = parse_time(session["expires_at"])
    except ValueError:
        return None
    return max(0.0, round((expires - now_dt).total_seconds(), 3))


def _elapsed_seconds(session: Dict[str, Any], now_dt: datetime) -> Optional[float]:
    if not session.get("started_at"):
        return None
    try:
        started = parse_time(session["started_at"])
    except ValueError:
        return None
    end = now_dt
    if session.get("completed_at"):
        end = parse_time(session["completed_at"])
    return max(0.0, round((end - started).total_seconds(), 3))


def _feedback_allowed(session: Dict[str, Any]) -> bool:
    if session["mode"] == "EXAM":
        return session["status"] in ("submitted", "completed", "expired")
    return True


def _attempt_feedback(attempt: Dict[str, Any], session: Dict[str, Any]) -> Dict[str, Any]:
    if not _feedback_allowed(session) or attempt.get("grading_status") != "graded":
        return {"available": False, "reason": (
            "feedback is withheld until the exam is submitted"
            if session["mode"] == "EXAM" else "no graded feedback for this attempt"
        )}
    feedback = {
        "available": True,
        "score": attempt.get("score"),
        "max_score": attempt.get("max_score"),
        "correctness": attempt.get("correctness"),
        "error_type": attempt.get("error_type"),
        "explanation": attempt.get("explanation") or "",
        "grading_status": attempt.get("grading_status"),
        "grading_source": attempt.get("grading_source"),
        "key_points_present": _json_load(attempt.get("key_points_present"), []),
        "missing_key_points": _json_load(attempt.get("missing_key_points"), []),
        "incorrect_points": _json_load(attempt.get("incorrect_points"), []),
    }
    if session["mode"] == "REVIEW":
        from medforge import tutor as TU

        policy = TU.verification_policy(attempt.get("evidence_state") or "INSUFFICIENT_EVIDENCE")
        feedback["teaching"] = {
            "message": policy["message"],
            "qualify": policy["qualify"],
            "evidence_refs": _json_load(attempt.get("evidence_refs"), []),
            "correct_answer": attempt.get("correct_answer") or "",
            "correct_choices": _json_load(attempt.get("correct_choices"), []),
        }
    return feedback


def _attempt_view(attempt: Dict[str, Any], session: Dict[str, Any],
                  question: bool = True) -> Dict[str, Any]:
    view = {
        "attempt_id": attempt.get("attempt_id"),
        "question_order": attempt.get("question_order"),
        "item_id": attempt.get("item_id"),
        "item_version": attempt.get("item_version"),
        "item_type": attempt.get("item_type"),
        "stem": attempt.get("stem") or "",
        "choices": _json_load(attempt.get("choices"), []),
        "difficulty": attempt.get("difficulty"),
        "topic": attempt.get("topic"), "mastery_key": attempt.get("mastery_key"),
        "presented_at": attempt.get("presented_at"),
        "answered_at": attempt.get("answered_at"),
        "answered": bool(attempt.get("answered_at")),
        "flagged": bool(int(attempt.get("flagged") or 0)),
        "flag_reason": attempt.get("flag_reason") or "",
        "grading_status": attempt.get("grading_status"),
        "injection_suspected": bool(int(attempt.get("injection_suspected") or 0)),
        "feedback": _attempt_feedback(attempt, session),
    }
    if question:
        view["learner_answer"] = attempt.get("learner_answer")
        view["confidence"] = attempt.get("confidence")
    return view


def create_assessment(
    *,
    blueprint_id: Optional[str] = None,
    blueprint: Optional[Dict[str, Any]] = None,
    title: Optional[str] = None,
    mode: str = "PRACTICE",
    scope_type: Optional[str] = None,
    scope_node_id: Optional[str] = None,
    item_count: Optional[int] = None,
    time_limit_minutes: Optional[int] = None,
    pass_threshold: Optional[float] = None,
    item_ids: Any = None,
    seed: str = "",
    assessment_id: Optional[str] = None,
    start: bool = True,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a persistent assessment session with a fixed, reproducible order."""
    ensure_assessment_tables()
    mode = str(mode or "PRACTICE").upper()
    if mode not in T.ASSESSMENT_MODES:
        raise ValueError(f"mode must be one of {T.ASSESSMENT_MODES}, got {mode!r}.")
    now_str = _now_str(now)
    if blueprint_id:
        blueprint = get_blueprint(blueprint_id)
    elif not blueprint:
        if not scope_type or not scope_node_id:
            raise ValueError("Provide blueprint_id or a (scope_type, scope_node_id) pair.")
        resolved = _scope_nodes(scope_type, scope_node_id)
        title = title or f"{scope_type}:{scope_node_id}"
        blueprint = create_blueprint(
            title, scope_type=scope_type, scope_node_id=scope_node_id,
            item_count=int(item_count or 10), time_limit_minutes=time_limit_minutes,
            pass_threshold=float(pass_threshold or T.ASSESSMENT_DEFAULT_PASS_THRESHOLD),
            seed=seed, now=now_str,
        )
    if item_ids:
        selection = {
            "items": [{"item_id": str(iid)} for iid in item_ids],
            "count": len(item_ids), "shortfalls": [], "coverage": {},
        }
        resolved_items: List[Dict[str, Any]] = []
        for iid in item_ids:
            item = get_item(str(iid))
            resolved_items.append({
                "item_id": item["item_id"], "item_version": item["item_version"],
                "item_type": item["item_type"], "difficulty": item["difficulty_target"],
                "topic": item["topic"], "mastery_key": item["mastery_key"],
                "curriculum_node_id": item["curriculum_node_id"],
            })
        selection["items"] = resolved_items
    else:
        selection = select_items(blueprint, assessment_id=assessment_id,
                                 limit=item_count or blueprint.get("item_count"))
        if not selection["items"]:
            return {"created": False, "error": "no eligible ACTIVE items for this blueprint",
                    "blueprint_id": blueprint.get("blueprint_id"), "selection": selection}
    assessment_id = _clean(assessment_id, 64) or "as-" + _sha1(
        blueprint.get("blueprint_id"), now_str, seed, str(random.getrandbits(48)), length=12
    )
    item_order = [{
        "item_id": entry["item_id"], "item_version": int(entry["item_version"]),
        "item_type": entry["item_type"], "mastery_key": entry["mastery_key"],
        "difficulty": int(entry["difficulty"]),
    } for entry in selection["items"]]
    con = _connect()
    try:
        con.execute(
            "INSERT INTO assessment_sessions (assessment_id,learner_key,blueprint_id,"
            "blueprint_version,title,mode,scope_type,scope_node_id,status,item_order,"
            "item_count,current_index,time_limit_minutes,pass_threshold,grading_pending,"
            "summary,remediation,model_calls,review_logged,content_version,seed,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (assessment_id, "local", blueprint.get("blueprint_id"),
             int(blueprint.get("blueprint_version") or 1),
             _clean(title or blueprint.get("title") or "Assessment", 300), mode,
             str(scope_type or blueprint.get("scope_type") or "topic").lower(),
             scope_node_id or blueprint.get("scope_node_id"), "created",
             _json_dump(item_order), len(item_order), 0,
             int(time_limit_minutes or blueprint.get("time_limit_minutes") or 0) or None,
             float(pass_threshold or blueprint.get("pass_threshold")
                   or T.ASSESSMENT_DEFAULT_PASS_THRESHOLD),
             0, _json_dump({}), _json_dump({}), 0, 0, T.ASSESSMENT_VERSION,
             _clean(seed, 80), now_str, now_str),
        )
        con.commit()
    finally:
        con.close()
    result: Dict[str, Any] = {"created": True, "assessment_id": assessment_id,
                              "selection": selection, "mode": mode}
    if start:
        result["state"] = start_assessment(assessment_id, now=now_str)
    return result


def start_assessment(assessment_id: str, now: Optional[str] = None) -> Dict[str, Any]:
    """Activate (or resume) a session; server timestamps define the clock."""
    session = _session_row(assessment_id)
    if session["status"] in T.ASSESSMENT_OPEN_STATUSES:
        now_str = _now_str(now)
        started_at = session.get("started_at") or now_str
        expires_at = session.get("expires_at")
        if session.get("time_limit_minutes") and not expires_at:
            expires_at = _fmt(parse_time(started_at) + timedelta(
                minutes=int(session["time_limit_minutes"])))
        con = _connect()
        try:
            con.execute(
                "UPDATE assessment_sessions SET status='active', started_at=?, expires_at=?,"
                " updated_at=? WHERE assessment_id=?",
                (started_at, expires_at, now_str, assessment_id),
            )
            con.commit()
        finally:
            con.close()
        _get_and_present(assessment_id, now=now_str)
    return get_assessment_state(assessment_id, now=now)


def _get_and_present(assessment_id: str, now: Optional[str] = None) -> Dict[str, Any]:
    session = _session_row(assessment_id)
    now_str = _now_str(now)
    if session["status"] in T.ASSESSMENT_OPEN_STATUSES:
        order = int(session.get("current_index") or 0) + 1
        if 1 <= order <= int(session.get("item_count") or 0):
            _ensure_attempt(session, order, now_str)
    return session


def get_current_item(assessment_id: str, *, public: bool = False,
                     now: Optional[str] = None) -> Dict[str, Any]:
    """Current question with mode-respecting feedback (presentation is recorded)."""
    session = _get_and_present(assessment_id, now=now)
    if session["status"] not in T.ASSESSMENT_OPEN_STATUSES:
        return {"assessment_id": assessment_id, "complete": True,
                "status": session["status"],
                "feedback_available": session["status"] in ("submitted", "completed", "expired")}
    order = int(session.get("current_index") or 0) + 1
    if order > int(session.get("item_count") or 0):
        return {"assessment_id": assessment_id, "at_end": True}
    attempt = _ensure_attempt(session, order, _now_str(now))
    view = _attempt_view(attempt, session, question=not public)
    view["assessment_id"] = assessment_id
    view["mode"] = session["mode"]
    return view if not public else public_assessment_view(view)


def get_assessment_state(assessment_id: str, public: bool = False,
                         now: Optional[str] = None) -> Dict[str, Any]:
    """Full session state: progress, timing, scores and the current question."""
    session = _session_row(assessment_id)
    now_str = _now_str(now)
    if session["status"] in T.ASSESSMENT_OPEN_STATUSES:
        session = _expire_if_due(session, now_str) or session
    attempts = _attempt_rows(assessment_id)
    summary = _json_load(session.get("summary"), {})
    mode = session["mode"]
    state = {
        "assessment_id": assessment_id,
        "title": session["title"], "mode": mode, "status": session["status"],
        "blueprint_id": session.get("blueprint_id"),
        "blueprint_version": session.get("blueprint_version"),
        "scope_type": session["scope_type"], "scope_node_id": session.get("scope_node_id"),
        "item_count": int(session["item_count"]),
        "current_index": int(session["current_index"]),
        "current_order": int(session["current_index"]) + 1,
        "started_at": session.get("started_at"), "expires_at": session.get("expires_at"),
        "completed_at": session.get("completed_at"),
        "elapsed_seconds": _elapsed_seconds(session, parse_time(now_str)),
        "remaining_seconds": _remaining_seconds(session, parse_time(now_str)),
        "time_limit_minutes": session.get("time_limit_minutes"),
        "pass_threshold": session.get("pass_threshold"),
        "raw_score": _round(session.get("raw_score")),
        "max_score": _round(session.get("max_score")),
        "percentage": _round(session.get("percentage")),
        "passed": (None if session.get("passed") is None else bool(session.get("passed"))),
        "progress": _progress(attempts),
        "feedback_policy": {"immediate": mode != "EXAM", "mode": mode,
                            "review_after_completion": mode == "EXAM"},
        "item_order": _item_order(session),
        "content_version": session.get("content_version"),
    }
    if session["status"] in T.ASSESSMENT_OPEN_STATUSES:
        current = get_current_item(assessment_id, now=now_str)
        state["current_item"] = None if "complete" in current else current
    elif summary:
        state["result"] = summary
    state = state if not public else public_assessment_view(state)
    return state


def _expire_if_due(session: Dict[str, Any], now_str: str) -> Optional[Dict[str, Any]]:
    if session["status"] not in T.ASSESSMENT_OPEN_STATUSES:
        return None
    remaining = _remaining_seconds(session, parse_time(now_str))
    if remaining is None or remaining > 0:
        return None
    complete_assessment(session["assessment_id"], reason="time_limit_reached",
                        now=now_str, _internal=True)
    return _session_row(session["assessment_id"])


def list_assessments(status: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    ensure_assessment_tables()
    con = _connect()
    try:
        if status:
            rows = con.execute(
                "SELECT assessment_id FROM assessment_sessions WHERE status=?"
                " ORDER BY updated_at DESC LIMIT ?", (status.lower(), max(1, int(limit))),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT assessment_id FROM assessment_sessions"
                " ORDER BY updated_at DESC LIMIT ?", (max(1, int(limit)),),
            ).fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        session = _session_row(row["assessment_id"])
        attempts = _attempt_rows(session["assessment_id"])
        out.append({
            "assessment_id": session["assessment_id"], "title": session["title"],
            "mode": session["mode"], "status": session["status"],
            "item_count": int(session["item_count"]),
            "progress": _progress(attempts),
            "percentage": _round(session.get("percentage")),
            "started_at": session.get("started_at"),
            "completed_at": session.get("completed_at"),
            "updated_at": session.get("updated_at"),
        })
    return out


def resume_assessment(assessment_id: Optional[str] = None,
                      now: Optional[str] = None) -> Dict[str, Any]:
    """Resume a named session, or the most recent still-open one."""
    if assessment_id:
        return get_assessment_state(assessment_id, now=now)
    ensure_assessment_tables()
    con = _connect()
    try:
        row = con.execute(
            "SELECT assessment_id FROM assessment_sessions WHERE status IN ('created','active')"
            " ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return {"resumed": False, "reason": "no open assessment"}
    state = get_assessment_state(row["assessment_id"], now=now)
    state["resumed"] = True
    return state


# ─── Grading (P7 primitives; multi-select is the one new deterministic grader) ───


def _coerce_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Parse JSON columns when grading straight from a stored attempt row."""
    out = dict(item or {})
    for field in ("choices", "correct_choices", "rubric", "scoring_policy", "evidence_refs"):
        value = out.get(field)
        if isinstance(value, str):
            out[field] = _json_load(value, {} if field == "scoring_policy" else [])
    return out


def _tutor_question(item: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt a P8 item to the P7 grading shape (no second grading engine)."""
    item = _coerce_item(item)
    choices = item.get("choices") or []
    correct = item.get("correct_choices") or []
    correct_option = None
    if choices and correct:
        keys = [c["key"] for c in choices]
        if correct[0] in keys:
            correct_option = keys.index(correct[0])
    return {
        "item_id": item.get("item_id"), "item_version": item.get("item_version"),
        "question_type": _TUTOR_TYPE.get(item.get("item_type"), "recall"),
        "prompt": item.get("stem") or "", "options": [c["text"] for c in choices],
        "correct_option": correct_option, "rubric": item.get("rubric") or [],
        "expected_answer": item.get("correct_answer") or "",
    }


def _parse_multi_answer(answer: Any, choices: List[Dict[str, str]]) -> List[str]:
    """Deterministic selection parsing: keys, letters, indices or option text."""
    entries: List[Any] = []
    if isinstance(answer, (list, tuple, set)):
        entries = list(answer)
    else:
        text = _clean(answer, 500)
        if not text:
            return []
        if "," in text or ";" in text:
            entries = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
        else:
            entries = [text]
    keys = [c["key"] for c in choices]
    selected: List[str] = []
    for entry in entries:
        text = _clean(entry, 500)
        resolved = None
        if text.upper() in keys:
            resolved = text.upper()
        elif len(text) == 1 and text.lower() in "abcdefgh"[: len(choices)]:
            resolved = keys["abcdefgh".index(text.lower())]
        elif text.isdigit() and 1 <= int(text) <= len(choices):
            resolved = keys[int(text) - 1]
        else:
            for choice in choices:
                if text and (text.lower() in choice["text"].lower()
                             or choice["text"].lower() in text.lower()):
                    resolved = choice["key"]
                    break
        if resolved and resolved not in selected:
            selected.append(resolved)
    return selected


def grade_multi_select(item: Dict[str, Any], answer: Any) -> Dict[str, Any]:
    """Deterministic multi-select grading per the item's configured policy."""
    item = _coerce_item(item)
    choices = item.get("choices") or []
    correct = list(item.get("correct_choices") or [])
    if not choices or not correct:
        return {"grading_status": "insufficient_evidence", "grading_source": "deterministic",
                "reason": "multi-select item has no graded options"}
    selected = _parse_multi_answer(answer, choices)
    correct_set, selected_set = set(correct), set(selected)
    policy = str((item.get("scoring_policy") or {}).get("multi_select") or "exact").lower()
    if policy == "partial":
        raw = (len(selected_set & correct_set) - len(selected_set - correct_set)) / float(len(correct_set))
        score = max(0.0, min(1.0, round(raw, ROUND)))
    else:
        score = 1.0 if selected_set == correct_set else 0.0
    if score >= T.TUTOR_PASS_SCORE:
        correctness, error_type = "correct", "none"
    elif score > 0:
        correctness, error_type = "partial", "minor"
    else:
        correctness, error_type = "incorrect", "conceptual"
    texts = {c["key"]: c["text"] for c in choices}
    return {
        "grading_status": "graded", "grading_source": "deterministic",
        "correctness": correctness, "score": score, "error_type": error_type,
        "explanation": ("All required options selected." if correctness == "correct"
                        else "Not all required options were selected correctly."),
        "key_points_present": [texts[k] for k in correct if k in selected_set],
        "missing_key_points": [texts[k] for k in correct if k not in selected_set],
        "incorrect_points": [texts[k] for k in selected if k not in correct_set],
        "confidence": 0.95, "scoring_policy": policy,
    }


def _apply_scoring_policy(grade: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    """Partial credit only when the item configuration explicitly allows it."""
    policy = item.get("scoring_policy") or {}
    if grade.get("grading_status") != "graded" or grade.get("score") is None:
        return grade
    score = float(grade["score"])
    if item.get("item_type") in _DETERMINISTIC_TYPES:
        return grade
    if not policy.get("partial_credit", True) and score not in (0.0, 1.0):
        grade = dict(grade)
        snapped = 1.0 if score >= T.TUTOR_PASS_SCORE else 0.0
        grade["score"] = snapped
        grade["correctness"] = "correct" if snapped == 1.0 else "incorrect"
        grade["error_type"] = "none" if snapped == 1.0 else grade.get("error_type", "unknown")
        grade["note"] = "partial credit disabled by item policy; score snapped deterministically"
    return grade


def grade_item(item: Dict[str, Any], answer: Any, model: Optional[str] = None,
               chat_fn: Optional[Callable[[str, str, str, float], str]] = None) -> Dict[str, Any]:
    """P8 grading entry point: dispatch to P7 grading or the multi-select grader."""
    from medforge import tutor as TU

    item = _coerce_item(item)
    item_type = item.get("item_type")
    if item_type == "MCQ_MULTI":
        grade = grade_multi_select(item, answer)
    else:
        grade = TU.evaluate_answer(_tutor_question(item), answer, model=model, chat_fn=chat_fn)
    grade = _apply_scoring_policy(grade, item)
    grade["injection_suspected"] = bool(
        grade.get("injection_suspected") or TU.injection_suspected(answer)
    )
    return grade


def _resolve_model(model: Optional[str],
                   chat_fn: Optional[Callable[[str, str, str, float], str]] = None) -> Optional[str]:
    """Default to the active local chat model for free-text grading when allowed."""
    if model or chat_fn is not None:
        return model or None
    if not getattr(T, "ASSESSMENT_AUTO_MODEL", True) or T.OFFLINE:
        return None
    try:
        from medforge.models import ensure_models

        return ensure_models() or None
    except Exception:
        return None


def _record_attempt_event(session: Dict[str, Any], attempt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Record one P6 learning event for a graded attempt (single write path)."""
    from medforge import learner_model as LM

    try:
        result = LM.record_learning_event(
            attempt.get("topic") or session["title"],
            score_fraction=float(attempt.get("score") or 0.0),
            item_type="question",
            item_id=f"{attempt['item_id']}#v{int(attempt['item_version'])}",
            correct=(str(attempt.get("correctness")) == "correct"),
            confidence=attempt.get("confidence"),
            response_time_seconds=attempt.get("response_time_seconds"),
            source="session",
            presented_at=attempt.get("presented_at"),
            answered_at=attempt.get("answered_at"),
            curriculum_node_id=session.get("scope_node_id"),
            content_version=T.ASSESSMENT_VERSION,
            mastery_key=attempt.get("mastery_key") or None,
        )
    except Exception:
        return None
    attempt_id = (result.get("attempt") or {}).get("attempt_id")
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_attempts SET learning_attempt_id=?, updated_at=? WHERE attempt_id=?",
            (attempt_id, utcnow(), int(attempt["attempt_id"])),
        )
        con.commit()
    finally:
        con.close()
    return result


# ─── Answering, navigation, flags ───


def _grade_fields(grade: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "score": grade.get("score"),
        "correctness": grade.get("correctness"),
        "error_type": grade.get("error_type"),
        "explanation": grade.get("explanation") or "",
        "key_points_present": _json_dump(grade.get("key_points_present") or []),
        "missing_key_points": _json_dump(grade.get("missing_key_points") or []),
        "incorrect_points": _json_dump(grade.get("incorrect_points") or []),
        "grading_status": grade.get("grading_status") or "ungraded",
        "grading_source": grade.get("grading_source") or "",
        "grader_version": T.ASSESSMENT_VERSION,
        "injection_suspected": 1 if grade.get("injection_suspected") else 0,
    }


def submit_assessment_answer(
    assessment_id: str,
    answer: Any,
    confidence: Optional[float] = None,
    *,
    item_id: Optional[str] = None,
    response_time_seconds: Optional[float] = None,
    model: Optional[str] = None,
    chat_fn: Optional[Callable[[str, str, str, float], str]] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist the answer, grade it through P7, record one P6 event.

    A graded answer is never overwritten or double-recorded; a retryable grade
    is retried in place and appends grading history. In EXAM mode the grade is
    stored but not revealed until completion.
    """
    from medforge import tutor as TU

    ensure_assessment_tables()
    now_str = _now_str(now)
    session = _session_row(assessment_id)
    if session["status"] not in T.ASSESSMENT_OPEN_STATUSES:
        return {"assessment_id": assessment_id, "already_complete": True,
                "status": session["status"],
                "result": _json_load(session.get("summary"), {})}
    session = _expire_if_due(session, now_str) or session
    if session["status"] not in T.ASSESSMENT_OPEN_STATUSES:
        return {"assessment_id": assessment_id, "time_expired": True,
                "status": session["status"],
                "result": _json_load(session.get("summary"), {})}
    if item_id:
        match = next((a for a in _attempt_rows(assessment_id)
                      if a["item_id"] == item_id and not a.get("answered_at")), None)
        order = int(match["question_order"]) if match else int(session["current_index"]) + 1
    else:
        order = int(session["current_index"]) + 1
    attempt = _ensure_attempt(session, order, now_str)
    if attempt.get("grading_status") == "graded" and attempt.get("answered_at"):
        return {"assessment_id": assessment_id, "already_answered": True,
                "attempt_id": attempt["attempt_id"],
                "feedback": _attempt_feedback(attempt, session),
                "state": get_assessment_state(assessment_id, now=now_str)}
    # Multi-select answers arrive as lists; store them as a stable "A,C" string so
    # the deterministic parser can always read them back (never a Python repr).
    if isinstance(answer, (list, tuple, set)):
        answer_text = ",".join(_clean(part, 200) for part in answer)
    else:
        answer_text = _clean(answer, 4000)
    confidence_value = None if confidence is None else max(0.0, min(1.0, float(confidence)))
    if response_time_seconds is None and attempt.get("presented_at"):
        try:
            delta = (parse_time(now_str) - parse_time(attempt["presented_at"])).total_seconds()
            response_time_seconds = max(0.0, round(delta, 3))
        except ValueError:
            response_time_seconds = None
    suspicious = TU.injection_suspected(answer_text)
    history = _json_load(attempt.get("grading_history"), [])
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_attempts SET learner_answer=?, confidence=?, answered_at=?,"
            " response_time_seconds=?, injection_suspected=?, grading_status='ungraded',"
            " updated_at=? WHERE attempt_id=?",
            (answer_text, confidence_value, now_str, response_time_seconds,
             1 if suspicious else 0, now_str, int(attempt["attempt_id"])),
        )
        con.commit()
    finally:
        con.close()
    attempt = _attempt_row(assessment_id, order) or attempt
    resolved_model = _resolve_model(model, chat_fn)
    grade = grade_item(attempt, answer_text, model=resolved_model, chat_fn=chat_fn)
    grading_status = str(grade.get("grading_status") or "ungraded")
    fields = _grade_fields(grade)
    fields["grading_attempts"] = int(attempt.get("grading_attempts") or 0) + 1
    history.append({
        "at": now_str, "grading_status": grading_status,
        "grading_source": grade.get("grading_source") or "",
        "score": grade.get("score"), "correctness": grade.get("correctness"),
        "model": resolved_model or "", "error": grade.get("error"),
        "note": grade.get("note"),
    })
    fields["grading_history"] = _json_dump(history)
    con = _connect()
    try:
        cols = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE assessment_attempts SET {cols}, updated_at=? WHERE attempt_id=?",
                    (*fields.values(), now_str, int(attempt["attempt_id"])))
        if grading_status == "graded":
            con.execute(
                "UPDATE assessment_sessions SET grading_pending=(SELECT count(*)"
                " FROM assessment_attempts WHERE assessment_id=? AND answered_at IS NOT NULL"
                " AND grading_status IN ('retryable','ungraded')),"
                " model_calls=model_calls+?, updated_at=? WHERE assessment_id=?",
                (assessment_id, 1 if grade.get("grading_source") == "model" else 0,
                 now_str, assessment_id),
            )
        con.commit()
    finally:
        con.close()
    attempt = _attempt_row(assessment_id, order) or attempt
    if grading_status == "graded" and not attempt.get("learning_attempt_id"):
        _record_attempt_event(session, attempt)
        attempt = _attempt_row(assessment_id, order) or attempt
    session = _session_row(assessment_id)
    result: Dict[str, Any] = {
        "assessment_id": assessment_id, "attempt_id": attempt["attempt_id"],
        "question_order": attempt["question_order"], "item_id": attempt["item_id"],
        "item_version": attempt["item_version"],
        "grading_status": grading_status,
    }
    if grading_status == "retryable":
        result.update({
            "retryable": True, "answer_saved": True,
            "reason": grade.get("error") or "grading failed",
            "note": "the answer is saved; retry grading without creating a new attempt",
        })
    elif grading_status == "insufficient_evidence":
        result.update({
            "abstained": True, "answer_saved": True,
            "reason": grade.get("reason") or "grading requires an evidence-backed rubric",
        })
    if session["mode"] == "EXAM" and session["status"] in T.ASSESSMENT_OPEN_STATUSES:
        result["feedback_withheld"] = True
    else:
        result["feedback"] = _attempt_feedback(attempt, session)
        result["grade"] = {k: grade.get(k) for k in
                           ("score", "correctness", "error_type", "explanation",
                            "grading_status", "grading_source")}
    result["state"] = get_assessment_state(assessment_id, now=now_str)
    return result


def retry_pending_grading(assessment_id: str, *, model: Optional[str] = None,
                          chat_fn: Optional[Callable[[str, str, str, float], str]] = None,
                          now: Optional[str] = None) -> Dict[str, Any]:
    """Re-grade retryable answers in place (grading history, one attempt)."""
    ensure_assessment_tables()
    now_str = _now_str(now)
    session = _session_row(assessment_id)
    resolved_model = _resolve_model(model, chat_fn)
    pending = [a for a in _attempt_rows(assessment_id)
               if a.get("answered_at") and a.get("grading_status") in ("retryable", "ungraded")]
    retried: List[Dict[str, Any]] = []
    for attempt in pending:
        grade = grade_item(attempt, attempt.get("learner_answer") or "",
                           model=resolved_model, chat_fn=chat_fn)
        grading_status = str(grade.get("grading_status") or "ungraded")
        fields = _grade_fields(grade)
        fields["grading_attempts"] = int(attempt.get("grading_attempts") or 0) + 1
        history = _json_load(attempt.get("grading_history"), [])
        history.append({"at": now_str, "grading_status": grading_status,
                        "grading_source": grade.get("grading_source") or "",
                        "score": grade.get("score"), "correctness": grade.get("correctness"),
                        "model": resolved_model or "", "error": grade.get("error"),
                        "note": grade.get("note"), "retry": True})
        fields["grading_history"] = _json_dump(history)
        con = _connect()
        try:
            cols = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE assessment_attempts SET {cols}, updated_at=? WHERE attempt_id=?",
                        (*fields.values(), now_str, int(attempt["attempt_id"])))
            con.commit()
        finally:
            con.close()
        fresh = _attempt_row(assessment_id, int(attempt["question_order"])) or {}
        if grading_status == "graded" and not fresh.get("learning_attempt_id"):
            _record_attempt_event(session, fresh)
        retried.append({"attempt_id": attempt["attempt_id"], "grading_status": grading_status,
                        "score": grade.get("score"), "item_id": attempt["item_id"]})
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_sessions SET grading_pending=(SELECT count(*)"
            " FROM assessment_attempts WHERE assessment_id=? AND answered_at IS NOT NULL"
            " AND grading_status IN ('retryable','ungraded')), updated_at=? WHERE assessment_id=?",
            (assessment_id, now_str, assessment_id),
        )
        con.commit()
    finally:
        con.close()
    session = _session_row(assessment_id)
    if session["status"] in ("completed", "expired", "submitted"):
        # Recompute stored results deterministically; never invent credit.
        summary = _compute_results(_session_row(assessment_id), _attempt_rows(assessment_id), now_str)
        _persist_results(assessment_id, summary, session["status"], now_str)
    return {"assessment_id": assessment_id, "retried": retried,
            "retried_count": len(retried),
            "state": get_assessment_state(assessment_id, now=now_str)}


def _find_attempt_for(assessment_id: str, session: Dict[str, Any],
                      order: Optional[int] = None,
                      item_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if item_id:
        return next((a for a in _attempt_rows(assessment_id) if a["item_id"] == item_id), None)
    target = int(order if order is not None else int(session["current_index"]) + 1)
    return _ensure_attempt(session, target, utcnow())


def flag_item(assessment_id: str, *, order: Optional[int] = None,
              item_id: Optional[str] = None, reason: str = "",
              flagged: bool = True, now: Optional[str] = None) -> Dict[str, Any]:
    """Mark/unmark a question for review. Flags survive restarts."""
    ensure_assessment_tables()
    now_str = _now_str(now)
    session = _session_row(assessment_id)
    attempt = _find_attempt_for(assessment_id, session, order=order, item_id=item_id)
    if attempt is None:
        return {"assessment_id": assessment_id, "flagged": False,
                "error": "no such question in this assessment"}
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_attempts SET flagged=?, flag_reason=?, updated_at=?"
            " WHERE attempt_id=?",
            (1 if flagged else 0, _clean(reason, 300), now_str, int(attempt["attempt_id"])),
        )
        con.commit()
    finally:
        con.close()
    return {"assessment_id": assessment_id, "attempt_id": attempt["attempt_id"],
            "question_order": attempt["question_order"], "item_id": attempt["item_id"],
            "flagged": bool(flagged), "reason": reason,
            "flagged_count": _progress(_attempt_rows(assessment_id))["flagged"]}


def next_item(assessment_id: str, now: Optional[str] = None) -> Dict[str, Any]:
    """Advance one question; answers are never lost by navigation."""
    ensure_assessment_tables()
    now_str = _now_str(now)
    session = _session_row(assessment_id)
    if session["status"] in T.ASSESSMENT_OPEN_STATUSES:
        session = _expire_if_due(session, now_str) or session
    if session["status"] not in T.ASSESSMENT_OPEN_STATUSES:
        return {"assessment_id": assessment_id, "complete": True,
                "status": session["status"], "state": get_assessment_state(assessment_id, now=now_str)}
    index = int(session["current_index"])
    if index + 1 >= int(session["item_count"]):
        progress = _progress(_attempt_rows(assessment_id))
        return {"assessment_id": assessment_id, "at_end": True, "progress": progress,
                "unanswered": progress["unanswered"],
                "note": "submit the assessment to finish"}
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_sessions SET current_index=?, updated_at=? WHERE assessment_id=?",
            (index + 1, now_str, assessment_id),
        )
        con.commit()
    finally:
        con.close()
    state = get_assessment_state(assessment_id, now=now_str)
    return {"assessment_id": assessment_id, "moved": "next",
            "current_order": state["current_order"], "current_item": state.get("current_item"),
            "progress": state["progress"]}


def previous_item(assessment_id: str, now: Optional[str] = None) -> Dict[str, Any]:
    """Go back one question where the mode allows it (answers are preserved)."""
    ensure_assessment_tables()
    now_str = _now_str(now)
    session = _session_row(assessment_id)
    if session["status"] in T.ASSESSMENT_OPEN_STATUSES:
        session = _expire_if_due(session, now_str) or session
    if session["status"] not in T.ASSESSMENT_OPEN_STATUSES:
        return {"assessment_id": assessment_id, "complete": True,
                "status": session["status"],
                "state": get_assessment_state(assessment_id, now=now_str)}
    index = int(session["current_index"])
    if index <= 0:
        return {"assessment_id": assessment_id, "at_start": True}
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_sessions SET current_index=?, updated_at=? WHERE assessment_id=?",
            (index - 1, now_str, assessment_id),
        )
        con.commit()
    finally:
        con.close()
    state = get_assessment_state(assessment_id, now=now_str)
    return {"assessment_id": assessment_id, "moved": "previous",
            "current_order": state["current_order"], "current_item": state.get("current_item"),
            "progress": state["progress"]}


# ─── Completion, results, remediation ───


def _review_grade(percentage: Optional[float]) -> int:
    if percentage is None:
        return 0
    fraction = float(percentage) / 100.0
    for threshold, grade in T.ASSESSMENT_REVIEW_GRADES:
        if fraction >= threshold:
            return grade
    return 0


def _domain_of(attempt: Dict[str, Any]) -> str:
    return attempt.get("mastery_key") or attempt.get("topic") or "unassigned"


def _compute_results(session: Dict[str, Any], attempts: List[Dict[str, Any]],
                     now_str: str) -> Dict[str, Any]:
    """Deterministic scoring: arithmetic only, no model, no false credit."""
    answered = [a for a in attempts if a.get("answered_at")]
    graded = [a for a in answered if a.get("grading_status") == "graded" and a.get("score") is not None]
    pending = [a for a in answered if a.get("grading_status") in ("retryable", "ungraded")]
    flagged = [a for a in attempts if int(a.get("flagged") or 0) == 1]
    raw = round(sum(float(a["score"]) for a in graded), ROUND)
    max_graded = round(sum(float(a.get("max_score") or 1.0) for a in graded), ROUND)
    max_all = round(sum(float(a.get("max_score") or 1.0) for a in answered), ROUND)
    percentage = round(raw / max_graded * 100.0, ROUND) if max_graded > 0 else None
    threshold = float(session.get("pass_threshold") or T.ASSESSMENT_DEFAULT_PASS_THRESHOLD)

    def aggregate(group: List[Dict[str, Any]]) -> Dict[str, Any]:
        got = round(sum(float(a.get("score") or 0.0) for a in group), ROUND)
        possible = round(sum(float(a.get("max_score") or 1.0) for a in group), ROUND)
        return {
            "answered": len(group), "raw_score": got, "max_score": possible,
            "percentage": (round(got / possible * 100.0, ROUND) if possible else None),
            "correct": sum(1 for a in group if a.get("correctness") == "correct"),
            "partial": sum(1 for a in group if a.get("correctness") == "partial"),
            "incorrect": sum(1 for a in group if a.get("correctness") == "incorrect"),
        }

    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    for attempt in graded:
        by_domain.setdefault(_domain_of(attempt), []).append(attempt)
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for attempt in graded:
        by_type.setdefault(attempt.get("item_type") or "unknown", []).append(attempt)
    by_band: Dict[str, List[Dict[str, Any]]] = {}
    for attempt in graded:
        by_band.setdefault(_difficulty_band(int(attempt.get("difficulty") or 2)), []).append(attempt)
    domain_scores = {k: aggregate(v) for k, v in sorted(by_domain.items())}
    type_scores = {k: aggregate(v) for k, v in sorted(by_type.items())}
    band_scores = {k: aggregate(v) for k, v in sorted(by_band.items())}

    confidences = [float(a["confidence"]) for a in answered if a.get("confidence") is not None]
    correct_conf = [float(a["confidence"]) for a in graded
                    if a.get("correctness") == "correct" and a.get("confidence") is not None]
    incorrect_conf = [float(a["confidence"]) for a in graded
                      if a.get("correctness") != "correct" and a.get("confidence") is not None]
    overconfident = [{"item_id": a["item_id"], "question_order": a["question_order"],
                      "confidence": a.get("confidence"), "correctness": a.get("correctness")}
                     for a in graded
                     if a.get("confidence") is not None
                     and float(a["confidence"]) >= T.TUTOR_HIGH_CONFIDENCE
                     and a.get("correctness") == "incorrect"]
    underconfident = [{"item_id": a["item_id"], "question_order": a["question_order"],
                       "confidence": a.get("confidence"), "correctness": a.get("correctness")}
                      for a in graded
                      if a.get("confidence") is not None
                      and float(a["confidence"]) <= T.TUTOR_LOW_CONFIDENCE
                      and a.get("correctness") == "correct"]
    times = [float(a["response_time_seconds"]) for a in answered
             if a.get("response_time_seconds") is not None]
    strongest = sorted(domain_scores,
                       key=lambda k: (-(domain_scores[k].get("percentage") or 0.0), k))[:3]
    weakest = sorted(domain_scores,
                     key=lambda k: ((domain_scores[k].get("percentage") or 0.0), k))[:3]
    return {
        "assessment_id": session["assessment_id"], "title": session["title"],
        "mode": session["mode"], "blueprint_id": session.get("blueprint_id"),
        "blueprint_version": session.get("blueprint_version"),
        "scope_type": session["scope_type"], "scope_node_id": session.get("scope_node_id"),
        "started_at": session.get("started_at"), "completed_at": now_str,
        "elapsed_seconds": _elapsed_seconds({**session, "completed_at": now_str},
                                            parse_time(now_str)),
        "content_version": session.get("content_version"),
        "score": {
            "raw_score": raw, "max_score": max_graded,
            "max_score_presented": max_all, "percentage": percentage,
            "pass_threshold": threshold,
            "passed": (None if percentage is None else bool(percentage >= threshold * 100.0)),
            "graded_items": len(graded), "pending_items": len(pending),
        },
        "counts": {
            "presented": len(attempts), "answered": len(answered),
            "unanswered": sum(1 for a in attempts if not a.get("answered_at")),
            "flagged": len(flagged), "graded": len(graded), "pending": len(pending),
        },
        "domains": domain_scores,
        "item_types": type_scores,
        "difficulty_bands": band_scores,
        "strongest_domains": strongest, "weakest_domains": weakest,
        "confidence": {
            "answered_with_confidence": len(confidences),
            "mean": _round(sum(confidences) / len(confidences)) if confidences else None,
            "correct_mean": _round(sum(correct_conf) / len(correct_conf)) if correct_conf else None,
            "incorrect_mean": (_round(sum(incorrect_conf) / len(incorrect_conf))
                               if incorrect_conf else None),
            "overconfident": overconfident, "underconfident": underconfident,
            "mismatch_count": len(overconfident) + len(underconfident),
        },
        "timing": {
            "mean_response_time_seconds": _round(sum(times) / len(times)) if times else None,
            "fastest_seconds": _round(min(times)) if times else None,
            "slowest_seconds": _round(max(times)) if times else None,
            "responses_with_time": len(times),
        },
        "flagged_items": [{"item_id": a["item_id"], "question_order": a["question_order"],
                           "reason": a.get("flag_reason") or ""} for a in flagged],
        "grading": {
            "pending_items": [{"item_id": a["item_id"], "question_order": a["question_order"],
                               "grading_status": a["grading_status"]} for a in pending],
            "sources": sorted({a.get("grading_source") for a in graded if a.get("grading_source")}),
            "injection_flagged": sum(1 for a in attempts if int(a.get("injection_suspected") or 0)),
        },
        "sample_size": {"graded_attempts": len(graded)},
        "generated_at": now_str, "engine_version": T.ASSESSMENT_VERSION,
        "note": ("percentages are raw score fractions for this assessment; they are not a "
                 "validated psychometric ability estimate"),
    }


def _persist_results(assessment_id: str, summary: Dict[str, Any], status: str,
                     now_str: str, session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    session = session or _session_row(assessment_id)
    score = summary["score"]
    review = summary.get("review") or {}
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_sessions SET status=?, completed_at=COALESCE(completed_at, ?),"
            " submitted_at=COALESCE(submitted_at, ?), elapsed_seconds=?, raw_score=?,"
            " max_score=?, percentage=?, passed=?, grading_pending=?, summary=?,"
            " review_logged=?, updated_at=? WHERE assessment_id=?",
            (status, now_str, now_str, summary.get("elapsed_seconds"),
             score["raw_score"], score["max_score"], score["percentage"],
             (None if score["passed"] is None else (1 if score["passed"] else 0)),
             summary["counts"]["pending"], _json_dump(summary),
             1 if review.get("review_logged") else int(session.get("review_logged") or 0),
             now_str, assessment_id),
        )
        con.commit()
    finally:
        con.close()
    return summary


def _sync_review_opportunity(session: Dict[str, Any], summary: Dict[str, Any],
                             now_str: str) -> Dict[str, Any]:
    """Create + grade one topic review opportunity via the existing SM-2 queue."""
    percentage = summary["score"]["percentage"]
    if percentage is None:
        return {"skipped": "no graded items"}
    mastery_keys = [d for d in summary.get("domains", {}) if d and d != "unassigned"]
    if not mastery_keys:
        return {"skipped": "no mastery key"}
    chosen = max(mastery_keys, key=lambda k: (
        summary["domains"][k].get("max_score") or 0.0, k))
    item_id = f"{chosen}:{T.ASSESSMENT_REVIEW_ITEM_SUFFIX}"
    grade = _review_grade(percentage)
    con = _connect()
    try:
        con.execute(
            "INSERT OR IGNORE INTO spaced_repetition_queue"
            " (item_type, item_id, due_date, created_at, updated_at) VALUES ('topic', ?, ?, ?, ?)",
            (item_id, now_str, now_str, now_str),
        )
        con.commit()
    finally:
        con.close()
    from medforge.learner import review_card

    try:
        result = review_card(item_id, grade, item_type="topic", now=now_str)
    except Exception as e:
        return {"item_id": item_id, "grade": grade, "review_logged": False,
                "error": str(e)[:300]}
    return {"item_id": item_id, "grade": grade, "review_logged": True,
            "due_date": result.get("due_date"), "interval_days": result.get("interval_days"),
            "scheduler": result.get("scheduler"), "state": result.get("state"),
            "percentage": percentage}


def complete_assessment(assessment_id: str, reason: Optional[str] = None,
                        now: Optional[str] = None, _internal: bool = False) -> Dict[str, Any]:
    """Finalize the session, compute honest results and sync one review item."""
    ensure_assessment_tables()
    session = _session_row(assessment_id)
    existing = _json_load(session.get("summary"), {})
    if session["status"] in ("submitted", "completed", "expired") and existing:
        return {"assessment_id": assessment_id, "already_complete": True,
                "result": existing, "status": session["status"]}
    now_str = _now_str(now)
    attempts = _attempt_rows(assessment_id)
    summary = _compute_results(session, attempts, now_str)
    summary["stop_reason"] = reason or "learner submitted"
    review = _sync_review_opportunity(session, summary, now_str)
    summary["review"] = review
    status = "expired" if str(reason) == "time_limit_reached" else "completed"
    con = _connect()
    try:
        con.execute(
            "UPDATE assessment_sessions SET status=?, submitted_at=COALESCE(submitted_at, ?),"
            " completed_at=COALESCE(completed_at, ?), elapsed_seconds=?, raw_score=?,"
            " max_score=?, percentage=?, passed=?, grading_pending=?, summary=?,"
            " review_logged=?, remediation=?, updated_at=? WHERE assessment_id=?",
            (status, now_str, now_str, summary.get("elapsed_seconds"),
             summary["score"]["raw_score"], summary["score"]["max_score"],
             summary["score"]["percentage"],
             (None if summary["score"]["passed"] is None
              else (1 if summary["score"]["passed"] else 0)),
             summary["counts"]["pending"], _json_dump(summary),
             1 if review.get("review_logged") else 0, _json_dump({}), now_str, assessment_id),
        )
        con.commit()
    finally:
        con.close()
    remediation = get_assessment_remediation(assessment_id, now=now_str)
    con = _connect()
    try:
        con.execute("UPDATE assessment_sessions SET remediation=?, updated_at=? WHERE assessment_id=?",
                    (_json_dump(remediation), now_str, assessment_id))
        con.commit()
    finally:
        con.close()
    return {"assessment_id": assessment_id, "status": status, "result": summary,
            "remediation": remediation, "review": review}


def get_assessment_result(assessment_id: str, public: bool = False,
                          now: Optional[str] = None) -> Dict[str, Any]:
    """Stored result for a completed assessment (EXAM requires completion)."""
    ensure_assessment_tables()
    session = _session_row(assessment_id)
    if session["status"] in T.ASSESSMENT_OPEN_STATUSES:
        return {"assessment_id": assessment_id, "available": False,
                "status": session["status"],
                "reason": ("feedback is withheld until the exam is submitted"
                           if session["mode"] == "EXAM" else
                           "the assessment is still in progress"),
                "state": get_assessment_state(assessment_id, now=now)}
    summary = _json_load(session.get("summary"), {})
    if not summary:
        summary = _compute_results(session, _attempt_rows(assessment_id), _now_str(now))
    if public:
        return {"available": True, "status": session["status"],
                "result": public_assessment_view(summary)}
    return {"assessment_id": assessment_id, "available": True, "status": session["status"],
            "result": summary, "remediation": _json_load(session.get("remediation"), {})}


def review_assessment(assessment_id: str, *, include_answers: bool = True,
                      now: Optional[str] = None) -> Dict[str, Any]:
    """Post-completion question review (with evidence references)."""
    session = _session_row(assessment_id)
    if session["status"] in T.ASSESSMENT_OPEN_STATUSES:
        return {"assessment_id": assessment_id, "available": False,
                "reason": "review becomes available after completion",
                "status": session["status"]}
    attempts = _attempt_rows(assessment_id)
    questions = []
    for attempt in attempts:
        view = _attempt_view(attempt, session)
        entry = {
            "question_order": attempt["question_order"], "item_id": attempt["item_id"],
            "item_version": attempt["item_version"], "item_type": attempt["item_type"],
            "stem": attempt["stem"], "choices": _json_load(attempt.get("choices"), []),
            "flagged": bool(int(attempt.get("flagged") or 0)),
            "correct_choices": _json_load(attempt.get("correct_choices"), []),
            "correct_answer": attempt.get("correct_answer") or "",
            "explanation": attempt.get("explanation") or "",
            "evidence_refs": _json_load(attempt.get("evidence_refs"), []),
            "grading_status": attempt.get("grading_status"),
            "score": attempt.get("score"),
            "feedback": view["feedback"],
        }
        if include_answers:
            entry["learner_answer"] = attempt.get("learner_answer")
            entry["confidence"] = attempt.get("confidence")
            entry["key_points_present"] = _json_load(attempt.get("key_points_present"), [])
        questions.append(entry)
    return {"assessment_id": assessment_id, "available": True, "status": session["status"],
            "questions": questions, "reviewed_at": _now_str(now)}


def get_assessment_remediation(assessment_id: str, now: Optional[str] = None) -> Dict[str, Any]:
    """Structured recommendations from P6 (no automatic tutor launch)."""
    from medforge import learner_model as LM

    ensure_assessment_tables()
    session = _session_row(assessment_id)
    summary = _json_load(session.get("summary"), {})
    if not summary:
        summary = _compute_results(session, _attempt_rows(assessment_id), _now_str(now))
    domains = summary.get("domains", {})
    scope_keys = {k for k in domains if k and k != "unassigned"}
    ranked = sorted(domains.items(), key=lambda kv: ((kv[1].get("percentage") or 0.0), kv[0]))
    weak_topics = [k for k, v in ranked if (v.get("percentage") or 0.0) < 70.0][:5]
    weaknesses = []
    try:
        for row in LM.get_weaknesses(limit=50):
            if scope_keys and row.get("topic_id") not in scope_keys:
                continue
            weaknesses.append({"topic_id": row.get("topic_id"), "concept": row.get("concept"),
                               "severity": row.get("severity"),
                               "weakness_score": row.get("weakness_score"),
                               "low_confidence": bool(row.get("low_confidence")),
                               "origin": row.get("origin")})
    except Exception:
        weaknesses = []
    prerequisite_gaps: List[Dict[str, Any]] = []
    for topic in (weak_topics or list(scope_keys))[:3]:
        try:
            risks = LM.get_prerequisite_risks(topic)
        except Exception:
            continue
        for risk in (risks.get("risks") or [])[:3]:
            gap = {"topic": topic, "prerequisite": risk.get("title"),
                   "relationship": risk.get("relationship_type"),
                   "mastery_percent": risk.get("mastery_percent"),
                   "impact": risk.get("impact")}
            if gap not in prerequisite_gaps:
                prerequisite_gaps.append(gap)
    review_priorities: List[Dict[str, Any]] = []
    try:
        priorities = LM.study_priority(limit=20, now=now)
        for row in (priorities.get("priorities") or priorities.get("items") or []):
            topic_key = row.get("topic") or row.get("topic_id") or row.get("mastery_key")
            if scope_keys and topic_key not in scope_keys:
                continue
            review_priorities.append({"topic": topic_key,
                                      "priority": row.get("priority"),
                                      "mastery_percent": row.get("mastery_percent"),
                                      "reasons": row.get("reasons") or row.get("components")})
    except Exception:
        review_priorities = []
    tutor_target = weak_topics[0] if weak_topics else next(iter(sorted(scope_keys)), None)
    return {
        "assessment_id": assessment_id,
        "weak_topics": weak_topics,
        "weak_concepts": weaknesses[:5],
        "prerequisite_gaps": prerequisite_gaps[:5],
        "review_priorities": review_priorities[:5],
        "tutor_remediation": {
            "action": "start_tutor_session", "topic": tutor_target,
            "mode": "correct", "reason": "assessment weaknesses in this scope",
            "launch": False,
            "note": "structured recommendation only; no session is started automatically",
        },
        "spaced_repetition": {
            "review_opportunity": summary.get("review") or {},
            "launch": False,
        },
        "source": "P6 learner model (recency-weighted)",
        "generated_at": _now_str(now),
        "engine_version": T.ASSESSMENT_VERSION,
    }


# ─── Item statistics and quality analytics ───


def _attempt_total_percentage(assessment_id: str) -> Optional[float]:
    session = _session_row(assessment_id)
    return None if session.get("percentage") is None else float(session["percentage"])


def _statistics_for(item_id: str, item_version: Optional[int] = None,
                    now: Optional[str] = None) -> Dict[str, Any]:
    item = get_item(item_id, item_version)
    version = int(item["item_version"])
    con = _connect()
    try:
        rows = [_row_dict(r) or {} for r in con.execute(
            "SELECT * FROM assessment_attempts WHERE item_id=? AND item_version=?",
            (item_id, version),
        ).fetchall()]
    finally:
        con.close()
    answered = [r for r in rows if r.get("answered_at")]
    graded = [r for r in answered if r.get("grading_status") == "graded" and r.get("score") is not None]
    sample = len(graded)
    correct = sum(1 for r in graded if r.get("correctness") == "correct")
    scores = [float(r["score"]) for r in graded]
    times = [float(r["response_time_seconds"]) for r in answered
             if r.get("response_time_seconds") is not None]
    confidences = [float(r["confidence"]) for r in answered if r.get("confidence") is not None]
    correct_conf = [float(r["confidence"]) for r in graded
                    if r.get("correctness") == "correct" and r.get("confidence") is not None]
    incorrect_conf = [float(r["confidence"]) for r in graded
                      if r.get("correctness") != "correct" and r.get("confidence") is not None]
    insufficient = sample < T.ASSESSMENT_MIN_ITEM_STAT_SAMPLE
    proportion = _round(correct / sample) if sample else None
    discrimination = None
    discrimination_note = T.ASSESSMENT_INSUFFICIENT_SAMPLE_LABEL
    if sample >= T.ASSESSMENT_MIN_DISCRIMINATION_SAMPLE:
        scored = [(float(_attempt_total_percentage(r["assessment_id"]) or 0.0), r)
                  for r in graded]
        scored.sort(key=lambda pair: pair[0])
        group_size = max(1, len(scored) // 3)
        lower = [r for _, r in scored[:group_size]]
        upper = [r for _, r in scored[-group_size:]]
        if len(lower) >= 2 and len(upper) >= 2:
            p_upper = sum(1 for r in upper if r.get("correctness") == "correct") / float(len(upper))
            p_lower = sum(1 for r in lower if r.get("correctness") == "correct") / float(len(lower))
            discrimination = _round(p_upper - p_lower)
            discrimination_note = "upper-third minus lower-third proportion correct"
    return {
        "item_id": item_id, "item_version": version, "item_type": item["item_type"],
        "status": item["status"], "difficulty_target": item["difficulty_target"],
        "evidence_state": item["evidence_state"],
        "sample": {
            "presented": len(rows), "answered": len(answered), "graded": sample,
            "insufficient_sample": bool(insufficient),
            "label": (T.ASSESSMENT_INSUFFICIENT_SAMPLE_LABEL if insufficient else None),
        },
        "exposure": {
            "times_presented": len(rows), "times_answered": len(answered),
            "last_exposure": (max((r.get("presented_at") or "" for r in rows), default="") or None),
            "omission_rate": (_round(1.0 - len(answered) / len(rows)) if rows else None),
        },
        "difficulty": {
            "proportion_correct": proportion,
            "correct_count": correct, "answered_count": len(answered),
            "label": (T.ASSESSMENT_INSUFFICIENT_SAMPLE_LABEL if insufficient else None),
        },
        "score": {
            "mean_score": _round(sum(scores) / len(scores)) if scores else None,
            "graded_attempts": len(scores),
        },
        "response_time": {
            "mean_seconds": _round(sum(times) / len(times)) if times else None,
            "observations": len(times),
        },
        "confidence": {
            "observations": len(confidences),
            "mean": _round(sum(confidences) / len(confidences)) if confidences else None,
            "correct_mean": _round(sum(correct_conf) / len(correct_conf)) if correct_conf else None,
            "incorrect_mean": (_round(sum(incorrect_conf) / len(incorrect_conf))
                               if incorrect_conf else None),
            "calibration_gap": (_round(sum(correct_conf) / len(correct_conf)
                                        - sum(incorrect_conf) / len(incorrect_conf))
                                if correct_conf and incorrect_conf else None),
            "label": (T.ASSESSMENT_INSUFFICIENT_SAMPLE_LABEL if insufficient else None),
        },
        "discrimination": discrimination,
        "discrimination_note": discrimination_note,
        "grading": {
            "sources": sorted({r.get("grading_source") for r in graded if r.get("grading_source")}),
            "retryable_attempts": sum(1 for r in answered if r.get("grading_status") == "retryable"),
            "inconsistent_grading": len({r.get("grading_source") for r in graded
                                         if r.get("grading_source")}) > 1,
        },
        "flagged_rate": (_round(sum(1 for r in rows if int(r.get("flagged") or 0))
                                / len(rows)) if rows else None),
        "computed_at": _now_str(now),
        "statistics_version": T.ASSESSMENT_VERSION,
        "note": ("values below "
                 f"{T.ASSESSMENT_MIN_ITEM_STAT_SAMPLE} graded answers are labelled "
                 f"'{T.ASSESSMENT_INSUFFICIENT_SAMPLE_LABEL}' and are not a validated "
                 "psychometric estimate"),
    }


def get_item_statistics(item_id: str, item_version: Optional[int] = None,
                        now: Optional[str] = None) -> Dict[str, Any]:
    """Transparent per-item statistics, always carrying sample size."""
    ensure_assessment_tables()
    return _statistics_for(item_id, item_version, now=now)


def compute_item_quality(item_id: str, item_version: Optional[int] = None,
                         now: Optional[str] = None) -> Dict[str, Any]:
    """Derive quality flags and persist them for review (never deletes)."""
    ensure_assessment_tables()
    stats = _statistics_for(item_id, item_version, now=now)
    sample = stats["sample"]["graded"]
    flags: List[str] = []
    reason: Dict[str, Any] = {}
    if sample >= T.ASSESSMENT_MIN_ITEM_STAT_SAMPLE:
        proportion = stats["difficulty"]["proportion_correct"]
        if proportion is not None and proportion >= T.ASSESSMENT_FLAG_TOO_EASY:
            flags.append("too_easy"); reason["too_easy"] = proportion
        if proportion is not None and proportion <= T.ASSESSMENT_FLAG_TOO_DIFFICULT:
            flags.append("too_difficult"); reason["too_difficult"] = proportion
        if stats["discrimination"] is not None and stats["discrimination"] < T.ASSESSMENT_FLAG_POOR_DISCRIMINATION:
            flags.append("poor_discriminator"); reason["poor_discriminator"] = stats["discrimination"]
        if stats["flagged_rate"] is not None and stats["flagged_rate"] >= T.ASSESSMENT_FLAG_FLAGGED_RATE:
            flags.append("possible_ambiguity"); reason["possible_ambiguity"] = stats["flagged_rate"]
    exposure = stats["exposure"]
    if (exposure["times_presented"] >= T.ASSESSMENT_MIN_ITEM_STAT_SAMPLE
            and exposure["omission_rate"] is not None
            and exposure["omission_rate"] >= T.ASSESSMENT_FLAG_SKIPPED_RATE):
        flags.append("frequently_skipped"); reason["frequently_skipped"] = exposure["omission_rate"]
    if stats["grading"]["inconsistent_grading"] or stats["grading"]["retryable_attempts"] >= 3:
        flags.append("inconsistent_grading")
    item = get_item(item_id, item_version)
    if item["duplicate_of"] or "possible_duplicate" in (item["quality_flags"] or []):
        flags.append("possible_duplicate")
        reason["possible_duplicate"] = item["duplicate_of"]
    now_str = _now_str(now)
    con = _connect()
    try:
        con.execute(
            "INSERT INTO assessment_item_quality (item_id,item_version,sample_size,"
            "metrics,flags,computed_at) VALUES (?,?,?,?,?,?)",
            (stats["item_id"], stats["item_version"], sample,
             _json_dump({k: stats[k] for k in
                         ("difficulty", "exposure", "confidence", "discrimination",
                          "score", "response_time", "grading", "flagged_rate")}),
             _json_dump(flags), now_str),
        )
        merged = sorted(set(_json_load(
            con.execute("SELECT quality_flags FROM assessment_items WHERE item_id=?",
                        (item_id,)).fetchone()[0], [])) | set(flags))
        con.execute("UPDATE assessment_items SET quality_flags=?, updated_at=? WHERE item_id=?",
                    (_json_dump(merged), now_str, item_id))
        con.commit()
    finally:
        con.close()
    return {"item_id": stats["item_id"], "item_version": stats["item_version"],
            "sample_size": sample, "flags": flags, "reasons": reason,
            "metrics": stats, "computed_at": now_str,
            "action": "flagged for review; nothing is auto-deleted" if flags else "no flags",
            "insufficient_sample": stats["sample"]["insufficient_sample"]}


# ─── Privacy / export ───


def public_assessment_view(view: Dict[str, Any]) -> Dict[str, Any]:
    """Redact private learner data (answers, confidence, key points, feedback)."""
    from medforge import tutor as TU

    private = {"learner_answer", "confidence", "key_points_present", "missing_key_points",
               "incorrect_points", "feedback", "flagged_items", "overconfident",
               "underconfident", "weak_concepts", "reason", "note"}
    out: Dict[str, Any] = {}
    for key, value in (view or {}).items():
        if key in private:
            continue
        if isinstance(value, dict):
            out[key] = public_assessment_view(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [public_assessment_view(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return TU.public_view(out)


def assessment_export(assessment_id: str, *, include_responses: bool = False) -> Dict[str, Any]:
    """Distributable summary. Responses are excluded unless explicitly requested.

    Evidence excerpts are never exported (locators only), matching the
    repository's privacy/copyright boundary.
    """
    ensure_assessment_tables()
    session = _session_row(assessment_id)
    summary = _json_load(session.get("summary"), {})
    export = {
        "assessment_id": assessment_id, "title": session["title"], "mode": session["mode"],
        "status": session["status"], "result": public_assessment_view(summary),
        "remediation": public_assessment_view(_json_load(session.get("remediation"), {})),
        "responses_included": bool(include_responses),
    }
    if include_responses:
        export["questions"] = review_assessment(assessment_id, include_answers=True)["questions"]
    return export
