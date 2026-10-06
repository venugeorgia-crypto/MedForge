---
name: medforge-release
description: Commit, release and verification discipline for MedForge — commit message style, staging rules, diff review, versioned releases and the current symlink, backups, and what must never be pushed or published.
license: MIT
metadata:
  category: development
---

# MedForge Release — commits, versions and boundaries

## Commit discipline

1. **Diff review before staging.** `git status --short` + `git diff` + read the
   hunks. Confirm only planned files changed; no debug prints, no leftover
   scaffolding, no `.pyc`, no `tmp/` artefacts.
2. **Stage explicitly.** List the files in `git add <paths>`. **Never**
   `git add -A` / `git add .` — the working tree often contains unrelated user
   edits.
3. **Commit message style** (matches P2–P9 history):

   ```
   P<phase>: <imperative summary> (V<migration> if schema changed)

   <one short paragraph: what and why>

   - <bulleted substance: modules, CLI, tests, migration, demos>
   - <defects found and fixed, with the regression-test consequence>

   🤖 Generated with Codebuff
   Co-Authored-By: Codebuff <noreply@codebuff.com>
   ```

   Note: heredoc messages must not contain backticks — they trigger command
   substitution inside `"$(cat <<'EOF' …)"`. Prefer `git commit -F <file>` for
   messages containing backticks or apostrophes.
4. **Push only when the task asks for it** (the phase loop does).
5. **Never commit**: `database/medforge.sqlite3` changes, `backups/`,
   `products/`, `logs/`, `tmp/`, `.job.lock`, `model-status.json`, demo homes,
   or anything containing learner data.

## Releases

- `releases/<version>-<hash>/` is immutable; **`current`** is the symlink that
  selects it. Never edit a released tree in place — cut a new one.
- `medforge_launch.py` / the installed commands resolve through `current`; a
  broken symlink is a total outage, so verify `readlink current` after any
  release change.
- Installer/launcher changes belong to the packaging phase and must keep the
  recovery path (previous release still selectable).

## Verification before declaring shipped

- [ ] full regression green (`.venv-v2.1/bin/python -m pytest tests/ -q`)
- [ ] live DB integrity + foreign keys ok if schema changed
- [ ] backup present and *validated* if migration ran
- [ ] docs updated in the same commit (ARCHITECTURE / V3_SCHEMA / CLI_USAGE /
      IMPLEMENTATION_MATRIX / phase matrix)
- [ ] git status clean apart from intended files
- [ ] commit message accurate about what was done, including defects found

## Boundaries

- Never push to a remote other than `origin`, never force-push.
- Never publish, upload or post generated medical content — that belongs to the
  publication/approval phase and requires explicit user instruction.
- Never send learner data or evidence excerpts anywhere off the machine.

## Load order

Last in the loop: after `medforge-phase` and the domain skills, immediately
before the commit/push steps of the loop.
