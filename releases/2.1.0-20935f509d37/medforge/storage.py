"""MedForge storage layer: SQLite metadata, ChromaDB vectors, V3 migration.

Reads paths dynamically from medforge.types so tests can redirect the
database to a temporary file.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import medforge.types as T
from medforge.utils import mkdirs, utcnow, chunks, batch
from medforge.models import embed

# ─── SQLite Schema (V2 Legacy) ───
LEGACY_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS chunks(
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    locator TEXT DEFAULT '',
    kind TEXT DEFAULT '',
    url TEXT DEFAULT '',
    quality REAL DEFAULT 0.5,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    id UNINDEXED, text, source, locator, tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS study_sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    topic TEXT NOT NULL,
    score REAL,
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS weaknesses(
    topic TEXT NOT NULL,
    concept TEXT NOT NULL,
    misses INTEGER DEFAULT 1,
    last_seen TEXT NOT NULL,
    PRIMARY KEY(topic, concept)
);
"""


def _import_core_database():
    """Import core.database modules, ensuring BASE is on sys.path."""
    base_str = str(T.BASE)
    if base_str not in sys.path:
        sys.path.insert(0, base_str)
    from core.database.migrate_v3 import migrate_database as run_migration

    return run_migration


def init_db() -> None:
    """Initialize the SQLite metadata database with the legacy schema.

    V3 migration is explicit: call migrate_database() or `medforge_core migrate`.
    """
    mkdirs()
    con = sqlite3.connect(T.META_DB)
    try:
        con.executescript(LEGACY_SCHEMA_DDL)
        con.commit()
    finally:
        con.close()


def migrate_database() -> Dict[str, Any]:
    """Run the V3 schema migration explicitly, with a verified backup first."""
    try:
        run_migration = _import_core_database()
        return run_migration(T.META_DB, create_backup=True)
    except ImportError as e:
        return {"status": "error", "reason": f"core.database not importable: {e}"}


def get_collection():
    """Get or create the ChromaDB collection for the active embedding model."""
    import chromadb

    client = chromadb.PersistentClient(path=str(T.CHROMA_DIR))
    if "embeddinggemma" in T.EMBED_MODEL:
        name = "medforge_v2"
    else:
        name = "mf_" + hashlib.sha256(T.EMBED_MODEL.encode()).hexdigest()[:16]
    return client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})


def upsert_records(records: List[Dict[str, Any]]) -> int:
    """Upsert chunk records into ChromaDB and the SQLite metadata store."""
    if not records:
        return 0
    init_db()
    col = get_collection()
    con = sqlite3.connect(T.META_DB)
    done = 0

    # Deduplicate by ID
    records = list({r["id"]: r for r in records}.values())

    try:
        for b in batch(records, 4):
            texts = [x["text"] for x in b]
            vecs = embed(texts)
            ids = [x["id"] for x in b]
            metas = []
            for x in b:
                m = x["metadata"]
                metas.append({
                    "source": m.get("source", ""),
                    "locator": m.get("locator", ""),
                    "kind": m.get("kind", ""),
                    "url": m.get("url", ""),
                    "quality": float(m.get("quality", 0.5)),
                })
            col.upsert(ids=ids, documents=texts, embeddings=vecs, metadatas=metas)

            for x in b:
                m = x["metadata"]
                vals = (
                    x["id"], x["text"], m.get("source", ""), m.get("locator", ""),
                    m.get("kind", ""), m.get("url", ""),
                    float(m.get("quality", 0.5)), utcnow(),
                )
                con.execute(
                    """INSERT OR REPLACE INTO chunks
                       (id,text,source,locator,kind,url,quality,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    vals,
                )
                con.execute("DELETE FROM chunks_fts WHERE id=?", (x["id"],))
                con.execute(
                    "INSERT INTO chunks_fts(id,text,source,locator) VALUES(?,?,?,?)",
                    (x["id"], x["text"], m.get("source", ""), m.get("locator", "")),
                )
            con.commit()
            done += len(b)
            print(f"\r  indexed {done}/{len(records)} chunks", end="", flush=True)
    finally:
        print()
        con.close()
    return done


__all__ = ["LEGACY_SCHEMA_DDL", "init_db", "migrate_database", "get_collection", "upsert_records"]
