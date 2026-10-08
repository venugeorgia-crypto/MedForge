# P10 — Publication / Review / Approval Workflow (V11)

Status: **audit + design; execution recorded at the end of this file.**
Baseline entering P10: P9 at `d29f2f0`, migration `10.0.0`, 266/266 tests.

## 1. Forensic audit

### What exists (KEEP / REUSE)

| Asset | Verdict | Why |
| --- | --- | --- |
| `content_artifacts.status` | **KEEP** | Already carries `DRAFT/VALIDATING/READY/NEEDS_REVIEW/BLOCKED`; V11 adds lifecycle columns without altering existing rows. |
| `content_consistency_report` (P9) | **REUSE** | Gate 5 (content consistency) is this function re-run at approval time. |
| P4 `evidence_snapshot` / claim statuses | **REUSE** | Gates 2–3 (evidence coverage, semantic status) read P4 as the only authority. |
| P3 provenance chain | **REUSE** | Gate 4 (citation continuity) walks artifact → content item → `evidence_refs` → P3/P4. |
| `content.py` `_sanitize` | **KEEP** | Generation-time gate; P10 adds an independent review layer on top. |
| P2 curriculum | **REUSE** | Gate 1 alignment = the content item's topic resolves to a curriculum node. |
| Legacy `product.py` | **REUSE** | Its packs already go through P4 `verify_product_claims`; P10 gates apply uniformly. |

### MISSING (built in P10)

- Lifecycle statuses beyond `READY` (`APPROVED`, `PUBLISHED`, `RETIRED`) and
  the state machine over them.
- Nine approval gates as executable checks with recorded results.
- Review queue with persistent records (issue → reviewer → resolution).
- Medical-risk scan forcing human review — never auto-approve.
- Copyright/export check (long verbatim runs refused).
- Private vs distributable export with redaction proof.

### CONFLICTS / resolutions

1. **`READY` vs `APPROVED`** — `READY` stays a *technical* state (checksums
   verify); `APPROVED` is a *judgement* state only the review gate grants.
   `PUBLISHED` requires `APPROVED`.
2. **Automated verification ≠ clinician approval** — the medical-risk gate
   *forces* human review; it never grants approval by itself.
3. **Learner data in artifacts** — artifacts are learner-independent by P9
   design; the export gate redacts anyway and records proof.

## 2. Design

### 2.1 Lifecycle

```
DRAFT → VALIDATING → READY ─┬→ APPROVED → PUBLISHED → RETIRED
                            └→ NEEDS_REVIEW → (fix) → VALIDATING
        BLOCKED ←— any state, terminal until a reviewer resolves it
```

- `APPROVED` only via `approve_artifact()` with all nine gates passed or every
  open issue resolved by a recorded reviewer.
- `PUBLISHED` requires `APPROVED` and writes an export bundle.
- `RETIRED` hides from publication, keeps history.
- `BLOCKED` is set by a failed gate with severity ≥ high.

### 2.2 V11 tables (additive)

| Table | Purpose | Key columns |
| --- | --- | --- |
| `review_queue` | one row per detected issue | review_id (PK), content_id, artifact_id, issue_type, severity CHECK (`low`/`medium`/`high`/`critical`), claim_ref, evidence_ref, detected_reason, detected_by, review_status CHECK (`open`/`resolved`/`waived`), reviewer, reviewed_at, resolution, created_at, updated_at |
| `review_history` | append-only review decisions | history_id (PK AUTOINCREMENT), review_id FK CASCADE, action, reviewer, note, created_at |
| `approval_records` | one row per gate evaluation batch | approval_id (PK), content_id, artifact_id, requested_status, gates (JSON), passed, reviewer, approved_at, created_at |

`content_artifacts` gains additive nullable columns: `publication_status`,
`approved_at`, `approved_by`, `published_at`, `published_path`, `retired_at`,
`retired_reason` — plus `PUBLICATION_STATUSES` enum
(`UNREVIEWED/APPROVED/PUBLISHED/RETIRED/BLOCKED`).

### 2.3 The nine gates (all executable, all recorded)

| # | Gate | Check |
| --- | --- | --- |
| 1 | Curriculum alignment | topic resolves to a curriculum node (P2) |
| 2 | Evidence coverage | every canonical fact carries ≥1 evidence ref |
| 3 | Semantic evidence status | no linked evidence is `UNSUPPORTED`/`CONTRADICTED`/`INSUFFICIENT_EVIDENCE` |
| 4 | Citation continuity | rendered text cites only labels known to the content item |
| 5 | Content consistency | `content_consistency_report(...)` is True |
| 6 | Medical-risk scan | no unreviewed high-risk pattern (doses, contraindications, emergency, procedures, criteria, recommendations) |
| 7 | Copyright/export check | no long verbatim source run beyond the excerpt bound; references are locators, not bulk text |
| 8 | Formatting validation | artifact file exists on disk, non-empty, checksum matches |
| 9 | Artifact generation success | artifact row exists with `render_mode` recorded and status ≥ `READY` |

