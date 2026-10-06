---
name: medforge-database
description: SQLite and migration safety for MedForge — the eleven-step migration protocol (backup, counts, integrity, foreign keys, idempotence) and the data that must never be deleted or rewritten.
license: MIT
metadata:
  category: development
---

# MedForge Database — migration and data safety

SQLite at `MEDFORGE_HOME/database/medforge.sqlite3` (schema in
`core/database/schema.py`, runners in `core/database/migrate_v*.py`). Schema
version is tracked in `schema_migrations` (currently `10.0.0`), **not** in
`PRAGMA user_version` (which is 0 — do not "fix" that).

## Every migration runs this eleven-step protocol

1. **Inspect the current schema** (`sqlite_master`, `schema_migrations`).
2. **Record row counts** for every table, before touching anything.
3. **Verified backup** into `backups/` — after it exists, open it and prove it
   (version list, no new tables, counts match, `integrity_check` ok). An
   unverified backup is not a backup.
4. **Additive only where possible.** `CREATE TABLE IF NOT EXISTS` + indexes; no
   column rewrites, no destructive `ALTER`.
5. **Execute** the migration through the runner (`ensure_*_v<n>`), which runs
   the previous runner first so curriculum foreign keys resolve.
6. **`PRAGMA integrity_check`** must be `ok`, else raise.
7. **`PRAGMA foreign_key_check`** must return zero rows, else raise.
8. **Compare row counts** after: pre-existing rows unchanged; only new
   `schema_migrations` rows added.
9. **Idempotence test** — run the migration a second time and prove no change
   (same table count, one version row). Use `--no-backup` for that second run so
   the pre-migration snapshot is not clobbered.
10. **Rollback / recovery consideration** — documented as "drop the N new
    tables" or an explicit statement that rollback is not possible.
11. **Diff review** — the runner and `schema.py` changes are the only schema
    diffs in the commit.

## Never

- Delete user data casually (or at all, outside an explicit, confirmed request).
- Rewrite migration history — no editing an applied `schema_migrations` row, no
  renumbering.
- Assume a backup succeeded without opening and validating it.
- Create duplicate sources of truth (a second mastery table, a parallel evidence
  store, a shadow queue).
- Silently repair corrupted data — report it, preserve it, ask.

## Keep history where reproducibility depends on it

Append-only or versioned by design — do not "clean these up":

| Data | Why it must survive |
| --- | --- |
| `learning_attempts` + `learner_model_state` | P6 mastery is recomputable from history |
| `assessment_sessions` + `assessment_attempts` (+ item snapshots) | historical assessments must replay exactly |
| `tutor_sessions` / `tutor_turns` / `tutor_questions` | transcripts stay inside the engine |
| `claims` / `evidence` / `claim_evidence` / `verification_runs` | P4 provenance chain and verification history |
| `study_actions` (append-only) | auditable study record |
| `content_items` (append-only by content_id) + `content_artifacts` (versioned) | content history is never rewritten |
| `review_log` | spaced-repetition scheduling depends on it |
| `schema_migrations` | migration history |

## Practical notes

- `user_version` is 0 by design; `schema_migrations` is authoritative.
- The metadata/vector DB is separate (`META_DB`, Chroma at `database/chroma`).
- Demo work uses an isolated `MEDFORGE_HOME`; **never** point demos at the live
  `database/`.
- Backups accumulate in `backups/medforge_pre_v<N>_backup.db` — keep the
  pattern and the naming.

## Load order

After `medforge-core` and `medforge-phase`. Mandatory for any change to
`core/database/**` or any task that writes to `database/`.
