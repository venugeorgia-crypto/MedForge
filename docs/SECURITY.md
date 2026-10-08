# SECURITY — Verified Properties and Posture

MedForge is local-first. There are no accounts, no telemetry and no cloud calls
unless the user explicitly configures a cloud provider. This document lists what
is *verified by tests* (`tests/test_security.py`) rather than what is intended.

## Verified properties

| Property | How it is enforced | Test |
| --- | --- | --- |
| Model traffic stays on loopback | `providers._http_json` refuses any `OLLAMA_URL` whose host is not `127.0.0.1`/`localhost`/`::1`, or that is not plain `http`, or that carries credentials | `test_non_loopback_ollama_url_is_refused`, `test_ipv4_private_and_public_hosts_both_refused`, `test_scheme_and_credential_rejected` |
| Web research is SSRF-hardened | `ingestion._resolve_and_validate`: HTTPS only, port 443 only, host allowlist, no credentials in URL, every resolved IP must be global | `tests/test_security.py::TestSSRFAllowlist` (5 cases incl. lookalike suffix) |
| No shell execution | no `shell=True`, no `os.system`, subprocess commands are argument lists (never shell strings) anywhere in shipped modules | `TestShellSafety` (source-level, runs over `current/medforge`, `medforge_core.py`, `core/database`) |
| Cloud is opt-in only | `openai` provider registers only when `MEDFORGE_PROVIDER=openai` **and** `MEDFORGE_OPENAI_API_KEY` are set at import; it refuses to run in offline mode or without a key | `TestCloudGate`, `TestOpenAITransportSeam` |
| Learner data never leaves the machine implicitly | every cloud call site takes an explicit text argument; nothing iterates learner rows into a provider | design + P12 router (no automatic provider switch) |
| Distributable exports exclude learner data and long verbatim runs | P10 `export_bundle(mode="distributable")` refuses learner/prompt fields and >400-char verbatim reuse | `tests/test_publication.py::TestCopyrightAndExport` |
| Source text and answers are data, never instructions | P4/P7 delimited-data prompts; injection markers recorded, never executed | `tests/test_evidence.py`, `tests/test_tutor.py` |
| High-risk medical content cannot auto-approve | P10 medical-risk scan opens reviews; a named reviewer must resolve them | `tests/test_publication.py::TestMedicalRiskScan` |
| Uploads cannot escape their directory | dashboard sanitizes uploaded filenames and verifies the `%PDF-` magic | P1 audit + dashboard tests |

## Cloud posture (honest)

- The OpenAI-compatible provider is **implemented** (`medforge/providers_openai.py`)
  with an injectable transport, and is exercised in tests through a stubbed
  transport. It has **not** been exercised against the live OpenAI service
  (no key in this environment). Treat live cloud use as unverified.
- When it runs, only the exact text passed to `chat`/`embed` is transmitted —
  the caller decides. Study pipelines default to the local provider.

## Secrets

- MedForge itself needs no API keys for its default local operation.
- If `MEDFORGE_OPENAI_API_KEY` is set, it is read from the environment at call
  time and never written to disk by MedForge. Tests assert the provider is not
  even registered without it.
- NCBI requests include `MEDFORGE_EMAIL` (optional) for polite rate limits; no
  other identifying data is sent.

## What is *not* claimed

- No protection against a compromised local account or a malicious file the user
  chooses to ingest beyond the stated validation.
- No network sandbox: if the user sets `OLLAMA_URL` to a loopback alias of
  another service, that is their configuration; the guard's job is loopback-only,
  and it holds.
