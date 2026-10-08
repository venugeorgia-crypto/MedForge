"""P13 — Knowledge Refresh + Source Update System (p13-refresh-v1, migration 14.0.0).

Detects when registered sources change, re-verifies the claims that depended on
them through P4's own verifier, records every check in an audit trail, and
raises notifications (never silently publishes changed evidence).

    sources → check_textbook_updates / check_pubmed_updates / check_web_updates
    → changed sources → reverify_claims_for_document (P4 verify_claim)
    → knowledge_refresh_runs + source_refresh_log + knowledge_notifications

Design rules:
- No duplicated domain logic: textbook registration/ingestion is P3's; claim
  verification is P4's; this module only decides *what* to re-check and records
  *why*.
- Network sources are opt-in per call and always skipped under MEDFORGE_OFFLINE.
- Every check is logged; changes create notifications; nothing is deleted.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import medforge.types as T
from medforge.utils import utcnow

REFRESH_VERSION = "p13-refresh-v1"

# Deterministic ordering of P4 verdicts for upgrade/downgrade accounting.
CLAIM_STATUS_RANK: Dict[str, int] = {
    "SUPPORTED": 3,
    "HUMAN_REVIEWED": 3,
    "PARTIALLY_SUPPORTED": 2,
    "PENDING": 1,
    "INSUFFICIENT_EVIDENCE": 1,
    "UNSUPPORTED": 0,
    "CONTRADICTED": 0,
    "NOT_FACTUAL": 3,
}

REFRESH_SOURCE_TYPES = ("textbook", "pubmed", "web", "course_pdf")
REFRESH_SEVERITY_FOR_SOURCE = {
    "textbook": "warning",
    "course_pdf": "warning",
    "pubmed": "info",
    "web": "info",
}

# Bounded work per run (8 GB M1): never crawl every chunk of a new edition.
MAX_CHUNKS_SCANNED = 400
MAX_CANDIDATES_PER_CLAIM = 3
MAX_CLAIMS_PER_DOCUMENT = 25


# ─── Table ensure (candidate-walk import, mirrors publication.py) ───

def ensure_refresh_tables() -> None:
    """Idempotently apply the V14 knowledge-refresh schema."""
    here = Path(__file__).resolve()
    candidates = [str(T.BASE)] + [str(p) for p in here.parents]
    for base_str in candidates:
        if base_str not in sys.path:
            sys.path.insert(0, base_str)
        try:
            from core.database.migrate_v14 import ensure_refresh_v14

            ensure_refresh_v14(T.META_DB, create_backup=False)
            return
        except ImportError:
            continue
    raise ImportError(
        "core.database.migrate_v14 not importable from: " + ", ".join(candidates)
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _uid(prefix: str) -> str:
    """Collision-free id: uuid4 entropy (same-second checks must not clash)."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _last_check(con: sqlite3.Connection, source_type: str, source_id: str) -> Optional[Dict[str, Any]]:
    # rowid is insertion-ordered: the true "last check" even within one second.
    row = con.execute(
        "SELECT * FROM source_refresh_log WHERE source_type=? AND source_id=?"
        " ORDER BY rowid DESC LIMIT 1",
        (source_type, source_id),
    ).fetchone()
    return dict(row) if row else None


