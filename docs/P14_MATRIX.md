# P14_MATRIX.md — Automation + Nightly Study Workflow

Status: **IMPLEMENTED + TESTED** (`p14-automation-v1`, migration `15.0.0`)

## 1. Audit (entry state)

- No scheduler, no nightly job, no automation audit table.
- Primitives that already exist and must be reused, not duplicated:
  - P13 `knowledge_refresh.run_refresh()` — source checks + claim re-verification.
  - `core/database/migrate_v3.create_snapshot_backup(source, dest)` — verified backup.
  - P9 `study.get_study_status()` / `recommend_next_action()` — prep signals.
  - `medforge_core.py` CLI — the automation entry point (`nightly` command).
- macOS: `launchd` user agents are the native scheduler; Linux: cron. The
  installer must be honest: write the plist, load it only when launchctl is
  available, and report exactly what happened.

## 2. Design

### Nightly sequence (`medforge/automation.py`)

```
nightly run:
  1. maintenance   — quick_check + disk-space + db size (diagnostics JSON)
  2. refresh       — P13 run_refresh(textbook[, pubmed, web])   (skippable)
  3. backup        — verified snapshot → backups/medforge_nightly_YYYYMMDD.db
                     (keep newest N=7; prune only files matching the pattern)
  4. study_prep    — P9 study status + recommended next action + open
                     knowledge notifications (the "what to study tomorrow" note)
  5. record        — one automation_runs row with per-step results
```

Each step is a small function returning `{status, ...}`; the run never hides a
failure — a failed step is recorded with its error and marks the run `failed`.
`dry_run=True` returns the planned steps and touches nothing.

### Scheduling (`schedule install|uninstall|status`)

- Writes `~/Library/LaunchAgents/com.medforge.nightly.plist` on macOS
  (StartCalendarInterval 03:00, RunAtLoad false, Label org.medforge.nightly)
  invoking `python medforge_core.py nightly`.
- `install` loads it with `launchctl load` when available; `--no-load` skips
  (tests exercise the file content without touching launchd).
- `uninstall` unloads (best effort) and removes the plist.
- On Linux, prints the exact cron line to add (no silent fake install).

### Data model (V15)

```sql
automation_jobs (job_id PK, name UNIQUE, schedule, enabled, last_run_id,
                 last_status, last_run_at, created_at, updated_at)
automation_runs (run_id PK, job_id NULL FK SET NULL, triggered_by, dry_run,
                 steps JSON, status, started_at, completed_at, error, created_at)
```

## 3. Acceptance criteria (tests must prove)

1. V15 migration additive + idempotent; chains V9→…→V15.
2. `run_nightly(dry_run=True)` returns the step list and writes nothing.
3. Full run executes: maintenance ok, refresh ok, backup file exists and passes
   `PRAGMA integrity_check`, study_prep contains a recommendation.
4. A failing step is recorded as `failed` with its error; the run status is
   `failed`; no exception escapes.
5. Backup retention keeps the newest N and never touches non-matching files.
6. `install_schedule(write_only)` writes a valid plist with the nightly command;
   `schedule_status()` reports it; `uninstall_schedule()` removes it.
7. Offline mode: refresh step is skipped/limited, recorded honestly.
8. CLI `nightly` + `schedule install|status|uninstall` work end-to-end.
9. Full regression green.

## 4. Execution results

### Defects found while implementing

- **`split(";")` migration bug**: the first draft of `migrate_v15` executed
  DDL fragments split on `;`, and a semicolon inside a `--` comment produced
  `near "the": syntax error`. Both `migrate_v14` and `migrate_v15` now use
  `con.executescript(...)`; P13's tests were rerun after the fix (33 passed).
- **CLI arity convention**: multi-argument actions are pipe-delimited like the
  rest of the CLI (`schedule "install|4|30|write-only"`), not space-separated.

### Gate results

- **V15 migration**: fresh OK (chains V4→…→V15), 2 tables
  (`automation_jobs`, `automation_runs`), `schema_migrations` `15.0.0`,
  idempotent rerun OK, verified backup.
- **Unit tests**: 15/15 in `tests/test_automation.py` (~1 s; fixture home,
  LaunchAgents override directory, no scheduler interaction, no Ollama).
- **Full regression**: **359 passed, 1 skipped** (344 P2–P13 + 15 P14).
- **Live DB**: 14.0.0 → 15.0.0, 52 → 54 tables, `integrity_check=ok`,
  `foreign_key_check` empty, idempotent rerun verified, backup
  `backups/medforge_pre_v15_backup_20261008T204030.db`.
- **CLI E2E** on `/tmp/mf-p13-demo`:
  - `nightly dry-run` → 4 planned steps, `recorded: false`;
  - `nightly` → `completed`; maintenance `quick_check=ok` (11.9 GB free),
    refresh `completed` (1 source checked), backup
    `/private/tmp/mf-p13-demo/backups/medforge_nightly_20261008.db`
    `integrity=ok`, study_prep ok;
  - `schedule "install|4|30|write-only"` → plist written and
    `plutil -lint` **OK**; `schedule status` installed=true; and
    `schedule uninstall` removed it.
- **P14 gate**: **PASS** — the nightly workflow exists, is recorded, is
  schedulable, and claims nothing it does not do.
