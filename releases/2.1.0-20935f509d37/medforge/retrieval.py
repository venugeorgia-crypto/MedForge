"""MedForge retrieval: hybrid BM25 + vector search with reciprocal rank fusion."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Tuple

import medforge.types as T
from medforge.storage import init_db, get_collection
from medforge.models import embed


def keyword_results(question: str, limit: int = 12) -> List[Dict[str, Any]]:
    """BM25 keyword search over SQLite FTS5."""
    init_db()
    terms = [t for t in re.findall(r"[A-Za-z0-9]+", question.lower()) if len(t) > 2][:12]
    if not terms:
        return []
    query = " OR ".join(f'"{t}"' for t in terms)
    con = sqlite3.connect(T.META_DB)
    try:
        rows = con.execute(
            """
            SELECT f.id, c.text, c.source, c.locator, c.kind, c.url, c.quality, bm25(chunks_fts)
            FROM chunks_fts f JOIN chunks c ON c.id=f.id
            WHERE chunks_fts MATCH ?
            ORDER BY bm25(chunks_fts)
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        con.close()
    return [
        {
            "id": r[0], "text": r[1], "source": r[2], "locator": r[3],
            "kind": r[4], "url": r[5], "quality": r[6], "rank": i + 1,
        }
        for i, r in enumerate(rows)
    ]


def vector_results(question: str, limit: int = 12) -> List[Dict[str, Any]]:
    """Semantic vector search via ChromaDB (requires Ollama embeddings)."""
    col = get_collection()
    if col.count() == 0:
        return []
    qvec = embed([question])[0]
    d = col.query(
        query_embeddings=[qvec],
        n_results=min(limit, col.count()),
        include=["documents", "metadatas", "distances"],
    )
    ids = (d.get("ids") or [[]])[0]
    docs = (d.get("documents") or [[]])[0]
    metas = (d.get("metadatas") or [[]])[0]
    ds = (d.get("distances") or [[]])[0]
    out: List[Dict[str, Any]] = []
    for i, _id in enumerate(ids):
        m = metas[i] or {}
        out.append({
            "id": _id, "text": docs[i], "source": m.get("source", ""),
            "locator": m.get("locator", ""), "kind": m.get("kind", ""),
            "url": m.get("url", ""), "quality": float(m.get("quality", 0.5)),
            "distance": ds[i] if i < len(ds) else 1.0, "rank": i + 1,
        })
    return out


def hybrid_retrieve(question: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Reciprocal-rank fusion of vector and keyword retrieval, quality-weighted.

    Falls back to keyword-only search when embeddings/Ollama are unavailable.
    """
    try:
        vr = vector_results(question, 14)
    except Exception as e:
        print(f"  Semantic retrieval unavailable; using keyword search: {e}")
        vr = []
    kr = keyword_results(question, 14)

    merged: Dict[str, Dict[str, Any]] = {}
    for resultset in (vr, kr):
        for r in resultset:
            x = merged.setdefault(r["id"], dict(r, score=0.0))
            x["score"] += (1.0 / (60 + r["rank"])) * (0.75 + 0.25 * float(r.get("quality", 0.5)))
    ranked = sorted(merged.values(), key=lambda x: x["score"], reverse=True)

    # Reject low-trust leftovers and obviously distant vector hits.
    keyword_ids = {r["id"] for r in kr}
    ranked = [
        r for r in ranked
        if (r.get("kind") == "course_pdf" or r.get("quality", 0) >= 0.85)
        and (r.get("kind") != "web" or "(page retrieved " in r.get("locator", ""))
        and (r["id"] in keyword_ids or r.get("distance", 1) <= 0.65)
    ]
    return ranked[:limit]


def source_pack(topic: str, limit: int = 10) -> Tuple[str, List[Dict[str, Any]]]:
    """Retrieve top sources and format them as labeled evidence blocks."""
    rows = hybrid_retrieve(topic, min(limit, 6))
    blocks: List[str] = []
    budget = 8000
    selected: List[Dict[str, Any]] = []
    for i, r in enumerate(rows, 1):
        r = dict(r)
        r["text"] = r["text"][: min(1300, budget)]
        budget -= len(r["text"])
        selected.append(r)
        r["label"] = f"S{i}"
        url = f"\nURL: {r['url']}" if r.get("url") else ""
        blocks.append(
            f"[S{i}] {r['source']} — {r['locator']}\n"
            f"TYPE: {r['kind']} | RANKING WEIGHT: {r['quality']:.2f} (not an accuracy score){url}\n"
            f"{r['text']}"
        )
    return "\n\n".join(blocks), selected


__all__ = ["keyword_results", "vector_results", "hybrid_retrieve", "source_pack"]
