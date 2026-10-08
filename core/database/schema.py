"""MedForge V3 Database Schema Definition.

Fully backward-compatible schema supporting:
- 360 ECTS Medical Curriculum Hierarchy
- Graph Prerequisite Dependencies
- Learner Mastery & Misconception Tracking
- Multi-modal Interactive Study Sessions
- SM-2 / FSRS Spaced Repetition Scheduling
- Medical Evidence Provenance (with Human vs. Animal study discrimination)
"""

# Allowed values for validation and type checking
NODE_TYPES = (
    "Year",
    "Semester",
    "Course",
    "Module",
    "Topic",
    "Subtopic",
    "Learning Objective",
)

PREREQUISITE_TYPES = ("strict", "recommended", "co-requisite")

WEAKNESS_SEVERITY = ("low", "medium", "high", "critical")

SESSION_TYPES = (
    "Learn",
    "Active Recall",
    "Case",
    "Viva",
    "Prerequisite Repair",
)

SPACED_REPETITION_STATES = ("new", "learning", "review", "relearning")

SPACED_REPETITION_ITEM_TYPES = ("card", "topic", "concept")

MEDICAL_PUBLICATION_TYPES = (
    "Guideline",
    "Textbook",
    "Meta-Analysis",
    "Research",
    "Educational",
)

# ─── V4: canonical curriculum engine (extends the V3 node-type enum) ───
# The directive hierarchy is SEMESTER → SUBJECT → WEEK → SEMINAR → TOPIC →
# SUBTOPIC → LEARNING OBJECTIVE. V3's enum (Year/Semester/Course/Module/…)
# cannot express Subject/Week/Seminar nodes, so the enum is extended and the
# curriculum_nodes CHECK constraint is rebuilt by core.database.migrate_v4
# (data-preserving; the table shipped empty). V3_SCHEMA_DDL itself is left
# untouched so the V3 migration record keeps its original meaning.
CURRICULUM_NODE_TYPES = NODE_TYPES + ("Subject", "Week", "Seminar")

# Rebuild template used by migrate_v4 when an old-CHECK curriculum_nodes table
# is detected. The temporary table name is rewritten to `curriculum_nodes`
# after the row-preserving copy; the self-referencing FK follows the rename.
V4_CURRICULUM_NODES_DDL = """
CREATE TABLE curriculum_nodes_v4_tmp (
    id TEXT PRIMARY KEY,
    parent_id TEXT NULL REFERENCES curriculum_nodes_v4_tmp(id) ON DELETE SET NULL,
    node_type TEXT NOT NULL CHECK(node_type IN ('Year', 'Semester', 'Course', 'Module', 'Topic', 'Subtopic', 'Learning Objective', 'Subject', 'Week', 'Seminar')),
    code TEXT DEFAULT '',
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    year INTEGER NULL CHECK(year IS NULL OR (year >= 1 AND year <= 6)),
    semester INTEGER NULL CHECK(semester IS NULL OR (semester >= 1 AND semester <= 12)),
    ects_weight REAL NOT NULL DEFAULT 0.0 CHECK(ects_weight >= 0.0),
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

# Index additions for the curriculum engine (idempotent). The parent is
# COALESCE'd so the unique constraint also covers root-level nodes (SQLite
# treats bare NULLs as distinct in unique indexes).
V4_SCHEMA_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_curriculum_parent_title
    ON curriculum_nodes(COALESCE(parent_id, ''), title);
CREATE INDEX IF NOT EXISTS idx_curriculum_type_order
    ON curriculum_nodes(node_type, order_index);
"""

# ─── V5: textbook provenance (book → chapter → section → page → chunk) ───
# First-class document/edition entities with stable deterministic ids so a
# future claim (P4) can point to exact evidence and re-imports stay idempotent.
TEXTBOOK_NODE_TYPES = ("Chapter", "Section", "Subsection")
TEXTBOOK_SOURCE_TYPES = (
    "textbook", "lecture_notes", "handout", "guideline", "paper", "other"
)
TEXTBOOK_PAGE_STATUS = ("ok", "no_text", "error")
TEXTBOOK_OCR_STATUS = ("not_needed", "pending", "unavailable", "complete")
TEXTBOOK_INGEST_STATUS = ("REGISTERED", "EXTRACTED", "PARTIAL", "FAILED")
CURRICULUM_TEXT_LINK_TYPES = ("primary", "supporting", "supplementary")

V5_SCHEMA_DDL = """
-- 1. Textbook documents (stable across editions)
CREATE TABLE IF NOT EXISTS textbook_documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT DEFAULT '',
    publisher TEXT DEFAULT '',
    edition_label TEXT DEFAULT '',
    publication_year INTEGER NULL,
    isbn TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'textbook' CHECK(source_type IN ('textbook', 'lecture_notes', 'handout', 'guideline', 'paper', 'other')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 2. Textbook editions (one row per distinct content hash; never overwritten)
CREATE TABLE IF NOT EXISTS textbook_editions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES textbook_documents(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    source_path TEXT DEFAULT '',
    page_count INTEGER NOT NULL DEFAULT 0,
    extracted_pages INTEGER NOT NULL DEFAULT 0,
    skipped_pages INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    ocr_status TEXT NOT NULL DEFAULT 'not_needed' CHECK(ocr_status IN ('not_needed', 'pending', 'unavailable', 'complete')),
    ocr_confidence REAL NULL,
    ingest_status TEXT NOT NULL DEFAULT 'REGISTERED' CHECK(ingest_status IN ('REGISTERED', 'EXTRACTED', 'PARTIAL', 'FAILED')),
    ingested_at TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(content_hash)
);

CREATE INDEX IF NOT EXISTS idx_tb_editions_doc ON textbook_editions(document_id);

-- 3. Chapter / Section / Subsection hierarchy
CREATE TABLE IF NOT EXISTS textbook_nodes (
    id TEXT PRIMARY KEY,
    edition_id TEXT NOT NULL REFERENCES textbook_editions(id) ON DELETE CASCADE,
    parent_id TEXT NULL REFERENCES textbook_nodes(id) ON DELETE CASCADE,
    node_type TEXT NOT NULL CHECK(node_type IN ('Chapter', 'Section', 'Subsection')),
    code TEXT DEFAULT '',
    title TEXT NOT NULL,
    order_index INTEGER NOT NULL DEFAULT 0,
    start_page INTEGER NULL,
    end_page INTEGER NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tb_nodes_edition ON textbook_nodes(edition_id);
CREATE INDEX IF NOT EXISTS idx_tb_nodes_parent ON textbook_nodes(parent_id);

-- 4. Page-level provenance (one row per PDF page)
CREATE TABLE IF NOT EXISTS textbook_pages (
    id TEXT PRIMARY KEY,
    edition_id TEXT NOT NULL REFERENCES textbook_editions(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK(page_number >= 1),
    node_id TEXT NULL REFERENCES textbook_nodes(id) ON DELETE SET NULL,
    text_chars INTEGER NOT NULL DEFAULT 0,
    extraction_status TEXT NOT NULL DEFAULT 'ok' CHECK(extraction_status IN ('ok', 'no_text', 'error')),
    ocr_status TEXT NOT NULL DEFAULT 'not_needed' CHECK(ocr_status IN ('not_needed', 'pending', 'unavailable', 'complete')),
    created_at TEXT NOT NULL,
    UNIQUE(edition_id, page_number)
);

CREATE INDEX IF NOT EXISTS idx_tb_pages_edition ON textbook_pages(edition_id, page_number);
CREATE INDEX IF NOT EXISTS idx_tb_pages_node ON textbook_pages(node_id);

-- 5. Structural chunks (mirror rows in the shared `chunks` store for retrieval)
CREATE TABLE IF NOT EXISTS textbook_chunks (
    id TEXT PRIMARY KEY,
    edition_id TEXT NOT NULL REFERENCES textbook_editions(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL,
    node_id TEXT NULL REFERENCES textbook_nodes(id) ON DELETE SET NULL,
    page_number INTEGER NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    word_count INTEGER NOT NULL DEFAULT 0,
    locator TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tb_chunks_edition ON textbook_chunks(edition_id, page_number, chunk_index);
CREATE INDEX IF NOT EXISTS idx_tb_chunks_node ON textbook_chunks(node_id);

-- 6. Curriculum ↔ textbook evidence links
CREATE TABLE IF NOT EXISTS curriculum_text_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    curriculum_node_id TEXT NOT NULL REFERENCES curriculum_nodes(id) ON DELETE CASCADE,
    edition_id TEXT NOT NULL REFERENCES textbook_editions(id) ON DELETE CASCADE,
    node_id TEXT NULL REFERENCES textbook_nodes(id) ON DELETE SET NULL,
    page_start INTEGER NULL,
    page_end INTEGER NULL,
    link_type TEXT NOT NULL DEFAULT 'primary' CHECK(link_type IN ('primary', 'supporting', 'supplementary')),
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ctl_curriculum ON curriculum_text_links(curriculum_node_id);
CREATE INDEX IF NOT EXISTS idx_ctl_edition ON curriculum_text_links(edition_id);
-- COALESCE'd so a link without a specific node still dedupes (NULLs are
-- distinct in bare unique indexes — lesson from the V4 curriculum index).
CREATE UNIQUE INDEX IF NOT EXISTS idx_ctl_unique
    ON curriculum_text_links(curriculum_node_id, edition_id, COALESCE(node_id, ''));
"""

