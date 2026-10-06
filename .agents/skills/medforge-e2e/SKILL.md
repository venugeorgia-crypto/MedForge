---
name: medforge-e2e
description: End-to-end verification for MedForge UI, CLI and web work — real startup, real CLI commands, rendered-UI inspection, isolated MEDFORGE_HOME demos, persisted-state and cross-process recovery checks.
license: MIT
metadata:
  category: development
---

# MedForge E2E — catching what unit tests miss

Hermetic unit tests prove logic. They do not prove that the CLI parses an
argument, that the dashboard renders, that a mission survives a restart, or
that a JSON column is actually written on disk. Any UI/CLI/web/persistence
change gets E2E.

## Rules for UI / CLI / web work

1. **Use real application startup.** Import the real `medforge_core.main()` /
   run the real `dashboard.py`; do not stub the app to test it.
2. **Verify the actual CLI command**, not a function call that bypasses the
   dispatcher: `.venv-v2.1/bin/python medforge_core.py <cmd> "<pipe|args>"`
   from `releases/<current>/`, with `MEDFORGE_HOME` set first.
3. **Verify the actual dashboard.** Launch Streamlit on a free port
   (`--server.headless true`), health-check `/_stcore/health`, click through the
   tab, and read the rendered page (screenshot/snapshot) rather than assuming.
   Port 8501 is usually the user's own instance — pick 8502+.
4. **Inspect the rendered UI**, including console errors and empty states, not
   just the HTTP status.
5. **Restart stale processes** when needed; a long-lived process from an earlier
   step must be health-checked before use and **stopped** at the end.
6. **Test persisted state**: reopen the DB after the flow and assert what was
   actually written (rows, JSON columns, file bytes, checksums).
7. **Isolated `MEDFORGE_HOME`** for anything that writes. Build it from a
   script, never point it at `database/`.
8. **Recovery is part of E2E**: kill the flow, resume in a *new* process, and
   prove no duplicated rows (e.g. `study_actions` count unchanged, no double
   `learning_attempt`).
9. **Capture evidence** as JSON in the demo home so the phase report can cite
   real numbers instead of recollections.

## Known traps this skill exists to catch

- `MEDFORGE_HOME` is read at import time — set the env var **before** importing
  `medforge`, and put the repo root and `current/` on `sys.path`.
- `current` is a symlink; running from `releases/…` means `.venv-v2.1` must be
  referenced by absolute path.
- Background processes started inside a synchronous shell call may be cleaned up
  when the call ends — use a real background process and verify it is still
  listening.
- Long-running Streamlit needs an explicit stop; do not leave ports listening.
- A CLI `argparse`-style dispatcher can break whole commands because one branch
  binds a local name (see the `status` shadowing bug) — after adding a branch,
  actually invoke the *other* commands too.
- Interface drift between engines (e.g. a P7 column named `session_objective`,
  not `objective`) surfaces only when the real engine runs.

## Minimal E2E checklist per change

- [ ] fresh isolated home built from a script
- [ ] happy path driven through real entry points
- [ ] one refusal/abstention path exercised (empty evidence, unknown id, bad args)
- [ ] persistence asserted by reading the DB/files back
- [ ] restart-and-resume asserted without duplicate rows
- [ ] CLI output captured; dashboard screenshot/snapshot if UI changed
- [ ] background processes stopped; ports released

## Load order

After `medforge-core` and `medforge-phase`. Required for `dashboard.py`,
`medforge_core.py`, and any feature whose acceptance criteria mention "the user
can see/do X".