Each gate returns `{gate, passed, severity, detail}`; the batch is stored in
`approval_records` whether it passes or fails.

### 2.4 Medical-risk scan

Deterministic pattern scan over rendered text for: numeric dose units
(`mg/kg`, `mcg`, `IU`, `mmol`, dosing intervals), contraindication phrasing,
emergency vocabulary, procedural verbs, diagnostic-criteria phrasing, and
treatment-recommendation phrasing. Matches create `review_queue` rows with
`issue_type=medical_risk`, severity `high` (dose/emergency) or `medium`, and
`BLOCK` approval until a reviewer resolves them. **The scan never approves.**

### 2.5 Review workflow

`list_reviews(status)`, `resolve_review(review_id, reviewer, resolution,
note)` — resolution writes `review_history`, updates the queue row, and if the
artifact was `BLOCKED` and no open high-severity issues remain, returns it to
`NEEDS_REVIEW` (still requiring the full gate run before approval).

### 2.6 Export modes

- `export_bundle(content_id, mode)` with `mode ∈ {private, distributable}`.
- Private: canonical content + bounded excerpts + validation JSON.
- Distributable: rendered artifacts + reference locators only; asserts
  (and tests prove) no learner fields, no prompts, no excerpt payloads over the
  bound. Written under `T.PRODUCTS/<slug>/exports/<mode>/` with a
  `manifest.json` recording checksums and the mode.

### 2.7 Privacy / performance / migration notes

- No learner data is read by any P10 function except the export redaction
  check, which only asserts absence.
- Gates are pure reads over existing rows + one consistency re-verify
  (checksums, single-digit ms).
- V11 is additive: three tables + seven nullable columns + indexes; rollback =
  drop the three tables.

## 3. Verification

- Unit: `tests/test_publication.py` — 20 tests, all passing (~5 s; uses the
  `_model_seam` import from `test_product_integration`, so no Ollama calls).
- Full regression: **286 passed** (266 pre-P10 + 20).
- Data integrity: `PRAGMA integrity_check` = ok, `foreign_key_check` empty,
  both on an isolated demo home and the live DB.

## 4. Execution results

(recorded 2026-10-08)

**Migration on the live DB** (`core/database/migrate_v11.py`, default backup
enabled):

- Backup written and verified: `backups/medforge_pre_v11_backup.db`.
- Tables 45 → 48 (`review_queue`, `review_history`, `approval_records`).
- `content_artifacts` gained seven nullable columns; existing rows read as
  `publication_status='UNREVIEWED'` (default), no data rewritten.
- `schema_migrations` row `11.0.0 | v11_publication_workflow` recorded.
- Idempotent rerun (`--no-backup`) succeeds; integrity ok after both runs.

**Isolated E2E** (`MEDFORGE_HOME=/tmp/mf-p10-demo`, canned model seam —
`/tmp/mf-p10-demo/e2e_result.json`):

1. Fresh home; `ensure_publication_v11` builds the full chain (V10 then V11).
2. P2 syllabus import + P3 textbook seeding; P9 canonical generation
   (`growth hormone physiology`, mode=model) + deterministic rendering of
   cheat_sheet / flashcards / quiz / study_guide.
3. `run_approval_gates` — all nine gates pass, not blocked.
4. `approve_artifact(reviewer="Dr. E2E Reviewer")` → APPROVED.
5. `export_bundle(private)` and `(distributable)` — both written with
   manifests; private manifest contains `canonical_content`, distributable
   does not; distributable note asserts no learner data / prompts / excerpts.
6. `publish_artifact(mode="distributable")` → PUBLISHED; every artifact row
   records `published_path` pointing at the on-disk manifest.
7. Integrity: ok / 0 FK violations / 48 tables / V11 tables present.

**CLI** (`medforge_core publication ...`): `reviews` → `[]`, bare command →
usage, `state` on the demo content → full lifecycle JSON; end-to-end command
flow `run-gates → approve → export` verified on the demo home (all true).

**Defects found and fixed during the run** (each covered by tests):

- `_artifact_row` without `artifact_type` picked an arbitrary latest-version
  row while state reporting sorts by `artifact_type` — lifecycle transitions
  and state reporting disagreed about which row was acted upon. Fixed:
  deterministic ordering (`artifact_type, artifact_version DESC`) plus
  content-level transitions now act on **all** matching artifacts.
- `run_approval_gates` validated only one artifact; a medical-risk finding in
  a non-primary derivative was missed. Fixed: gates 4/6/7/8/9 now scan every
  rendered artifact and aggregate.
- Test harness initially called the real Ollama model (41 s/generation) —
  fixed by reusing the `_model_seam` fixture (47 s → 5 s suite).
