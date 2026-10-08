# PUBLICATION.md — Publication / Review / Approval Workflow (P10)

Status: **IMPLEMENTED + TESTED** (`p10-publication-v1`, migration `11.0.0`)

No generated medical product jumps from generation to publication. Every
artifact rendered by the P9 factory passes nine deterministic approval gates,
records findings in an append-auditable review queue, and requires a named
human reviewer before APPROVED → PUBLISHED.

## Lifecycle

Per artifact row (`content_artifacts.publication_status`):

```
UNREVIEWED → NEEDS_REVIEW → APPROVED → PUBLISHED → RETIRED
      \---------- BLOCKED ← (any high-severity gate failure)
BLOCKED → NEEDS_REVIEW  (only after ALL open high/critical reviews resolved/waived)
```

Overall content status in `get_publication_state` is the strictest artifact
status: BLOCKED > RETIRED > PUBLISHED > APPROVED > NEEDS_REVIEW > UNREVIEWED.

## Approval gates (`run_approval_gates(content_id[, artifact_type])`)

Every **rendered artifact** of the content item is checked — a defect in any
derivative blocks publication:

1. `curriculum_alignment` — topic resolves to a P2 node (medium)
2. `evidence_coverage` — every canonical fact carries refs (high)
3. `semantic_evidence_status` — P4 verdicts contain no UNSUPPORTED/CONTRADICTED (high)
4. `citation_continuity` — rendered citations ⊆ content evidence labels (high)
5. `content_consistency` — P9 consistency report re-verifies checksums (high)
6. `medical_risk_scan` — deterministic HIGH_RISK_PATTERNS scan (doses,
   contraindications, emergencies, procedures, diagnostic criteria,
   clinical recommendations); any hit forces a review, never auto-approval (high)
7. `copyright_export` — longest verbatim source run ≤ 400 chars (high)
8. `formatting_validation` — files exist, non-empty, checksums match (medium)
9. `artifact_generation` — artifact status publication-eligible + render_mode (medium)

A failing high-severity gate sets `BLOCKED` and records a `review_queue` row
(idempotent per content + issue_type + reason) plus an `approval_records`
batch. Gates never self-approve medical risk.

## Review queue

`review_queue` rows carry content_id, artifact_id, issue_type
(medical_risk / evidence_gap / unsupported_claim / contradicted_claim /
citation_break / consistency / copyright / formatting / curriculum_gap),
severity (low/medium/high/critical), detected_reason, detected_by, status
(open/resolved/waived), reviewer, resolution.

- `approve_artifact(content_id, reviewer)` refuses blank reviewers and refuses
  approval while open high/critical reviews exist or gates fail.
- `resolve_review(review_id, reviewer, resolved|waived, note)` appends to the
  append-only `review_history`, then returns BLOCKED content to NEEDS_REVIEW
  once no open high/critical issue remains (a fresh gate run is still required).
- `retire_artifact(content_id, reason)` marks all artifacts RETIRED with a reason.

## Export modes (`export_bundle(content_id, mode)`, `publish_artifact`)

- `private` — manifest includes full canonical content + private note.
- `distributable` — rendered artifacts + reference locators only; the export
  is **refused** if any artifact contains learner/prompt fields (`learner:`,
  `mastery:`, `confidence:`, `session_id:`, `prompt:` …) or a verbatim source
  run > 400 chars.
- Bundles: `products/<slug>/exports/<mode>/` with `manifest.json` (checksums,
  files, references, mode note). PUBLISHED rows record `published_path`.

## Migration 11.0.0

`core/database/migrate_v11.py::ensure_publication_v11()` — additive and
idempotent: calls `ensure_study_v10` first, writes a snapshot backup to
`backups/medforge_pre_v11_backup.db`, creates `review_queue`, `review_history`,
`approval_records` (+ indexes, FK CASCADE to content_items), adds seven nullable
`content_artifacts` columns (`publication_status` default `UNREVIEWED`,
`approved_at/by`, `published_at/path`, `retired_at/reason`), records the
`schema_migrations` row, and verifies `integrity_check` + `foreign_key_check`.
Rollback: drop the three new tables (existing rows are untouched).

## CLI

```
medforge_core publication run-gates <content_id>[|artifact_type]
medforge_core publication approve <content_id>|<reviewer>[|artifact_type]
medforge_core publication publish <content_id>[|private|distributable[|artifact_type]]
medforge_core publication retire <content_id>|<reason>[|artifact_type]
medforge_core publication state <content_id>
medforge_core publication queue [status] | reviews [status]
medforge_core publication resolve <review_id>|<reviewer>|<resolved|waived>[|note]
medforge_core publication export <content_id>[|mode|artifact_type]
```

## Tests

`tests/test_publication.py` — 20 tests (schema additive/idempotent, all nine
gates, tampered-artifact blocking, medical-risk review forcing, full
approve→publish lifecycle, both export modes, learner-field refusal), plus the
P10 E2E recorded in `docs/P10_MATRIX.md`. All 286 tests pass.