# ─── V6: evidence graph (claims → evidence → verification history) ───
# Generated text stops being "lines with [S#] labels" and becomes explicit
# claim records with inspectable evidence relationships and an append-only
# verification history. Purely additive tables.
CLAIM_TYPES = (
    "fact", "definition", "mechanism", "association", "causation",
    "clinical", "epidemiology", "question", "instruction", "non_factual",
)
CLAIM_VERIFICATION_STATUS = (
    "PENDING", "SUPPORTED", "PARTIALLY_SUPPORTED", "UNSUPPORTED",
    "CONTRADICTED", "INSUFFICIENT_EVIDENCE", "NOT_FACTUAL", "HUMAN_REVIEWED",
)
CLAIM_REVIEW_STATUS = ("auto", "needs_review", "human_reviewed", "rejected")
EVIDENCE_TYPES = ("textbook", "course_pdf", "pubmed", "web", "guideline", "other")
CLAIM_EVIDENCE_RELATIONSHIPS = (
    "supports", "partially_supports", "contradicts", "insufficient", "related",
)
VERIFICATION_RESULTS = (
    "SUPPORTED", "PARTIALLY_SUPPORTED", "UNSUPPORTED", "CONTRADICTED",
    "INSUFFICIENT_EVIDENCE",
)

_CLAIM_TYPE_SQL = ", ".join(f"'{v}'" for v in CLAIM_TYPES)
_CLAIM_STATUS_SQL = ", ".join(f"'{v}'" for v in CLAIM_VERIFICATION_STATUS)
_CLAIM_REVIEW_SQL = ", ".join(f"'{v}'" for v in CLAIM_REVIEW_STATUS)
_EVIDENCE_TYPE_SQL = ", ".join(f"'{v}'" for v in EVIDENCE_TYPES)
_RELATIONSHIP_SQL = ", ".join(f"'{v}'" for v in CLAIM_EVIDENCE_RELATIONSHIPS)
_VERIFICATION_RESULT_SQL = ", ".join(f"'{v}'" for v in VERIFICATION_RESULTS)

V6_SCHEMA_DDL = f"""
-- 1. Claims (content-addressed: same normalized text = one claim record)
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    claim_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL UNIQUE,
    claim_type TEXT NOT NULL DEFAULT 'fact' CHECK(claim_type IN ({_CLAIM_TYPE_SQL})),
    topic TEXT DEFAULT '',
    curriculum_node_id TEXT NULL REFERENCES curriculum_nodes(id) ON DELETE SET NULL,
    source_labels TEXT DEFAULT '',
    source_file TEXT DEFAULT '',
    generation_run TEXT DEFAULT '',
    verification_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(verification_status IN ({_CLAIM_STATUS_SQL})),
    verification_confidence REAL NULL CHECK(verification_confidence IS NULL OR (verification_confidence >= 0.0 AND verification_confidence <= 1.0)),
    review_status TEXT NOT NULL DEFAULT 'auto' CHECK(review_status IN ({_CLAIM_REVIEW_SQL})),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(verification_status);
CREATE INDEX IF NOT EXISTS idx_claims_topic ON claims(topic);
CREATE INDEX IF NOT EXISTS idx_claims_curriculum ON claims(curriculum_node_id);

-- 2. Evidence records (structured provenance; no invented locators)
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL DEFAULT '',
    chunk_id TEXT DEFAULT '',
    evidence_type TEXT NOT NULL DEFAULT 'other' CHECK(evidence_type IN ({_EVIDENCE_TYPE_SQL})),
    document_id TEXT NULL,
    edition_id TEXT NULL REFERENCES textbook_editions(id) ON DELETE SET NULL,
    textbook_node_id TEXT NULL REFERENCES textbook_nodes(id) ON DELETE SET NULL,
    chapter_title TEXT DEFAULT '',
    section_title TEXT DEFAULT '',
    page_number INTEGER NULL CHECK(page_number IS NULL OR page_number >= 1),
    locator TEXT DEFAULT '',
    url TEXT DEFAULT '',
    quality REAL NOT NULL DEFAULT 0.0 CHECK(quality >= 0.0 AND quality <= 1.0),
    excerpt TEXT NOT NULL,
    excerpt_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_chunk ON evidence(chunk_id);
CREATE INDEX IF NOT EXISTS idx_evidence_edition ON evidence(edition_id);
CREATE INDEX IF NOT EXISTS idx_evidence_type ON evidence(evidence_type);

-- 3. Claim ↔ evidence relationships (one deduplicated edge per pair)
CREATE TABLE IF NOT EXISTS claim_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    relationship TEXT NOT NULL DEFAULT 'related' CHECK(relationship IN ({_RELATIONSHIP_SQL})),
    support_confidence REAL NULL CHECK(support_confidence IS NULL OR (support_confidence >= 0.0 AND support_confidence <= 1.0)),
    verification_method TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(claim_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS idx_claim_evidence_claim ON claim_evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_evidence ON claim_evidence(evidence_id);

-- 4. Verification history (append-only; never overwritten)
CREATE TABLE IF NOT EXISTS verification_runs (
    verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
    evidence_id TEXT NULL REFERENCES evidence(evidence_id) ON DELETE SET NULL,
    method TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL CHECK(result IN ({_VERIFICATION_RESULT_SQL})),
    confidence REAL NULL CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    notes TEXT DEFAULT '',
    verifier TEXT DEFAULT '',
    verifier_version TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_verification_claim ON verification_runs(claim_id);
CREATE INDEX IF NOT EXISTS idx_verification_time ON verification_runs(created_at);
"""

# ─── V7: recency-weighted learner model (P6) ───
# Append-only performance events + a materialized, versioned learner state.
# The legacy learner_mastery table keeps its incremental-mean semantics as a
# compatibility surface; the recency-weighted estimate lives here.
LEARNING_ATTEMPT_ITEM_TYPES = ("session", "card", "question", "concept", "topic", "manual")
LEARNING_ATTEMPT_SOURCES = ("session", "review", "manual", "backfill")
LEARNER_WEAKNESS_ORIGINS = ("manual", "review", "learner_model", "tutor")

