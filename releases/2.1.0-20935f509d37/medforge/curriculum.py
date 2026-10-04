"""MedForge V4 curriculum engine: canonical hierarchy + syllabus import.

Implements Phase 2 of the master build directive on top of the existing
(curriculum_nodes / prerequisites) V3 tables:

    SEMESTER -> SUBJECT -> WEEK -> SEMINAR -> TOPIC -> SUBTOPIC -> LEARNING OBJECTIVE

- a pasted weekly (or seminar-level) syllabus becomes structured curriculum
  data with minimal manual work
- topic normalization + duplicate detection keep the tree clean
- every studied topic resolves to its curriculum position (topic_path)
- progress state derives from learner_mastery joined on topic slugs

All DB access targets the active T.META_DB at call time (test-redirectable),
and the schema self-heals via core.database.migrate_v4.ensure_curriculum_v4,
mirroring the ensure_v3_tables() pattern in medforge.learner.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import medforge.types as T
from medforge.utils import slugify, utcnow

# Canonical curriculum chain (Phase 2). Stored in curriculum_nodes.node_type.
HIERARCHY: Tuple[str, ...] = (
    "Semester", "Subject", "Week", "Seminar", "Topic", "Subtopic", "Learning Objective",
)
SEMESTER_ABBREV = {"Semester": "SEM", "Subject": "SUB", "Week": "WK", "Seminar": "SEMNR",
                   "Topic": "TOPIC", "Subtopic": "SUBTOPIC", "Learning Objective": "LO",
                   "Year": "YEAR", "Course": "COURSE", "Module": "MODULE"}

__all__ = [
    "HIERARCHY", "normalize_title", "title_key", "ensure_curriculum_tables",
    "find_node", "ensure_node", "get_children", "import_syllabus",
    "parse_syllabus", "add_prerequisite", "remove_prerequisite",
    "curriculum_tree", "topic_path", "curriculum_progress", "detect_duplicates",
]


# ─── Schema self-healing ───


def _core_import():
    """Import core.database.migrate_v4, walking candidate bases like learner.py.

    T.BASE first (tests redirect it), then resolved parents of this file —
    covers the live install where T.META_DB lives elsewhere but core/ sits at
    the workspace root (current/ is a symlink into releases/<tag>/).
    """
    here = Path(__file__).resolve()
    candidates = [str(T.BASE)] + [str(p) for p in here.parents]
    for base_str in candidates:
        if base_str not in sys.path:
            sys.path.insert(0, base_str)
        try:
            from core.database.migrate_v4 import ensure_curriculum_v4

            return ensure_curriculum_v4
        except ImportError:
            continue
    raise ImportError(
        "core.database.migrate_v4 not importable from: " + ", ".join(candidates)
    )


def ensure_curriculum_tables() -> Dict[str, Any]:
    """Bring the active META_DB to the V4 curriculum schema (idempotent)."""
    from medforge.utils import mkdirs as _mkdirs

    _mkdirs()
    ensure_v4 = _core_import()
    return ensure_v4(T.META_DB)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    return con


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return None if row is None else {k: row[k] for k in row.keys()}


# ─── Normalization / identity ───


_WS_RE = re.compile(r"\s+")
_TRAIL_PUNCT_RE = re.compile(r"[\s:;,.\u2013\u2014-]+$")


def normalize_title(title: str) -> str:
    """Canonical display form: trimmed, whitespace-collapsed, trailing junk cut."""
    t = _WS_RE.sub(" ", (title or "").strip())
    return _TRAIL_PUNCT_RE.sub("", t) or (title or "").strip()


def title_key(title: str) -> str:
    """Case/whitespace/punctuation-insensitive comparison key."""
    t = normalize_title(title).casefold()
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return re.sub(r"\s+", " ", t)


def node_id(node_type: str, parent_id: Optional[str], title: str) -> str:
    """Deterministic content+position-addressed id (dedup by design)."""
    raw = f"{node_type}|{parent_id or 'ROOT'}|{title_key(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ─── Node primitives ───


def find_node(
    node_type: str,
    title: str,
    parent_id: Optional[str] = None,
    con: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Find a sibling node by normalized title (duplicate-safe lookup)."""
    if node_type not in T.CURRICULUM_NODE_TYPES:
        raise ValueError(
            f"node_type must be one of {T.CURRICULUM_NODE_TYPES}, got {node_type!r}."
        )
    own_con = con is None
    con = con or _connect()
    try:
        rows = con.execute(
            "SELECT * FROM curriculum_nodes WHERE node_type=? AND parent_id IS ?",
            (node_type, parent_id),
        ).fetchall()
        want = title_key(title)
        for r in rows:
            if title_key(r["title"]) == want:
                return _row_dict(r)
        return None
    finally:
        if own_con:
            con.close()