def _record_check(
    con: sqlite3.Connection,
    source_type: str,
    source_id: str,
    status: str,
    changes: int = 0,
    content_hash: Optional[str] = None,
    etag: Optional[str] = None,
    pmid_list: Optional[List[str]] = None,
    error: Optional[str] = None,
) -> str:
    refresh_id = _uid("ref")
    now = utcnow()
    con.execute(
        """INSERT INTO source_refresh_log
           (refresh_id, source_type, source_id, last_checked_at, last_content_hash,
            last_etag, last_pmid_list, status, changes_detected, error_msg, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (refresh_id, source_type, source_id, now, content_hash, etag,
         json.dumps(pmid_list) if pmid_list is not None else None,
         status, int(changes), error, now),
    )
    return refresh_id


# ─── Textbook updates (file-hash based, fully offline) ───

def _latest_edition(con: sqlite3.Connection, document_id: str) -> Optional[Dict[str, Any]]:
    """The most recently INSERTED edition (rowid tie-break: two editions can
    share a created_at second, and edition ids are hashes, not ordered)."""
    row = con.execute(
        "SELECT * FROM textbook_editions WHERE document_id=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (document_id,),
    ).fetchone()
    return dict(row) if row else None


def diff_textbook_editions(old_edition_id: str, new_edition_id: str) -> Dict[str, Any]:
    """Chunk-level diff between two editions (text-hash sets; nothing deleted)."""
    ensure_refresh_tables()
    con = _connect()
    try:
        def hashes(edition_id: str):
            return {
                r["text_hash"]
                for r in con.execute(
                    "SELECT text_hash FROM textbook_chunks WHERE edition_id=?", (edition_id,)
                )
            }

        old, new = hashes(old_edition_id), hashes(new_edition_id)
        pages_old = {
            r[0] for r in con.execute(
                "SELECT page_number FROM textbook_pages WHERE edition_id=?", (old_edition_id,))
        }
        pages_new = {
            r[0] for r in con.execute(
                "SELECT page_number FROM textbook_pages WHERE edition_id=?", (new_edition_id,))
        }
        return {
            "added_chunks": len(new - old),
            "removed_chunks": len(old - new),
            "unchanged_chunks": len(old & new),
            "pages_added": len(pages_new - pages_old),
            "pages_removed": len(pages_old - pages_new),
        }
    finally:
        con.close()


def check_textbook_updates(
    register_changes: bool = True,
    ingest_new: bool = True,
    embed_text: bool = False,
) -> List[Dict[str, Any]]:
    """Compare each registered textbook's file hash against its latest edition.

    A changed file is registered as a NEW edition through P3 (old edition is
    never overwritten) and, when ``ingest_new``, ingested keyword-only by
    default so refresh stays offline-safe. Every check is logged.
    """
    ensure_refresh_tables()
    from medforge import textbook as TB

    results: List[Dict[str, Any]] = []
    con = _connect()
    try:
        docs = [dict(r) for r in con.execute(
            "SELECT * FROM textbook_documents ORDER BY title COLLATE NOCASE"
        )]
    finally:
        con.close()

    for doc in docs:
        document_id = doc["id"]
        con = _connect()
        try:
            edition = _latest_edition(con, document_id)
            if edition is None:
                _record_check(con, "textbook", document_id, "skipped",
                              error="no edition registered")
                con.commit()
                results.append({"source_type": "textbook", "source_id": document_id,
                                "status": "skipped", "reason": "no edition registered"})
                continue
            source_path = Path(edition.get("source_path") or "")
            if not source_path or not source_path.is_file():
                _record_check(con, "textbook", document_id, "skipped",
                              content_hash=edition.get("content_hash"),
                              error="source file not available on disk")
                con.commit()
                results.append({"source_type": "textbook", "source_id": document_id,
                                "status": "skipped",
                                "reason": "source file not available on disk"})
                continue
            current_hash = _sha256_file(source_path)
            old_edition_id = edition["id"]
            if current_hash == edition.get("content_hash"):
                _record_check(con, "textbook", document_id, "ok",
                              content_hash=current_hash, changes=0)
                con.commit()
                results.append({"source_type": "textbook", "source_id": document_id,
                                "title": doc["title"], "status": "ok",
                                "edition_id": old_edition_id, "changes_detected": 0})
                continue

            entry: Dict[str, Any] = {
                "source_type": "textbook", "source_id": document_id,
                "title": doc["title"], "status": "changed",
                "old_edition_id": old_edition_id,
                "old_content_hash": edition.get("content_hash"),
                "new_content_hash": current_hash,
                "changes_detected": 1,
            }
            if register_changes:
                # Re-register with the STORED document metadata so the new
                # content hash attaches to the same document identity (a bare
                # path would re-derive metadata from the filename and split the
                # document in two).
                registration = TB.register_textbook(
                    str(source_path),
                    title=doc.get("title") or None,
                    authors=doc.get("authors") or None,
                    publisher=doc.get("publisher") or None,
                    edition=doc.get("edition_label") or None,
                    publication_year=doc.get("publication_year") or None,
                    isbn=doc.get("isbn") or None,
                    subject=doc.get("subject") or None,
                    source_type=doc.get("source_type") or "textbook",
                )
                new_edition = registration["edition"]
                entry["new_edition_id"] = new_edition["id"]
                if ingest_new:
                    TB.ingest_textbook(new_edition["id"], embed_text=embed_text)
                    entry["diff"] = diff_textbook_editions(old_edition_id, new_edition["id"])
            _record_check(con, "textbook", document_id, "changed",
                          content_hash=current_hash, changes=1)
            con.commit()
            results.append(entry)
        except Exception as exc:  # honest: record the error, never swallow silently
            try:
                _record_check(con, "textbook", document_id, "error", error=str(exc))
                con.commit()
            except Exception:
                pass
            results.append({"source_type": "textbook", "source_id": document_id,
                            "status": "error", "error": str(exc)})
        finally:
            con.close()
    return results


# ─── PubMed updates (injectable fetcher; skipped offline) ───

def _default_pubmed_pmids(query: str, retmax: int = 20) -> List[str]:
    """E-utilities esearch → PMID list. Network; never called offline."""
    import requests

    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    common: Dict[str, str] = {"tool": "MedForge"}
    if T.NCBI_EMAIL:
        common["email"] = T.NCBI_EMAIL
    r = requests.get(base + "/esearch.fcgi", params={
        **common, "db": "pubmed", "term": query, "retmode": "json",
        "retmax": retmax, "sort": "date",
    }, timeout=30)
    r.raise_for_status()
    return list(r.json().get("esearchresult", {}).get("idlist", []))


def check_pubmed_updates(
    queries: List[Any],
    fetcher: Optional[Callable[[str, int], List[str]]] = None,
    retmax: int = 20,
) -> List[Dict[str, Any]]:
    """Detect new PMIDs for each query since its last logged check."""
    ensure_refresh_tables()
    if T.OFFLINE:
        return [{"source_type": "pubmed", "source_id": q if isinstance(q, str) else q.get("query", ""),
                 "status": "skipped", "reason": "offline mode"} for q in queries]
    fetch = fetcher or _default_pubmed_pmids
    results: List[Dict[str, Any]] = []
    for item in queries:
        query = item if isinstance(item, str) else str(item.get("query", ""))
        if not query:
            continue
        con = _connect()
        try:
            previous = _last_check(con, "pubmed", query)
            known: List[str] = []
            if previous and previous.get("last_pmid_list"):
                try:
                    known = json.loads(previous["last_pmid_list"]) or []
                except (TypeError, ValueError):
                    known = []
            try:
                current = [str(p) for p in fetch(query, retmax)]
            except Exception as exc:
                _record_check(con, "pubmed", query, "error", error=str(exc))
                con.commit()
                results.append({"source_type": "pubmed", "source_id": query,
                                "status": "error", "error": str(exc)})
                continue
            new_pmids = [p for p in current if p not in set(known)]
            status = "changed" if (known and new_pmids) else "ok"
            _record_check(con, "pubmed", query, status, changes=len(new_pmids),
                          pmid_list=current)
            con.commit()
            results.append({"source_type": "pubmed", "source_id": query,
                            "status": status, "total_pmids": len(current),
                            "new_pmids": new_pmids,
                            "changes_detected": len(new_pmids) if known else 0,
                            "baseline": not bool(known)})
        finally:
            con.close()
    return results


# ─── Web updates (conditional fetch via injectable fetcher) ───

def _default_web_fetcher(url: str, etag: Optional[str]) -> Dict[str, Any]:
    """Conditional GET for trusted domains; SSRF checks reuse P1's validator."""
    import requests
    from medforge.ingestion import _resolve_and_validate

    _resolve_and_validate(url)  # raises before any request when unsafe
    headers = {"User-Agent": "MedForge/2.1 (research)"}
    if etag:
        headers["If-None-Match"] = etag
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 304:
        return {"status": 304, "text": "", "etag": etag, "final_url": url}
    r.raise_for_status()
    return {"status": 200, "text": r.text, "etag": r.headers.get("ETag"),
            "final_url": str(r.url)}


def check_web_updates(
    urls: List[Any],
    fetcher: Optional[Callable[[str, Optional[str]], Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Conditional-GET each URL; 304 means unchanged, a new body hash means changed."""
    ensure_refresh_tables()
    if T.OFFLINE:
        return [{"source_type": "web", "source_id": u if isinstance(u, str) else u.get("url", ""),
                 "status": "skipped", "reason": "offline mode"} for u in urls]
    fetch = fetcher or _default_web_fetcher
    results: List[Dict[str, Any]] = []
    for item in urls:
        url = item if isinstance(item, str) else str(item.get("url", ""))
        if not url:
            continue
        con = _connect()
        try:
            previous = _last_check(con, "web", url)
            last_etag = previous.get("last_etag") if previous else None
            last_hash = previous.get("last_content_hash") if previous else None
            try:
                fetched = fetch(url, last_etag)
            except Exception as exc:
                _record_check(con, "web", url, "error", error=str(exc))
                con.commit()
                results.append({"source_type": "web", "source_id": url,
                                "status": "error", "error": str(exc)})
                continue
            if int(fetched.get("status", 200)) == 304:
                _record_check(con, "web", url, "ok", content_hash=last_hash,
                              etag=last_etag, changes=0)
                con.commit()
                results.append({"source_type": "web", "source_id": url,
                                "status": "ok", "changes_detected": 0,
                                "not_modified": True})
                continue
            body = fetched.get("text", "") or ""
            content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            changed = bool(last_hash) and content_hash != last_hash
            status = "changed" if changed else "ok"
            _record_check(con, "web", url, status, content_hash=content_hash,
                          etag=fetched.get("etag"), changes=1 if changed else 0)
            con.commit()
            results.append({"source_type": "web", "source_id": url, "status": status,
                            "changes_detected": 1 if changed else 0,
                            "final_url": fetched.get("final_url", url)})
        finally:
            con.close()
    return results


# ─── Claim re-verification (P4 owns verification) ───

def _token_set(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())}


def _candidates_from_edition(con: sqlite3.Connection, edition_id: str,
                             claim_text: str, document_title: str,
                             limit: int = MAX_CANDIDATES_PER_CLAIM) -> List[Dict[str, Any]]:
    """Bounded, deterministic best-overlap chunks from the new edition."""
    rows = con.execute(
        "SELECT id, text, locator, page_number FROM textbook_chunks"
        " WHERE edition_id=? LIMIT ?", (edition_id, MAX_CHUNKS_SCANNED),
    ).fetchall()
    claim_tokens = _token_set(claim_text)
    scored = []
    for r in rows:
        overlap = len(claim_tokens & _token_set(r["text"]))
        scored.append((overlap, -len(r["text"] or ""), r["id"], r))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    out = []
    for overlap, _, _, r in scored[:limit]:
        if overlap <= 0:
            continue
        out.append({
            "id": r["id"], "text": r["text"],
            "source": document_title or "Textbook",
            "locator": r["locator"] or f"p. {r['page_number']}",
            "kind": "textbook", "url": "", "quality": 0.95,
        })
    return out


def reverify_claims_for_document(
    document_id: str,
    verifier_fn: Optional[Callable[[str, str, Dict[str, Any]], str]] = None,
    model: Optional[str] = None,
    max_claims: int = MAX_CLAIMS_PER_DOCUMENT,
) -> Dict[str, Any]:
    """Re-verify claims whose evidence came from this document, against the
    latest edition. Uses P4's verify_claim — no second verification system."""
    ensure_refresh_tables()
    from medforge import evidence as E

    con = _connect()
    try:
        doc = con.execute("SELECT title FROM textbook_documents WHERE id=?",
                          (document_id,)).fetchone()
        title = doc["title"] if doc else ""
        edition = _latest_edition(con, document_id)
        if edition is None:
            return {"document_id": document_id, "reverified": 0, "reason": "no edition"}
        claims = con.execute(
            """SELECT DISTINCT c.claim_id, c.claim_text, c.verification_status
               FROM claims c
               JOIN claim_evidence ce ON ce.claim_id = c.claim_id
               JOIN evidence e ON e.evidence_id = ce.evidence_id
               WHERE e.document_id = ?
               LIMIT ?""",
            (document_id, max_claims),
        ).fetchall()
        prepared = []
        for c in claims:
            prepared.append({
                "claim_id": c["claim_id"],
                "claim_text": c["claim_text"],
                "before": c["verification_status"],
                "candidates": _candidates_from_edition(
                    con, edition["id"], c["claim_text"], title),
            })
    finally:
        con.close()

    reverified = upgraded = downgraded = 0
    details: List[Dict[str, Any]] = []
    for item in prepared:
        try:
            outcome = E.verify_claim(item["claim_id"], model=model,
                                     verifier_fn=verifier_fn,
                                     candidates=item["candidates"])
        except Exception as exc:
            details.append({"claim_id": item["claim_id"], "error": str(exc)})
            continue
        after = outcome.get("status")
        before_rank = CLAIM_STATUS_RANK.get(item["before"], 1)
        after_rank = CLAIM_STATUS_RANK.get(after, 1)
        if after_rank > before_rank:
            upgraded += 1
        elif after_rank < before_rank:
            downgraded += 1
        reverified += 1
        details.append({"claim_id": item["claim_id"], "before": item["before"],
                        "after": after, "candidates": len(item["candidates"])})
    return {
        "document_id": document_id,
        "edition_id": edition["id"],
        "claims_considered": len(prepared),
        "reverified": reverified,
        "upgraded": upgraded,
        "downgraded": downgraded,
        "details": details,
    }


# ─── Orchestration ───

def run_refresh(
    triggered_by: str = "manual",
    sources: Optional[List[str]] = None,
    pubmed_queries: Optional[List[Any]] = None,
    web_urls: Optional[List[Any]] = None,
    verifier_fn: Optional[Callable[[str, str, Dict[str, Any]], str]] = None,
    model: Optional[str] = None,
    register_changes: bool = True,
    ingest_new: bool = True,
    embed_text: bool = False,
) -> Dict[str, Any]:
    """One refresh run: check sources → re-verify affected claims → notify.

    Network sources (pubmed, web) require explicit queries/urls and are always
    skipped under MEDFORGE_OFFLINE. Records a knowledge_refresh_runs row;
    failures mark the run failed with the error message.
    """
    if triggered_by not in ("scheduled", "manual", "webhook"):
        raise ValueError("triggered_by must be scheduled|manual|webhook")
    ensure_refresh_tables()
    wanted = [s for s in (sources or ["textbook", "pubmed", "web"])]
    if T.OFFLINE:
        wanted = [s for s in wanted if s in ("textbook", "course_pdf")]

    run_id = _uid("krr")
    started = utcnow()
    con = _connect()
    try:
        con.execute(
            """INSERT INTO knowledge_refresh_runs
               (run_id, triggered_by, started_at, status)
               VALUES (?,?,?, 'running')""",
            (run_id, triggered_by, started),
        )
        con.commit()
    finally:
        con.close()

    changes: List[Dict[str, Any]] = []
    reverify_summary: List[Dict[str, Any]] = []
    notifications = 0
    errors: List[str] = []
    try:
        if "textbook" in wanted or "course_pdf" in wanted:
            textbook_results = check_textbook_updates(
                register_changes=register_changes, ingest_new=ingest_new,
                embed_text=embed_text)
            changes.extend(textbook_results)
            for entry in textbook_results:
                if entry.get("status") == "changed" and register_changes:
                    summary = reverify_claims_for_document(
                        entry["source_id"], verifier_fn=verifier_fn, model=model)
                    reverify_summary.append(summary)
                    notifications += _notify_for_change(
                        run_id, "textbook", entry["source_id"],
                        summary="textbook file changed; new edition registered",
                        affected=summary.get("reverified", 0))
        if "pubmed" in wanted and pubmed_queries:
            for entry in check_pubmed_updates(pubmed_queries):
                changes.append(entry)
                if entry.get("status") == "changed":
                    notifications += _notify_for_change(
                        run_id, "pubmed", entry["source_id"],
                        summary=f"{len(entry.get('new_pmids', []))} new PubMed records",
                        affected=0)
        if "web" in wanted and web_urls:
            for entry in check_web_updates(web_urls):
                changes.append(entry)
                if entry.get("status") == "changed":
                    notifications += _notify_for_change(
                        run_id, "web", entry["source_id"],
                        summary="web page content changed", affected=0)
    except Exception as exc:
        errors.append(str(exc))

    checked = len(changes)
    changed = sum(1 for c in changes if c.get("status") == "changed")
    reverified = sum(s.get("reverified", 0) for s in reverify_summary)
    upgraded = sum(s.get("upgraded", 0) for s in reverify_summary)
    downgraded = sum(s.get("downgraded", 0) for s in reverify_summary)
    completed = utcnow()
    con = _connect()
    try:
        con.execute(
            """UPDATE knowledge_refresh_runs SET
                 sources_checked=?, sources_changed=?, claims_reverified=?,
                 claims_upgraded=?, claims_downgraded=?, notifications_created=?,
                 completed_at=?, status=?, error_msg=?
               WHERE run_id=?""",
            (checked, changed, reverified, upgraded, downgraded, notifications,
             completed, "failed" if errors else "completed",
             "; ".join(errors) if errors else None, run_id),
        )
        con.commit()
    finally:
        con.close()

    return {
        "run_id": run_id,
        "refresh_version": REFRESH_VERSION,
        "triggered_by": triggered_by,
        "offline": bool(T.OFFLINE),
        "sources_checked": checked,
        "sources_changed": changed,
        "claims_reverified": reverified,
        "claims_upgraded": upgraded,
        "claims_downgraded": downgraded,
        "notifications_created": notifications,
        "changes": changes,
        "reverify": reverify_summary,
        "errors": errors,
        "status": "failed" if errors else "completed",
        "started_at": started,
        "completed_at": completed,
    }


def _notify_for_change(run_id: str, source_type: str, source_id: str,
                       summary: str, affected: int) -> int:
    con = _connect()
    try:
        con.execute(
            """INSERT INTO knowledge_notifications
               (notification_id, refresh_run_id, source_type, source_id,
                change_summary, affected_claims, severity, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (_uid("ntf"), run_id, source_type, source_id, summary, int(affected),
             REFRESH_SEVERITY_FOR_SOURCE.get(source_type, "info"), utcnow()),
        )
        con.commit()
        return 1
    finally:
        con.close()


# ─── Status + notifications ───

def get_refresh_status() -> Dict[str, Any]:
    ensure_refresh_tables()
    con = _connect()
    try:
        last_run = con.execute(
            "SELECT * FROM knowledge_refresh_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        open_notifications = con.execute(
            "SELECT count(*) FROM knowledge_notifications WHERE acknowledged=0"
        ).fetchone()[0]
        sources = [dict(r) for r in con.execute(
            """SELECT s.* FROM source_refresh_log s
               JOIN (SELECT source_type, source_id, MAX(last_checked_at) held
                     FROM source_refresh_log GROUP BY source_type, source_id) m
                 ON m.source_type = s.source_type AND m.source_id = s.source_id
                AND m.held = s.last_checked_at
               ORDER BY s.source_type, s.source_id"""
        )]
        return {
            "refresh_version": REFRESH_VERSION,
            "offline": bool(T.OFFLINE),
            "last_run": dict(last_run) if last_run else None,
            "open_notifications": open_notifications,
            "sources": sources,
        }
    finally:
        con.close()


def list_notifications(acknowledged: Optional[int] = None, limit: int = 50) -> List[Dict[str, Any]]:
    ensure_refresh_tables()
    con = _connect()
    try:
        if acknowledged is None:
            rows = con.execute(
                "SELECT * FROM knowledge_notifications ORDER BY created_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM knowledge_notifications WHERE acknowledged=?"
                " ORDER BY created_at DESC LIMIT ?",
                (int(acknowledged), int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def acknowledge_notification(notification_id: str) -> Dict[str, Any]:
    ensure_refresh_tables()
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM knowledge_notifications WHERE notification_id=?",
            (notification_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown notification_id: {notification_id}")
        con.execute(
            "UPDATE knowledge_notifications SET acknowledged=1, acknowledged_at=?"
            " WHERE notification_id=?", (utcnow(), notification_id))
        con.commit()
        updated = dict(con.execute(
            "SELECT * FROM knowledge_notifications WHERE notification_id=?",
            (notification_id,)).fetchone())
        return updated
    finally:
        con.close()


__all__ = [
    "REFRESH_VERSION",
    "CLAIM_STATUS_RANK",
    "ensure_refresh_tables",
    "diff_textbook_editions",
    "check_textbook_updates",
    "check_pubmed_updates",
    "check_web_updates",
    "reverify_claims_for_document",
    "run_refresh",
    "get_refresh_status",
    "list_notifications",
    "acknowledge_notification",
]