_ATTEMPT_ITEM_SQL = ", ".join(f"'{v}'" for v in LEARNING_ATTEMPT_ITEM_TYPES)
_ATTEMPT_SOURCE_SQL = ", ".join(f"'{v}'" for v in LEARNING_ATTEMPT_SOURCES)

V7_SCHEMA_DDL = f"""
-- 1. Learning attempts / performance events (append-only evidence)
CREATE TABLE IF NOT EXISTS learning_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NULL REFERENCES interactive_sessions(id) ON DELETE SET NULL,
    curriculum_node_id TEXT NULL REFERENCES curriculum_nodes(id) ON DELETE SET NULL,
    mastery_key TEXT NOT NULL,
    item_type TEXT NOT NULL DEFAULT 'session' CHECK(item_type IN ({_ATTEMPT_ITEM_SQL})),
    item_id TEXT DEFAULT '',
    presented_at TEXT NULL,
    answered_at TEXT NOT NULL,
    score REAL NOT NULL CHECK(score >= 0.0 AND score <= 1.0),
    correct INTEGER NULL CHECK(correct IS NULL OR correct IN (0, 1)),
    learner_confidence REAL NULL CHECK(learner_confidence IS NULL OR (learner_confidence >= 0.0 AND learner_confidence <= 1.0)),
    response_time_seconds REAL NULL CHECK(response_time_seconds IS NULL OR response_time_seconds >= 0.0),
    source TEXT NOT NULL DEFAULT 'manual' CHECK(source IN ({_ATTEMPT_SOURCE_SQL})),
    content_version TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts_key ON learning_attempts(mastery_key, answered_at);
CREATE INDEX IF NOT EXISTS idx_attempts_time ON learning_attempts(answered_at);
CREATE INDEX IF NOT EXISTS idx_attempts_session ON learning_attempts(session_id);
CREATE INDEX IF NOT EXISTS idx_attempts_item ON learning_attempts(item_type, item_id);

-- 2. Materialized current learner state (one row per mastery key)
CREATE TABLE IF NOT EXISTS learner_model_state (
    mastery_key TEXT PRIMARY KEY,
    curriculum_node_id TEXT NULL REFERENCES curriculum_nodes(id) ON DELETE SET NULL,
    model_version TEXT NOT NULL,
    half_life_days REAL NOT NULL DEFAULT 21.0 CHECK(half_life_days > 0.0),
    mastery_score REAL NOT NULL CHECK(mastery_score >= 0.0 AND mastery_score <= 1.0),
    weighted_evidence REAL NOT NULL DEFAULT 0.0 CHECK(weighted_evidence >= 0.0),
    evidence_count INTEGER NOT NULL DEFAULT 0 CHECK(evidence_count >= 0),
    recent_performance REAL NULL CHECK(recent_performance IS NULL OR (recent_performance >= 0.0 AND recent_performance <= 1.0)),
    historical_performance REAL NULL CHECK(historical_performance IS NULL OR (historical_performance >= 0.0 AND historical_performance <= 1.0)),
    consistency REAL NULL CHECK(consistency IS NULL OR (consistency >= 0.0 AND consistency <= 1.0)),
    uncertainty REAL NOT NULL DEFAULT 1.0 CHECK(uncertainty >= 0.0 AND uncertainty <= 1.0),
    confidence_estimate REAL NULL CHECK(confidence_estimate IS NULL OR (confidence_estimate >= 0.0 AND confidence_estimate <= 1.0)),
    confidence_calibration REAL NULL CHECK(confidence_calibration IS NULL OR (confidence_calibration >= -1.0 AND confidence_calibration <= 1.0)),
    last_attempt_at TEXT NULL,
    last_success_at TEXT NULL,
    last_failure_at TEXT NULL,
    created_at TEXT NOT NULL,
    mastery_updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_state_version ON learner_model_state(model_version);
CREATE INDEX IF NOT EXISTS idx_model_state_node ON learner_model_state(curriculum_node_id);
CREATE INDEX IF NOT EXISTS idx_model_state_score ON learner_model_state(mastery_score);
"""

# Additive V7 columns on the legacy weaknesses table (ALTER TABLE ADD COLUMN;
# applied idempotently by migrate_v7 — existing rows keep sane defaults).
V7_WEAKNESS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("curriculum_node_id", "TEXT NULL"),
    ("origin", "TEXT NOT NULL DEFAULT 'manual'"),
    ("weakness_score", "REAL NULL"),
    ("failure_count", "INTEGER NOT NULL DEFAULT 0"),
    ("recent_failure_rate", "REAL NULL"),
    ("prerequisite_impact", "INTEGER NOT NULL DEFAULT 0"),
    ("review_priority", "REAL NULL"),
    ("low_confidence", "INTEGER NOT NULL DEFAULT 0"),
    ("last_failure_at", "TEXT NULL"),
    ("recovered_at", "TEXT NULL"),
)

# ─── V8: interactive adaptive tutor (P7) ───
# Sessions are an explicit, persisted stage machine (a refresh/restart resumes
# from the stored stage, question and pending answer); turns are the append-only
# transcript and the idempotence key for learning events; questions carry the
# stable, content-addressed items the tutor asks (P8 extends this later).
TUTOR_SESSION_MODES = (
    "explain", "socratic", "drill", "correct", "case", "review",
    "prerequisite_repair",
)
TUTOR_STAGES = (
    "TEACH", "ASK", "WAITING_FOR_ANSWER", "EVALUATE", "EXPLAIN", "ADAPT",
    "COMPLETE",
)
TUTOR_SESSION_STATUSES = ("active", "waiting", "blocked", "completed", "aborted")
TUTOR_QUESTION_TYPES = ("mcq", "short_answer", "recall", "clinical_reasoning")
TUTOR_CORRECTNESS = ("correct", "partial", "incorrect", "ungraded")
TUTOR_ERROR_TYPES = ("none", "minor", "conceptual", "unknown")
TUTOR_GRADING_STATUSES = (
    "graded", "insufficient_evidence", "retryable", "ungraded",
)
TUTOR_TURN_KINDS = ("teach", "ask", "answer", "grade", "explain", "adapt", "summary")

_MODE_SQL = ", ".join(f"'{v}'" for v in TUTOR_SESSION_MODES)
_STAGE_SQL = ", ".join(f"'{v}'" for v in TUTOR_STAGES)
_STATUS_SQL = ", ".join(f"'{v}'" for v in TUTOR_SESSION_STATUSES)
_QTYPE_SQL = ", ".join(f"'{v}'" for v in TUTOR_QUESTION_TYPES)
_CORRECT_SQL = ", ".join(f"'{v}'" for v in TUTOR_CORRECTNESS)
_ETYPE_SQL = ", ".join(f"'{v}'" for v in TUTOR_ERROR_TYPES)
_GSTATUS_SQL = ", ".join(f"'{v}'" for v in TUTOR_GRADING_STATUSES)
_KIND_SQL = ", ".join(f"'{v}'" for v in TUTOR_TURN_KINDS)

