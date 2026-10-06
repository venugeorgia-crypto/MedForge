# MedForge skill index

Nine domain skills plus one meta skill. Each lives in its own directory as
`SKILL.md` with frontmatter (`name` matches the directory exactly). Load
`AGENTS.md` first for the stable invariants; use this index to pick skills.

## Discovery

| Skill name | Directory | Purpose | When to use |
| --- | --- | --- | --- |
| `medforge-core` | `.agents/skills/medforge-core/` | Architecture, engine ownership, dependency direction, repo layout | Every MedForge task — load first |
| `medforge-phase` | `.agents/skills/medforge-phase/` | The mandatory development loop and its non-negotiable rules | Every behavioural change |
| `medforge-medical-qa` | `.agents/skills/medforge-medical-qa/` | Medical safety: evidence requirement, abstention, high-risk flags | Generating or displaying medical content |
| `medforge-evidence` | `.agents/skills/medforge-evidence/` | Provenance chain, P4 statuses, prompt-injection containment | `textbook.py`, `evidence.py`, `retrieval.py`, citations |
| `medforge-database` | `.agents/skills/medforge-database/` | Eleven-step migration protocol, append-only data | `core/database/**`, anything touching `database/` |
| `medforge-e2e` | `.agents/skills/medforge-e2e/` | Real startup, CLI/dashboard verification, recovery | `dashboard.py`, `medforge_core.py`, persisted state |
| `medforge-products` | `.agents/skills/medforge-products/` | Canonical content model, rendering, caching, versioning | `content.py`, artifact types/statuses, exports |
| `medforge-performance` | `.agents/skills/medforge-performance/` | 8 GB budgets, bounded retrieval, measurement honesty | `models.py`, loops, memory/disk claims |
| `medforge-release` | `.agents/skills/medforge-release/` | Staging, commit style, releases, boundaries | Before commit/push |
| `meta` | `.agents/skills/meta/` | Cross-domain lessons only | When a lesson fits no domain skill |

## Dependency / load order

```
medforge-core            (always, first)
        |
        v
medforge-phase           (always, second)
        |
        +-- medforge-evidence     ---- with medforge-medical-qa
        +-- medforge-medical-qa
        +-- medforge-database
        +-- medforge-e2e
        +-- medforge-products     ---- with medforge-evidence + medical-qa
        +-- medforge-performance
        +-- medforge-release      (last, before commit/push)
```

Typical loads by task shape:

| Task | Load |
| --- | --- |
| "add a CLI command" | core, phase, e2e, release |
| "change the schema / add a migration" | core, phase, database, release |
| "generate or improve study content" | core, phase, evidence, medical-qa, products |
| "speed up X / reduce memory" | core, phase, performance |
| "the dashboard looks wrong" | core, phase, e2e |
| "verify a phase end to end" | core, phase, e2e, release |

## Rules of interaction

- Domain skills never override `medforge-core` invariants or the `medforge-phase`
  loop; they specialise them.
- `medforge-database` and `medforge-medical-qa` can veto a change; a veto must
  be answered with evidence, not by weakening the rule.
- `meta` never stores one-off bug details or task-specific requirements.

## Maintenance

- Adding a skill: create `<dir>/SKILL.md`, set `name` to the directory name,
  add a row here.
- Renaming: rename the directory, the `name` field, and this row together.
- Validating: `.venv-v2.1/bin/python tools/validate_skills.py` (frontmatter,
  name/dir match, duplicates, INDEX parity).
