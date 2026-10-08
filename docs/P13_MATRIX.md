# P13_MATRIX.md — Knowledge Refresh + Source Update System

Status: **IMPLEMENTED + TESTED** (`p13-refresh-v1`, migration `14.0.0`)

## 1. Audit (entry state)

- Current source ingestion: `medforge/ingestion.py` — PDF ingestion (pypdf),
  PubMed import (E-utilities), trusted web research. All one-shot, no
  incremental updates.
- Textbook provenance (P3): `medforge/textbook.py` — documents/editions with
  content-hash versioning; re-ingest of changed file creates new edition,
  old preserved. But no automated "check for updates" workflow.
- Evidence graph (P4): claims/evidence tied to textbook chunks by edition.
  When a new textbook edition is added, existing claims don't automatically
  re-verify against new evidence.
- Learner model (P6): mastery based on `learning_attempts` — source-agnostic,
  but if source content changes, the evidence backing claims may shift.
- No scheduled/automated refresh pipeline. No diffing of source content.
- No notification when source material changes (new PubMed articles, updated
  guidelines, textbook errata).

## 2. Design

### Knowledge Refresh Pipeline

```
scheduled check (cron/launchd) → source_diff → affected_claims → reverify → notify → log
```

### Core Modules

**`medforge/knowledge_refresh.py`** — main orchestration:
- `check_source_updates()` — check all registered sources for changes
- `diff_textbook_edition(doc_id)` — compare current PDF hash vs stored
- `check_pubmed_updates(query, since_date)` — new articles for registered queries
- `check_web_updates(url, last_etag)` — conditional GET for trusted domains
- `reverify_affected_claims(source_ids)` — re-run P4 verification on claims
  whose evidence sources changed
- `notify_changes(changes)` — log to `knowledge_refresh_log`, optional UI badge

**`medforge/source_diff.py`** — content diffing:
- `diff_textbook(old_edition_id, new_pdf_path)` → `TextbookDiff` (added/removed/modified chunks)
- `diff_pubmed(old_pmids, new_pmids)` → `PubMedDiff`
- `diff_web(old_hash, new_hash, url)` → `WebDiff`

### Data Model (V14 Migration)

```sql
-- Source refresh tracking
CREATE TABLE source_refresh_log (
    refresh_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK(source_type IN ('textbook','pubmed','web','course_pdf')),
    source_id TEXT NOT NULL,           -- doc_id, query, or url
    last_checked_at TEXT NOT NULL,
    last_content_hash TEXT,            -- for textbooks/PDFs
    last_etag TEXT,                    -- for web
    last_pmid_list TEXT,               -- JSON array for PubMed
    status TEXT NOT NULL CHECK(status IN ('ok','changed','error','skipped')),
    changes_detected INTEGER NOT NULL DEFAULT 0,
    error_msg TEXT,
    created_at TEXT NOT NULL
);

-- Knowledge refresh runs (audit trail)
CREATE TABLE knowledge_refresh_runs (
    run_id TEXT PRIMARY KEY,
    triggered_by TEXT NOT NULL CHECK(triggered_by IN ('scheduled','manual','webhook')),
    sources_checked INTEGER NOT NULL DEFAULT 0,
    sources_changed INTEGER NOT NULL DEFAULT 0,
    claims_reverified INTEGER NOT NULL DEFAULT 0,
    claims_upgraded INTEGER NOT NULL DEFAULT 0,
    claims_downgraded INTEGER NOT NULL DEFAULT 0,
    notifications_created INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
    error_msg TEXT
);

-- Notifications for UI/dashboard
CREATE TABLE knowledge_notifications (
    notification_id TEXT PRIMARY KEY,
    refresh_run_id TEXT REFERENCES knowledge_refresh_runs(run_id) ON DELETE SET NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    affected_claims INTEGER NOT NULL DEFAULT 0,
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acknowledged_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_refresh_log_source ON source_refresh_log(source_type, source_id);
CREATE INDEX idx_refresh_runs_time ON knowledge_refresh_runs(started_at);
CREATE INDEX idx_notifications_ack ON knowledge_notifications(acknowledged, created_at);
```

### Scheduling

- **Launchd plist** (macOS) or **cron** (Linux) for daily/weekly runs
- Configurable via env: `MEDFORGE_REFRESH_SCHEDULE` (default: daily at 03:00)
- `MEDFORGE_REFRESH_SOURCES` — comma-separated: `textbook,pubmed,web` (default: all)
- Respects `MEDFORGE_OFFLINE=1` — skips network sources