V8_SCHEMA_DDL = f"""
-- 1. Tutor sessions (explicit, restart-surviving stage machine)
CREATE TABLE IF NOT EXISTS tutor_sessions (
    tutor_session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    mastery_key TEXT NOT NULL,
    curriculum_node_id TEXT NULL REFERENCES curriculum_nodes(id) ON DELETE SET NULL,
    session_objective TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL CHECK(mode IN ({_MODE_SQL})),
    stage TEXT NOT NULL CHECK(stage IN ({_STAGE_SQL})),
    status TEXT NOT NULL CHECK(status IN ({_STATUS_SQL})),
    target_source TEXT NOT NULL DEFAULT 'recommended',
    target_reason TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT 'quick',
    target_interactions INTEGER NOT NULL DEFAULT 3 CHECK(target_interactions >= 1),
    interaction_count INTEGER NOT NULL DEFAULT 0 CHECK(interaction_count >= 0),
    question_number INTEGER NOT NULL DEFAULT 0 CHECK(question_number >= 0),
    correct_count INTEGER NOT NULL DEFAULT 0 CHECK(correct_count >= 0),
    partial_count INTEGER NOT NULL DEFAULT 0 CHECK(partial_count >= 0),
    incorrect_count INTEGER NOT NULL DEFAULT 0 CHECK(incorrect_count >= 0),
    difficulty INTEGER NOT NULL DEFAULT 2 CHECK(difficulty >= 1 AND difficulty <= 5),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_failures >= 0),
    consecutive_successes INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_successes >= 0),
    concept TEXT DEFAULT '',
    current_question_id TEXT NULL,
    current_explanation TEXT DEFAULT '',
    pending_answer TEXT NULL,
    pending_confidence REAL NULL CHECK(pending_confidence IS NULL OR (pending_confidence >= 0.0 AND pending_confidence <= 1.0)),
    verification_status TEXT NULL,
    evidence_refs TEXT DEFAULT '[]',
    concepts_covered TEXT DEFAULT '[]',
    prerequisites_visited TEXT DEFAULT '[]',
    mastery_at_start REAL NULL,
    confidence_at_start REAL NULL,
    summary TEXT NULL,
    model_error TEXT NULL,
    model_calls INTEGER NOT NULL DEFAULT 0 CHECK(model_calls >= 0),
    review_recorded INTEGER NOT NULL DEFAULT 0 CHECK(review_recorded IN (0, 1)),
    tutor_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    completed_at TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tutor_status ON tutor_sessions(status, last_activity_at);
CREATE INDEX IF NOT EXISTS idx_tutor_key ON tutor_sessions(mastery_key);
CREATE INDEX IF NOT EXISTS idx_tutor_node ON tutor_sessions(curriculum_node_id);

-- 2. Tutor questions (stable, content-addressed items; P8 extends later)
CREATE TABLE IF NOT EXISTS tutor_questions (
    item_id TEXT PRIMARY KEY,
    tutor_session_id INTEGER NULL REFERENCES tutor_sessions(tutor_session_id) ON DELETE SET NULL,
    curriculum_node_id TEXT NULL REFERENCES curriculum_nodes(id) ON DELETE SET NULL,
    topic TEXT NOT NULL DEFAULT '',
    mastery_key TEXT NOT NULL DEFAULT '',
    concept TEXT NOT NULL DEFAULT '',
    question_type TEXT NOT NULL CHECK(question_type IN ({_QTYPE_SQL})),
    difficulty INTEGER NOT NULL DEFAULT 2 CHECK(difficulty >= 1 AND difficulty <= 5),
    prompt TEXT NOT NULL,
    expected_answer TEXT NOT NULL DEFAULT '',
    rubric TEXT DEFAULT '[]',
    options TEXT DEFAULT '[]',
    correct_option INTEGER NULL,
    evidence_refs TEXT DEFAULT '[]',
    verification_status TEXT NOT NULL DEFAULT 'INSUFFICIENT_EVIDENCE',
    question_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tutor_q_session ON tutor_questions(tutor_session_id);
CREATE INDEX IF NOT EXISTS idx_tutor_q_key ON tutor_questions(mastery_key, concept);

-- 3. Tutor turns (append-only transcript + learning-event idempotence)
CREATE TABLE IF NOT EXISTS tutor_turns (
    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tutor_session_id INTEGER NOT NULL REFERENCES tutor_sessions(tutor_session_id) ON DELETE CASCADE,
    turn_number INTEGER NOT NULL CHECK(turn_number >= 1),
    kind TEXT NOT NULL CHECK(kind IN ({_KIND_SQL})),
    stage TEXT NOT NULL DEFAULT '',
    mode TEXT DEFAULT '',
    concept TEXT DEFAULT '',
    question_id TEXT NULL,
    question_type TEXT DEFAULT '',
    difficulty INTEGER NULL,
    prompt TEXT DEFAULT '',
    expected_answer TEXT DEFAULT '',
    rubric TEXT DEFAULT '[]',
    learner_answer TEXT NULL,
    learner_confidence REAL NULL,
    score REAL NULL CHECK(score IS NULL OR (score >= 0.0 AND score <= 1.0)),
    correctness TEXT NULL CHECK(correctness IS NULL OR correctness IN ({_CORRECT_SQL})),
    error_type TEXT NULL CHECK(error_type IS NULL OR error_type IN ({_ETYPE_SQL})),
    explanation TEXT DEFAULT '',
    missing_key_points TEXT DEFAULT '[]',
    incorrect_points TEXT DEFAULT '[]',
    evidence_refs TEXT DEFAULT '[]',
    verification_status TEXT NULL,
    grading_status TEXT NULL CHECK(grading_status IS NULL OR grading_status IN ({_GSTATUS_SQL})),
    grading_source TEXT DEFAULT '',
    injection_suspected INTEGER NOT NULL DEFAULT 0 CHECK(injection_suspected IN (0, 1)),
    attempt_id INTEGER NULL,
    misconception_id INTEGER NULL,
    model_used TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(tutor_session_id, turn_number)
);

CREATE INDEX IF NOT EXISTS idx_tutor_turn_session ON tutor_turns(tutor_session_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_tutor_turn_attempt ON tutor_turns(attempt_id);
"""

# ─── V9: question-level assessment engine (P8) ───
# Item identity is separated from immutable per-version content so a historical
# assessment keeps its exact `item_id + item_version` meaning forever. Sessions
# are separate from tutor sessions; attempts are the question-level record and
# the exposure ledger; quality analytics are appended, never destructive.
ASSESSMENT_ITEM_TYPES = (
    "MCQ_SINGLE", "MCQ_MULTI", "TRUE_FALSE", "SHORT_ANSWER",
    "CLINICAL_REASONING", "RECALL",
)
ASSESSMENT_ITEM_STATUSES = (
    "DRAFT", "VALIDATION", "REVIEW_REQUIRED", "APPROVED", "ACTIVE",
    "RETIRED", "REJECTED",
)
ASSESSMENT_MODES = ("PRACTICE", "EXAM", "REVIEW")
ASSESSMENT_SESSION_STATUSES = (
    "created", "active", "submitted", "completed", "expired", "abandoned",
)
ASSESSMENT_SCOPE_TYPES = ("topic", "seminar", "week", "subject", "custom")
ASSESSMENT_ITEM_CORRECTNESS = ("correct", "partial", "incorrect", "ungraded")
ASSESSMENT_BLUEPRINT_STATUSES = ("DRAFT", "ACTIVE", "RETIRED")

# ─── V10 study-intelligence enums ───
STUDY_PLAN_STATUSES = ("ACTIVE", "COMPLETED", "RETIRED")
STUDY_MISSION_STATUSES = ("created", "active", "completed", "abandoned")
STUDY_ACTION_TYPES = (
    "NEW_TEACHING", "REVIEW", "DRILL", "PREREQUISITE_REPAIR", "TUTOR",
    "ASSESS", "REMEDIATION", "RECALL", "SPACED_REVIEW",
)
STUDY_ACTION_STATUSES = ("pending", "in_progress", "done", "skipped", "failed")
STUDY_TOPIC_STATES = (
    "NOT_STARTED", "IN_PROGRESS", "STUDIED", "ASSESSING",
    "MASTERED_ESTIMATE", "REVIEW_DUE",
)
CONTENT_ARTIFACT_TYPES = (
    "study_guide", "cheat_sheet", "flashcards", "quiz", "mind_map", "script",
)
CONTENT_ARTIFACT_STATUSES = ("DRAFT", "VALIDATING", "READY", "NEEDS_REVIEW", "BLOCKED")
ADAPTATION_PROFILES = ("weak", "developing", "strong", "underconfident", "overconfident")

