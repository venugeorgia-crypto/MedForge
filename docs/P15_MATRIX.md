# P15_MATRIX.md — Reliability + Security + Performance Hardening

Status: **IMPLEMENTED + TESTED** (`p15-doctor-v1`; no schema change — deliberately)

## 1. Audit (entry state)

- Backups existed per migration, but there was **no** verification command, no
  restore path, and no repair command for a user or an automated run.
- `ingest._resolve_and_validate` validated scheme/host/port/DNS but **did not
  reject credentials in URLs**, contrary to the property claimed in the
  implementation matrix.
- The provider layer offered an "OpenAI opt-in" hook whose module did not exist:
  a fake-cloud path that silently did nothing (flagged as feature theater).
- No measured performance guards; no security regression tests beyond P10's
  export refusals.
- Compatible surface: `doctor` CLI command already existed (models + quick_check)
  — it must keep working.

## 2. Design

### `medforge/doctor.py` (diagnostics, verification, restore, repair)

- `diagnose()` — schema versions (numeric ordering), pending migrations
  (V3 counts as satisfied when its baseline tables exist), integrity,
  foreign keys, tables, disk, backup inventory with verification of the newest,
  provider reachability, job-lock path. `healthy` is derived from the same
  evidence, and `problems` names each failure concretely.
- `verify_backup(path)` — read-only open, integrity, table count, versions.
- `restore_backup(backup, target=None)` — refuses unverified files; keeps a
  `medforge_prerestore_<ts>.db` safety copy of the current database; verifies
  the restored file afterwards.
- `repair(rebuild_fts, vacuum)` — quick_check first (a non-database file is
  reported failed, never "repaired"); FTS index is re-derived from `chunks`
  (it is a plain FTS5 table — `'rebuild'` is for external-content tables and
  silently emptied the index, found by test); VACUUM only when free disk ≥ 2×.
- `providers_openai.py` — a real, gated OpenAI-compatible client (chat, embed)
  with an injectable transport; refuses offline; never registered unless
  `MEDFORGE_PROVIDER=openai` **and** `MEDFORGE_OPENAI_API_KEY` are set.

### Hardening fixes

- `ingestion._resolve_and_validate` now rejects URLs containing credentials.
- No new tables: this phase is behaviour, not schema. Migration stays `15.0.0`.

### Measured performance guards (`tests/test_perf.py`)

| Measurement | Result (M1 Air, fixture DB) |
| --- | --- |
| FTS match over 10 000 chunks | **0.8 ms** |
| Claim listing (LIMIT 50) over 5 000 claims | **0.35 ms** |
| `diagnose()` full report | **5 ms** |
| Backup (352 KB db) + verify | **3 ms** + **1 ms** |

The four assertions live in `tests/test_perf.py` and run on every regression.

## 3. Acceptance criteria (proved by tests)

1. Diagnostics report missing DB, pending migrations, integrity, backups,
   provider honestly; `healthy` false when any problem exists.
2. Backup verification: good / missing / corrupt cases.
3. Restore round-trips data, keeps a safety copy, refuses unverified input.
4. Repair rebuilds FTS; a non-database file reports failed.
5. Loopback guard rejects non-loopback hosts, schemes, credentials.
6. SSRF allowlist rejects http, non-allowlisted, wrong-port, credential and
   lookalike-suffix URLs.
7. Shipped code never uses `shell=True`, `os.system`, or shell-string commands.
8. Cloud provider not registered by default; requires opt-in + key; refuses
   offline and without a key; transport exercised through an injected stub.
9. Performance bounds hold (no algorithmic regression).

## 4. Execution results

- **Tests**: 34 new (11 doctor, 19 security, 4 perf) — all passing.
- **Full regression**: **393 passed, 1 skipped** (359 + 34).
- **CLI E2E** on `/tmp/mf-p13-demo`:
  - `diagnose` → `healthy: true`, 54 tables, `applied: […14.0.0, 15.0.0]`,
    pending `[]`;
  - `diagnose verify-backup <nightly>` → ok, integrity ok, 54 tables,
    versions ordered numerically;
  - `diagnose "restore|<backup>|<target>"` → restored + verification ok;
  - `diagnose repair` → quick_check ok + FTS rebuild (rows re-derived).
- **Defects found and fixed**:
  1. `_resolve_and_validate` accepted credentials-in-URL (documented property was
     false) → now rejected, with a security test.
  2. FTS "repair" via `'rebuild'` silently emptied a plain FTS5 index → now
     re-derived from `chunks`, test asserts a deleted row is searchable again.
  3. Lexicographic version ordering reported `9.0.0` as the latest migration →
     numeric ordering everywhere (`_version_key`).
  4. A functioning demo home was reported unhealthy because the baseline `3.0.0`
     row was absent while its tables existed → `baseline_v3_present` logic.
  5. The OpenAI opt-in hook pointed at a non-existent module (fake-cloud path)
     → real gated client with an injectable transport and refusal tests.
- **P15 gate**: **PASS** — diagnostics, recovery, repair, security and
  performance guards are implemented, measured and honestly reported.

## 5. Honest limitations

- Performance numbers are fixture-scale (10 k chunks); retrieval quality under
  real textbooks is bounded by design (top-k, no re-ranking).
- `restore_backup` replaces the database file in place; running processes must be
  restarted afterwards (documented in OPERATIONS.md).
- The OpenAI provider is implemented and unit-exercised through a stubbed
  transport; it has **not** been exercised against the live OpenAI API (no key
  available, local-first default). Cloud use remains opt-in and unverified
  against the real service.
