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
    CURRICULUM_NODE_TYPES,
    TEXTBOOK_NODE_TYPES,
    TEXTBOOK_SOURCE_TYPES,
    TEXTBOOK_PAGE_STATUS,
    TEXTBOOK_OCR_STATUS,
    TEXTBOOK_INGEST_STATUS,
    CURRICULUM_TEXT_LINK_TYPES,
    SOURCE_PRIORITY,
    SYSTEM_EVIDENCE,
    CLAIM_TYPES,
    CLAIM_VERIFICATION_STATUS,
    CLAIM_REVIEW_STATUS,
    EVIDENCE_TYPES,
    CLAIM_EVIDENCE_RELATIONSHIPS,
    VERIFICATION_RESULTS,
    TUTOR_VERSION,
    TUTOR_SESSION_MODES,
    TUTOR_STAGES,
    TUTOR_SESSION_STATUSES,
    TUTOR_SESSION_GOALS,
    TUTOR_DEFAULT_GOAL,
    TUTOR_QUESTION_TYPES,
    TUTOR_CORRECTNESS,
    TUTOR_GRADING_STATUSES,
    TUTOR_ERROR_TYPES,
    TUTOR_PASS_SCORE,
    TUTOR_MAX_INTERACTIONS,
    TUTOR_MAX_TURNS,
    TUTOR_DIFFICULTY_MIN,
    TUTOR_DIFFICULTY_MAX,
    TUTOR_LOW_CONFIDENCE,
    TUTOR_HIGH_CONFIDENCE,
    TUTOR_OBJECTIVE_MASTERY,
    TUTOR_OBJECTIVE_RECENT,
    ASSESSMENT_VERSION,
    ASSESSMENT_ITEM_TYPES,
    ASSESSMENT_ITEM_STATUSES,
    ASSESSMENT_BLUEPRINT_STATUSES,
    ASSESSMENT_MODES,
    ASSESSMENT_SESSION_STATUSES,
    ASSESSMENT_SCOPE_TYPES,
    ASSESSMENT_DIFFICULTY_BANDS,
    ASSESSMENT_MIN_ITEM_STAT_SAMPLE,
    ASSESSMENT_MIN_DISCRIMINATION_SAMPLE,
    ASSESSMENT_INSUFFICIENT_SAMPLE_LABEL,
    ASSESSMENT_ITEM_QUALITY_FLAGS,
    ASSESSMENT_DEFAULT_PASS_THRESHOLD,
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
    record_weakness, resolve_weakness, log_study_result, complete_session,
    learner_snapshot, normalize_score,
    card_item_id, import_flashcards, due_items, review_card, review_analytics,
    spaced_repetition_snapshot, SCHEDULERS, DEFAULT_SCHEDULER,
)
from medforge.learner_model import (
    ensure_learner_model_tables, decay_weight, compute_model_state,
    record_learning_event, recalculate_mastery, recalculate_all, get_mastery,
    get_confidence, get_recent_performance, get_weaknesses,
    get_prerequisite_risks, get_review_priority, study_priority,
    detect_weaknesses, learner_history, learner_summary, curriculum_report,
)
from medforge.tutor import (
    ensure_tutor_tables, start_tutor_session, get_tutor_state, resume_tutor_session,
    select_tutor_target, generate_teaching_step, generate_question, submit_answer,
    evaluate_answer, adapt_tutor, record_tutor_learning_event,
    complete_tutor_session, get_tutor_summary, next_tutor_step, grade_mcq,
    grade_key_points, grade_free_text, list_tutor_sessions, public_view,
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
from medforge.curriculum import (
    HIERARCHY, normalize_title, title_key, ensure_curriculum_tables,
    find_node, ensure_node, get_children, import_syllabus, parse_syllabus,
    add_prerequisite, remove_prerequisite, curriculum_tree, topic_path,
    curriculum_progress, detect_duplicates,
)
from medforge.textbook import (
    ensure_textbook_tables, resolve_metadata, register_textbook,
    ingest_textbook, list_textbooks, book_structure, link_curriculum_text,
    unlink_curriculum_text, textbook_evidence_for_topic,
    suggest_curriculum_links, registered_source_paths,
)
from medforge.assessment import (
    ensure_assessment_tables, create_item, get_item, list_items, revise_item,
    retire_item, validate_item, approve_item, reject_item, detect_duplicate_items,
    generate_items, grade_item, grade_multi_select, create_blueprint,
    validate_blueprint, get_blueprint, list_blueprints, select_items,
    create_assessment, start_assessment, get_assessment_state, get_current_item,
    submit_assessment_answer, retry_pending_grading, flag_item, next_item,
    previous_item, complete_assessment, get_assessment_result,
    get_item_statistics, compute_item_quality, get_assessment_remediation,
    review_assessment, list_assessments, resume_assessment,
    public_assessment_view, assessment_export,
)
from medforge.evidence import (
    ensure_evidence_tables, normalize_claim_text, claim_id_for, classify_claim,
    extract_claims, store_claims, store_evidence, evidence_id_for_source,
    pack_label_map, verify_claim, verify_product_claims, claims_list, claim_info,
    evidence_snapshot, aggregate_verdicts, parse_verifier_response,
    curriculum_node_id_for_topic, VERIFIER_SYSTEM, FACTUAL_CLAIM_TYPES,
)
from medforge.study import (
    STUDY_VERSION, PLANNER_VERSION, REASON_WEIGHTS, MASTERED_PCT, STUDIED_PCT,
    ACTION_MINUTES, SHORTENED_MIN_MINUTES,
    ensure_study_tables, gather_learner_context, get_study_status,
    recommend_next_action, get_knowledge_gaps, get_readiness,
    build_study_plan, get_plan, list_plans, get_today_plan,
    start_study_mission, get_mission_state, list_missions,
    complete_mission_action, complete_mission, harvest_mission_results,
    launch_mission_engines, get_study_history, adaptation_profile,
)
from medforge.content import (
    CONTENT_VERSION, PROMPT_VERSION,
    generate_canonical_content, get_content, list_content,
    render_study_products, get_product_status, list_artifacts,
    artifact_provenance, content_consistency_report,
    canonical_fallback_from_evidence,
)

__version__ = VERSION

__all__ = [
    # types
    "VERSION", "BASE", "APP", "DOCS", "PRODUCTS", "DBDIR", "CHROMA_DIR",
    "META_DB", "TMP", "LOGS", "OLLAMA_URL", "EMBED_MODEL", "PREFERRED_CHAT",
    "FALLBACK_MODELS", "NCBI_EMAIL", "OFFLINE", "TRUSTED_DOMAINS",
    "NODE_TYPES", "PREREQUISITE_TYPES", "WEAKNESS_SEVERITY", "SESSION_TYPES",
    "SPACED_REPETITION_STATES", "SPACED_REPETITION_ITEM_TYPES",
    "MEDICAL_PUBLICATION_TYPES", "CURRICULUM_NODE_TYPES", "SYSTEM_EVIDENCE",
    "TUTOR_VERSION", "TUTOR_SESSION_MODES", "TUTOR_STAGES",
    "TUTOR_SESSION_STATUSES", "TUTOR_SESSION_GOALS", "TUTOR_DEFAULT_GOAL",
    "TUTOR_QUESTION_TYPES", "TUTOR_CORRECTNESS", "TUTOR_GRADING_STATUSES",
    "TUTOR_ERROR_TYPES", "TUTOR_PASS_SCORE", "TUTOR_MAX_INTERACTIONS",
    "TUTOR_MAX_TURNS", "TUTOR_DIFFICULTY_MIN", "TUTOR_DIFFICULTY_MAX",
    "TUTOR_LOW_CONFIDENCE", "TUTOR_HIGH_CONFIDENCE",
    "TUTOR_OBJECTIVE_MASTERY", "TUTOR_OBJECTIVE_RECENT",
    # question-level assessment engine (V9/P8)
    "ASSESSMENT_VERSION", "ASSESSMENT_ITEM_TYPES", "ASSESSMENT_ITEM_STATUSES",
    "ASSESSMENT_BLUEPRINT_STATUSES", "ASSESSMENT_MODES",
    "ASSESSMENT_SESSION_STATUSES", "ASSESSMENT_SCOPE_TYPES",
    "ASSESSMENT_DIFFICULTY_BANDS", "ASSESSMENT_MIN_ITEM_STAT_SAMPLE",
    "ASSESSMENT_MIN_DISCRIMINATION_SAMPLE", "ASSESSMENT_INSUFFICIENT_SAMPLE_LABEL",
    "ASSESSMENT_ITEM_QUALITY_FLAGS", "ASSESSMENT_DEFAULT_PASS_THRESHOLD",
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
    "DEFAULT_SCHEDULER", "complete_session", "normalize_score",
    # recency-weighted learner model (V7/P6)
    "ensure_learner_model_tables", "decay_weight", "compute_model_state",
    "record_learning_event", "recalculate_mastery", "recalculate_all",
    "get_mastery", "get_confidence", "get_recent_performance", "get_weaknesses",
    "get_prerequisite_risks", "get_review_priority", "study_priority",
    "detect_weaknesses", "learner_history", "learner_summary",
    "curriculum_report",
    # interactive adaptive tutor (V8/P7)
    "ensure_tutor_tables", "start_tutor_session", "get_tutor_state",
    "resume_tutor_session", "select_tutor_target", "generate_teaching_step",
    "generate_question", "submit_answer", "evaluate_answer", "adapt_tutor",
    "record_tutor_learning_event", "complete_tutor_session", "get_tutor_summary",
    "next_tutor_step", "grade_mcq", "grade_key_points", "grade_free_text",
    "list_tutor_sessions", "public_view",
    # question-level assessment engine (V9/P8)
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
    # curriculum engine (V4)
    "HIERARCHY", "normalize_title", "title_key", "ensure_curriculum_tables",
    "find_node", "ensure_node", "get_children", "import_syllabus",
    "parse_syllabus", "add_prerequisite", "remove_prerequisite",
    "curriculum_tree", "topic_path", "curriculum_progress", "detect_duplicates",
    # textbook provenance (V5)
    "TEXTBOOK_NODE_TYPES", "TEXTBOOK_SOURCE_TYPES", "TEXTBOOK_PAGE_STATUS",
    "TEXTBOOK_OCR_STATUS", "TEXTBOOK_INGEST_STATUS", "CURRICULUM_TEXT_LINK_TYPES",
    "SOURCE_PRIORITY",
    "ensure_textbook_tables", "resolve_metadata", "register_textbook",
    "ingest_textbook", "list_textbooks", "book_structure", "link_curriculum_text",
    "unlink_curriculum_text", "textbook_evidence_for_topic",
    "suggest_curriculum_links", "registered_source_paths",
    # evidence graph (V6)
    "ensure_evidence_tables", "normalize_claim_text", "claim_id_for",
    "classify_claim", "extract_claims", "store_claims", "store_evidence",
    "evidence_id_for_source", "pack_label_map", "verify_claim",
    "verify_product_claims", "claims_list", "claim_info", "evidence_snapshot",
    "aggregate_verdicts", "parse_verifier_response",
    "curriculum_node_id_for_topic", "VERIFIER_SYSTEM", "FACTUAL_CLAIM_TYPES",
    "CLAIM_TYPES", "CLAIM_VERIFICATION_STATUS", "CLAIM_REVIEW_STATUS",
    "EVIDENCE_TYPES", "CLAIM_EVIDENCE_RELATIONSHIPS", "VERIFICATION_RESULTS",
    # study intelligence orchestrator (V10/P9)
    "STUDY_VERSION", "PLANNER_VERSION", "CONTENT_VERSION", "PROMPT_VERSION",
    "REASON_WEIGHTS", "MASTERED_PCT", "STUDIED_PCT",
    "ACTION_MINUTES", "SHORTENED_MIN_MINUTES",
    "ensure_study_tables", "gather_learner_context", "get_study_status",
    "recommend_next_action", "get_knowledge_gaps", "get_readiness",
    "build_study_plan", "get_plan", "list_plans", "get_today_plan",
    "start_study_mission", "get_mission_state", "list_missions",
    "complete_mission_action", "complete_mission", "harvest_mission_results",
    "launch_mission_engines", "get_study_history", "adaptation_profile",
    # canonical content + product rendering (V10/P9)
    "generate_canonical_content", "get_content", "list_content",
    "render_study_products", "get_product_status", "list_artifacts",
    "artifact_provenance", "content_consistency_report",
    "canonical_fallback_from_evidence",
]