# ─── V11 publication/review enums ───
PUBLICATION_STATUSES = ("UNREVIEWED", "APPROVED", "PUBLISHED", "RETIRED", "BLOCKED")
REVIEW_SEVERITIES = ("low", "medium", "high", "critical")
REVIEW_STATUSES = ("open", "resolved", "waived")
REVIEW_ISSUE_TYPES = (
    "medical_risk", "evidence_gap", "unsupported_claim", "contradicted_claim",
    "citation_break", "consistency", "copyright", "formatting", "curriculum_gap",
)
EXPORT_MODES = ("private", "distributable")

# ─── V12 video production enums ───
VIDEO_STATUSES = ("queued", "rendering", "READY", "FAILED")
VIDEO_ASPECTS = ("16:9", "9:16", "1:1")

_AITYPE_SQL = ", ".join(f"'{v}'" for v in ASSESSMENT_ITEM_TYPES)
_AISTATUS_SQL = ", ".join(f"'{v}'" for v in ASSESSMENT_ITEM_STATUSES)
_AMODE_SQL = ", ".join(f"'{v}'" for v in ASSESSMENT_MODES)
_ASTATUS_SQL = ", ".join(f"'{v}'" for v in ASSESSMENT_SESSION_STATUSES)
_ASCOPE_SQL = ", ".join(f"'{v}'" for v in ASSESSMENT_SCOPE_TYPES)
_ACORRECT_SQL = ", ".join(f"'{v}'" for v in ASSESSMENT_ITEM_CORRECTNESS)
_ABPSTATUS_SQL = ", ".join(f"'{v}'" for v in ASSESSMENT_BLUEPRINT_STATUSES)

V9_SCHEMA_DDL = f"""
-- 1. Assessment item identity + lifecycle (stable item_id)
CREATE TABLE IF NOT EXISTS assessment_items (
    item_id TEXT PRIMARY KEY,
    item_type TEXT NOT NULL CHECK(item_type IN ({_AITYPE_SQL})),
    status TEXT NOT NULL CHECK(status IN ({_AISTATUS_SQL})),
    current_version INTEGER NOT NULL DEFAULT 1 CHECK(current_version >= 1),
    topic TEXT NOT NULL DEFAULT '',
    mastery_key TEXT NOT NULL DEFAULT '',
    concept TEXT NOT NULL DEFAULT '',
    curriculum_node_id TEXT NULL REFERENCES curriculum_nodes(id) ON DELETE SET NULL,
    author TEXT NOT NULL DEFAULT 'medforge',
    source_kind TEXT NOT NULL DEFAULT 'manual',
    generation_mode TEXT NOT NULL DEFAULT 'manual',
    item_model_version TEXT NOT NULL DEFAULT '',
    quality_flags TEXT NOT NULL DEFAULT '[]',
    retired_at TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assess_item_status ON assessment_items(status, item_type);
CREATE INDEX IF NOT EXISTS idx_assess_item_key ON assessment_items(mastery_key);
CREATE INDEX IF NOT EXISTS idx_assess_item_node ON assessment_items(curriculum_node_id);

-- 2. Immutable item content per version (historical reproducibility)
CREATE TABLE IF NOT EXISTS assessment_item_versions (
    item_id TEXT NOT NULL REFERENCES assessment_items(item_id) ON DELETE CASCADE,
    item_version INTEGER NOT NULL CHECK(item_version >= 1),
    stem TEXT NOT NULL,
    difficulty_target INTEGER NOT NULL DEFAULT 2 CHECK(difficulty_target >= 1 AND difficulty_target <= 5),
    choices TEXT NOT NULL DEFAULT '[]',
    correct_choices TEXT NOT NULL DEFAULT '[]',
    rubric TEXT NOT NULL DEFAULT '[]',
    correct_answer TEXT NOT NULL DEFAULT '',
    explanation TEXT NOT NULL DEFAULT '',
    scoring_policy TEXT NOT NULL DEFAULT '{{}}',
    evidence_requirement TEXT NOT NULL DEFAULT 'SUPPORTED',
    evidence_state TEXT NOT NULL DEFAULT 'INSUFFICIENT_EVIDENCE',
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    claim_refs TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL,
    validation_report TEXT NOT NULL DEFAULT '{{}}',
    duplicate_of TEXT NULL,
    item_model_version TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT 'medforge',
    created_at TEXT NOT NULL,
    UNIQUE(item_id, item_version)
);

CREATE INDEX IF NOT EXISTS idx_assess_ver_item ON assessment_item_versions(item_id, item_version);
CREATE INDEX IF NOT EXISTS idx_assess_ver_hash ON assessment_item_versions(content_hash);

-- 3. Assessment blueprints (deterministic, versioned definition)
CREATE TABLE IF NOT EXISTS assessment_blueprints (
    blueprint_id TEXT PRIMARY KEY,
    blueprint_version INTEGER NOT NULL DEFAULT 1 CHECK(blueprint_version >= 1),
    title TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK(scope_type IN ({_ASCOPE_SQL})),
    scope_node_id TEXT NULL REFERENCES curriculum_nodes(id) ON DELETE SET NULL,
    scope_node_ids TEXT NOT NULL DEFAULT '[]',
    item_count INTEGER NOT NULL CHECK(item_count >= 1 AND item_count <= 500),
    type_distribution TEXT NOT NULL DEFAULT '{{}}',
    difficulty_distribution TEXT NOT NULL DEFAULT '{{}}',
    topic_distribution TEXT NOT NULL DEFAULT '{{}}',
    prerequisite_coverage REAL NOT NULL DEFAULT 0.0 CHECK(prerequisite_coverage >= 0.0 AND prerequisite_coverage <= 1.0),
    time_limit_minutes INTEGER NULL CHECK(time_limit_minutes IS NULL OR time_limit_minutes >= 1),
    pass_threshold REAL NOT NULL DEFAULT 0.70 CHECK(pass_threshold >= 0.0 AND pass_threshold <= 1.0),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ({_ABPSTATUS_SQL})),
    validation_report TEXT NOT NULL DEFAULT '{{}}',
    seed TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assess_bp_scope ON assessment_blueprints(scope_type, scope_node_id);

-- 4. Assessment sessions (separate from tutor sessions)
CREATE TABLE IF NOT EXISTS assessment_sessions (
    assessment_id TEXT PRIMARY KEY,
    learner_key TEXT NOT NULL DEFAULT 'local',
    blueprint_id TEXT NULL REFERENCES assessment_blueprints(blueprint_id) ON DELETE SET NULL,
    blueprint_version INTEGER NULL,
    title TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL CHECK(mode IN ({_AMODE_SQL})),
    scope_type TEXT NOT NULL DEFAULT 'topic' CHECK(scope_type IN ({_ASCOPE_SQL})),
    scope_node_id TEXT NULL,
    status TEXT NOT NULL CHECK(status IN ({_ASTATUS_SQL})),
    item_order TEXT NOT NULL DEFAULT '[]',
    item_count INTEGER NOT NULL DEFAULT 0 CHECK(item_count >= 0),
    current_index INTEGER NOT NULL DEFAULT 0 CHECK(current_index >= 0),
    time_limit_minutes INTEGER NULL CHECK(time_limit_minutes IS NULL OR time_limit_minutes >= 1),
    started_at TEXT NULL,
    expires_at TEXT NULL,
    submitted_at TEXT NULL,
    completed_at TEXT NULL,
    elapsed_seconds REAL NULL CHECK(elapsed_seconds IS NULL OR elapsed_seconds >= 0.0),
    raw_score REAL NOT NULL DEFAULT 0.0 CHECK(raw_score >= 0.0),
    max_score REAL NOT NULL DEFAULT 0.0 CHECK(max_score >= 0.0),
    percentage REAL NULL CHECK(percentage IS NULL OR (percentage >= 0.0 AND percentage <= 100.0)),
    pass_threshold REAL NOT NULL DEFAULT 0.70 CHECK(pass_threshold >= 0.0 AND pass_threshold <= 1.0),
    passed INTEGER NULL CHECK(passed IS NULL OR passed IN (0, 1)),
    grading_pending INTEGER NOT NULL DEFAULT 0 CHECK(grading_pending >= 0),
    summary TEXT NOT NULL DEFAULT '{{}}',
    remediation TEXT NOT NULL DEFAULT '{{}}',
    model_calls INTEGER NOT NULL DEFAULT 0 CHECK(model_calls >= 0),
    review_logged INTEGER NOT NULL DEFAULT 0 CHECK(review_logged IN (0, 1)),
    content_version TEXT NOT NULL DEFAULT '',
    seed TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assess_sess_status ON assessment_sessions(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_assess_sess_key ON assessment_sessions(learner_key);
CREATE INDEX IF NOT EXISTS idx_assess_sess_bp ON assessment_sessions(blueprint_id);

-- 5. Question-level attempts (append-only answers, retry grading in place)
CREATE TABLE IF NOT EXISTS assessment_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id TEXT NOT NULL REFERENCES assessment_sessions(assessment_id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    item_version INTEGER NOT NULL CHECK(item_version >= 1),
    question_order INTEGER NOT NULL CHECK(question_order >= 1),
    stem TEXT NOT NULL DEFAULT '',
    item_type TEXT NOT NULL DEFAULT '',
    mastery_key TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    difficulty INTEGER NOT NULL DEFAULT 2 CHECK(difficulty >= 1 AND difficulty <= 5),
    choices TEXT NOT NULL DEFAULT '[]',
    correct_choices TEXT NOT NULL DEFAULT '[]',
    rubric TEXT NOT NULL DEFAULT '[]',
    correct_answer TEXT NOT NULL DEFAULT '',
    scoring_policy TEXT NOT NULL DEFAULT '{{}}',
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    evidence_state TEXT NOT NULL DEFAULT '',
    presented_at TEXT NULL,
    answered_at TEXT NULL,
    learner_answer TEXT NULL,
    correctness TEXT NULL CHECK(correctness IS NULL OR correctness IN ({_ACORRECT_SQL})),
    score REAL NULL CHECK(score IS NULL OR (score >= 0.0 AND score <= 1.0)),
    max_score REAL NOT NULL DEFAULT 1.0 CHECK(max_score > 0.0),
    confidence REAL NULL CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    response_time_seconds REAL NULL CHECK(response_time_seconds IS NULL OR response_time_seconds >= 0.0),
    grading_status TEXT NOT NULL DEFAULT 'ungraded' CHECK(grading_status IN ({_GSTATUS_SQL})),
    grading_source TEXT NOT NULL DEFAULT '',
    grader_version TEXT NOT NULL DEFAULT '',
    error_type TEXT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    key_points_present TEXT NOT NULL DEFAULT '[]',
    missing_key_points TEXT NOT NULL DEFAULT '[]',
    incorrect_points TEXT NOT NULL DEFAULT '[]',
    grading_history TEXT NOT NULL DEFAULT '[]',
    grading_attempts INTEGER NOT NULL DEFAULT 0 CHECK(grading_attempts >= 0),
    flagged INTEGER NOT NULL DEFAULT 0 CHECK(flagged IN (0, 1)),
    flag_reason TEXT NOT NULL DEFAULT '',
    injection_suspected INTEGER NOT NULL DEFAULT 0 CHECK(injection_suspected IN (0, 1)),
    learning_attempt_id INTEGER NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(assessment_id, question_order)
);

CREATE INDEX IF NOT EXISTS idx_assess_att_session ON assessment_attempts(assessment_id, question_order);
CREATE INDEX IF NOT EXISTS idx_assess_att_item ON assessment_attempts(item_id, item_version);
CREATE INDEX IF NOT EXISTS idx_assess_att_answered ON assessment_attempts(answered_at);

-- 6. Item quality analytics (append-only; nothing is auto-deleted)
CREATE TABLE IF NOT EXISTS assessment_item_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    item_version INTEGER NOT NULL,
    sample_size INTEGER NOT NULL DEFAULT 0 CHECK(sample_size >= 0),
    metrics TEXT NOT NULL DEFAULT '{{}}',
    flags TEXT NOT NULL DEFAULT '[]',
    computed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assess_quality_item ON assessment_item_quality(item_id, item_version);
"""

