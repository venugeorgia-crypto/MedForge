# P13 — Knowledge Refresh + Source Update System

**Version:** `p13-refresh-v1` | **Migration:** `14.0.0` (adds `source_refresh_log`,
`knowledge_refresh_runs`, `knowledge_notifications`)

---

## Why

Medical knowledge moves. A textbook PDF gets a new printing, PubMed publishes new
articles, a trusted web page is revised. MedForge should notice *without* silently
changing what it believes: every change is detected, logged, the claims that depended
on the changed source are **re-verified through P4's own verifier**, and a notification
is raised for the learner.

## Pipeline

```
sources ──► check_textbook_updates (file hash)
        ──► check_pubmed_updates   (PMID sets, injectable fetcher)
        ──► check_web_updates      (conditional GET / ETag)
             │
             ▼  changed source
   reverify_claims_for_document (P4 verify_claim, bounded candidates)
             │
             ▼
   knowledge_refresh_runs + source_refresh_log + knowledge_notifications
```

---

## Public API (`medforge.knowledge_refresh`)

```python
from medforge import knowledge_refresh as KR

# One full run: check sources → re-verify affected claims → notify
result = KR.run_refresh(triggered_by="manual", sources=["textbook", "pubmed", "web"],
                        pubmed_queries=["growth hormone"], web_urls=["https://..."],
                        verifier_fn=None, model=None)

# Individual checks
KR.check_textbook_updates(register_changes=True, ingest_new=True, embed_text=False)
KR.check_pubmed_updates(["growth hormone"], fetcher=None)
KR.check_web_updates(["https://example.org/page"], fetcher=None)

# Claim re-verification for one document (never deletes history)
KR.reverify_claims_for_document(document_id, verifier_fn=None, model=None)

# Audit + notifications
KR.get_refresh_status()
KR.list_notifications(acknowledged=0)
KR.acknowledge_notification(notification_id)
```

### Change detection semantics

| Source | Unchanged means | Changed means | Registered artifact |
| --- | --- | --- | --- |
| textbook | file sha256 == stored edition hash | hash differs → new P3 edition (old preserved) + chunk diff | `source_refresh_log` |
| pubmed | current PMID set ⊆ stored set | new PMIDs since last check | `source_refresh_log` (PMID list) |
| web | HTTP 304 or same body hash | new body hash | `source_refresh_log` (ETag + hash) |

`diff_textbook_editions(old, new)` reports `added_chunks`, `removed_chunks`,
`unchanged_chunks`, `pages_added`, `pages_removed` — nothing is ever deleted.

### Re-verification policy (owned by P4)

- Claims are found via `claim_evidence → evidence.document_id` (only claims that
  actually used this document).
- Candidates are bounded (≤400 chunks scanned, ≤3 per claim, ≤25 claims/document)
  and chosen by deterministic term overlap with the claim text.
- Empty candidate set → **deterministic abstention** (`INSUFFICIENT_EVIDENCE`,
  zero model calls). The verifier is never asked without evidence.
- Upgrade/downgrade accounting uses the rank table `CLAIM_STATUS_RANK`; every
  attempt is appended to `verification_runs` by P4 itself.

### Offline behaviour

`MEDFORGE_OFFLINE=1` skips `pubmed` and `web` checks (recorded as `skipped`,
not logged as source state). Textbook checks are fully local (file hashing) and
still run.

---

## CLI

```bash
medforge refresh check textbook                    # check local textbooks
medforge refresh check textbook,pubmed|growth hormone
medforge refresh check web||https://nice.org.uk/...
medforge refresh status                            # last run + open notifications
medforge refresh notifications unread              # or: all
medforge refresh ack <notification_id>
medforge refresh reverify <document_id>
```

---

## Database (V14)

```sql
source_refresh_log      -- one row per check (status ok/changed/error/skipped)
knowledge_refresh_runs  -- one row per orchestration run (counts + status)
knowledge_notifications -- changes awaiting acknowledgement (severity info/warning)
```

All three are additive; `migrate_v14.ensure_refresh_v14()` chains V9→…→V14,
takes a verified snapshot backup, and verifies integrity + foreign keys.

---

## Integration points

- **P3** owns registration/ingestion — refresh calls `register_textbook` with the
  *stored* document metadata so a changed file attaches to the same document identity.
- **P4** owns verification — refresh never re-implements it.
- **P10** can consume `knowledge_notifications` to flag published content whose
  evidence changed (display-only today; no automatic retirement).
- **Dashboard/CLI** surface status via `get_refresh_status()` / `refresh status`.

---

## Honest limitations

- No launchd/cron installer yet (P14 automation will own scheduling); runs are
  manual or invoked by a scheduler you provide.
- PubMed refresh compares PMID sets, not article contents (a revised abstract with
  the same PMID is not detected).
- Web fetch honours the P1 SSRF allowlist; non-allowlisted domains are refused.
- PDF *content* changes inside the same page set are reported as chunk add/remove
  counts, not semantic diffs.
