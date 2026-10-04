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