_SPLANS_SQL = ", ".join(f"'{v}'" for v in STUDY_PLAN_STATUSES)
_SMISS_SQL = ", ".join(f"'{v}'" for v in STUDY_MISSION_STATUSES)
_SATYPE_SQL = ", ".join(f"'{v}'" for v in STUDY_ACTION_TYPES)
_SASTATUS_SQL = ", ".join(f"'{v}'" for v in STUDY_ACTION_STATUSES)
_SCART_SQL = ", ".join(f"'{v}'" for v in CONTENT_ARTIFACT_TYPES)
_SCAS_SQL = ", ".join(f"'{v}'" for v in CONTENT_ARTIFACT_STATUSES)
_SPROF_SQL = ", ".join(f"'{v}'" for v in ADAPTATION_PROFILES)

V10_SCHEMA_DDL = f"""
-- 1. Persistent, reproducible study plans (never rewritten; new versions append)
CREATE TABLE IF NOT EXISTS study_plans (
    plan_id TEXT PRIMARY KEY,
    learner_key TEXT NOT NULL DEFAULT 'local',
    title TEXT NOT NULL DEFAULT '',
    scope_type TEXT NOT NULL DEFAULT 'subject',
    scope_node_id TEXT NULL,
    scope_titles TEXT NOT NULL DEFAULT '[]',
    objective TEXT NOT NULL DEFAULT '',
    target_date TEXT NULL,
    daily_minutes INTEGER NOT NULL DEFAULT 30 CHECK(daily_minutes >= 5 AND daily_minutes <= 480),
    priorities TEXT NOT NULL DEFAULT '[]',
    actions TEXT NOT NULL DEFAULT '[]',
    gaps TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ({_SPLANS_SQL})),
    planner_version TEXT NOT NULL DEFAULT '',
    config TEXT NOT NULL DEFAULT '{{}}',
    seed TEXT NOT NULL DEFAULT '',
    plan_version INTEGER NOT NULL DEFAULT 1 CHECK(plan_version >= 1),
    supersedes_plan_id TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_study_plans_status ON study_plans(status, updated_at);

-- 2. Study missions: orchestrated sequences over P7/P8/SR with explicit completion
CREATE TABLE IF NOT EXISTS study_missions (
    mission_id TEXT PRIMARY KEY,
    plan_id TEXT NULL REFERENCES study_plans(plan_id) ON DELETE SET NULL,
    learner_key TEXT NOT NULL DEFAULT 'local',
    topic TEXT NOT NULL,
    mastery_key TEXT NOT NULL DEFAULT '',
    curriculum_node_id TEXT NULL REFERENCES curriculum_nodes(id) ON DELETE SET NULL,
    objective TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL CHECK(action_type IN ({_SATYPE_SQL})),
    adaptation_profile TEXT NOT NULL DEFAULT 'developing' CHECK(adaptation_profile IN ({_SPROF_SQL})),
    steps TEXT NOT NULL DEFAULT '[]',
    estimated_minutes INTEGER NOT NULL DEFAULT 20 CHECK(estimated_minutes >= 1),
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    expected_outcome TEXT NOT NULL DEFAULT '',
    completion_criteria TEXT NOT NULL DEFAULT '{{}}',
    status TEXT NOT NULL DEFAULT 'created' CHECK(status IN ({_SMISS_SQL})),
    tutor_session_id TEXT NULL,
    assessment_id TEXT NULL,
    current_step INTEGER NOT NULL DEFAULT 0 CHECK(current_step >= 0),
    results TEXT NOT NULL DEFAULT '{{}}',
    started_at TEXT NULL,
    completed_at TEXT NULL,
    study_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_study_missions_status ON study_missions(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_study_missions_topic ON study_missions(topic);

-- 3. Append-only action history (auditable; one row per completed/attempted action)
CREATE TABLE IF NOT EXISTS study_actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NULL REFERENCES study_missions(mission_id) ON DELETE SET NULL,
    plan_id TEXT NULL,
    topic TEXT NOT NULL DEFAULT '',
    mastery_key TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL CHECK(action_type IN ({_SATYPE_SQL})),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ({_SASTATUS_SQL})),
    reason TEXT NOT NULL DEFAULT '',
    engine_ref TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '{{}}',
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_study_actions_topic ON study_actions(topic, occurred_at);
CREATE INDEX IF NOT EXISTS idx_study_actions_mission ON study_actions(mission_id);

-- 4. Canonical evidence-grounded content (one per topic+sources+config)
CREATE TABLE IF NOT EXISTS content_items (
    content_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    mastery_key TEXT NOT NULL DEFAULT '',
    curriculum_node_id TEXT NULL REFERENCES curriculum_nodes(id) ON DELETE SET NULL,
    sources_digest TEXT NOT NULL DEFAULT '',
    config_digest TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '{{}}',
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    generation_mode TEXT NOT NULL DEFAULT 'model',
    content_version TEXT NOT NULL DEFAULT '',
    study_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_items_topic ON content_items(topic, created_at);

-- 5. Rendered artifacts with provenance + quality-gate status
CREATE TABLE IF NOT EXISTS content_artifacts (
    artifact_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES content_items(content_id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL CHECK(artifact_type IN ({_SCART_SQL})),
    artifact_version INTEGER NOT NULL DEFAULT 1 CHECK(artifact_version >= 1),
    adaptation_profile TEXT NOT NULL DEFAULT 'developing' CHECK(adaptation_profile IN ({_SPROF_SQL})),
    path TEXT NOT NULL DEFAULT '',
    checksum TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ({_SCAS_SQL})),
    validation TEXT NOT NULL DEFAULT '{{}}',
    render_mode TEXT NOT NULL DEFAULT 'deterministic',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_artifacts_content ON content_artifacts(content_id);
CREATE INDEX IF NOT EXISTS idx_content_artifacts_topic_type ON content_artifacts(artifact_type);
"""

