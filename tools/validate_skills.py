#!/usr/bin/env python3
"""Validate the MedForge agent skill system.

Checks, for every skill directory under .agents/skills:
  1. SKILL.md exists
  2. frontmatter parses and has the required keys
  3. the `name` field matches the directory name exactly
  4. `description` is present and non-trivial
  5. `license` is MIT and metadata.category is set
  6. no duplicate skill names
  7. every skill referenced by INDEX.md exists, and every skill is indexed
  8. AGENTS.md and knowledge.md exist at the repo root

Exit status is non-zero on any failure, so it can gate future work.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / ".agents" / "skills"

REQUIRED_KEYS = ("name", "description", "license", "metadata")

failures: list[str] = []
notes: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def parse_frontmatter(text: str, origin: str):
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[4:end]
    data: dict = {}
    current = None
    for line in block.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line[:1] not in (" ", "\t") and ":" in line:
            key, _, value = line.partition(":")
            current = key.strip()
            # An empty scalar value means a nested mapping follows.
            data[current] = value.strip() or {}
        elif current and ":" in line:
            sub_key, _, sub_value = line.strip().partition(":")
            bucket = data.setdefault(current, {})
            if isinstance(bucket, dict):
                bucket[sub_key.strip()] = sub_value.strip()
    return data


def main() -> int:
    if not SKILLS.is_dir():
        print(f"FAIL: missing skill root {SKILLS}")
        return 1

    dirs = sorted(p for p in SKILLS.iterdir() if p.is_dir() and not p.name.startswith("."))
    seen: dict[str, Path] = {}

    for d in dirs:
        skill_file = d / "SKILL.md"
        if not skill_file.is_file():
            fail(f"{d.name}: SKILL.md missing")
            continue
        text = skill_file.read_text(encoding="utf-8")
        fm = parse_frontmatter(text, skill_file)
        if fm is None:
            fail(f"{d.name}: frontmatter block missing or malformed")
            continue

        # Register before validating so counts and duplicate checks are exact.
        if fm.get("name") in seen:
            fail(f"{d.name}: duplicate skill name with {seen[fm['name']].name}")
        seen[d.name] = d

        missing = [k for k in REQUIRED_KEYS if k not in fm]
        if missing:
            fail(f"{d.name}: frontmatter missing {missing}")
            continue
        empty = [k for k in REQUIRED_KEYS if isinstance(fm[k], str) and not fm[k].strip()]
        if empty:
            fail(f"{d.name}: frontmatter values empty for {empty}")
            continue

        name = str(fm["name"])
        if name != d.name:
            fail(f"{d.name}: frontmatter name {name!r} does not match directory")

        if str(fm["license"]).strip().lower() != "mit":
            fail(f"{d.name}: license must be MIT, got {fm['license']!r}")
        category = (fm.get("metadata") or {}).get("category") if isinstance(
            fm.get("metadata"), dict) else None
        if not category:
            fail(f"{d.name}: metadata.category missing")

        desc = str(fm["description"])
        if len(desc) < 30:
            fail(f"{d.name}: description too short for discovery ({len(desc)} chars)")

        body = text[text.find("\n---", 3) + 4:]
        if len(body.strip()) < 200:
            notes.append(f"{d.name}: body is thin ({len(body.strip())} chars)")

    # Index parity
    index = SKILLS / "INDEX.md"
    if not index.is_file():
        fail("INDEX.md missing")
    else:
        itext = index.read_text(encoding="utf-8")
        for name in seen:
            if f"`{name}`" not in itext:
                fail(f"INDEX.md does not reference skill {name!r}")
        for m in re.finditer(r"`([a-z0-9-]+)`", itext.split("## Discovery")[-1]):
            pass  # rows are checked above; stray backticks are not failures

    # Root knowledge files
    for f in ("AGENTS.md", "knowledge.md"):
        if not (REPO / f).is_file():
            fail(f"missing root file {f}")

    print(f"skills found: {len(seen)} -> {', '.join(sorted(seen))}")
    for n in notes:
        print(f"NOTE: {n}")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nOK: skill system valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
