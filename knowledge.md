# knowledge.md — MedForge project knowledge

Synchronized with `AGENTS.md` (invariants) and `.agents/skills/INDEX.md`
(skill index). Keep this file factual and short; detail belongs in `docs/`.

## Architecture at a glance

```
CURRICULUM (P2)
      |
      v
STUDY ENGINE (P9 orchestrator)  ← medforge/study.py
      |
      +------------+-------------+
      v            v             v
  LEARNER (P6)  EVIDENCE (P4)  PRIORITY (P6)
      |            |             |
      +------------+-------------+
                   v
            STUDY STRATEGY
                   |
      +------------+-------------+
      v            v             v
  TUTOR (P7)  ASSESSMENT (P8)  REVIEW/SR (learner.py)
                   |
                   v
          PRODUCT FACTORY (content.py)
                   |
                   v
            STUDY RECORD (V10 tables)
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
| V3–V10 schema | `core/database/schema.py`, `core/database/migrate_v*.py` | `docs/V3_SCHEMA.md` |

## Verified state

- Migration chain: `3.0.0 → 4.0.0 → 5.0.0 → 6.0.0 → 7.0.0 → 8.0.0 → 9.0.0 → 10.0.0`.
- V10 tables: `study_plans`, `study_missions`, `study_actions`,
  `content_items`, `content_artifacts`.
- Test suite: 266 passing.
- Backups: `backups/medforge_pre_v{4..10}_backup.db`, each verified against the
  pre-migration state.

## Key API facts agents keep needing

- `textbook_evidence_for_topic(...)` returns key **`links`**.
- P6 `study_priority` items carry
  `{topic, mastery, mastery_percent, evidence_count, overdue_days, components,
  priority, reasons}` — use `get_recent_performance(key)` for
  `recent_performance`/`trend`.
- Tutor session goal must be one of `10min|20min|30min|quick` or an interaction
  count; the tutor session column is `session_objective`.
- `create_assessment` needs `blueprint_id` or `(scope_type, scope_node_id)`.
- `content_items` are content-addressed; the model name is part of the cache key.
- Canonical citation validation accepts a `refs` array *or* inline `[S#]` and
  materialises it; uncited elements are dropped.
- `SUMMARY_ARTIFACT_TYPES = (cheat_sheet, mind_map, script)` — partial coverage
  by design; the completeness gate applies to `study_guide`/`flashcards`/`quiz`.
- `study_actions` is append-only (one row per planned action); step results live
  in `study_missions.results`.

## Conventions

- CLI: `medforge_core.py <command> "<pipe|separated|args>"`; P9 commands are
  `study-intel` and `product-intel`.
- Commit messages: `P<phase>: <summary>` (+ body + Codebuff footer).
- Evidence demos: isolated `MEDFORGE_HOME` under `/tmp`, JSON evidence files
  saved in the demo home.
- Every implemented phase has a `docs/P<N>_MATRIX.md` with KEEP/EXTEND/REUSE/
  MISSING/CONFLICT verdicts plus an execution-results section.

## Lessons that generalize

See `.agents/skills/meta/SKILL.md`. Highlights: set `MEDFORGE_HOME` before
importing `medforge`; prefer `git commit -F` for long messages; verify
background servers actually listen; re-read files before editing; count INSERT
placeholders against columns on wide tables.
