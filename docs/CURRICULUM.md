# MedForge Curriculum Engine (V4)

Phase 2 of the master build directive: the canonical curriculum layer that
makes every studied topic traceable to its position in the syllabus.

## Hierarchy

```
SEMESTER → SUBJECT → WEEK → SEMINAR → TOPIC → SUBTOPIC → LEARNING OBJECTIVE
```

Everything is stored in the existing `curriculum_nodes` table (a generic
parent-linked tree) and the `prerequisites` table. The V4 migration
(`core/database/migrate_v4.py`, recorded as `4.0.0`) widens the node-type
CHECK with `Subject`, `Week` and `Seminar` and adds uniqueness/ordering
indexes. The migration is idempotent, row-preserving, and self-healing: the
first curriculum call on any database applies it automatically.

## Syllabus import

A pasted weekly or seminar syllabus becomes structured curriculum data with
minimal manual work. Accepted line forms (markdown headings tolerated,
case-insensitive):

| Line form | Becomes |
| --- | --- |
| `Semester 3: <title>` | Semester root (number stored) |
| `Subject: <title>` or a heading outside weeks | Subject (each new heading starts another subject) |
| `Week 2` / `Week 2: <theme>` | Week node (order = week number) |
| `Seminar: <title>` / `Seminar 2: <title>` | Seminar under the current week |
| `- <text>` (top-level bullet) | Topic under the current seminar or week |
| indented bullet (2+ spaces) | Subtopic of the current topic |
| `LO:` / `Objective:` / `Outcome:` line | Learning Objective of the nearest preceding topic (or seminar/week) |
| plain line inside a week | Topic |

Rules that keep imports safe:

- **Idempotent** — rerunning the same text creates nothing new; nodes are
  matched by normalized title inside their parent.
- **Normalization** — case, whitespace and punctuation variants of a title
  (`GH axis`, `gh   axis!`, `GH Axis`) map to one node.
- **Duplicate detection** — `detect_duplicates()` reports any same-type
  siblings whose normalized titles collide; the database also enforces
  uniqueness of exact titles per parent.
- A week with a theme but no topics promotes the theme to a topic, so a
  one-line syllabus (`Week 3: Adrenal medulla`) still yields a study target.
- Seminar-level import: pass `week_number` when the text has seminars before
  any `Week` line.

### CLI

```bash
medforge syllabus semester3.txt        # weekly import from a file
cat syllabus.txt | medforge syllabus   # or pipe it in
medforge syllabus seminar.txt -- ...   # see path/prereq conventions below
medforge curriculum                    # progress + duplicate report (JSON)
medforge path "GH and IGF-1 axis"      # traceability chain for a studied topic
medforge prereq "IGF-1 mediation|GH axis|recommended"   # prerequisite edge
```

`prereq` is pipe-delimited because topic titles contain spaces:
`<topic>|<prerequisite>[|strict|recommended|co-requisite]`.

### Dashboard

The **CURRICULUM** tab offers: syllabus paste-import (with optional
subject/semester/week fields), progress metrics (subjects, weeks, topics,
studied, mastered ≥70), the full tree with progress, a topic-traceability
lookup, and a prerequisite form.

## Traceability

`topic_path(topic)` resolves a studied topic — by normalized title **or by
slug** (the `learner_mastery.topic_id` namespace, e.g. `anterior-pituitary-
hormones`) — and returns the full ancestor chain:

```
Semester: Semester / Subject: Endocrinology Block / Week: Hypothalamus &
Pituitary / Seminar: Pituitary hormones / Topic: Anterior pituitary hormones
```

`curriculum_progress()` joins `learner_mastery` (topic-slug namespace) with
curriculum topics and reports, per subject/week/seminar: topics total,
studied, mastered (≥70), and average mastery. Studied topics that match no
curriculum node appear in `summary.studied_unmapped` — the backlog for your
next syllabus import.

## Programmatic API

```python
from medforge import (import_syllabus, parse_syllabus, curriculum_tree,
                      topic_path, curriculum_progress, add_prerequisite,
                      detect_duplicates, ensure_node, find_node, get_children)

parsed = parse_syllabus(text)                      # structure without writing
result = import_syllabus(text, subject_title="Endocrinology",
                         semester_title="Semester 3", week_number=None)
result["created"], result["created_total"], result["idempotent_repeat"]
node = ensure_node("Topic", "GH axis", parent_id=week["id"])
chain = topic_path("GH axis")["position"]
```

## Tests and recovery

- `tests/test_curriculum.py` (15 tests): parsing (weekly, seminar-level,
  one-line weeks), idempotent re-import, ordering, normalization/dedup,
  prerequisite graph (idempotence + validation errors), traceability by
  title and slug, progress join, tree nesting, and the V4 migration
  (row-preserving rebuild from an old-CHECK database, idempotent rerun,
  fresh-database self-heal).
- **Recovery path:** every curriculum write is SQLite-transactional; the
  explicit migration takes a snapshot backup (`backups/medforge_pre_v4_backup.db`)
  before any rebuild, and `migrate_v3.rollback_migration()` restores it.
- **Known limitations:** `Year`/`Course`/`Module` node types from the V3
  360-ECTS scheme remain valid but are not produced by the importer; the
  importer is line-based (no table/PDF syllabus parsing); duplicates are
  reported, not auto-merged.
