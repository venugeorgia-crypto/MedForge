---
name: meta
description: Project-level lesson capture for MedForge — records only reusable, cross-domain knowledge that does not belong to a specific skill, and explicitly excludes one-off bug details and task-specific notes.
license: MIT
metadata:
  category: development
---

# Meta — reusable MedForge lessons

This skill captures lessons that span domains and would otherwise be
rediscovered the hard way. It is deliberately narrow.

## What belongs here

- A **recurring trap** in the repository or toolchain (e.g. "env vars are read
  at import time", "the venv does not exist inside `releases/…`").
- A **process improvement** that applies to more than one phase (e.g. "run the
  real CLI after adding a dispatch branch").
- A **convention** the team keeps re-deriving (naming, doc placement, evidence
  capture).

## What does NOT belong here

- Specific bug details from a single run (those live in the phase matrix's
  "defects found" section, with their regression tests).
- Temporary task requirements or a user's one-off instructions.
- Feature-specific implementation notes (those belong in the domain skill or
  `docs/`).
- Anything that would duplicate `medforge-core` invariants or
  `medforge-phase` loop rules.

## How to add a lesson

Append one bullet under the newest heading below, in this shape:

- **<short imperative lesson>** — <one sentence of why, and where it bites>.

If a lesson outgrows "one bullet" it should be promoted into the relevant
domain skill or a `docs/` file, and removed from here.

## Lessons

- **Set `MEDFORGE_HOME` before importing `medforge`** — the value is captured at
  import time, so a probe that sets it afterwards silently targets the wrong
  home and can read or write the live database.
- **Prefer `git commit -F <file>` for long messages** — backticks inside a
  `"$(cat <<'EOF' …)"` heredoc trigger command substitution and abort the
  commit mid-argument.
- **Quote heredocs and keep them ASCII** — dotted attribute access and
  backticks in a payload have both mangled write-once shell scripts here.
- **Verify a background server survived the tool call that started it** — a
  server launched inside a synchronous shell call can be reaped when that call
  returns, and "it printed its URL" is not proof it is listening.
- **Re-read a file immediately before editing it** — across long sessions the
  remembered contents drift from disk, and stale edits silently clobber
  someone else's work.
- **Count placeholders and columns separately for wide INSERTs** — a 24-column
  insert with 23 `?` fails only at runtime against the real DB, which unit
  tests with narrow fixtures may not catch.