# V11 publication enums (SQL fragments)
_PUBSQL = ", ".join(f"'{v}'" for v in PUBLICATION_STATUSES)
_RSEVSQL = ", ".join(f"'{v}'" for v in REVIEW_SEVERITIES)
_RSTATSQL = ", ".join(f"'{v}'" for v in REVIEW_STATUSES)
_RITYPESQL = ", ".join(f"'{v}'" for v in REVIEW_ISSUE_TYPES)

V11_SCHEMA_DDL = f"""
-- 1. Detected issues needing human review (one row per issue)
CREATE TABLE IF NOT EXISTS review_queue (
    review_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES content_items(content_id) ON DELETE CASCADE,
    artifact_id TEXT NULL,
    issue_type TEXT NOT NULL CHECK(issue_type IN ({_RITYPESQL})),
    severity TEXT NOT NULL CHECK(severity IN ({_RSEVSQL})),
    claim_ref TEXT NULL,
    evidence_ref TEXT NULL,
    detected_reason TEXT NOT NULL DEFAULT '',
    detected_by TEXT NOT NULL DEFAULT 'gate',
    review_status TEXT NOT NULL DEFAULT 'open' CHECK(review_status IN ({_RSTATSQL})),
    reviewer TEXT NULL,
    reviewed_at TEXT NULL,
    resolution TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_queue_content ON review_queue(content_id, review_status);
CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(review_status, severity);

-- 2. Append-only review decision history
CREATE TABLE IF NOT EXISTS review_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL REFERENCES review_queue(review_id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    reviewer TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_history_review ON review_history(review_id);

-- 3. One row per approval-gate evaluation batch (success OR failure)
CREATE TABLE IF NOT EXISTS approval_records (
    approval_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES content_items(content_id) ON DELETE CASCADE,
    artifact_id TEXT NULL,
    requested_status TEXT NOT NULL,
    gates TEXT NOT NULL DEFAULT '[]',
    passed INTEGER NOT NULL DEFAULT 0 CHECK(passed IN (0, 1)),
    reviewer TEXT NULL,
    approved_at TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_records_content ON approval_records(content_id, created_at);
"""

V12_SCHEMA_DDL = """
-- P11 video production: one row per (content item, aspect) render attempt.
CREATE TABLE IF NOT EXISTS video_renders (
    video_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES content_items(content_id) ON DELETE CASCADE,
    aspect TEXT NOT NULL CHECK(aspect IN ('16:9', '9:16', '1:1')),
    status TEXT NOT NULL CHECK(status IN ('queued', 'rendering', 'READY', 'FAILED')),
    path TEXT NULL,
    manifest_path TEXT NULL,
    duration_s REAL NULL,
    width INTEGER NULL,
    height INTEGER NULL,
    size_bytes INTEGER NULL,
    scene_count INTEGER NULL,
    renderer_version TEXT NULL,
    storyboard_checksum TEXT NULL,
    error TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_renders_content ON video_renders(content_id);
CREATE INDEX IF NOT EXISTS idx_video_renders_status ON video_renders(status);
"""

# V13 provider abstraction enums
PROVIDER_TYPES = ("ollama", "lmstudio", "openai", "mock")
MODEL_RUN_TASKS = ("teaching", "grading", "generation", "assessment", "reasoning", "general", "embedding")

V13_SCHEMA_DDL = """
-- P12 provider abstraction: model run audit trail
CREATE TABLE IF NOT EXISTS model_runs (
    run_id TEXT PRIMARY KEY,
    task TEXT NOT NULL CHECK(task IN ('teaching', 'grading', 'generation', 'assessment', 'reasoning', 'general', 'embedding')),
    model TEXT NOT NULL,
    provider TEXT NOT NULL CHECK(provider IN ('ollama', 'lmstudio', 'openai', 'mock')),
    provider_display TEXT NOT NULL DEFAULT '',
    content_id TEXT NULL REFERENCES content_items(content_id) ON DELETE SET NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0 CHECK(tokens_in >= 0),
    tokens_out INTEGER NOT NULL DEFAULT 0 CHECK(tokens_out >= 0),
    latency_ms INTEGER NOT NULL DEFAULT 0 CHECK(latency_ms >= 0),
    success INTEGER NOT NULL DEFAULT 0 CHECK(success IN (0, 1)),
    error TEXT NULL,
    model_size_gb REAL NULL,
    model_context INTEGER NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_runs_task ON model_runs(task, created_at);
CREATE INDEX IF NOT EXISTS idx_model_runs_content ON model_runs(content_id);
CREATE INDEX IF NOT EXISTS idx_model_runs_provider ON model_runs(provider, model);
"""

V13_TABLES = ("model_runs",)