def next_order_index(
    node_type: str, parent_id: Optional[str], con: sqlite3.Connection
) -> int:
    row = con.execute(
        "SELECT max(order_index) FROM curriculum_nodes WHERE node_type=? AND parent_id IS ?",
        (node_type, parent_id),
    ).fetchone()
    return (row[0] or 0) + 1


def ensure_node(
    node_type: str,
    title: str,
    parent_id: Optional[str] = None,
    order_index: Optional[int] = None,
    code: str = "",
    description: str = "",
    year: Optional[int] = None,
    semester: Optional[int] = None,
    ects_weight: float = 0.0,
    con: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Idempotently return the child node, creating it when absent.

    Duplicate detection: within the same parent, titles that differ only by
    case/whitespace/punctuation map to the same node.
    """
    if node_type not in T.CURRICULUM_NODE_TYPES:
        raise ValueError(
            f"node_type must be one of {T.CURRICULUM_NODE_TYPES}, got {node_type!r}."
        )
    clean = normalize_title(title)
    if not clean:
        raise ValueError("Title must not be empty.")
    own_con = con is None
    con = con or _connect()
    try:
        existing = find_node(node_type, clean, parent_id, con)
        if existing is not None:
            return existing
        nid = node_id(node_type, parent_id, clean)
        now = utcnow()
        idx = order_index if order_index is not None else next_order_index(
            node_type, parent_id, con
        )
        con.execute(
            """INSERT INTO curriculum_nodes
               (id, parent_id, node_type, code, title, description, year, semester,
                ects_weight, order_index, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (nid, parent_id, node_type, code, clean, description,
             year, semester, ects_weight, idx, now, now),
        )
        con.commit()
        return _row_dict(con.execute(
            "SELECT * FROM curriculum_nodes WHERE id=?", (nid,)
        ).fetchone()) or {}
    finally:
        if own_con:
            con.close()


def get_children(
    node_type: Optional[str] = None,
    parent_id: Optional[str] = None,
    con: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """Children of a node (optionally filtered by type), ordered for the syllabus."""
    own_con = con is None
    con = con or _connect()
    try:
        if node_type:
            rows = con.execute(
                "SELECT * FROM curriculum_nodes WHERE node_type=? AND parent_id IS ?"
                " ORDER BY order_index, title COLLATE NOCASE",
                (node_type, parent_id),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM curriculum_nodes WHERE parent_id IS ?"
                " ORDER BY order_index, title COLLATE NOCASE",
                (parent_id,),
            ).fetchall()
        return [_row_dict(r) for r in rows]
    finally:
        if own_con:
            con.close()


# ─── Syllabus parsing (pasted text → structured curriculum) ───

_WEEK_RE = re.compile(
    r"^\s*#{0,4}\s*week\s*(\d+)\s*[:\-\u2013\u2014]?\s*(.*)$", re.IGNORECASE
)
_SEMINAR_RE = re.compile(
    r"^\s*#{0,4}\s*seminar\s*(?:\d+\s*)?[:\-\u2013\u2014]?\s*(.+)$", re.IGNORECASE
)
_SUBJECT_RE = re.compile(
    r"^\s*#{0,4}\s*subject\s*[:\-\u2013\u2014]?\s*(.+)$", re.IGNORECASE
)
_SEMESTER_RE = re.compile(
    r"^\s*#{0,4}\s*semester\s*(\d+)?\s*[:\-\u2013\u2014]?\s*(.*)$", re.IGNORECASE
)
_LO_RE = re.compile(
    r"^\s*(?:lo|learning objective|objectives?|outcome)s?\s*"
    r"[:\-\u2013\u2014]\s*(.+)$", re.IGNORECASE
)
_BULLET_RE = re.compile(r"^(\s*)[-*\u2022\u2013]\s+(.+)$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+)$")


def _new_week(number: int, theme: str) -> Dict[str, Any]:
    return {
        "number": number, "title": normalize_title(theme) or f"Week {number}",
        "has_theme": bool(normalize_title(theme)),
        "seminars": [], "topics": [], "objectives": [],
    }


def _new_seminar(title: str) -> Dict[str, Any]:
    return {"title": normalize_title(title), "topics": [], "objectives": []}


def _new_topic(title: str) -> Dict[str, Any]:
    return {"title": normalize_title(title), "subtopics": [], "objectives": []}


def parse_syllabus(
    text: str,
    subject_title: Optional[str] = None,
    semester_title: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse a pasted weekly/seminar syllabus into structured curriculum data.

    Accepted line forms (markdown headings tolerated, case-insensitive):
      - `Semester 3: <title>` / `Semester: <title>`   (optional)
      - `Subject: <title>` or any heading outside weeks (subject boundaries)
      - `Week 2`, `Week 2: <theme>` (headings or plain lines)
      - `Seminar: <title>`, `Seminar 2: <title>`
      - top-level bullets   → topics      (`- Anterior pituitary hormones`)
      - indented bullets    → subtopics   (2+ spaces under a topic)
      - `LO:` / `Objective:`/ `Outcome:` lines → learning objectives
      - a plain non-empty line inside a week that is not a bullet is a topic
    A week with a theme but no topics also records the theme as a topic, so a
    one-line syllabus ("Week 3: Adrenal medulla") still yields a topic.
    """
    if not (text or "").strip():
        raise ValueError("Syllabus text must not be empty.")

    semester: Dict[str, Any] = {"title": None, "number": None}
    subjects: List[Dict[str, Any]] = []
    subject: Optional[Dict[str, Any]] = None
    week: Optional[Dict[str, Any]] = None
    seminar: Optional[Dict[str, Any]] = None
    topic: Optional[Dict[str, Any]] = None

    def current_subject() -> Dict[str, Any]:
        nonlocal subject
        if subject is None:
            subject = {
                "title": normalize_title(subject_title) if subject_title else None,
                "weeks": [], "loose_seminars": [], "loose_topics": [], "objectives": [],
            }
            subjects.append(subject)
        return subject

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        wm = _WEEK_RE.match(line)
        if wm and re.search(r"week", line, re.IGNORECASE):
            week = _new_week(int(wm.group(1)), wm.group(2) or "")
            current_subject()["weeks"].append(week)
            seminar = None
            topic = None
            continue

        sm = _SEMINAR_RE.match(line)
        if sm:
            seminar = _new_seminar(sm.group(1))
            if week is not None:
                week["seminars"].append(seminar)
            else:
                current_subject()["loose_seminars"].append(seminar)
            topic = None
            continue

        subm = _SUBJECT_RE.match(line)
        if subm:
            subject = {
                "title": normalize_title(subm.group(1)),
                "weeks": [], "loose_seminars": [], "loose_topics": [], "objectives": [],
            }
            subjects.append(subject)
            week = None
            seminar = None
            topic = None
            continue

        semm = _SEMESTER_RE.match(line)
        if semm and re.search(r"semester", line, re.IGNORECASE):
            semester["number"] = int(semm.group(1)) if semm.group(1) else None
            semester["title"] = normalize_title(semm.group(2)) or None
            continue

        lom = _LO_RE.match(line)
        if lom:
            objective = normalize_title(lom.group(1))
            if topic is not None:
                topic["objectives"].append(objective)
            elif seminar is not None:
                seminar["objectives"].append(objective)
            elif week is not None:
                week["objectives"].append(objective)
            else:
                current_subject()["objectives"].append(objective)
            continue

        bm = _BULLET_RE.match(line)
        if bm:
            indent, text_part = len(bm.group(1)), normalize_title(bm.group(2))
            if indent >= 2 and topic is not None:
                topic["subtopics"].append(text_part)
            elif week is not None:
                target = seminar if seminar is not None else week
                topic = _new_topic(text_part)
                if seminar is not None:
                    seminar["topics"].append(topic)
                else:
                    week["topics"].append(topic)
            elif seminar is not None:
                # Seminar-level syllabus: bullets under an open (loose) seminar.
                topic = _new_topic(text_part)
                seminar["topics"].append(topic)
            else:
                topic = _new_topic(text_part)
                current_subject()["loose_topics"].append(topic)
            continue

        hm = _HEADING_RE.match(line)
        heading_text = normalize_title(hm.group(1)) if hm else normalize_title(line)
        if week is None and subject is not None and not heading_text.lower().startswith(
            ("week ", "seminar", "subject", "semester")
        ):
            # A new top-level heading outside weeks starts another subject.
            subject = {
                "title": heading_text,
                "weeks": [], "loose_seminars": [], "loose_topics": [], "objectives": [],
            }
            subjects.append(subject)
            continue
        if week is not None:
            topic = _new_topic(heading_text)
            if seminar is not None:
                seminar["topics"].append(topic)
            else:
                week["topics"].append(topic)
            continue
        if subject_title is None and subject is None and semester["title"] is None:
            # First heading before any structure is the document title; treat
            # it as the subject when nothing else names one.
            current_subject()["title"] = heading_text
            continue
        current_subject()  # noise line outside any structure

    # One-line weeks: theme also becomes a topic when nothing else did.
    for s in subjects:
        for w in s["weeks"]:
            if w["has_theme"] and not w["topics"] and not w["seminars"]:
                w["topics"].append(_new_topic(w["title"]))

    return {"semester": semester, "subjects": subjects}


# ─── Import (structured data → curriculum_nodes) ───


def _ensure_counted(node_type: str, title: str, parent_id: Optional[str],
                    counters: Dict[str, int], con: sqlite3.Connection,
                    **kwargs: Any) -> Dict[str, Any]:
    """ensure_node + created/existing bookkeeping for import summaries."""
    existing = find_node(node_type, title, parent_id, con)
    node = ensure_node(node_type, title, parent_id, con=con, **kwargs)
    if existing is None:
        counters[node_type] = counters.get(node_type, 0) + 1
    return node


def import_syllabus(
    text: str,
    subject_title: Optional[str] = None,
    semester_title: Optional[str] = None,
    week_number: Optional[int] = None,
) -> Dict[str, Any]:
    """Import a pasted syllabus into curriculum_nodes (idempotent).

    Weekly import: the text provides `Week N` headings (subjects optional).
    Seminar-level import: pass `week_number` (and optionally `subject_title`);
    seminars/topics parsed before any Week line attach to that week.
    Rerunning the same text creates nothing new — nodes are matched by
    normalized title within their parent.
    """
    ensure_curriculum_tables()
    parsed = parse_syllabus(text, subject_title=subject_title,
                            semester_title=semester_title)
    counters: Dict[str, int] = {}
    con = _connect()
    try:
        # Semester root.
        sem_number = parsed["semester"]["number"]
        sem_title = (parsed["semester"]["title"] or semester_title
                     or (f"Semester {sem_number}" if sem_number else None)
                     or "Semester")
        semester_node = _ensure_counted(
            "Semester", sem_title, None, counters, con,
            semester=sem_number,
        )

        subjects = parsed["subjects"] or []
        if not subjects:
            subjects = [{
                "title": normalize_title(subject_title) if subject_title else "Unassigned subject",
                "weeks": [], "loose_seminars": [], "loose_topics": [], "objectives": [],
            }]

        week_counts: Dict[int, int] = {}
        for s in subjects:
            s_title = s.get("title") or normalize_title(subject_title) or "Unassigned subject"
            subject_node = _ensure_counted(
                "Subject", s_title, semester_node["id"], counters, con,
            )

            def import_topic_list(topics: List[Dict[str, Any]],
                                  parent_id: str) -> None:
                for t in topics:
                    tnode = _ensure_counted(
                        "Topic", t["title"], parent_id, counters, con,
                    )
                    for st in t["subtopics"]:
                        _ensure_counted("Subtopic", st, tnode["id"], counters, con)
                    for lo in t["objectives"]:
                        _ensure_counted("Learning Objective", lo, tnode["id"], counters, con)

            def import_seminar(sem: Dict[str, Any], week_id: str) -> None:
                snode = _ensure_counted(
                    "Seminar", sem["title"], week_id, counters, con,
                )
                import_topic_list(sem["topics"], snode["id"])
                for lo in sem["objectives"]:
                    _ensure_counted(
                        "Learning Objective", lo, snode["id"], counters, con,
                    )

            for w in s["weeks"]:
                wnode = _ensure_counted(
                    "Week", w["title"], subject_node["id"], counters, con,
                    order_index=w["number"], code=f"W{w['number']:02d}",
                )
                week_counts[w["number"]] = week_counts.get(w["number"], 0) + 1
                for sem in w["seminars"]:
                    import_seminar(sem, wnode["id"])
                import_topic_list(w["topics"], wnode["id"])
                for lo in w["objectives"]:
                    _ensure_counted(
                        "Learning Objective", lo, wnode["id"], counters, con,
                    )

            # Seminar-level import: loose seminars/topics land in `week_number`.
            target_week_id: Optional[str] = None
            if week_number is not None:
                wnode = _ensure_counted(
                    "Week", f"Week {int(week_number)}", subject_node["id"],
                    counters, con,
                    order_index=int(week_number), code=f"W{int(week_number):02d}",
                )
                target_week_id = wnode["id"]
            for sem in s["loose_seminars"]:
                if target_week_id is None:
                    raise ValueError(
                        "Syllabus has seminars before any Week line; pass week_number="
                        " for a seminar-level import."
                    )
                import_seminar(sem, target_week_id)
            import_topic_list(s["loose_topics"],
                              target_week_id or subject_node["id"])
            for lo in s["objectives"]:
                _ensure_counted(
                    "Learning Objective", lo,
                    target_week_id or subject_node["id"], counters, con,
                )

        con.commit()
        created_total = sum(counters.values())
        return {
            "semester": semester_node,
            "created": counters,
            "created_total": created_total,
            "weeks": sorted(week_counts),
            "idempotent_repeat": created_total == 0,
        }
    finally:
        con.close()


# ─── Prerequisites ───


def _find_topic_node(title: str, con: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """Resolve a Topic (or Subtopic) node by normalized title or slug."""
    want_key, want_slug = title_key(title), slugify(normalize_title(title))
    rows = con.execute(
        "SELECT * FROM curriculum_nodes WHERE node_type IN ('Topic','Subtopic')"
    ).fetchall()
    fallback: Optional[Dict[str, Any]] = None
    for r in rows:
        if title_key(r["title"]) == want_key:
            return _row_dict(r)
        if fallback is None and slugify(r["title"]) == want_slug:
            fallback = _row_dict(r)
    return fallback


def add_prerequisite(
    topic_title: str,
    prerequisite_title: str,
    relationship_type: str = "strict",
    dependency_weight: float = 1.0,
) -> Dict[str, Any]:
    """Record that `topic_title` requires `prerequisite_title` (idempotent)."""
    if relationship_type not in T.PREREQUISITE_TYPES:
        raise ValueError(
            f"relationship_type must be one of {T.PREREQUISITE_TYPES},"
            f" got {relationship_type!r}."
        )
    ensure_curriculum_tables()
    con = _connect()
    try:
        topic = _find_topic_node(topic_title, con)
        prereq = _find_topic_node(prerequisite_title, con)
        missing = [
            f"{label} {title!r} is not in the curriculum"
            for label, title, node in (
                ("topic", topic_title, topic),
                ("prerequisite", prerequisite_title, prereq),
            ) if node is None
        ]
        if missing:
            raise ValueError("; ".join(missing))
        if topic["id"] == prereq["id"]:
            raise ValueError("A topic cannot be its own prerequisite.")
        now = utcnow()
        con.execute(
            """INSERT INTO prerequisites
               (topic_id, prerequisite_id, dependency_weight, relationship_type, created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(topic_id, prerequisite_id) DO NOTHING""",
            (topic["id"], prereq["id"], float(dependency_weight),
             relationship_type, now),
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM prerequisites WHERE topic_id=? AND prerequisite_id=?",
            (topic["id"], prereq["id"]),
        ).fetchone()
        return _row_dict(row) or {}
    finally:
        con.close()


def remove_prerequisite(topic_title: str, prerequisite_title: str) -> int:
    """Delete a prerequisite edge; returns rows removed."""
    ensure_curriculum_tables()
    con = _connect()
    try:
        topic = _find_topic_node(topic_title, con)
        prereq = _find_topic_node(prerequisite_title, con)
        if topic is None or prereq is None:
            return 0
        cur = con.execute(
            "DELETE FROM prerequisites WHERE topic_id=? AND prerequisite_id=?",
            (topic["id"], prereq["id"]),
        )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


# ─── Tree, traceability, progress ───


def curriculum_tree() -> List[Dict[str, Any]]:
    """Full curriculum as a nested tree rooted at Semester nodes."""
    ensure_curriculum_tables()
    con = _connect()
    try:
        rows = [_row_dict(r) for r in con.execute(
            "SELECT * FROM curriculum_nodes ORDER BY order_index, title COLLATE NOCASE"
        )]
    finally:
        con.close()
    by_parent: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)

    def build(node: Dict[str, Any]) -> Dict[str, Any]:
        children = by_parent.get(node["id"], [])
        return {**node, "children": [build(c) for c in children]}

    roots = [r for r in rows if r["node_type"] == "Semester" and r["parent_id"] is None]
    return [build(r) for r in roots]


def topic_path(topic: str) -> Optional[Dict[str, Any]]:
    """Trace a studied topic to its curriculum position.

    Matches Topic/Subtopic nodes by normalized title or by slug (the
    learner_mastery.topic_id namespace). Returns the node and the full
    ancestor chain (root first), or None when the topic is unmapped.
    """
    if not (topic or "").strip():
        raise ValueError("Topic must not be empty.")
    ensure_curriculum_tables()
    con = _connect()
    try:
        node = _find_topic_node(topic, con)
        if node is None:
            return None
        chain: List[Dict[str, Any]] = [node]
        cursor = node
        while cursor["parent_id"]:
            row = con.execute(
                "SELECT * FROM curriculum_nodes WHERE id=?", (cursor["parent_id"],)
            ).fetchone()
            if row is None:
                break
            cursor = _row_dict(row)
            chain.append(cursor)
        chain.reverse()
        return {
            "node": node,
            "chain": chain,
            "position": " / ".join(
                f"{c['node_type']}: {c['title']}" for c in chain
            ),
        }
    finally:
        con.close()


def curriculum_progress() -> Dict[str, Any]:
    """Per-subject progress joined with learner_mastery (topic-slug namespace)."""
    ensure_curriculum_tables()
    con = _connect()
    try:
        mastery_rows = con.execute(
            "SELECT topic_id, mastery_score, confidence_score, total_attempts,"
            " last_attempt_at FROM learner_mastery"
        ).fetchall()
        nodes = con.execute(
            "SELECT * FROM curriculum_nodes ORDER BY order_index, title COLLATE NOCASE"
        ).fetchall()
    finally:
        con.close()
    mastery_by_slug = {r["topic_id"]: _row_dict(r) for r in mastery_rows}
    by_parent: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for r in nodes:
        by_parent.setdefault(r["parent_id"], []).append(_row_dict(r))

    def topic_mastery(topic_node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return mastery_by_slug.get(slugify(topic_node["title"]))

    def summarize_topics(parent_id: str) -> Dict[str, Any]:
        topics = by_parent.get(parent_id, [])
        entries = []
        scores: List[float] = []
        studied = mastered = 0
        for t in topics:
            if t["node_type"] not in ("Topic", "Subtopic"):
                continue
            m = topic_mastery(t)
            score = m["mastery_score"] if m else None
            if score is not None:
                scores.append(score)
                studied += 1
                if score >= 70.0:
                    mastered += 1
            entries.append({
                "title": t["title"], "id": t["id"],
                "mastery": score, "attempts": m["total_attempts"] if m else 0,
                "last_attempt_at": m["last_attempt_at"] if m else None,
            })
        return {
            "topics": entries,
            "topics_total": len(entries),
            "topics_studied": studied,
            "topics_mastered": mastered,
            "avg_mastery": round(sum(scores) / len(scores), 1) if scores else None,
        }

    subjects_out: List[Dict[str, Any]] = []
    mapped_slugs: set = set()
    for semester in [n for n in nodes if n["node_type"] == "Semester"]:
        for subj in by_parent.get(semester["id"], []):
            if subj["node_type"] != "Subject":
                continue
            weeks_out = []
            for wk in by_parent.get(subj["id"], []):
                if wk["node_type"] != "Week":
                    continue
                week_summary = summarize_topics(wk["id"])
                seminars_out = []
                for semnr in by_parent.get(wk["id"], []):
                    if semnr["node_type"] != "Seminar":
                        continue
                    seminars_out.append({
                        "title": semnr["title"], "id": semnr["id"],
                        **summarize_topics(semnr["id"]),
                    })
                    for e in seminars_out[-1]["topics"]:
                        mapped_slugs.add(slugify(e["title"]))
                for e in week_summary["topics"]:
                    mapped_slugs.add(slugify(e["title"]))
                weeks_out.append({
                    "number": wk["order_index"], "title": wk["title"],
                    "id": wk["id"], "seminars": seminars_out,
                    **week_summary,
                })
            all_topic_entries = [
                e for w in weeks_out for e in w["topics"]
            ] + [
                e for w in weeks_out for s in w["seminars"] for e in s["topics"]
            ]
            scores = [e["mastery"] for e in all_topic_entries if e["mastery"] is not None]
            subjects_out.append({
                "title": subj["title"], "id": subj["id"],
                "semester": semester["title"],
                "weeks": weeks_out,
                "topics_total": len(all_topic_entries),
                "topics_studied": sum(1 for e in all_topic_entries if e["mastery"] is not None),
                "topics_mastered": sum(1 for e in all_topic_entries if (e["mastery"] or 0) >= 70.0),
                "avg_mastery": round(sum(scores) / len(scores), 1) if scores else None,
            })

    studied_unmapped = sorted(set(mastery_by_slug) - mapped_slugs)
    return {
        "subjects": subjects_out,
        "summary": {
            "subjects": len(subjects_out),
            "weeks": sum(len(s["weeks"]) for s in subjects_out),
            "topics": sum(s["topics_total"] for s in subjects_out),
            "topics_studied": sum(s["topics_studied"] for s in subjects_out),
            "topics_mastered": sum(s["topics_mastered"] for s in subjects_out),
            "studied_unmapped": studied_unmapped,
        },
    }


def detect_duplicates() -> List[Dict[str, Any]]:
    """Same-type siblings whose normalized titles collide (report only)."""
    ensure_curriculum_tables()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT id, parent_id, node_type, title FROM curriculum_nodes"
        ).fetchall()
    finally:
        con.close()
    groups: Dict[Tuple[str, Optional[str], str], List[Dict[str, Any]]] = {}
    for r in rows:
        d = _row_dict(r)
        groups.setdefault(
            (d["node_type"], d["parent_id"], title_key(d["title"])), []
        ).append(d)
    return [
        {"node_type": k[0], "parent_id": k[1], "title_key": k[2], "ids": [n["id"] for n in v],
         "titles": [n["title"] for n in v]}
        for k, v in sorted(groups.items()) if len(v) > 1
    ]
