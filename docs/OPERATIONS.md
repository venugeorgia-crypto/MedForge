# OPERATIONS — Diagnose, Back Up, Restore, Repair

Everything here runs locally. Nothing is sent anywhere.

## Health check

```bash
medforge diagnose                     # full report (exit 1 when unhealthy)
medforge doctor                       # legacy check: models + DB + vectors
```

`diagnose` reports, with the evidence it used:

| Field | Meaning |
| --- | --- |
| `integrity`, `foreign_key_violations` | `PRAGMA integrity_check` / `foreign_key_check` |
| `tables` | user table count |
| `migrations_applied`, `migrations_pending` | numeric-ordered versions; `3.0.0` counts as satisfied when its baseline tables exist |
| `disk_free_mb` | free space at `MEDFORGE_HOME` (warns under 500 MB) |
| `backups.count`, `backups.newest_verified` | inventory + verification of the newest file |
| `provider` | active provider, reachability, installed models |
| `problems`, `healthy` | derived from the above — never assumed |

## Backups

| Where | What |
| --- | --- |
| `backups/medforge_nightly_YYYYMMDD.db` | nightly verified snapshot (newest 7 kept) |
| `backups/medforge_pre_v<N>_backup_*.db` | automatic pre-migration snapshot |
| `backups/medforge_prerestore_*.db` | automatic copy of the DB *before* any restore |

```bash
medforge nightly "only|backup"                       # take one now
medforge diagnose verify-backup backups/<file>.db    # is it usable?
```

A backup is only called usable after `integrity_check` passes **on the copy**.

## Restore (the rollback path)

```bash
medforge diagnose "restore|backups/medforge_pre_v15_backup_20261008T204030.db"
medforge diagnose "restore|backups/<file>.db|/tmp/other-target.sqlite3"
```

Behaviour: the backup is verified first (unverified files are refused with a
`ValueError`), the current database is copied to
`backups/medforge_prerestore_<timestamp>.db`, then the restore is performed and
the result re-verified. Restart any running dashboard/CLI afterwards — open
connections keep the old file.

## Repair

```bash
medforge diagnose repair              # quick_check + re-derive the FTS index
medforge diagnose "repair|vacuum"     # also compact (needs 2x db size free)
```

Repair never deletes learner data: the only index it touches is `chunks_fts`,
which is fully derivable from `chunks`. VACUUM is skipped when free disk is low.

## Migrations

```bash
medforge migrate                                    # baseline schema
.venv-v2.1/bin/python -m core.database.migrate_v15 --db database/medforge.sqlite3
```

Every runner is additive and idempotent, takes a verified backup, and verifies
integrity + foreign keys. `diagnose` lists anything still pending.

## Failure modes and what MedForge does

| Failure | Behaviour |
| --- | --- |
| Corrupt database file | `diagnose`/`repair` report `failed` with the SQLite error; no guessing |
| Model provider unreachable | reported as a problem; content phases abstain instead of inventing output |
| Low disk (< 500 MB) | reported; downloads refuse; VACUUM skipped |
| Interrupted long job | `job_lock` refuses a second concurrent job (clear message) |
| Another job running | same lock; wait or finish the other job |
| Bad/partial backup | `verify-backup` fails; `restore` refuses it |
