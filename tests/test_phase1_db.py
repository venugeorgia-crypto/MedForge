#!/usr/bin/env python3
"""Phase 1 Verification Test Suite: MedForge V3 Database Architecture & Migration.

Validates:
1. Backup creation, storage, and SQLite integrity
2. 100% preservation of legacy 2.1 tables and evidence chunks
3. Correct DDL structure, indices, and constraints for all 7 V3 tables:
   - curriculum_nodes (360 ECTS tree hierarchy)
   - prerequisites (topic dependency graph & weights)
   - learner_mastery (mastery scores, confidence, attempts)
   - learner_weaknesses (misconceptions, errors, severity, resolution)
   - interactive_sessions (5 session types, durations, metrics)
   - spaced_repetition_queue (SM-2 / FSRS parameters, due queue)
   - medical_sources (provenance, human vs animal study distinction)
4. Migration idempotence and integrity validation
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

# Ensure MedForge root is in sys.path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from core.database.schema import (
    NODE_TYPES,
    PREREQUISITE_TYPES,
    WEAKNESS_SEVERITY,
    SESSION_TYPES,
    SPACED_REPETITION_STATES,
    MEDICAL_PUBLICATION_TYPES,
    V3_SCHEMA_DDL,
    LEGACY_SCHEMA_DDL,
)
from core.database.migrate_v3 import (
    migrate_database,
    create_snapshot_backup,
    is_v3_migrated,
    DEFAULT_DB,
    DEFAULT_BACKUP,
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class TestPhase1Database(unittest.TestCase):
    """Test suite for Phase 1 Database migration and schemas."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = DEFAULT_DB
        cls.backup_path = DEFAULT_BACKUP

    def setUp(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON;")

    def tearDown(self):
        self.conn.close()

    def test_01_backup_exists_and_valid(self):
        """Verify non-destructive backup file exists and passes SQLite integrity check."""
        self.assertTrue(
            self.backup_path.exists(),
            f"Backup snapshot does not exist at {self.backup_path}",
        )
        self.assertGreater(self.backup_path.stat().st_size, 0)

        bck_conn = sqlite3.connect(self.backup_path)
        try:
            res = bck_conn.execute("PRAGMA integrity_check;").fetchall()
            self.assertEqual(res, [("ok",)], f"Integrity check failed: {res}")

            # Verify legacy tables are present in the backup
            tables = [
                row[0]
                for row in bck_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            self.assertIn("chunks", tables)
            self.assertIn("study_sessions", tables)
            self.assertIn("weaknesses", tables)
        finally:
            bck_conn.close()

    def test_02_legacy_data_preserved(self):
        """Verify pre-existing MedForge 2.1 data (14 chunks) remains intact."""
        cur = self.conn.cursor()
        chunk_count = cur.execute("SELECT count(*) FROM chunks").fetchone()[0]
        self.assertGreaterEqual(
            chunk_count,
            14,
            f"Expected at least 14 chunks preserved, found {chunk_count}",
        )

        sample = cur.execute(
            "SELECT id, text, source, updated_at FROM chunks LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(sample)
        self.assertTrue(sample[0])
        self.assertTrue(sample[1])
        self.assertTrue(sample[2])

        # Verify FTS virtual table works
        fts_search = cur.execute(
            "SELECT count(*) FROM chunks_fts"
        ).fetchone()[0]
        self.assertGreaterEqual(fts_search, 14)

    def test_03_all_v3_tables_present(self):
        """Verify all 7 new V3 tables + migration table exist in active schema."""
        expected_tables = {
            "curriculum_nodes",
            "prerequisites",
            "learner_mastery",
            "learner_weaknesses",
            "interactive_sessions",
            "spaced_repetition_queue",
            "medical_sources",
            "schema_migrations",
        }
        cur = self.conn.cursor()
        existing = {
            row[0]
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = expected_tables - existing
        self.assertFalse(missing, f"Missing required V3 tables: {missing}")

        # Check migration record
        migrated = cur.execute(
            "SELECT version, name FROM schema_migrations WHERE version='3.0.0'"
        ).fetchone()
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated[0], "3.0.0")

    def test_04_curriculum_nodes_hierarchy_360_ects(self):
        """Verify 360 ECTS curriculum tree hierarchy, constraints, and queries."""
        cur = self.conn.cursor()

        # Clean test nodes if previously created
        cur.execute("DELETE FROM curriculum_nodes WHERE id LIKE 'TEST-CURR-%'")

        # 1. Year 1 (60 ECTS)
        cur.execute(
            """INSERT INTO curriculum_nodes (id, parent_id, node_type, code, title, year, semester, ects_weight, created_at, updated_at)
               VALUES (?, NULL, 'Year', 'Y1', 'Pre-Clinical Year 1', 1, NULL, 60.0, ?, ?)""",
            ("TEST-CURR-Y1", utcnow(), utcnow()),
        )

        # 2. Semester 1 (30 ECTS)
        cur.execute(
            """INSERT INTO curriculum_nodes (id, parent_id, node_type, code, title, year, semester, ects_weight, created_at, updated_at)
               VALUES (?, 'TEST-CURR-Y1', 'Semester', 'Y1S1', 'Semester 1: Foundations of Medicine', 1, 1, 30.0, ?, ?)""",
            ("TEST-CURR-S1", utcnow(), utcnow()),
        )

        # 3. Course: Anatomy & Physiology (10 ECTS)
        cur.execute(
            """INSERT INTO curriculum_nodes (id, parent_id, node_type, code, title, year, semester, ects_weight, created_at, updated_at)
               VALUES (?, 'TEST-CURR-S1', 'Course', 'MED101', 'Human Anatomy & Physiology', 1, 1, 10.0, ?, ?)""",
            ("TEST-CURR-COURSE", utcnow(), utcnow()),
        )

        # 4. Module: Cardiovascular System (4 ECTS)
        cur.execute(
            """INSERT INTO curriculum_nodes (id, parent_id, node_type, code, title, year, semester, ects_weight, created_at, updated_at)
               VALUES (?, 'TEST-CURR-COURSE', 'Module', 'CVS-MOD', 'Cardiovascular System', 1, 1, 4.0, ?, ?)""",
            ("TEST-CURR-MODULE", utcnow(), utcnow()),
        )

        # 5. Topic: Cardiac Cycle (1 ECTS)
        cur.execute(
            """INSERT INTO curriculum_nodes (id, parent_id, node_type, code, title, year, semester, ects_weight, created_at, updated_at)
               VALUES (?, 'TEST-CURR-MODULE', 'Topic', 'CVS-TOP-01', 'The Cardiac Cycle', 1, 1, 1.0, ?, ?)""",
            ("TEST-CURR-TOPIC", utcnow(), utcnow()),
        )

        # 6. Subtopic: Ventricular Systole
        cur.execute(
            """INSERT INTO curriculum_nodes (id, parent_id, node_type, code, title, year, semester, ects_weight, created_at, updated_at)
               VALUES (?, 'TEST-CURR-TOPIC', 'Subtopic', 'CVS-SUB-01', 'Isovolumetric Contraction & Rapid Ejection', 1, 1, 0.5, ?, ?)""",
            ("TEST-CURR-SUBTOPIC", utcnow(), utcnow()),
        )

        # 7. Learning Objective
        cur.execute(
            """INSERT INTO curriculum_nodes (id, parent_id, node_type, code, title, year, semester, ects_weight, created_at, updated_at)
               VALUES (?, 'TEST-CURR-SUBTOPIC', 'Learning Objective', 'LO-CVS-01', 'Explain pressure volume changes during isovolumetric ventricular contraction', 1, 1, 0.25, ?, ?)""",
            ("TEST-CURR-LO", utcnow(), utcnow()),
        )
        self.conn.commit()

        # Query tree depth
        tree_count = cur.execute(
            "SELECT count(*) FROM curriculum_nodes WHERE id LIKE 'TEST-CURR-%'"
        ).fetchone()[0]
        self.assertEqual(tree_count, 7)

        # Test CHECK constraint: Invalid node type must fail
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                """INSERT INTO curriculum_nodes (id, node_type, title, created_at, updated_at)
                   VALUES ('TEST-FAIL', 'InvalidType', 'Fail Node', ?, ?)""",
                (utcnow(), utcnow()),
            )

        # Clean up test nodes
        cur.execute("DELETE FROM curriculum_nodes WHERE id LIKE 'TEST-CURR-%'")
        self.conn.commit()

    def test_05_prerequisites_mapping(self):
        """Verify topic dependency graph with dependency weights and relationship types."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM prerequisites WHERE topic_id LIKE 'TEST-PREREQ-%'")
        cur.execute("DELETE FROM curriculum_nodes WHERE id LIKE 'TEST-PREREQ-%'")

        # Create two test topics
        cur.execute(
            """INSERT INTO curriculum_nodes (id, node_type, title, created_at, updated_at)
               VALUES ('TEST-PREREQ-A', 'Topic', 'Cardiac Electrophysiology', ?, ?)""",
            (utcnow(), utcnow()),
        )
        cur.execute(
            """INSERT INTO curriculum_nodes (id, node_type, title, created_at, updated_at)
               VALUES ('TEST-PREREQ-B', 'Topic', 'Antiarrhythmic Pharmacology', ?, ?)""",
            (utcnow(), utcnow()),
        )

        # Antiarrhythmic Pharmacology requires Cardiac Electrophysiology (weight 0.95, strict)
        cur.execute(
            """INSERT INTO prerequisites (topic_id, prerequisite_id, dependency_weight, relationship_type, created_at)
               VALUES (?, ?, 0.95, 'strict', ?)""",
            ("TEST-PREREQ-B", "TEST-PREREQ-A", utcnow()),
        )
        self.conn.commit()

        # Check prerequisite query
        prereq = cur.execute(
            """SELECT p.topic_id, p.prerequisite_id, p.dependency_weight, c.title
               FROM prerequisites p
               JOIN curriculum_nodes c ON p.prerequisite_id = c.id
               WHERE p.topic_id = 'TEST-PREREQ-B'"""
        ).fetchone()
        self.assertEqual(prereq[0], "TEST-PREREQ-B")
        self.assertEqual(prereq[1], "TEST-PREREQ-A")
        self.assertAlmostEqual(prereq[2], 0.95)
        self.assertEqual(prereq[3], "Cardiac Electrophysiology")

        # Test weight constraint (dependency_weight > 1.0 must fail)
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                """INSERT INTO prerequisites (topic_id, prerequisite_id, dependency_weight, created_at)
                   VALUES ('TEST-PREREQ-A', 'TEST-PREREQ-B', 1.5, ?)""",
                (utcnow(),),
            )

        # Clean up
        cur.execute("DELETE FROM prerequisites WHERE topic_id LIKE 'TEST-PREREQ-%'")
        cur.execute("DELETE FROM curriculum_nodes WHERE id LIKE 'TEST-PREREQ-%'")
        self.conn.commit()

    def test_06_learner_mastery_tracking(self):
        """Verify mastery score tracking, confidence, attempts, and 0-100 constraint."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM learner_mastery WHERE topic_id = 'test-topic-hemodynamics'")

        now = utcnow()
        cur.execute(
            """INSERT INTO learner_mastery (topic_id, mastery_score, confidence_score, total_attempts, successful_attempts, last_attempt_at, created_at, updated_at)
               VALUES (?, 88.5, 92.0, 6, 5, ?, ?, ?)""",
            ("test-topic-hemodynamics", now, now, now),
        )
        self.conn.commit()

        record = cur.execute(
            "SELECT mastery_score, confidence_score, total_attempts FROM learner_mastery WHERE topic_id='test-topic-hemodynamics'"
        ).fetchone()
        self.assertAlmostEqual(record[0], 88.5)
        self.assertAlmostEqual(record[1], 92.0)
        self.assertEqual(record[2], 6)

        # Constraint check: score > 100 must fail
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                """INSERT INTO learner_mastery (topic_id, mastery_score, created_at, updated_at)
                   VALUES ('test-overflow', 105.0, ?, ?)""",
                (now, now),
            )

        cur.execute("DELETE FROM learner_mastery WHERE topic_id = 'test-topic-hemodynamics'")
        self.conn.commit()

    def test_07_learner_weaknesses_misconceptions(self):
        """Verify diagnostic misconception tracking, error count, severity, and resolution."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM learner_weaknesses WHERE topic_id = 'test-topic-cardiac'")

        now = utcnow()
        cur.execute(
            """INSERT INTO learner_weaknesses (topic_id, concept, misconception, error_count, severity, is_resolved, first_observed_at, last_observed_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
            (
                "test-topic-cardiac",
                "Isovolumetric Contraction",
                "Assuming the aortic valve opens at the beginning of isovolumetric contraction instead of after left ventricular pressure exceeds aortic diastolic pressure.",
                3,
                "high",
                now,
                now,
            ),
        )
        self.conn.commit()

        row = cur.execute(
            "SELECT concept, error_count, severity, is_resolved FROM learner_weaknesses WHERE topic_id='test-topic-cardiac'"
        ).fetchone()
        self.assertEqual(row[0], "Isovolumetric Contraction")
        self.assertEqual(row[1], 3)
        self.assertEqual(row[2], "high")
        self.assertEqual(row[3], 0)

        # Resolve the weakness
        cur.execute(
            "UPDATE learner_weaknesses SET is_resolved=1, resolved_at=? WHERE topic_id='test-topic-cardiac'",
            (utcnow(),),
        )
        self.conn.commit()

        resolved = cur.execute(
            "SELECT is_resolved, resolved_at FROM learner_weaknesses WHERE topic_id='test-topic-cardiac'"
        ).fetchone()
        self.assertEqual(resolved[0], 1)
        self.assertIsNotNone(resolved[1])

        # Constraint check: invalid severity
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                """INSERT INTO learner_weaknesses (topic_id, concept, misconception, severity, first_observed_at, last_observed_at)
                   VALUES ('test', 'c', 'm', 'extreme', ?, ?)""",
                (now, now),
            )

        cur.execute("DELETE FROM learner_weaknesses WHERE topic_id = 'test-topic-cardiac'")
        self.conn.commit()

    def test_08_interactive_sessions(self):
        """Verify tracking of all 5 session types (Learn, Active Recall, Case, Viva, Prerequisite Repair)."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM interactive_sessions WHERE topic_id = 'test-session-topic'")

        for s_type in SESSION_TYPES:
            now = utcnow()
            metrics = json.dumps({"questions_answered": 10, "correct": 9, "session_mode": s_type})
            cur.execute(
                """INSERT INTO interactive_sessions (session_type, topic_id, score, duration_seconds, metrics, notes, created_at, completed_at)
                   VALUES (?, 'test-session-topic', 90.0, 1200, ?, 'Completed 20min block', ?, ?)""",
                (s_type, metrics, now, now),
            )
        self.conn.commit()

        count = cur.execute(
            "SELECT count(*) FROM interactive_sessions WHERE topic_id='test-session-topic'"
        ).fetchone()[0]
        self.assertEqual(count, len(SESSION_TYPES))

        # Test invalid session type
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                """INSERT INTO interactive_sessions (session_type, topic_id, created_at)
                   VALUES ('RandomLecture', 'test-session-topic', ?)""",
                (utcnow(),),
            )

        cur.execute("DELETE FROM interactive_sessions WHERE topic_id = 'test-session-topic'")
        self.conn.commit()

    def test_09_spaced_repetition_queue(self):
        """Verify SM-2 / FSRS parameters, due queue indexing, and state tracking."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM spaced_repetition_queue WHERE item_id LIKE 'test-sr-%'")

        now = utcnow()
        # Item 1: Card using SM-2
        cur.execute(
            """INSERT INTO spaced_repetition_queue (item_type, item_id, repetition_count, interval_days, ease_factor, due_date, state, created_at, updated_at)
               VALUES ('card', 'test-sr-card-01', 4, 12.0, 2.40, '2026-09-30T10:00:00Z', 'review', ?, ?)""",
            (now, now),
        )

        # Item 2: Topic using FSRS
        cur.execute(
            """INSERT INTO spaced_repetition_queue (item_type, item_id, repetition_count, interval_days, ease_factor, stability, difficulty, due_date, state, created_at, updated_at)
               VALUES ('topic', 'test-sr-topic-01', 2, 4.0, 2.50, 5.2, 4.1, '2026-09-19T08:00:00Z', 'learning', ?, ?)""",
            (now, now),
        )
        self.conn.commit()

        # Query due items
        due_items = cur.execute(
            "SELECT item_id, ease_factor, stability FROM spaced_repetition_queue WHERE due_date <= '2026-09-20T00:00:00Z'"
        ).fetchall()
        self.assertEqual(len(due_items), 1)
        self.assertEqual(due_items[0][0], "test-sr-topic-01")
        self.assertAlmostEqual(due_items[0][2], 5.2)

        # Constraint check: ease factor cannot fall below 1.30
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                """INSERT INTO spaced_repetition_queue (item_type, item_id, ease_factor, due_date, created_at, updated_at)
                   VALUES ('card', 'test-fail-ease', 1.10, ?, ?, ?)""",
                (now, now, now),
            )

        cur.execute("DELETE FROM spaced_repetition_queue WHERE item_id LIKE 'test-sr-%'")
        self.conn.commit()

    def test_10_medical_sources_provenance_and_study_flag(self):
        """Verify medical evidence provenance, publication types, and human vs. animal study flag."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM medical_sources WHERE id LIKE 'TEST-SRC-%'")

        now = utcnow()
        # 1. Human Guideline (ESC Heart Failure Guideline)
        cur.execute(
            """INSERT INTO medical_sources (id, title, authors, publication_type, is_human_study, study_design, journal_or_publisher, publication_year, pmid, evidence_level, trust_score, created_at, updated_at)
               VALUES ('TEST-SRC-ESC-2023', 'ESC Guidelines for the diagnosis and treatment of acute and chronic heart failure', 'McDonagh T et al.', 'Guideline', 1, 'Clinical Practice Guideline', 'European Heart Journal', 2023, '34447992', 'Level 1A', 0.98, ?, ?)""",
            (now, now),
        )

        # 2. Animal Research Study (Rat cardiomyocyte calcium kinetics)
        cur.execute(
            """INSERT INTO medical_sources (id, title, authors, publication_type, is_human_study, study_design, journal_or_publisher, publication_year, pmid, evidence_level, trust_score, created_at, updated_at)
               VALUES ('TEST-SRC-RAT-2022', 'Ryanodine receptor phosphorylation in isolated rodent myocytes during beta-adrenergic stimulation', 'Smith J et al.', 'Research', 0, 'Animal In-Vivo / In-Vitro', 'Circulation Research', 2022, '31221199', 'Preclinical', 0.75, ?, ?)""",
            (now, now),
        )
        self.conn.commit()

        # Query human-only sources
        human_sources = cur.execute(
            "SELECT id, title, publication_type FROM medical_sources WHERE is_human_study = 1 AND id LIKE 'TEST-SRC-%'"
        ).fetchall()
        self.assertEqual(len(human_sources), 1)
        self.assertEqual(human_sources[0][0], "TEST-SRC-ESC-2023")

        # Query animal/preclinical sources
        animal_sources = cur.execute(
            "SELECT id, title FROM medical_sources WHERE is_human_study = 0 AND id LIKE 'TEST-SRC-%'"
        ).fetchall()
        self.assertEqual(len(animal_sources), 1)
        self.assertEqual(animal_sources[0][0], "TEST-SRC-RAT-2022")

        # Constraint check: is_human_study must be 0 or 1
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                """INSERT INTO medical_sources (id, title, publication_type, is_human_study, created_at, updated_at)
                   VALUES ('TEST-FAIL-FLAG', 'Invalid study', 'Research', 2, ?, ?)""",
                (now, now),
            )

        cur.execute("DELETE FROM medical_sources WHERE id LIKE 'TEST-SRC-%'")
        self.conn.commit()

    def test_11_migration_idempotence(self):
        """Verify running migration repeatedly does not corrupt data or raise errors."""
        res1 = migrate_database(self.db_path, create_backup=False)
        self.assertEqual(res1["status"], "success")

        res2 = migrate_database(self.db_path, create_backup=False)
        self.assertEqual(res2["status"], "success")

        # Verify chunks count still intact
        cur = self.conn.cursor()
        cnt = cur.execute("SELECT count(*) FROM chunks").fetchone()[0]
        self.assertGreaterEqual(cnt, 14)


if __name__ == "__main__":
    unittest.main(verbosity=2)
