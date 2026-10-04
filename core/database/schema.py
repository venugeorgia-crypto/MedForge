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
