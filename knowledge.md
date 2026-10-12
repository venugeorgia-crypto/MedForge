# knowledge.md — MedForge project knowledge

Synchronized with `AGENTS.md` (invariants) and `.agents/skills/INDEX.md`
(skill index). Keep this file factual and short; detail belongs in `docs/`.

## Architecture at a glance

```
CURRICULUM (P2)
      |
      v
TEXTBOOKS / MEDICAL SOURCES (P3)
      |
      v
EVIDENCE GRAPH (P4)
      |
      v
LEARNER MODEL (P6)
      |
      v
STUDY INTELLIGENCE (P9 orchestrator)  ← medforge/study.py
      |
      +------------+-------------+
      v            v             v
  TUTOR (P7)  ASSESSMENT (P8)  REVIEW/SR (learner.py)
      |            |             |
      +------------+-------------+
                    v
           CANONICAL CONTENT (P9 content.py)
                    |
                    v
           PRODUCTS + VIDEO (P11)
                    |
                    v
           PUBLICATION (P10) → PRIVATE / DISTRIBUTABLE
```

## Phase → module → doc

| Phase | Module | Doc |
| --- | --- | --- |
| P2 curriculum | `medforge/curriculum.py` | `docs/CURRICULUM.md` |
| P3 textbook provenance | `medforge/textbook.py` | `docs/TEXTBOOKS.md`, `docs/P3_MATRIX.md` |
| P4 evidence graph | `medforge/evidence.py` | `docs/EVIDENCE.md`, `docs/P4_MATRIX.md` |
| P6 learner model | `medforge/learner_model.py` | `docs/LEARNER_MODEL.md`, `docs/P6_MATRIX.md` |
| P7 tutor | `medforge/tutor.py` | `docs/TUTOR.md`, `docs/P7_MATRIX.md` |
| P8 assessment | `medforge/assessment.py` | `docs/ASSESSMENT.md`, `docs/P8_MATRIX.md` |
| P9 study intelligence | `medforge/study.py` | `docs/STUDY_INTELLIGENCE.md`, `docs/P9_MATRIX.md` |
| P9 product factory | `medforge/content.py` | `docs/PRODUCT_FACTORY.md` |
| P10 publication | `medforge/publication.py` | `docs/PUBLICATION.md`, `docs/P10_MATRIX.md` |
| P11 video | `medforge/video.py` | `docs/VIDEO.md`, `docs/P11_MATRIX.md` |
| P12 provider/router | `medforge/providers.py`, `medforge/router.py` | `docs/MODEL_ROUTER.md`, `docs/P12_MATRIX.md` |
| P13 knowledge refresh | `medforge/knowledge_refresh.py` | `docs/KNOWLEDGE_REFRESH.md`, `docs/P13_MATRIX.md` |
| P14 automation | `medforge/automation.py` | `docs/AUTOMATION.md`, `docs/P14_MATRIX.md` |
| P15 hardening | `medforge/doctor.py` | `docs/SECURITY.md`, `docs/OPERATIONS.md`, `docs/P15_MATRIX.md` |
| P16 evaluation | `medforge/evaluation.py` | `docs/EVALUATION.md`, `docs/P16_MATRIX.md` |
| P17 packaging | `medforge/launch.py` | `docs/OPERATIONS.md` |
| P18 unified orchestration | `medforge/study.py` (start_todays_study) | `docs/STUDY_INTELLIGENCE.md` |
| V3–V16 schema | `core/database/schema.py`, `core/database/migrate_v*.py` | `docs/V3_SCHEMA.md` |

## Verified state

- Migration chain: `3.0.0 → 4.0.0 → 5.0.0 → 6.0.0 → 7.0.0 → 8.0.0 → 9.0.0 → 10.0.0 → 11.0.0 → 12.0.0 → 13.0.0 → 14.0.0 → 15.0.0 → 16.0.0`.
- V10 tables: `study_plans`, `study_missions`, `study_actions`, `content_items`, `content_artifacts`.
- V11 tables: `review_queue`, `review_history`, `approval_records` + publication columns on `content_artifacts`.
- V12 table: `video_renders`.
- V13 table: `model_runs`.
- V14 tables: `source_refresh_log`, `knowledge_refresh_runs`, `knowledge_notifications`.
- V15 tables: `automation_jobs`, `automation_runs`.
- V16 tables: `evaluation_runs`, `evaluation_cases`.
- Test suite: 454 passing (1 skipped integration).
- Backups: `backups/medforge_pre_v{4..16}_backup*.db`, each verified against pre-migration state with `integrity_check` + `foreign_key_check`.

## Key API facts agents keep needing

- `textbook_evidence_for_topic(...)` returns key **`links`**.
- P6 `study_priority` items carry
  `{topic, mastery, mastery_percent, evidence_count, overdue_days, components, priority, reasons}` — use `get_recent_performance(key)` for `recent_performance`/`trend`.
- Tutor session goal must be one of `10min|20min|30min|quick` or an interaction count; the tutor session column is `session_objective`.
- `create_assessment` needs `blueprint_id` or `(scope_type, scope_node_id)`.
- `content_items` are content-addressed; the model name is part of the cache key.
- Canonical citation validation accepts a `refs` array *or* inline `[S#]` and materialises it; uncited elements are dropped.
- `SUMMARY_ARTIFACT_TYPES = (cheat_sheet, mind_map, script)` — partial coverage by design; the completeness gate applies to `study_guide`/`flashcards`/`quiz`.
- `study_actions` is append-only (one row per planned action); step results live in `study_missions.results`.
- Unified orchestration: `start_todays_study(topic, goal, budget_minutes, run_tutor, run_assessment, generate_products, run_quality_gates, export_mode)` returns full traceability dict.
- History tracing: `get_unified_history(topic)` returns chronological timeline of study_action → tutor_session → tutor_turn → assessment → assessment_attempt → learning_attempt → sr_card_due → sr_review → canonical_content → claim.
- Private/distributable export: `export_bundle(content_id, mode="private|distributable")` — distributable rejects learner fields, prompts, long verbatim runs (>400 chars).

## Conventions

- CLI: `medforge_core.py <command> "<pipe|separated|args>"`; P9 commands are `study-intel` and `product-intel`; P18 commands are `study-today` and `study-history`; P17 commands are `launch verify|run|restart|upgrade|rollback|diagnose`.
- Commit messages: `P<phase>: <summary>` (+ body + Codebuff footer).
- Evidence demos: isolated `MEDFORGE_HOME` under `/tmp`, JSON evidence files saved in the demo home.
- Every implemented phase has a `docs/P<N>_MATRIX.md` with KEEP/EXTEND/REUSE/MISSING/CONFLICT verdicts plus an execution-results section.
- Test suite: 454 passing, 1 skipped (integration).

## Lessons that generalize

See `.agents/skills/meta/SKILL.md`. Highlights: set `MEDFORGE_HOME` before importing `medforge`; prefer `git commit -F` for long messages; verify background servers actually listen; re-read files before editing; count INSERT placeholders against columns on wide tables.