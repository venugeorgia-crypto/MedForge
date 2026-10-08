"""Unit tests for medforge.retrieval (offline-safe, no Ollama required).

The database is redirected to a temporary file via medforge.types.META_DB,
which retrieval reads dynamically at call time. Rows are inserted directly
into SQLite so no embedding model is needed.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

import medforge.types as T
from medforge.storage import init_db
from medforge.retrieval import hybrid_retrieve, keyword_results, source_pack
from medforge.utils import chunks

ROWS = [
    ("test-1", "The cardiac cycle involves systole and diastole phases of the heart.",
     "Test Source", "page 1", "course_pdf", "", 0.90),
    ("test-2", "During ventricular systole pressure rises until the aortic valve opens.",
     "Test Source", "page 2", "course_pdf", "", 0.90),
    ("test-3", "Isovolumetric contraction occurs when all valves are closed.",
     "PubMed PMID 12345", "Abstract", "pubmed", "https://pubmed.ncbi.nlm.nih.gov/12345/", 0.96),
    ("test-4", "Web research result about cardiac physiology from a trusted domain.",
     "nih.gov article", "nih.gov (page retrieved 2026-01-01T00:00:00Z)", "web",
     "https://nih.gov/article", 0.96),
]


@pytest.fixture()
def test_db():
    """Redirect META_DB to a temp database seeded with known chunks."""
    original = T.META_DB
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    import os
    os.close(fd)
    os.unlink(path)  # let sqlite create it fresh
    T.META_DB = Path(path)
    try:
        init_db()
        con = sqlite3.connect(path)
        try:
            for rid, text, source, locator, kind, url, quality in ROWS:
                con.execute(
                    "INSERT INTO chunks(id,text,source,locator,kind,url,quality,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (rid, text, source, locator, kind, url, quality, "2026-01-01T00:00:00Z"),
                )
                con.execute(
                    "INSERT INTO chunks_fts(id,text,source,locator) VALUES(?,?,?,?)",
                    (rid, text, source, locator),
                )
            con.commit()
        finally:
            con.close()
        yield Path(path)
    finally:
        T.META_DB = original
        if Path(path).exists():
            Path(path).unlink()


def test_chunks():
    """Text chunking produces overlapping windows and skips tiny fragments."""
    text = " ".join(["word"] * 500)
    result = chunks(text, max_words=100, overlap=20)
    assert len(result) > 1
    assert all(len(r.split()) >= 20 for r in result)
    assert len(result[0].split()) == 100

    # A 4-word fragment is below the 20-word minimum and is dropped
    assert chunks("This is a short text.", max_words=100, overlap=20) == []


def test_keyword_results(test_db):
    """BM25 keyword search finds seeded chunks and rejects empty/short queries."""
    results = keyword_results("cardiac cycle", limit=5)
    assert len(results) >= 1
    ids = {r["id"] for r in results}
    assert "test-1" in ids  # contains both "cardiac" and "cycle"
    for r in results:
        assert {"id", "text", "source", "kind", "quality", "rank"} <= set(r)
        assert r["rank"] >= 1

    assert keyword_results("", limit=5) == []
    # Terms shorter than 3 chars are filtered out
    assert keyword_results("a b c", limit=5) == []


def test_hybrid_retrieve(test_db):
    """Hybrid retrieval returns scored, quality-filtered results even without
    a running Ollama (vector search degrades to keyword-only)."""
    results = hybrid_retrieve("cardiac cycle", limit=5)
    assert isinstance(results, list)
    for r in results:
        assert "score" in r
        # Quality gate: course_pdf passes by kind, everything else needs >= 0.85
        assert r.get("kind") == "course_pdf" or r.get("quality", 0) >= 0.85
    ids = {r["id"] for r in results}
    # test-1 is a course_pdf matching both query terms
    assert "test-1" in ids


def test_source_pack(test_db):
    """source_pack labels retrieved sources S1..Sn and returns usable blocks."""
    text, sources = source_pack("cardiac cycle", limit=3)
    assert isinstance(text, str)
    assert isinstance(sources, list)
    if sources:
        assert all(s["label"].startswith("S") for s in sources)
        assert len({s["label"] for s in sources}) == len(sources)
        assert "[S1]" in text


def test_vector_results_requires_ollama(test_db):
    """Vector search either works (Ollama up) or raises cleanly (Ollama down)."""
    from medforge.retrieval import vector_results
    from medforge.models import ollama_alive

    if not ollama_alive():
        with pytest.raises(Exception):
            vector_results("heart", limit=5)
    else:
        try:
            results = vector_results("heart", limit=5)
            assert isinstance(results, list)
        except RuntimeError as e:
            # Ollama might be running without embeddings support (501 error)
            if "501" in str(e) or "embeddings" in str(e).lower():
                pytest.skip("Ollama running without embeddings support")
            raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
