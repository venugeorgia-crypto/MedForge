"""P10 — Publication / Review / Approval workflow (V11).

A formal content lifecycle over P9's canonical content and artifacts:
DRAFT → VALIDATING → READY → APPROVED → PUBLISHED → RETIRED, with BLOCKED and
NEEDS_REVIEW as gate-driven detours.

Design invariants:
- APPROVED is a recorded *judgement* granted only by approve_artifact() after
  nine executable gates pass (or every open issue is resolved by a reviewer).
- The medical-risk scan FORCES human review; automated checks never approve
  high-risk content by themselves.
- PUBLISHED requires APPROVED; exports record their mode and checksums.
- Nothing here re-implements an engine: evidence status is read from P4,
  curriculum from P2, consistency from P9's own report.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import medforge.types as T
from medforge.utils import slugify, utcnow

CONTENT_VERSION = "p10-publication-v1"

__all__ = [
    "PUBLICATION_VERSION",
    "HIGH_RISK_PATTERNS",
    "COPYRIGHT_EXCERPT_CHARS",
    "ensure_publication_tables",
    "run_approval_gates",
    "approve_artifact",
    "publish_artifact",
    "retire_artifact",
    "get_publication_state",
    "list_publication_queue",
    "export_bundle",
    "list_reviews",
    "resolve_review",
    "_scan_medical_risk",
    "_citation_labels",
]

PUBLICATION_VERSION = "p10-publication-v1"

# Bounded verbatim reuse for export: a longer verbatim run of a single source
# sentence/passage is treated as a copyright concern (gate 7).
COPYRIGHT_EXCERPT_CHARS = 400

# ─── Medical-risk patterns (deterministic, review-forcing — never approving) ───

HIGH_RISK_PATTERNS: List[Dict[str, Any]] = [
    {"issue": "drug_dose", "severity": "high",
     "pattern": r"\b\d+(?:[.,]\d+)?\s?(?:mg|mcg|µg|ug|g|IU|units?|mmol|mEq|ml|mL)\b(?:\s?(?:/|per)\s?(?:kg|day|dose|h|hr|hour|min))?\b"},
    {"issue": "drug_dose", "severity": "high",
     "pattern": r"\b(?:once|twice|three|four|\d+)\s?(?:daily|times?\s?(?:a\s)?day|hourly|weekly)\b"},
    {"issue": "contraindication", "severity": "medium",
     "pattern": r"\b(?:contraindicated|contraindications?|should not be (?:given|used|prescribed)|avoid in|is avoided in)\b"},
    {"issue": "emergency_treatment", "severity": "high",
     "pattern": r"\b(?:medical emergency|treat(?:ment)? (?:of|for) (?:shock|storm|crisis|arrest)|resuscitation|urgent(?:ly)? (?:treat|manage)|immediately? (?:give|administer))\b"},
    {"issue": "procedure", "severity": "high",
     "pattern": r"\b(?:incise|suture|cannulat\w*|intubat\w*|catheter(?:ize|ise)|puncture|inject(?:ion)? (?:of|into)|aspirat\w* (?:the|a))\b"},
    {"issue": "diagnostic_criteria", "severity": "medium",
     "pattern": r"\b(?:diagnos(?:is|tic) (?:criteria|requires|is made)|criterion|criteria for diagnosis)\b"},
    {"issue": "clinical_recommendation", "severity": "medium",
     "pattern": r"\b(?:patients? should(?: be)?|first[- ]line (?:therapy|treatment)|treat(?:ment)? of choice|recommended (?:dose|regimen|therapy)|initiate (?:therapy|treatment))\b"},
]


def _now() -> str:
    return utcnow()


def _uid(prefix: str) -> str:
    seed = f"{prefix}{_now()}{datetime.now(timezone.utc).timestamp()}"
    return f"{prefix}-{hashlib.sha1(seed.encode()).hexdigest()[:12]}"


def ensure_publication_tables() -> None:
    """Idempotently apply the V11 publication schema (candidate-walk import)."""
    here = Path(__file__).resolve()
    candidates = [str(T.BASE)] + [str(p) for p in here.parents]
    for base_str in candidates:
        if base_str not in sys.path:
            sys.path.insert(0, base_str)
        try:
            from core.database.migrate_v11 import ensure_publication_v11

            ensure_publication_v11(T.META_DB)
            return
        except ImportError:
            continue
    raise ImportError(
        "core.database.migrate_v11 not importable from: " + ", ".join(candidates)
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    return con


def _json_load(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


# ─── Artifact / content accessors (read P9's own tables) ───


def _artifact_rows(content_id: str, artifact_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """All artifact rows for a content item, ordered to match get_publication_state
    (artifact_type ASC, then newest version first) so lifecycle transitions and
    state reporting never disagree about which row was acted upon."""
    con = _connect()
    try:
        if artifact_type:
            rows = con.execute(
                "SELECT * FROM content_artifacts WHERE content_id=? AND artifact_type=?"
                " ORDER BY artifact_version DESC, artifact_id",
                (content_id, artifact_type)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM content_artifacts WHERE content_id=?"
                " ORDER BY artifact_type, artifact_version DESC, artifact_id",
                (content_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _artifact_row(content_id: str, artifact_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
    rows = _artifact_rows(content_id, artifact_type)
    return rows[0] if rows else None


def _content_row(content_id: str) -> Optional[Dict[str, Any]]:
    con = _connect()
    try:
        row = con.execute("SELECT * FROM content_items WHERE content_id=?",
                          (content_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["content"] = _json_load(d.get("content"), {})
        d["evidence_refs"] = _json_load(d.get("evidence_refs"), [])
        return d
    finally:
        con.close()


def _artifact_text(artifact: Dict[str, Any]) -> str:
    p = Path(artifact.get("path") or "")
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return ""


def _citation_labels(text: str) -> set:
    """Labels cited inline in rendered prose, e.g. [S1]."""
    return set(re.findall(r"\[(S\d+)\]", text or ""))


# ─── Gate 6: medical-risk scan ───


def _scan_medical_risk(text: str) -> List[Dict[str, Any]]:
    """Deterministic high-risk scan. Returns findings; NEVER approves."""
    findings: List[Dict[str, Any]] = []
    for spec in HIGH_RISK_PATTERNS:
        for match in re.finditer(spec["pattern"], text or "", re.IGNORECASE):
            start = max(0, match.start() - 60)
            findings.append({
                "issue_type": "medical_risk",
                "severity": spec["severity"],
                "detected_reason": f"{spec['issue']}: ...{text[start:match.end() + 40].strip()}...",
            })
            break  # one finding per pattern class is enough to force review
    return findings


# ─── Gate 7: copyright / export check ───


def _longest_verbatim_run(text: str, source_texts: List[str]) -> int:
    """Longest run of artifact text that appears verbatim in any single source."""
    norm_artifact = " ".join((text or "").split()).lower()
    longest = 0
    for src in source_texts:
        norm_src = " ".join((src or "").split()).lower()
        if not norm_src:
            continue
        # sliding window over artifact sentences per source
        for sentence in re.split(r"(?<=[.!?])\s+", norm_artifact):
            if len(sentence) > COPYRIGHT_EXCERPT_CHARS and sentence in norm_src:
                longest = max(longest, len(sentence))
    return longest


# ─── The nine gates ───


def run_approval_gates(content_id: str, artifact_type: Optional[str] = None,
                       now: Optional[str] = None) -> Dict[str, Any]:
    """Evaluate all nine approval gates and record the batch.

    Returns {"passed": bool, "gates": [...], "approval_id", "blocked": bool}.
    Gates never mutate content; they detect and record.
    """
    ensure_publication_tables()
    now = now or _now()
    item = _content_row(content_id)
    if not item:
        raise ValueError(f"Unknown content_id {content_id!r}")
    rows = _artifact_rows(content_id, artifact_type)
    if not rows:
        raise ValueError(f"No rendered artifact for content_id {content_id!r}")
    artifact = rows[0]  # deterministic primary row for approval_records
    gates: List[Dict[str, Any]] = []
    open_findings: List[Dict[str, Any]] = []

    def gate(num: int, name: str, passed: bool, severity: str, detail: str,
             finding: Optional[Dict[str, Any]] = None) -> None:
        row = {"gate": num, "name": name, "passed": bool(passed),
               "severity": severity, "detail": detail}
        gates.append(row)
        if finding:
            open_findings.append(finding)

    # 1. Curriculum alignment — P2 owns curriculum.
    node_id = item.get("curriculum_node_id")
    aligned = bool(node_id)
    if not aligned:
        try:
            from medforge.evidence import curriculum_node_id_for_topic

            aligned = curriculum_node_id_for_topic(item["topic"]) is not None
        except Exception:
            aligned = False
    gate(1, "curriculum_alignment", aligned, "medium",
         f"curriculum_node_id={node_id!r}" + ("" if aligned else " and topic unresolved in P2"),
         None if aligned else {
             "issue_type": "curriculum_gap", "severity": "medium",
             "detected_reason": "content topic does not resolve to a curriculum node"})

    # 2. Evidence coverage — every canonical fact cites something.
    facts = item.get("content", {}).get("key_facts", []) or []
    uncited = [f.get("fact", "")[:60] for f in facts if not (f.get("refs") or [])]
    gate(2, "evidence_coverage", not facts or not uncited, "high",
         f"{len(facts)} facts, {len(uncited)} uncited",
         None if not uncited else {
             "issue_type": "evidence_gap", "severity": "high",
             "detected_reason": f"canonical facts without evidence refs: {uncited[:3]}"})

    # 3. Semantic evidence status — read P4 verdicts for this content's claims.
    bad_status: List[str] = []
    try:
        from medforge.evidence import claims_list

        listing = claims_list(topic=item["topic"], limit=50) or {}
        for claim in (listing.get("claims") or []):
            status = str(claim.get("verification_status") or claim.get("status") or "").upper()
            if status in ("UNSUPPORTED", "CONTRADICTED"):
                bad_status.append(f"{claim.get('claim_id')}:{status}")
    except Exception as exc:  # P4 unreadable → cannot claim semantic support
        bad_status.append(f"P4 unreadable: {exc}")
    gate(3, "semantic_evidence_status", not bad_status, "high",
         "; ".join(bad_status[:3]) or "no unsupported/contradicted claims",
         None if not bad_status else {
             "issue_type": "unsupported_claim", "severity": "high",
             "detected_reason": f"P4 negative verdicts: {bad_status[:3]}"})

    # 4–9. Artifact-level gates: EVERY rendered artifact must pass, not just
    # the newest version — a defect in any derivative must block publication.
    # Precompute per-artifact results, then emit gates 4..9 in canonical order.
    source_texts = [r.get("text", "") for r in (item.get("evidence_refs") or [])]
    # Fall back to P3 chunk text via evidence_refs' chunk ids handled at render;
    # content_items.evidence_refs store locators, so also scan stored sources.
    if not source_texts:
        try:
            from medforge.retrieval import source_pack

            _t, srcs = source_pack(item["topic"], 6)
            source_texts = [s.get("text", "") for s in srcs]
        except Exception:
            source_texts = []
    known = {r.get("label") for r in item.get("evidence_refs", []) if r.get("label")}
    unknown_all: set = set()
    cite_rows: List[str] = []
    risk_all: List[Dict[str, Any]] = []
    longest = 0
    fmt_rows: List[str] = []
    fmt_ok = True
    tech_rows: List[str] = []
    tech_ok = True
    for artifact in rows:
        atype = artifact.get("artifact_type") or "?"
        text = _artifact_text(artifact)

        cited = _citation_labels(text)
        unknown = sorted(cited - known) if known else sorted(cited)
        unknown_all.update(unknown)
        if unknown:
            cite_rows.append(f"{atype}: {unknown[:6]}")

        for f in _scan_medical_risk(text):
            risk_all.append(dict(f, detected_by="gate",
                                 _artifact_id=artifact.get("artifact_id"),
                                 _detail=f"{atype}: {f['detected_reason']}"))

        run_len = _longest_verbatim_run(text, source_texts) if source_texts else 0
        longest = max(longest, run_len)

        p = Path(artifact.get("path") or "")
        on_disk = p.is_file() and p.stat().st_size > 0
        checksum_ok = False
        if on_disk:
            checksum_ok = (hashlib.sha256(p.read_bytes()).hexdigest()
                           == artifact.get("checksum"))
        fmt_ok = fmt_ok and on_disk and checksum_ok
        fmt_rows.append(f"{atype}: exists={on_disk} checksum_ok={checksum_ok}")

        a_ok = artifact.get("status") in ("READY", "APPROVED", "PUBLISHED") and bool(
            artifact.get("render_mode"))
        tech_ok = tech_ok and a_ok
        tech_rows.append(f"{atype}: status={artifact.get('status')!r}"
                         f" render_mode={artifact.get('render_mode')!r}")

    gate(4, "citation_continuity", not unknown_all, "high",
         f"cited across {len(rows)} artifact(s); unknown={sorted(unknown_all)[:6]}"
         if unknown_all else f"cited across {len(rows)} artifact(s); no unknown labels",
         None if not unknown_all else {
             "issue_type": "citation_break", "severity": "high",
             "detected_reason": f"artifact cites labels absent from content item: {cite_rows[0]}"})

    # 5. Content consistency — P9's own report re-verifies checksums.
    try:
        from medforge.content import content_consistency_report

        report = content_consistency_report(content_id)
        consistent = report.get("consistent") is True
        detail = f"consistent={report.get('consistent')} artifacts={len(report.get('artifacts', []))}"
    except Exception as exc:
        consistent, detail = False, f"consistency report failed: {exc}"
    gate(5, "content_consistency", consistent, "high", detail,
         None if consistent else {
             "issue_type": "consistency", "severity": "high",
             "detected_reason": detail})

    # 6. Medical-risk scan — deterministic, review-forcing.
    gate(6, "medical_risk_scan", not risk_all, "high",
         "; ".join(f["_detail"][:80] for f in risk_all)
         or f"no high-risk patterns in {len(rows)} artifact(s)",
         risk_all[0] if risk_all else None)
    for extra in risk_all[1:4]:
        open_findings.append(dict(extra))

    # 7. Copyright / export check — bounded verbatim reuse.
    gate(7, "copyright_export", longest <= COPYRIGHT_EXCERPT_CHARS, "high",
         f"longest verbatim run={longest} chars (bound {COPYRIGHT_EXCERPT_CHARS})",
         None if longest <= COPYRIGHT_EXCERPT_CHARS else {
             "issue_type": "copyright", "severity": "high",
             "detected_reason": f"{longest}-char verbatim run exceeds excerpt bound"})

    # 8. Formatting validation — files exist, non-empty, checksums match.
    gate(8, "formatting_validation", fmt_ok, "medium",
         "; ".join(fmt_rows),
         None if fmt_ok else {
             "issue_type": "formatting", "severity": "medium",
             "detected_reason": "artifact file missing, empty, or checksum mismatch"})

    # 9. Artifact generation success — rendered deterministically with status.
    gate(9, "artifact_generation", tech_ok, "medium",
         "; ".join(tech_rows),
         None if tech_ok else {
             "issue_type": "formatting", "severity": "medium",
             "detected_reason": f"artifact status {artifact.get('status')!r} is not publication-eligible"})

    passed = all(g["passed"] for g in gates)
    blocked = any((not g["passed"]) and g["severity"] in ("high", "critical") for g in gates)

    # Record findings in the review queue (idempotent per content+type+reason).
    con = _connect()
    try:
        for finding in open_findings:
            reason = finding.get("detected_reason", "")[:400]
            dup = con.execute(
                "SELECT 1 FROM review_queue WHERE content_id=? AND issue_type=?"
                " AND detected_reason=? AND review_status='open'",
                (content_id, finding.get("issue_type", "consistency"), reason)).fetchone()
            if dup:
                continue
            rid = _uid("rev")
            con.execute(
                "INSERT INTO review_queue (review_id, content_id, artifact_id, issue_type,"
                " severity, detected_reason, detected_by, review_status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,'open',?,?)",
                (rid, content_id, finding.get("_artifact_id") or artifact.get("artifact_id"),
                 finding.get("issue_type", "consistency"),
                 finding.get("severity", "medium"), reason,
                 finding.get("detected_by", "gate"), now, now))
        con.commit()
    finally:
        con.close()

    # Record the gate batch itself.
    approval_id = _uid("appr")
    con = _connect()
    try:
        con.execute(
            "INSERT INTO approval_records (approval_id, content_id, artifact_id,"
            " requested_status, gates, passed, created_at) VALUES (?,?,?,?,?,?,?)",
            (approval_id, content_id, artifact.get("artifact_id"), "APPROVED",
             _json_dump(gates), 1 if passed else 0, now))
        if blocked:
            con.execute(
                "UPDATE content_artifacts SET publication_status='BLOCKED'"
                " WHERE content_id=?", (content_id,))
        con.commit()
    finally:
        con.close()

    return {"approval_id": approval_id, "passed": passed, "blocked": blocked,
            "gates": gates, "content_id": content_id,
            "artifact_id": artifact.get("artifact_id"), "recorded_at": now}


# ─── Lifecycle transitions ───


def approve_artifact(content_id: str, reviewer: str, artifact_type: Optional[str] = None,
                     now: Optional[str] = None) -> Dict[str, Any]:
    """Grant APPROVED after gates pass and no blocking review is open."""
    ensure_publication_tables()
    now = now or _now()
    if not reviewer or not str(reviewer).strip():
        raise ValueError("approve_artifact requires a named reviewer")
    gates = run_approval_gates(content_id, artifact_type, now=now)
    artifact = _artifact_row(content_id, artifact_type)
    con = _connect()
    try:
        blocking = con.execute(
            "SELECT COUNT(*) FROM review_queue WHERE content_id=? AND review_status='open'"
            " AND severity IN ('high','critical')", (content_id,)).fetchone()[0]
        if blocking:
            return {"approved": False, "reason": f"{blocking} open high-severity review(s)",
                    "gates": gates["gates"]}
        if not gates["passed"]:
            return {"approved": False, "reason": "approval gates not all passed",
                    "gates": gates["gates"]}
        con.execute(
            "UPDATE content_artifacts SET publication_status='APPROVED', approved_at=?,"
            " approved_by=? WHERE content_id=? AND (? IS NULL OR artifact_type=?)",
            (now, reviewer.strip(), content_id, artifact_type, artifact_type))
        con.execute(
            "UPDATE approval_records SET reviewer=?, approved_at=? WHERE approval_id=?",
            (reviewer.strip(), now, gates["approval_id"]))
        con.commit()
    finally:
        con.close()
    return {"approved": True, "content_id": content_id,
            "artifact_id": artifact["artifact_id"],
            "artifact_ids": [a["artifact_id"] for a in _artifact_rows(content_id, artifact_type)],
            "reviewer": reviewer.strip(),
            "approved_at": now, "gates": gates["gates"]}


def publish_artifact(content_id: str, mode: str = "distributable",
                     artifact_type: Optional[str] = None,
                     now: Optional[str] = None) -> Dict[str, Any]:
    """PUBLISH an APPROVED artifact by writing its export bundle."""
    ensure_publication_tables()
    now = now or _now()
    if mode not in ("private", "distributable"):
        raise ValueError("mode must be 'private' or 'distributable'")
    rows = _artifact_rows(content_id, artifact_type)
    if not rows:
        raise ValueError(f"No artifact for content_id {content_id!r}")
    unapproved = [r for r in rows if r.get("publication_status") != "APPROVED"]
    if unapproved:
        return {"published": False,
                "reason": f"publication_status={unapproved[0].get('publication_status')!r};"
                          " PUBLISHED requires APPROVED"}
    bundle = export_bundle(content_id, mode=mode, artifact_type=artifact_type, now=now)
    if not bundle.get("written"):
        return {"published": False, "reason": bundle.get("reason", "export failed")}
    con = _connect()
    try:
        con.execute(
            "UPDATE content_artifacts SET publication_status='PUBLISHED', published_at=?,"
            " published_path=? WHERE content_id=? AND (? IS NULL OR artifact_type=?)",
            (now, bundle["manifest_path"], content_id, artifact_type, artifact_type))
        con.commit()
    finally:
        con.close()
    return {"published": True, "mode": mode, "artifact_id": rows[0]["artifact_id"],
            "artifact_ids": [r["artifact_id"] for r in rows],
            "manifest": bundle["manifest_path"], "published_at": now}


def retire_artifact(content_id: str, reason: str, artifact_type: Optional[str] = None,
                    now: Optional[str] = None) -> Dict[str, Any]:
    ensure_publication_tables()
    now = now or _now()
    rows = _artifact_rows(content_id, artifact_type)
    if not rows:
        raise ValueError(f"No artifact for content_id {content_id!r}")
    con = _connect()
    try:
        con.execute(
            "UPDATE content_artifacts SET publication_status='RETIRED', retired_at=?,"
            " retired_reason=? WHERE content_id=? AND (? IS NULL OR artifact_type=?)",
            (now, (reason or "")[:400], content_id, artifact_type, artifact_type))
        con.commit()
    finally:
        con.close()
    return {"retired": True, "artifact_id": rows[0]["artifact_id"],
            "artifact_ids": [r["artifact_id"] for r in rows], "reason": reason}


def get_publication_state(content_id: str) -> Dict[str, Any]:
    """Lifecycle state for every artifact version of a content item."""
    ensure_publication_tables()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT artifact_id, artifact_type, artifact_version, status, publication_status,"
            " approved_at, approved_by, published_at, published_path, retired_at, retired_reason,"
            " path, checksum FROM content_artifacts WHERE content_id=?"
            " ORDER BY artifact_type, artifact_version", (content_id,)).fetchall()
        open_reviews = con.execute(
            "SELECT review_id, issue_type, severity, detected_reason, review_status"
            " FROM review_queue WHERE content_id=? ORDER BY created_at DESC", (content_id,)).fetchall()
    finally:
        con.close()
    artifacts = [dict(r) for r in rows]
    overall = "UNREVIEWED"
    if artifacts:
        statuses = {a["publication_status"] or "UNREVIEWED" for a in artifacts}
        if "BLOCKED" in statuses:
            overall = "BLOCKED"
        elif "PUBLISHED" in statuses:
            overall = "PUBLISHED"
        elif "APPROVED" in statuses:
            overall = "APPROVED"
        elif "RETIRED" in statuses and statuses <= {"RETIRED", "UNREVIEWED"}:
            overall = "RETIRED"
    return {"content_id": content_id, "artifacts": artifacts,
            "overall_publication_status": overall,
            "open_reviews": [dict(r) for r in open_reviews],
            "publication_version": PUBLICATION_VERSION}


def list_publication_queue(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    ensure_publication_tables()
    con = _connect()
    try:
        if status:
            rows = con.execute(
                "SELECT * FROM review_queue WHERE review_status=? ORDER BY created_at DESC LIMIT ?",
                (status, max(1, limit))).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM review_queue ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ─── Review queue ───


def resolve_review(review_id: str, reviewer: str, resolution: str,
                   note: str = "", now: Optional[str] = None) -> Dict[str, Any]:
    """Record a reviewer decision (append-only history + queue update)."""
    ensure_publication_tables()
    now = now or _now()
    if not reviewer or not str(reviewer).strip():
        raise ValueError("resolve_review requires a named reviewer")
    if resolution not in ("resolved", "waived"):
        raise ValueError("resolution must be 'resolved' or 'waived'")
    con = _connect()
    try:
        row = con.execute("SELECT * FROM review_queue WHERE review_id=?",
                          (review_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown review_id {review_id!r}")
        con.execute(
            "INSERT INTO review_history (review_id, action, reviewer, note, created_at)"
            " VALUES (?,?,?,?,?)",
            (review_id, resolution, reviewer.strip(), note or "", now))
        con.execute(
            "UPDATE review_queue SET review_status=?, reviewer=?, reviewed_at=?,"
            " resolution=?, updated_at=? WHERE review_id=?",
            (resolution, reviewer.strip(), now, (note or "")[:400], now, review_id))
        # If this was the last blocking issue on a BLOCKED artifact, return it to
        # NEEDS_REVIEW (a full gate run is still required before APPROVED).
        content_id = row["content_id"]
        remaining = con.execute(
            "SELECT COUNT(*) FROM review_queue WHERE content_id=? AND review_status='open'"
            " AND severity IN ('high','critical')", (content_id,)).fetchone()[0]
        unblocked: Optional[str] = None
        if remaining == 0:
            con.execute(
                "UPDATE content_artifacts SET publication_status='NEEDS_REVIEW'"
                " WHERE content_id=? AND publication_status='BLOCKED'", (content_id,))
            unblocked = content_id
        con.commit()
    finally:
        con.close()
    return {"review_id": review_id, "review_status": resolution,
            "reviewer": reviewer.strip(), "resolved_at": now,
            "unblocked_content_id": unblocked}


def list_reviews(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    return list_publication_queue(status=status, limit=limit)


# ─── Export bundles ───


def export_bundle(content_id: str, mode: str = "private",
                  artifact_type: Optional[str] = None,
                  now: Optional[str] = None) -> Dict[str, Any]:
    """Write a private or distributable bundle for a content item.

    private: canonical content + bounded evidence excerpts + validation JSON.
    distributable: rendered artifacts + reference locators ONLY — asserted to
    contain no learner fields, no prompts, and no long verbatim source runs.
    """
    ensure_publication_tables()
    now = now or _now()
    if mode not in ("private", "distributable"):
        raise ValueError("mode must be 'private' or 'distributable'")
    item = _content_row(content_id)
    if not item:
        raise ValueError(f"Unknown content_id {content_id!r}")
    artifacts = []
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM content_artifacts WHERE content_id=?"
            " ORDER BY artifact_type, artifact_version", (content_id,)).fetchall()
        latest: Dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            if artifact_type and d["artifact_type"] != artifact_type:
                continue
            latest[d["artifact_type"]] = d
        artifacts = list(latest.values())
    finally:
        con.close()
    if not artifacts:
        return {"written": False, "reason": "no rendered artifacts"}

    outdir = T.PRODUCTS / slugify(item["topic"]) / "exports" / mode
    outdir.mkdir(parents=True, exist_ok=True)
    copied: List[Dict[str, Any]] = []

    LEARNER_KEYS = ("learner", "mastery", "learner_key", "confidence", "transcript",
                    "history", "answers", "session_id", "tutor_session", "prompt")

    for a in artifacts:
        src = Path(a.get("path") or "")
        if not src.is_file():
            return {"written": False, "reason": f"artifact file missing: {src}"}
        text = src.read_text(encoding="utf-8")
        if mode == "distributable":
            low = text.lower()
            for key in LEARNER_KEYS:
                if re.search(rf"\b{key}\b\s*:", low):
                    return {"written": False,
                            "reason": f"distributable export refused: learner/prompt field {key!r} in {src.name}"}
            longest = _longest_verbatim_run(text, [
                s.get("text", "") for s in (item.get("evidence_refs") or [])])
            if longest > COPYRIGHT_EXCERPT_CHARS:
                return {"written": False,
                        "reason": f"distributable export refused: {longest}-char verbatim run"}
        dest = outdir / src.name
        dest.write_text(text, encoding="utf-8")
        copied.append({"file": dest.name, "artifact_id": a["artifact_id"],
                       "artifact_type": a["artifact_type"],
                       "artifact_version": a["artifact_version"],
                       "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()})

    manifest: Dict[str, Any] = {
        "content_id": content_id, "topic": item["topic"], "mode": mode,
        "exported_at": now, "publication_version": PUBLICATION_VERSION,
        "files": copied,
        "references": [{"label": r.get("label"), "source": r.get("source"),
                        "locator": r.get("locator")}
                       for r in (item.get("evidence_refs") or [])],
    }
    if mode == "private":
        manifest["canonical_content"] = item["content"]
        manifest["note"] = "PRIVATE: contains canonical content and evidence detail; do not distribute"
    else:
        manifest["note"] = ("DISTRIBUTABLE: rendered artifacts + reference locators only;"
                            " no learner data, prompts, or long excerpts")
    manifest_path = outdir / "manifest.json"
    manifest_path.write_text(_json_dump(manifest), encoding="utf-8")
    return {"written": True, "mode": mode, "content_id": content_id,
            "outdir": str(outdir), "manifest_path": str(manifest_path),
            "files": copied, "exported_at": now}
