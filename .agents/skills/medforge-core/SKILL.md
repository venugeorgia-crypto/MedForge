---
name: medforge-core
description: MedForge architectural invariants, module ownership map and dependency direction — load FIRST for any MedForge work so new code reuses the authoritative engines instead of duplicating them.
license: MIT
metadata:
  category: development
---

# MedForge Core — architectural invariants

MedForge is a **local-first** medical study platform: a Python 3.12 app on the
user's Mac (M1 MacBook Air, 8 GB RAM, ~256 GB free-disk discipline) with a
Streamlit dashboard, a pipe-argument CLI, SQLite storage and local Ollama
models. Nothing ships to a server.

## Authoritative engines — never duplicate, always reuse

| Phase | System | Module | Public entry points P9+ must reuse |
| --- | --- | --- | --- |
| P2 | Curriculum | `medforge/curriculum.py` | `curriculum_tree`, `curriculum_progress`, `topic_path`, `find_node`, `get_children`, `import_syllabus` |
| P3 | Textbook provenance | `medforge/textbook.py` | register/ingest/link, `textbook_evidence_for_topic` (returns key `links`) |
| P4 | Evidence graph | `medforge/evidence.py` | `evidence_snapshot`, `claims_list`, `verify_product_claims`, `store_evidence`, `evidence_snapshot()` |
| P6 | Recency-weighted learner model | `medforge/learner_model.py` | `get_mastery`, `get_confidence`, `get_recent_performance`, `get_weaknesses`, `get_prerequisite_risks`, `study_priority`, `record_learning_event`, `recalculate_mastery` |
| P7 | Interactive adaptive tutor | `medforge/tutor.py` | `start_tutor_session`, `get_tutor_state`, `resume_tutor_session`, `complete_tutor_session`, `get_tutor_summary` |
| P8 | Question-level assessment | `medforge/assessment.py` | `create_assessment`, `get_assessment_state`, `submit_assessment_answer`, `complete_assessment`, `get_assessment_result`, `get_assessment_remediation`, `create_item`, `validate_item`, `approve_item` |
| P9 | Study intelligence orchestrator | `medforge/study.py` | `get_study_status`, `recommend_next_action`, `build_study_plan`, `get_today_plan`, `start_study_mission`, `get_mission_state`, `complete_mission_action`, `get_knowledge_gaps`, `get_readiness`, `adaptation_profile`, `get_study_history` |
| P9 | Canonical product factory | `medforge/content.py` | `generate_canonical_content`, `render_study_products`, `regenerate_product`, `get_product_status`, `list_artifacts`, `artifact_provenance`, `content_consistency_report` |

**Ownership rules (hard):**

- P4 is the **only** authority on evidence state. Never invent a second
  verification or evidence-status system.
- P6 is the **only** authority on learner state/mastery. Never write a second
  mastery, confidence or priority algorithm.
- P7 is the **only** authority on interactive tutoring and answer grading
  (`tutor.evaluate_answer`). P8 reuses it — so must everything else.
- P8 is the **only** authority on assessment/blueprints/item banks.
- P9 (`study.py`, `content.py`) is an **orchestrator**: it selects and wires,
  it computes no domain truth of its own.
- Spaced repetition stays in `medforge/learner.py` (SM-2 + FSRS, `review_log`).

## Dependency direction (do not invert)

```
P2 curriculum
  → P3 textbook provenance
    → P4 evidence graph
      → P6 learner model
        → P7 tutor
          → P8 assessment
            → P9 study intelligence / product factory
              → P10+ publication, video, router, automation
```

Later phases read earlier phases through public functions. Earlier phases must
never import a later one. If you feel the need to add a dependency arrow
pointing backwards, stop — that is a design smell, not a patch.

## Invariants

1. **Local-first.** All learner data, transcripts and products stay on the Mac.
   The only outbound calls are search providers (opt-in) and the local model.
2. **Learner data is private.** Mastery, history, transcripts and confidence are
   never embedded in distributable output.
3. **Evidence excerpts are private by default.** Distributable output carries
   citations and locators, not long excerpts.
4. **A citation label (`[S1]`) is not semantic proof.** It is a pointer. Only a
   P4 verification status is proof.
5. **No feature may be claimed unless tested.** A feature without a test is a
   claim, and unverified claims are how medical software hurts people.
6. **Backward compatibility.** Existing CLI commands, dashboard tabs and public
   function signatures are contracts. Extend, do not break or rename.
7. **Smallest safe patch.** Prefer editing the one file that owns the
   responsibility over adding a parallel module.
8. **Avoid monolithic files.** `medforge_core.main()` is already a giant
   dispatch — do not add a second one; add focused modules.
9. **Document architecture changes** in `docs/ARCHITECTURE.md` +
   `docs/IMPLEMENTATION_MATRIX.md` + a phase matrix in the same commit.

## Repository layout facts (load before editing paths)

- The active release is reached through the **`current`** symlink:
  `releases/<version>-<hash>/` holds `medforge/`, `medforge_core.py`,
  `dashboard.py`. Repo root holds `core/` (database schema + migrations),
  `tests/`, `docs/`, `database/`, `backups/`.
- `MEDFORGE_HOME` (defaults to the repo root) is read **at import time** by
  `medforge/types.py`. Any probe or demo must set that env var **before**
  importing `medforge`, and must put both the repo root and `current/` on
  `sys.path`.
- Tests import through `tests/conftest.py`; hermetic tests monkeypatch the
  model seam and swap `T.META_DB`/`T.PRODUCTS` to temp paths.
- Python is `.venv-v2.1/bin/python` (absolute path — the venv does not exist
  inside `releases/…`).
- `T.STUDY_ACTION_TYPES` etc. live in `core/database/schema.py`, **not** in
  `medforge/types.py`.

## Load order

Consider this skill first, then `medforge-phase` for the loop, then whichever
domain skill matches the task (evidence, medical QA, database, e2e, products,
performance, release).
