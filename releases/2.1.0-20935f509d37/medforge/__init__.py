"""MedForge core package — modular medical education engine.

Import order follows the dependency DAG:
types → utils → models → storage → retrieval → generation → export → ingestion → product
"""

from medforge.types import (
    VERSION,
    BASE,
    APP,
    DOCS,
    PRODUCTS,
    DBDIR,
    CHROMA_DIR,
    META_DB,
    TMP,
    LOGS,
    OLLAMA_URL,
    EMBED_MODEL,
    PREFERRED_CHAT,
    FALLBACK_MODELS,
    NCBI_EMAIL,
    OFFLINE,
    TRUSTED_DOMAINS,
    NODE_TYPES,
    PREREQUISITE_TYPES,
    WEAKNESS_SEVERITY,
    SESSION_TYPES,
    SPACED_REPETITION_STATES,
    SPACED_REPETITION_ITEM_TYPES,
    MEDICAL_PUBLICATION_TYPES,
    SYSTEM_EVIDENCE,
)
from medforge.utils import (
    mkdirs, sh, slugify, utcnow, atomic_text, job_lock, serialized,
    chunks, batch,
)
from medforge.models import (
    ensure_models, ensure_ollama_running, model_names, model_exists,
    test_chat_model, stop_model, embed, chat, ollama_alive, pull_model, http_json,
)
from medforge.storage import (
    init_db, get_collection, upsert_records, migrate_database,
)
from medforge.learner import (
    ensure_v3_tables, start_session, record_session, update_mastery,
    record_weakness, resolve_weakness, log_study_result, learner_snapshot,
    card_item_id, import_flashcards, due_items, review_card, review_analytics,
    spaced_repetition_snapshot, SCHEDULERS, DEFAULT_SCHEDULER,
)
from medforge.retrieval import (
    hybrid_retrieve, source_pack, keyword_results, vector_results,
)
from medforge.generation import (
    generate_text, citation_audit, parse_tsv_cards,
)
from medforge.export import (
    write_pdf, make_anki,
)
from medforge.ingestion import (
    ingest_pdfs, pubmed_import, web_research, fetch_web_text, domain_quality,
)
from medforge.product import (
    build_product, product_dir, save_state, ask, study, status,
)

__version__ = VERSION

__all__ = [
    # types
    "VERSION", "BASE", "APP", "DOCS", "PRODUCTS", "DBDIR", "CHROMA_DIR",
    "META_DB", "TMP", "LOGS", "OLLAMA_URL", "EMBED_MODEL", "PREFERRED_CHAT",
    "FALLBACK_MODELS", "NCBI_EMAIL", "OFFLINE", "TRUSTED_DOMAINS",
    "NODE_TYPES", "PREREQUISITE_TYPES", "WEAKNESS_SEVERITY", "SESSION_TYPES",
    "SPACED_REPETITION_STATES", "SPACED_REPETITION_ITEM_TYPES",
    "MEDICAL_PUBLICATION_TYPES", "SYSTEM_EVIDENCE",
    # utils
    "mkdirs", "sh", "slugify", "utcnow", "atomic_text", "job_lock", "serialized",
    "chunks", "batch",
    # models
    "ensure_models", "ensure_ollama_running", "model_names", "model_exists",
    "test_chat_model", "stop_model", "embed", "chat", "ollama_alive",
    "pull_model", "http_json",
    # storage
    "init_db", "get_collection", "upsert_records", "migrate_database",
    # learner tracking (V3 tables)
    "ensure_v3_tables", "start_session", "record_session", "update_mastery",
    "record_weakness", "resolve_weakness", "log_study_result", "learner_snapshot",
    # spaced repetition (V3 queue)
    "card_item_id", "import_flashcards", "due_items", "review_card",
    "review_analytics", "spaced_repetition_snapshot", "SCHEDULERS",
    "DEFAULT_SCHEDULER",
    # retrieval
    "hybrid_retrieve", "source_pack", "keyword_results", "vector_results",
    # generation
    "generate_text", "citation_audit", "parse_tsv_cards",
    # export
    "write_pdf", "make_anki",
    # ingestion
    "ingest_pdfs", "pubmed_import", "web_research", "fetch_web_text",
    "domain_quality",
    # product
    "build_product", "product_dir", "save_state", "ask", "study", "status",
]
