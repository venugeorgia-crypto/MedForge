# P14 — Automation + Nightly Study Workflow

**Version:** `p14-automation-v1` | **Migration:** `15.0.0` (`automation_jobs`,
`automation_runs`)

---

## The nightly job

```
medforge nightly
  1. maintenance  PRAGMA quick_check, DB size, free disk
  2. refresh      P13 source checks + claim re-verification (textbook; network
                  only when queries/urls are configured and not offline)
  3. backup       verified snapshot → backups/medforge_nightly_YYYYMMDD.db
                  (integrity_check on the COPY; keep newest 7, prune only
                  files matching medforge_nightly_*.db)
  4. study_prep   P9 study status + recommended next action + open refresh
                  notifications ("what to study tomorrow")
  5. record       one automation_runs row with per-step JSON results
```

Rules:

- **No hidden failures.** A step that raises is recorded with `status: failed`
  and its error; the run's status becomes `failed` and `error_msg` names the
  failing steps. Later steps still run (one broken area does not stop the backup).
- **Dry runs write nothing** — `nightly dry-run` returns the planned steps and
  creates no tables or rows.
- **Backups are verified on the copy**, and pruning never touches files that
  are not nightly snapshots (e.g. `medforge_pre_v15_backup_*.db`).

## Scheduling (macOS launchd)

```bash
medforge schedule install                # daily 03:00, loads the agent
medforge schedule "install|4|30"         # daily 04:30
medforge schedule "install|3|0|write-only"   # write the plist, do not load
medforge schedule status
medforge schedule uninstall
```

- Label `org.medforge.nightly`, plist at `~/Library/LaunchAgents/org.medforge.nightly.plist`
  (override the directory with `MEDFORGE_LAUNCH_AGENTS`).
- `ProgramArguments` = `[current python, medforge_core.py, nightly]`,
  `RunAtLoad=false`, `StartCalendarInterval` with the chosen hour/minute, logs
  to `<home>/logs/nightly.log`. The payload passes `plutil -lint`.
- On non-macOS, `install` performs **no** install and returns the exact cron
  line to add instead — no fake success.
- `uninstall` unloads (best effort) and removes the plist.

## API (`medforge.automation`)

```python
from medforge import automation as A

A.run_nightly(triggered_by="manual", dry_run=False, retention=7,
              pubmed_queries=None, web_urls=None, steps=None)
A.maintenance_step(); A.refresh_step(); A.backup_step(); A.study_prep_step()
A.list_runs(limit=20); A.get_automation_status()
A.install_schedule(hour=3, minute=0, write_only=False)
A.uninstall_schedule(); A.schedule_status()
```

## Database (V15)

```sql
automation_jobs -- one row per named workflow (nightly), last status/run
automation_runs -- one row per execution: steps JSON, status, error, timings
```

## Integration

- **P13** owns refresh; **P9** owns study signals; the backup is the same
  SQLite snapshot mechanism used by every migration. Automation adds ordering,
  auditing and scheduling — no domain logic.

## Honest limitations

- The nightly job runs when the machine wakes the agent; a slept Mac runs it
  late, not at 03:00 sharp (launchd semantics).
- Linux scheduling is guidance (a cron line), not an installer.
- `study_prep` records the recommendation; it does not silently create plans or
  start sessions.