# V3 Schema DDL statements
V3_SCHEMA_DDL = """
-- Migration tracking table
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

-- 1. Curriculum Hierarchy (360 ECTS European Medical Degree Architecture)
CREATE TABLE IF NOT EXISTS curriculum_nodes (
    id TEXT PRIMARY KEY,
    parent_id TEXT NULL REFERENCES curriculum_nodes(id) ON DELETE SET NULL,
    node_type TEXT NOT NULL CHECK(node_type IN ('Year', 'Semester', 'Course', 'Module', 'Topic', 'Subtopic', 'Learning Objective')),
    code TEXT DEFAULT '',
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    year INTEGER NULL CHECK(year IS NULL OR (year >= 1 AND year <= 6)),
    semester INTEGER NULL CHECK(semester IS NULL OR (semester >= 1 AND semester <= 12)),
    ects_weight REAL NOT NULL DEFAULT 0.0 CHECK(ects_weight >= 0.0),
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_curriculum_parent ON curriculum_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_curriculum_type ON curriculum_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_curriculum_year_sem ON curriculum_nodes(year, semester);

-- 2. Topic Prerequisite Dependency Graph
CREATE TABLE IF NOT EXISTS prerequisites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id TEXT NOT NULL REFERENCES curriculum_nodes(id) ON DELETE CASCADE,
    prerequisite_id TEXT NOT NULL REFERENCES curriculum_nodes(id) ON DELETE CASCADE,
    dependency_weight REAL NOT NULL DEFAULT 1.0 CHECK(dependency_weight >= 0.0 AND dependency_weight <= 1.0),
    relationship_type TEXT NOT NULL DEFAULT 'strict' CHECK(relationship_type IN ('strict', 'recommended', 'co-requisite')),
    created_at TEXT NOT NULL,
    UNIQUE(topic_id, prerequisite_id)
);

CREATE INDEX IF NOT EXISTS idx_prereq_topic ON prerequisites(topic_id);
CREATE INDEX IF NOT EXISTS idx_prereq_prerequisite ON prerequisites(prerequisite_id);

-- 3. Learner Topic Mastery
CREATE TABLE IF NOT EXISTS learner_mastery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id TEXT NOT NULL UNIQUE,
    mastery_score REAL NOT NULL DEFAULT 0.0 CHECK(mastery_score >= 0.0 AND mastery_score <= 100.0),
    confidence_score REAL NOT NULL DEFAULT 0.0 CHECK(confidence_score >= 0.0 AND confidence_score <= 100.0),
    total_attempts INTEGER NOT NULL DEFAULT 0 CHECK(total_attempts >= 0),
    successful_attempts INTEGER NOT NULL DEFAULT 0 CHECK(successful_attempts >= 0),
    last_attempt_at TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mastery_topic ON learner_mastery(topic_id);
CREATE INDEX IF NOT EXISTS idx_mastery_score ON learner_mastery(mastery_score);

-- 4. Learner Misconceptions & Diagnostic Weaknesses
CREATE TABLE IF NOT EXISTS learner_weaknesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id TEXT NOT NULL,
    concept TEXT NOT NULL,
    misconception TEXT NOT NULL,
    error_count INTEGER NOT NULL DEFAULT 1 CHECK(error_count >= 1),
    severity TEXT NOT NULL DEFAULT 'medium' CHECK(severity IN ('low', 'medium', 'high', 'critical')),
    is_resolved INTEGER NOT NULL DEFAULT 0 CHECK(is_resolved IN (0, 1)),
    resolved_at TEXT NULL,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_weakness_topic ON learner_weaknesses(topic_id);
CREATE INDEX IF NOT EXISTS idx_weakness_status ON learner_weaknesses(is_resolved, severity);

-- 5. Interactive Study Sessions
CREATE TABLE IF NOT EXISTS interactive_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_type TEXT NOT NULL CHECK(session_type IN ('Learn', 'Active Recall', 'Case', 'Viva', 'Prerequisite Repair')),
    topic_id TEXT NOT NULL,
    score REAL DEFAULT 0.0 CHECK(score >= 0.0 AND score <= 100.0),
    duration_seconds INTEGER NOT NULL DEFAULT 0 CHECK(duration_seconds >= 0),
    metrics TEXT DEFAULT '{}',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_type ON interactive_sessions(session_type);
CREATE INDEX IF NOT EXISTS idx_sessions_topic ON interactive_sessions(topic_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON interactive_sessions(created_at);

-- 6. Spaced Repetition Queue (SM-2 / FSRS scheduling parameters)
CREATE TABLE IF NOT EXISTS spaced_repetition_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL DEFAULT 'card' CHECK(item_type IN ('card', 'topic', 'concept')),
    item_id TEXT NOT NULL,
    repetition_count INTEGER NOT NULL DEFAULT 0 CHECK(repetition_count >= 0),
    interval_days REAL NOT NULL DEFAULT 0.0 CHECK(interval_days >= 0.0),
    ease_factor REAL NOT NULL DEFAULT 2.50 CHECK(ease_factor >= 1.30),
    stability REAL DEFAULT 0.0 CHECK(stability >= 0.0),
    difficulty REAL DEFAULT 0.0 CHECK(difficulty >= 0.0 AND difficulty <= 10.0),
    due_date TEXT NOT NULL,
    last_reviewed_at TEXT NULL,
    last_grade INTEGER NULL CHECK(last_grade IS NULL OR (last_grade >= 0 AND last_grade <= 5)),
    state TEXT NOT NULL DEFAULT 'new' CHECK(state IN ('new', 'learning', 'review', 'relearning')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(item_type, item_id)
);

CREATE INDEX IF NOT EXISTS idx_sr_due_date ON spaced_repetition_queue(due_date);
CREATE INDEX IF NOT EXISTS idx_sr_state ON spaced_repetition_queue(state);
CREATE INDEX IF NOT EXISTS idx_sr_item ON spaced_repetition_queue(item_type, item_id);

-- 6b. Spaced Repetition Review History (append-only log of every review)
CREATE TABLE IF NOT EXISTS review_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL DEFAULT 'card' CHECK(item_type IN ('card', 'topic', 'concept')),
    item_id TEXT NOT NULL,
    grade INTEGER NOT NULL CHECK(grade >= 0 AND grade <= 5),
    scheduler TEXT NOT NULL DEFAULT 'sm2',
    interval_before REAL NOT NULL DEFAULT 0.0 CHECK(interval_before >= 0.0),
    interval_after REAL NOT NULL DEFAULT 0.0 CHECK(interval_after >= 0.0),
    ease_before REAL NOT NULL DEFAULT 2.50 CHECK(ease_before >= 1.30),
    ease_after REAL NOT NULL DEFAULT 2.50 CHECK(ease_after >= 1.30),
    state_before TEXT NOT NULL DEFAULT 'new' CHECK(state_before IN ('new', 'learning', 'review', 'relearning')),
    state_after TEXT NOT NULL DEFAULT 'new' CHECK(state_after IN ('new', 'learning', 'review', 'relearning')),
    reviewed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_log_item ON review_log(item_type, item_id);
CREATE INDEX IF NOT EXISTS idx_review_log_time ON review_log(reviewed_at);

-- 7. Medical Evidence Provenance
CREATE TABLE IF NOT EXISTS medical_sources (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT DEFAULT '',
    publication_type TEXT NOT NULL CHECK(publication_type IN ('Guideline', 'Textbook', 'Meta-Analysis', 'Research', 'Educational')),
    is_human_study INTEGER NOT NULL DEFAULT 1 CHECK(is_human_study IN (0, 1)),
    study_design TEXT DEFAULT '',
    journal_or_publisher TEXT DEFAULT '',
    publication_year INTEGER NULL,
    pmid TEXT DEFAULT '',
    doi TEXT DEFAULT '',
    url TEXT DEFAULT '',
    evidence_level TEXT DEFAULT '',
    trust_score REAL DEFAULT 0.8 CHECK(trust_score >= 0.0 AND trust_score <= 1.0),
    raw_metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_type ON medical_sources(publication_type);
CREATE INDEX IF NOT EXISTS idx_sources_human ON medical_sources(is_human_study);
CREATE INDEX IF NOT EXISTS idx_sources_pmid ON medical_sources(pmid);
CREATE INDEX IF NOT EXISTS idx_sources_doi ON medical_sources(doi);
"""

# V2 legacy schema statements for new clean databases or test environments
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