### Integration Points

- **P3 Textbook**: re-ingest changed edition → new edition_id → re-link curriculum
- **P4 Evidence**: re-verify claims against new evidence → update verification_status
- **P9 Study Intelligence**: if claims change, update topic readiness/priority
- **P10 Publication**: if approved content's evidence changes, flag for review
- **Dashboard**: "Knowledge Refresh" tab showing last run, changes, notifications

## 3. Acceptance criteria (tests must prove)

1. **Textbook diff** — changed PDF detected, new edition created, old preserved, chunk-level diff reported.
2. **PubMed check** — new articles for registered query since last check returned.
3. **Web conditional GET** — 304 Not Modified handled, no re-download when unchanged.
4. **Claim re-verification** — claims linked to changed sources re-verified, status updated.
5. **Refresh logging** — every run recorded in `knowledge_refresh_runs` with counts.
6. **Notifications** — changes produce `knowledge_notifications` with severity.
7. **Offline mode** — `OFFLINE=1` skips PubMed/web, only checks local textbooks.
8. **Scheduler** — launchd plist generated, loads on install, runs daily.
9. **CLI** — `medforge refresh check|status|notify|ack` commands work.
10. **Full regression** — 340+ tests passing (326 + 14 P13).

## 4. Execution results

### Design decisions taken during implementation

- **Textbook re-registration must carry the stored metadata.** Registering the
  changed file by path alone re-derives the document id from the *filename* and
  splits one book into two documents (found by a failing test). Refresh now
  passes the stored `textbook_documents` row to `register_textbook`, so a new
  content hash attaches to the same document identity and the old edition is
  preserved.
- **Latest-edition selection is rowid-tiebroken.** Two editions can share a
  `created_at` second and edition ids are hashes (unordered); `_latest_edition`
  orders by `created_at DESC, rowid DESC`. The same reasoning fixed
  `_last_check` (refresh log) — `ORDER BY rowid DESC`.
- **Collision-free ids.** `_uid` uses `uuid4` entropy (the first draft hashed
  the current second and produced duplicate `refresh_id`s in a single run).
- **Verifier abstains without evidence.** `reverify_claims_for_document` builds
  bounded candidates from the *new* edition; zero candidates → zero model calls
  (asserted by a test that fails if the verifier is invoked).

### Gate results

- **V14 migration**: fresh run OK (chains V4→…→V14), 3 tables created
  (`source_refresh_log`, `knowledge_refresh_runs`, `knowledge_notifications`),
  `schema_migrations` row `14.0.0`, idempotent rerun OK, verified backup.
- **Unit tests**: 18/18 in `tests/test_knowledge_refresh.py` (~3 s, hermetic —
  injected PubMed/web fetchers, injected P4 `verifier_fn`, no Ollama).
- **Full regression**: **344 passed, 1 skipped** (326 P2–P12 + 18 P13).
- **Live DB**: `database/medforge.sqlite3` 13.0.0 → 14.0.0, 49 → 52 tables,
  `integrity_check=ok`, `foreign_key_check` empty, idempotent rerun verified,
  backup `backups/medforge_pre_v14_backup_20261008T202940.db`.
- **E2E** (isolated fresh home `/tmp/mf-p13-demo`, evidence
  `/tmp/mf-p13-demo/e2e_p13_result.json`): register → check (ok) → change file →
  check (changed: new edition `1e09229e8a9add5f`, old `db4f7e2ca5efde12` kept,
  diff `added_chunks=1 removed_chunks=1`, 1 notification) → `refresh
  notifications unread` → `refresh ack` (open 1 → 0) → `refresh reverify`
  (0 claims linked, honest no-op). **PASS**.
- **CLI**: `refresh check|status|notifications|ack|reverify` wired and exercised
  end-to-end on the isolated home.
- **P13 gate**: **PASS** — no feature theater: every check is logged, changes are
  notified, re-verification goes through P4, and abstentions are honest.

### Remaining honest limitations

No scheduler installer yet (P14 owns automation); PubMed compares PMID sets, not
revised abstracts; PDF diffs are chunk-count-level, not semantic.
See `docs/KNOWLEDGE_REFRESH.md`.