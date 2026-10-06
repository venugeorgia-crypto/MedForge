---
name: medforge-phase
description: Enforces the mandatory MedForge development loop (audit → acceptance tests → smallest safe patch → regression → data integrity → E2E → diff review → document → commit → push) for any substantial MedForge change.
license: MIT
metadata:
  category: development
---

# MedForge Phase — the mandatory development loop

Any MedForge task that changes behaviour (not just docs or a one-line typo)
runs this loop, in this order, without skipping a step:

```
AUDIT → REQUIREMENTS → ACCEPTANCE TESTS → SMALLEST SAFE PATCH → TEST
→ FULL REGRESSION → DATA INTEGRITY → E2E → UI/CLI VALIDATION
→ DIFF REVIEW → DOCUMENT → COMMIT → PUSH
```

## What each step means here

| Step | Concrete MedForge action |
| --- | --- |
| AUDIT | Read the owning module, its public API and the relevant `docs/P*_MATRIX.md`. Map what exists before proposing anything new. |
| REQUIREMENTS | Restate the ask as exact outputs: file paths, function names/signatures, schema columns, CLI commands, formats, constraints. |
| ACCEPTANCE TESTS | Write or extend tests in `tests/` that encode those outputs **before** implementing. Reuse the hermetic fixture pattern (monkeypatch the model seam, swap `T.META_DB`/`T.PRODUCTS` to tmp). |
| SMALLEST SAFE PATCH | Edit the module that owns the responsibility. No parallel systems, no rewrites. |
| TEST | Run the new tests: `.venv-v2.1/bin/python -m pytest tests/<file> -q --tb=short` from repo root. |
| FULL REGRESSION | `.venv-v2.1/bin/python -m pytest tests/ -q --tb=short` — never skip, never narrow the selection to make it pass. |
| DATA INTEGRITY | If schema or stored data is touched: backup → row counts before → migrate → `PRAGMA integrity_check` + `foreign_key_check` → counts after → idempotence re-run. See `medforge-database`. |
| E2E | Isolated `MEDFORGE_HOME` demo that drives the real engines end to end and saves JSON evidence. |
| UI/CLI VALIDATION | Run the actual CLI command and, for dashboard work, an actual Streamlit instance on a free port (8501 is usually the user's). |
| DIFF REVIEW | `git diff --stat` + read the hunks. Only planned files changed. Check for stray debug prints, leftover `or True`, shadowed names. |
| DOCUMENT | Update the phase matrix, `docs/ARCHITECTURE.md`, `docs/V3_SCHEMA.md`, `docs/CLI_USAGE.md`, `docs/IMPLEMENTATION_MATRIX.md` as applicable — in the same commit. |
| COMMIT / PUSH | House style: `P<phase>: <summary>` + body + `🤖 Generated with Codebuff` + `Co-Authored-By: Codebuff <noreply@codebuff.com>`. Never stage with `git add -A`. |

## Non-negotiable rules

- **Inspect before editing.** Re-read the file you are about to change; do not
  trust memory of an earlier read.
- **Implementation matrix for substantial work** — a `docs/P<N>_MATRIX.md` with
  KEEP / EXTEND / REUSE / MISSING / CONFLICT verdicts, written before code.
- **Acceptance criteria before implementation**, always.
- **Never skip the full regression.** "The new tests pass" is not a green run.
- **Never weaken a test to get green.** No deleted assertions, no
  `pytest.skip` on the changed path, no broad `# type: ignore`, no swallowing
  exceptions. If a behaviour genuinely changed, update the *expectation* and
  say why in the commit message.
- **Every real bug discovered gets a regression test** that fails without the
  fix. Prefer guards that fail loudly (e.g. an AST check over `main()`) over
  comments.
- **Do not declare completion with unchecked acceptance criteria.** Say which
  criteria are unproven instead of rounding up.
- **Report unavailable tests honestly** ("could not run X because Y"), never
  as passes.
- **Preserve recovery paths**: backups, migrations, resumable jobs, symlinked
  `current` release.
- **Stop at explicit phase boundaries.** If the task says "do not start P10",
  that boundary is hard, even when the next phase looks obvious.

## Working agreements that have repeatedly mattered

- Demo/E2E homes are isolated: `MEDFORGE_HOME=/tmp/mf-<phase>-demo`, built by a
  script, reset deliberately, never pointed at `database/`.
- Keep `/tmp` write payloads well-formed; heredocs containing dotted attribute
  access or backticks can trip shell parsing — prefer writing a file with the
  editing tools, or ASCII-only heredocs.
- Long jobs (Streamlit, batch generation) run as background processes, are
  health-checked, and are **stopped** when verification ends.

## Load order

After `medforge-core`. Before any domain skill. If the task is migration-heavy,
also load `medforge-database`; if it touches UI/CLI, `medforge-e2e`; if it
ships, `medforge-release`.
