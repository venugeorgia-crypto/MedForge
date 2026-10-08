# MedForge Implementation Matrix — Phase 0 Forensic Audit

**Audit date:** 2026-10-04 · **Code audited:** `releases/2.1.0-20935f509d37` (the running 2.1.0 build),
`core/database/`, `tests/`, `docs/`, `tools/`, live DB `database/medforge.sqlite3`.

**Method:** every source module read directly (types, storage, learner, product, generation,
retrieval, ingestion, export, models, utils, dashboard, CLI, launchers, installer builder),
schema + migration read in full, live DB queried read-only, all tests and docs reviewed.
"COMPLETE" below means working end-to-end with validation; "PARTIAL" means real but incomplete;
verdicts are KEEP / FIX / REFACTOR / REPLACE / MISSING.

Live DB row counts at audit time: `curriculum_nodes=0, prerequisites=0, learner_mastery=0,
learner_weaknesses=0, interactive_sessions=0, spaced_repetition_queue=119, review_log=0,
chunks=47, schema_migrations=1`.

---

## P1 Foundation (keep-list verification)

| Requirement | Current implementation | Source file | Actual behavior | Test coverage | Risk | Verdict |
|---|---|---|---|---|---|---|
| SQLite metadata store | chunks + chunks_fts (FTS5), legacy study_sessions/weaknesses | `medforge/storage.py`, `core/database/schema.py` | Verified: 47 chunks live; FTS used by retrieval | `tests/test_phase1_db.py`, `test_retrieval.py` | Low | **KEEP** |
| Chroma vector store | PersistentClient, per-embed-model collection name | `medforge/storage.py` | Working; falls back to keyword-only when down | `test_retrieval.py` | Low | **KEEP** |
| Ollama chat + embeddings | loopback-only guard, capability checks, 3.6 GB size cap, model unload | `medforge/models.py` | Enforces 127.0.0.1; `keep_alive` + `stop_model()` free RAM; qwen3:4b-instruct fallback chain | mocked tests (test_generation/models) | Low | **KEEP** |
| Streamlit dashboard | 7 tabs (PRODUCT/STUDY/REVIEW/ASK/LIBRARY/HISTORY/STATUS) | `dashboard.py` | Working, verified in browser | manual verification only | Low | **KEEP** |
| PDF ingestion | pypdf page extraction, sha256 change-detection, stale-chunk retirement | `medforge/ingestion.py` | Page-level chunks → Chroma+SQLite; reports "may need OCR" for scanned PDFs | `test_ingestion.py` (fixtures) | Medium (no OCR) | **KEEP; OCR MISSING** |
| PubMed import | E-utilities esearch/efetch XML, chunk+index | `medforge/ingestion.py` | Works online; degrades gracefully offline | fixture tests | Low | **KEEP** |
| Trusted web retrieval | HTTPS-only, port 443, domain allowlist, resolved-IP private-range check, size/time caps | `medforge/ingestion.py` `_resolve_and_validate` | SSRF-hardened (no localhost/private IPs, no credentials-in-URL) | `test_ingestion.py` | Low | **KEEP** |
| Learner tables | learner_mastery, learner_weaknesses, interactive_sessions | `core/database/schema.py`, `medforge/learner.py` | Actively used by study/REVIEW flows; self-healing DDL | `test_learner.py`, `test_spaced_repetition.py` | Low | **KEEP** |
| Spaced repetition (SM-2/FSRS) | SM-2 + FSRS-4.5, lapse→relearning, review_log, analytics, leeches | `medforge/learner.py` | 119 scheduled cards live; weakness lifecycle verified | `test_spaced_repetition.py` (14 tests) | Low | **KEEP** |
| Anki export | genanki deck, escaped fields, NeedsReview tag | `medforge/export.py` | Works from validated flashcards.csv | `test_export.py` | Low | **KEEP** |
| Resumable products | state.json step checksums, atomic writes, resume detection | `medforge/product.py`, `medforge/utils.py` | Resume verified; source-snapshot digest stops on evidence change | `test_spaced_repetition.py` (auto-import) | Low | **KEEP** |
| Backup/migration | verified snapshot backup, integrity check, rollback, data-loss detector | `core/database/migrate_v3.py` | Tested | `test_phase1_db.py` | Low | **KEEP** |
| Local launcher | `medforge_launch.py`, `MEDFORGE.command` → `~/bin/medforge` | launchers | macOS-only; requires prior install | none (out of unit-test scope) | Low | **KEEP** |

## P2 Curriculum engine

| Requirement | Current implementation | Source file | Actual behavior | Test coverage | Risk | Verdict |
|---|---|---|---|---|---|---|
| Curriculum hierarchy tables | `curriculum_nodes` (Year/Semester/Course/Module/Topic/Subtopic/LO) | `core/database/schema.py` | **Table exists but is DEAD** — 0 rows, no app code reads/writes it | schema tests only | High if left dead | **MISSING** → build on it (this session) |
| Prerequisite graph | `prerequisites` table | `core/database/schema.py` | **DEAD** — 0 rows, no app code | schema tests only | High | **MISSING** → build on it |
| Weekly syllabus import | none | — | — | — | — | **MISSING** → implement (this session) |
| Seminar import / topic normalization / duplicate detection / ordering / progress | none | — | — | — | — | **MISSING** → implement (this session) |
| Topic traceability to curriculum | none (topics are free-text slugs) | `medforge/learner.py` | mastery keyed on `slugify(topic)` only | — | High | **MISSING** → implement (this session) |

## P3 Textbook knowledge engine — **IMPLEMENTED (V5, this session)**

| Requirement | Implementation | Source file | Actual behavior | Test coverage | Risk | Verdict |
|---|---|---|---|---|---|---|
| Textbook metadata (title/authors/publisher/edition/year/ISBN/subject/source type) | `textbook_documents` + `textbook_editions` with deterministic ids | `medforge/textbook.py`, `core/database/schema.py` | First-class; metadata from args → sidecar JSON → PDF metadata → filename; nothing invented | `test_register_metadata_and_identity`, `test_sidecar_and_filename_metadata` | Low | **DONE** |
| Edition identity + versioning | `content_hash` unique; edition id = sha1(doc\|hash) | `medforge/textbook.py` | Same file idempotent; changed file = new edition, old preserved | `test_same_file_reimport_is_idempotent`, `test_changed_file_creates_new_edition_and_preserves_old` | Low | **DONE** |
| Chapter/Section/Page/Chunk hierarchy | `textbook_nodes`/`textbook_pages`/`textbook_chunks`; bookmarks or conservative heading regex; implicit `Body` fallback | `medforge/textbook.py` | Page-wise streaming; node page ranges incl. descendant pages | `test_ingest_structure_and_provenance`, `test_no_structure_pdf_gets_body_chapter`, `test_book_structure_bounded` | Low | **DONE** |
| Stable locators | chunk rows carry edition/document/node/page/chunk_index + human locator (`p. N · Chapter / Section`) | `medforge/textbook.py` | Verified in tests and CLI evidence output | provenance tests | Low | **DONE** |
| OCR status | `textbook_pages.extraction_status/ocr_status`; edition `ocr_status`, `skipped_pages` | `medforge/textbook.py` | No-text pages parked `pending`, never guessed; no OCR engine yet (architecture ready) | `test_ingest_structure_and_provenance`, `test_no_text_page_ocr_pending` (via PARTIAL assertions) | Low | **DONE (detection only)** |
| TOPIC ↔ TEXTBOOK mapping | `curriculum_text_links` (COALESCE-unique) + `textbook_evidence_for_topic()` + read-only suggestions | `medforge/textbook.py` | Link idempotent; discovery returns metadata + bounded previews | `test_curriculum_link_discovery_and_unlink`, `test_link_validation_errors`, `test_suggestions_are_read_only` | Low | **DONE** |
| Source priority | `SOURCE_PRIORITY` ranks textbook 0.95 via the existing `quality` field | `medforge/types.py` | Retrieval picks textbooks up with zero retrieval-code changes; web never silently equated | regression + shared-chunk assertions | Low | **DONE** |
| Tables / figure references | not extracted | — | Honest gap: tables inside PDFs are read as text; figures/plan not modeled | — | Medium | **MISSING (P11)** |

## P4 Evidence graph

| Requirement | Current implementation | Source file | Actual behavior | Test coverage | Risk | Verdict |
|---|---|---|---|---|---|---|
| Citation-label validation | `citation_audit`: per-line [S#] label scan vs source labels, HTML/JSON report | `medforge/generation.py` | Honest compatibility floor: report states "semantic support has NOT been verified"; `[S#]` labels resolve to evidence records via `pack_label_map()` | `test_generation.py`, `test_legacy_source_labels_resolve_to_evidence_records` | Low | **KEEP (as floor)** |
| Claim records (claim_id, text, normalized text, type, topic, curriculum link, run, statuses) | `claims` table (V6) + deterministic extraction from generated text | `medforge/evidence.py`, `core/database/schema.py` | Content-addressed and duplicate-safe; headings/code/prompts/questions/instructions excluded; original wording preserved | `test_claim_extraction_handles_and_skips_correctly`, `test_duplicate_claim_detection`, `test_normalization_is_deterministic_and_non_aggressive` | Low | **DONE** |
| Evidence records with exact provenance | `evidence` table; joined from `textbook_chunks`/`textbook_nodes` (never invented) | `medforge/evidence.py` | document/edition/chapter/section/page/chunk + bounded excerpt (≤900 chars); idempotent ids | `test_evidence_creation_preserves_exact_locator`, `test_evidence_creation_is_idempotent_and_never_invents_provenance`, `test_excerpt_is_bounded` | Low | **DONE** |
| Claim↔evidence relationships + verification history | `claim_evidence` (UNIQUE pair) + append-only `verification_runs` | `medforge/evidence.py`, `core/database/schema.py` | One edge per pair; every attempt retained; malformed verifier output degrades to INSUFFICIENT_EVIDENCE | `test_duplicate_relationships_prevented_but_history_appended`, `test_supported_claim`, `test_malformed_verifier_response_never_upgrades` | Low | **DONE** |
| Semantic support / contradiction / population / date checks | model-assisted per (claim, evidence) pair with delimited data-only prompt; strict JSON verdict | `medforge/evidence.py` | Supported/partially/unsupported/contradicted/insufficient; `context_mismatch` recorded; aggregation never upgrades; no keyword-overlap verdicts | `test_supported_claim`, `test_partially_supported_claim`, `test_unsupported_claim_detected_and_flagged`, `test_contradiction_is_representable_without_a_winner`, `test_aggregation_rules_are_deterministic` | Medium | **DONE (model-assisted, fallible by design)** |
| Explicit INSUFFICIENT state | machine-enforced abstention: no candidates → `INSUFFICIENT_EVIDENCE` with zero model calls | `medforge/evidence.py` | Verifier errors/malformed answers also abstain; never an upgrade | `test_insufficient_evidence_abstains_without_any_model_call`, `test_malformed_verifier_response_never_upgrades` | Low | **DONE** |
| Prompt-injection resistance in verification | excerpts passed as delimited data; system prompt declares source text untrusted, never instructions; delimiter tokens neutralized | `medforge/evidence.py` | Injection text stored as data and cannot change a verdict | `test_verifier_prompt_is_delimited_data_and_system_declares_untrusted` | Low | **DONE** |
| Curriculum → claim → evidence traceability | `claims.curriculum_node_id` + `claim_info()` provenance chain | `medforge/evidence.py` | Topic → claim → evidence → edition → chapter → section → page verified end-to-end | `test_source_metadata_and_curriculum_trace_end_to_end` | Low | **DONE** |
| Distribution safety of excerpts | excerpts live in the private SQLite DB; pack artifact `evidence-graph.json` is excerpt-free | `medforge/evidence.py`, `medforge/product.py` | Verified: excerpt text absent from the pack file | `test_product_pipeline_extracts_verifies_and_keeps_excerpts_private` | Low | **DONE** |

## P5 Source hierarchy

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Per-task source ranking | single generic `quality` weight (domain trust 0.55–1.0) | `medforge/types.py`, `medforge/retrieval.py` | No task-conditioned hierarchy; quality is a *ranking hint only* (labeled as such in prompts/reports) | `test_retrieval.py` | Medium | **PARTIAL** |
| Preserve authority separately from rank | `publication_type` exists in `medical_sources` (dead table) | `core/database/schema.py` | Not connected to retrieval | — | Medium | **MISSING wiring** |

## P6 Learner model — **IMPLEMENTED (V7)**

Model documentation: `docs/LEARNER_MODEL.md`; audit + execution results:
`docs/P6_MATRIX.md`.

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Topic mastery + confidence | incremental mean over attempts + pass-share | `medforge/learner.py::update_mastery` | Real, persisted, tested; **kept** as the legacy compatibility surface | yes | Low | **KEEP** |
| Recency-weighted / question-level evidence | exponential decay (half-life 21 d) with prior shrinkage over append-only `learning_attempts`; question-level fields (`item_type`, `correct`, `confidence`, response time) recorded | `learner_model.py`, `schema.py` (V7), `learner.py` hooks | Recency dominates stale samples; ten old 0.95s lose to three recent 0.30s | 32 P6 tests | Low | **DONE (`p6-rwm-v1`)** |
| Concept/subtopic mastery, learning velocity, forgetting | per-topic state with recent/historical split, consistency, ESS-based uncertainty; prerequisite impact via the P2 graph | `learner_model.py` | Trend + uncertainty exposed; prerequisite gaps reported read-only and fed to priority | yes | Low | **PARTIAL → trend present, subtopic rollup still per-topic** |
| Weakness severity, recovery, review priority | deterministic detector (known vs possible/low-confidence, auto-recovery) + weighted priority with exposed components | `learner_model.py::detect_weaknesses`, `study_priority` | Verified end-to-end: medium weakness at 0.55, auto-recovery after 0.80+0.90 | yes | Low | **DONE** |
| Confidence calibration | confidence tracked separately, signed gap + direction, `None` when no observations | `learner_model.py::get_confidence` | Over-/under-confident cases verified (gap +0.574 / −0.6289) | yes | Low | **DONE** |
| Full recalculation from history | pure rebuild from stored attempts at the stored evaluation time | `learner_model.py::recalculate_mastery/_all` | `matches_stored: true`, mismatches [] | yes | Low | **DONE** |
| Survives restart/upgrade/regeneration | SQLite-backed; content-addressed card ids survive pack rebuild; V7 additive/idempotent | `learner.py`, `migrate_v7.py` | Verified by idempotent import + migration tests | yes | Low | **KEEP** |

## P7 Adaptive tutor — **IMPLEMENTED (V8)**

Documentation: `docs/TUTOR.md`; audit + execution results: `docs/P7_MATRIX.md`.

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Interactive ask→answer→evaluate→adapt loop | persisted stage machine (`TEACH→ASK→WAITING_FOR_ANSWER→EVALUATE→EXPLAIN→ADAPT→COMPLETE`) in `tutor_sessions`/`tutor_turns` | `medforge/tutor.py`, `schema.py` (V8) | Multi-turn sessions survive refresh/restart; a finished session cannot be re-answered | 37 P7 tests | Low | **DONE (`p7-tutor-v1`)** |
| Weak-concept remediation flow | target selection from P6 `study_priority` (weakness, recent failure, overdue review, prerequisite risk) with stored reason; explicit user topic overrides | `tutor.py::select_tutor_target` | Deterministic, reason + components persisted on the session | yes | Low | **DONE** |
| Evidence-grounded teaching | P3 textbook links first, bounded retrieval second, persisted through P4 `store_evidence`; headings stripped, off-concept rows excluded, exact duplicates collapsed | `tutor.py::gather_evidence`, `assess_evidence` | Verification policy gates teaching: SUPPORTED teaches, PARTIALLY qualifies, UNSUPPORTED/INSUFFICIENT abstain (blocked session), CONTRADICTED surfaces disagreement; no self-verification | yes | Low | **DONE** |
| Adaptive questions + grading | 7 modes, 4 question types, content-addressed items, deterministic MCQ grading, model-assisted free-text grading with strict JSON + deterministic fallback | `tutor.py`, `types.py` | Confidence-mismatch and repeated-failure/success adaptation verified end-to-end; prerequisite repair falls back to `simplify` when the prerequisite has no evidence | yes | Low | **DONE** |
| Learner-state + SR integration | `record_learning_event` per graded interaction (turn stores `attempt_id`), conceptual errors → `origin='tutor'` misconceptions, one `review_card` sync on completion | `tutor.py`, `learner_model.py`, `learner.py` | No second learner system; model failure keeps the answer, records no grade/event, and is retryable | yes | Low | **DONE** |

## P8 Question-level assessment engine — **IMPLEMENTED (V9)**

Documentation: `docs/ASSESSMENT.md`; audit + execution results: `docs/P8_MATRIX.md`.

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Versioned item bank | `assessment_items` + immutable `assessment_item_versions` per `(item_id, item_version)`; 6 item types; content_hash per version | `medforge/assessment.py`, `schema.py` (V9) | Material changes create a new version (`revise_item`); old versions never mutate, so historical attempts replay exactly; `retire_item` removes from selection without rewriting history | 47 P8 tests | Low | **DONE (`p8-assessment-v1`)** |
| Evidence-gated generation + validation | `generate_items` → DRAFT only; validation rejects-not-fixes; approval refuses UNSUPPORTED/CONTRADICTED/INSUFFICIENT_EVIDENCE; PARTIALLY_SUPPORTED needs explicit recorded review | `assessment.py` (reuses P3/P4/P7 `gather_evidence`/`extract_key_points`/`assess_evidence`) | Distractors are real evidence sentences off-concept; <2 distractors → type fallback, never invented options; duplicate detection reports, never merges | yes | Low | **DONE** |
| Blueprints + deterministic selection | scope × type/difficulty/topic distributions, time limit, pass threshold, seed; most-constrained-slot-first scheduler with reported relaxations; exposure as ordering preference | `assessment.py::create_blueprint/validate_blueprint/select_items` | Same seed reproduces the same selection; feasible blueprints honored exactly (`relaxations=0` verified); shortfalls reported, never fabricated | yes | Low | **DONE** |
| Assessment sessions + scoring | persistent sessions separate from `tutor_sessions`; attempt snapshot per question; PRACTICE/EXAM/REVIEW modes; server-side timing; deterministic scoring + partial credit | `assessment.py`, `schema.py` (V9) | EXAM withholds feedback until submission; expiry preserves completed attempts; graded answers never overwritten (retryable re-graded in place with `grading_history`) | yes | Low | **DONE** |
| Item statistics + remediation | transparent stats with `sample_size` ("insufficient sample" < 5; discrimination ≥ 8 with upper/lower thirds); quality flags recorded; P6-driven remediation payload with `launch: false` | `assessment.py::get_item_statistics/compute_item_quality/get_assessment_remediation` | Lightweight reporting, not validated psychometrics; weakness/prerequisite/priority signals read from P6 — no second learner algorithm | yes | Low | **DONE** |

## P9 (this phase) Study intelligence + product factory integration — **IMPLEMENTED (V10)**

Documentation: `docs/STUDY_INTELLIGENCE.md`, `docs/PRODUCT_FACTORY.md`; audit +
execution results: `docs/P9_MATRIX.md`.

> Numbering note: the original Phase 0 roadmap numbered "P9 = spaced learning"
> and "P10 = product factory". The delivered P9 phase is the *integration*
> phase — study intelligence orchestrating P2/P3/P4/P6/P7/P8 plus the canonical
> product factory — so both appear here. P10 is now publication/approval.

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Study intelligence layer | `get_study_status`, `recommend_next_action`, `get_knowledge_gaps`, `get_readiness`, `adaptation_profile` | `medforge/study.py` (V10) | Reads P2/P3/P4/P6/P8/SR through public APIs only; derived topic states with centralized thresholds; every recommendation carries machine-readable `reasons` + a WHY sentence | 28 P9 tests | Low | **DONE (`p9-study-v1`)** |
| Deterministic target + action selection | weighted reason codes (`weakness`, `uncertainty`, `overdue`, `recent_failure`, `prerequisite_impact`, `not_started`, `assessment_gap`, `review_due`) → `NEW_TEACHING/REVIEW/DRILL/PREREQUISITE_REPAIR/TUTOR/ASSESS/REMEDIATION/RECALL/SPACED_REVIEW` | `study.py` | Declining topics use P6's own recency-weighted trend (`<= -0.15`); no second mastery algorithm was written | yes | Low | **DONE** |
| Persistent reproducible plans + daily plan + time budgets | versioned `study_plans` (re-planning supersedes, never rewrites); `get_today_plan` greedy fit with `did_not_fit` | `study.py`, `schema.py` (V10) | Real per-action durations; actions shortened to fit before being dropped; a 10-minute budget no longer produces an empty day | yes | Low | **DONE** |
| Missions bound to real engines | `study_missions` stores `tutor_session_id` / `assessment_id`; `launch_mission_engines` re-attaches idempotently | `study.py` → P7/P8 public APIs | Real P7 sessions and real P8 assessments; refusals recorded as honest abstentions, never faked; restart-safe with no duplicated study events | yes | Low | **DONE** |
| Canonical content model + deterministic rendering | one evidence-cited `content_items` row per (topic, sources, config, prompt, model); 6 artifact types rendered from one fact list | `medforge/content.py` (V10) | Uncited model output is dropped element-by-element; fallback sentences are verbatim evidence; cache key includes the model, so switching models cannot silently reuse another model's output | 23 P9 tests | Low | **DONE (`p9-content-v1`)** |
| Provenance, checksums, versioning, tamper detection | `content_artifacts` rows + `artifact_provenance` + `content_consistency_report` | `content.py` | artifact → content item → evidence → P3/P4 chain; on-disk sha256 re-verification detects hand-edits; artifact versions accumulate; changed sources produce a new `content_id` (append-only history) | yes | Low | **DONE** |
| Learner-adapted products | `render_study_products(adaptation=…)` changes emphasis only | `content.py`, `study.py` | Profile read from P6; one file per (type, profile); the canonical item is learner-independent and never rewritten | yes | Low | **DONE** |
| V10 migration | 5 additive tables (`study_plans`, `study_missions`, `study_actions`, `content_items`, `content_artifacts`) + 8 indexes | `core/database/migrate_v10.py` | Idempotent; verifies integrity + foreign keys; verified backup; live DB 9.0.0 → 10.0.0 with all 438 pre-existing rows preserved | yes | Low | **DONE** |
| CLI + dashboard surface | `study-intel` / `product-intel`; dashboard STUDY tab = P9 Study Home | `medforge_core.py`, `dashboard.py` | Legacy `study`, `product` and self-study logging untouched; dashboard shows today's budget/plan, gaps, declining topics, recommendation + WHY, mission start | yes | Low | **DONE** |

Still open after this phase (unchanged, not P9 scope): mechanism diagram, OCR,
PDF tables/callouts, video renderer, provider abstraction, distributable bundle
— see the rows below.

## P10 (this phase) Publication / review / approval workflow — **IMPLEMENTED (V11)**

> Numbering note: an older roadmap section below still labels "P10 product
> factory / P11 PDF / P12 video" — that numbering is obsolete. The delivered
> P10 is the publication/approval workflow below; execution results:
> `docs/P10_MATRIX.md`, `docs/PUBLICATION.md`.

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Formal content lifecycle | DRAFT/VALIDATING/NEEDS_REVIEW/APPROVED/PUBLISHED/RETIRED/BLOCKED on per-artifact rows | `medforge/publication.py` (V11) | `UNREVIEWED → NEEDS_REVIEW → APPROVED → PUBLISHED → RETIRED`; BLOCKED on any high-severity failure; content status = strictest artifact status | 20 P10 tests | Low | **DONE (`p10-publication-v1`)** |
| Nine approval gates | all nine implemented and enforced over EVERY rendered artifact | `publication.py::run_approval_gates` | curriculum alignment, evidence coverage, P4 verdicts, citation continuity, consistency checksums, medical-risk scan, copyright bound (400 chars), formatting, artifact generation | gate-name + tamper tests | Low | **DONE** |
| Medical safety: review-forcing, never self-approving | dose/contraindication/emergency/procedure/criteria/recommendation patterns force open reviews | `publication.py::HIGH_RISK_PATTERNS` | any hit blocks APPROVED until a named reviewer resolves/waives; automated runs can never approve risk | dedicated tests | Low | **DONE** |
| Review queue + history | review_queue + append-only review_history + approval_records | V11 tables | idempotent findings per content+type+reason; resolution unblocks BLOCKED→NEEDS_REVIEW when no high issue remains | lifecycle tests | Low | **DONE** |
| Private / distributable separation | export modes with hard refusals | `publication.py::export_bundle` | distributable refuses learner/prompt fields and >400-char verbatim runs; private carries canonical content; manifests record checksums | export tests + E2E | Low | **DONE** |
| CLI surface | `publication run-gates|approve|publish|retire|state|queue|reviews|resolve|export` | `medforge_core.py` | verified on isolated demo home | CLI E2E | Low | **DONE** |

Still open in P10 scope: dashboard Publication tab (CLI/engines complete;
rendering-only work, no domain logic pending).

## P11 (this phase) Professional Medical Video Production — **IMPLEMENTED (V12)**

Documentation: `docs/VIDEO.md`; audit + execution results: `docs/P11_MATRIX.md`.

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Deterministic storyboard from canonical content | scene list with claim/evidence refs, on-screen labels preserved | `medforge/video.py::build_storyboard` | Same canonical item ⇒ same storyboard checksum; 10 scenes for typical topic | 14 P11 tests | Low | **DONE (`p11-video-v1`)** |
| Frame rendering (Pillow) | 1920×1080 / 1080×1920 / 1080×1080 exact dims per aspect | `video.py::render_frames` | Frames exist on disk with correct pixel dimensions | frame tests | Low | **DONE** |
| Local TTS narration (pluggable seam) | macOS `say` default; seam allows piper/coqui/elevenlabs | `video.py::synthesize_narration` + `_tts_seam` | Tests patch the seam; E2E uses real `say` | TTS seam test | Low | **DONE** |
| ffmpeg assembly + concat | per-scene still+audio → H.264/AAC segments → concat → faststart MP4 | `video.py::assemble_video` | Single-pass CRF 18, preset medium | render tests | Low | **DONE** |
| ffprobe QA gate | container, video/audio codec, dims, duration, frames, clean decode | `video.py::probe_video` | FAIL on any check → status FAILED, error recorded; never hidden | QA tests + corrupt file test | Low | **DONE** |
| Captions (SRT + VTT) | hand-written, monotonic, one cue per scene, parse cleanly | `video.py::write_captions` | No pysrt/webvtt dependency | caption format tests | Low | **DONE** |
| Persistence (V12) | `video_renders` table with status, path, manifest, checksums | `migrate_v12.py`, `schema.py` V12 DDL | Chains V9→V10→V11→V12; idempotent; verified backup | migration tests | Low | **DONE** |
| CLI surface | `video render|state|probe` | `medforge_core.py` | Verified on isolated demo home | CLI E2E | Low | **DONE** |
| E2E real MP4 evidence | three aspects rendered, ffprobe-validated, evidence JSON | `/tmp/mf-p10-demo/e2e_p11_result.json` | h264/aac, clean decode, correct dims, ~40s, 864–977 KB each | manual + automated | Low | **DONE** |

## P12 (this phase) Provider abstraction + model router — **IMPLEMENTED (V13)**

Documentation: `docs/P12_MATRIX.md`.

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Provider protocol + registry | `Provider` protocol; Ollama (default local) + Mock (testing); OpenAI only on explicit opt-in | `medforge/providers.py` | Loopback-only for Ollama; cloud never default | 27 P12 tests | Low | **DONE (`p12-router-v1`)** |
| Task-aware model router | teaching→largest context, grading→smallest, reasoning→thinking-capable, embedding→embed models; size guard | `medforge/router.py` | `MEDFORGE_CHAT_MODEL`/`MEDFORGE_EMBED_MODEL` overrides; `MEDFORGE_MODEL_SIZE_GB` (3.6 default) | yes | Low | **DONE** |
| Backward compatibility | `models.py` public functions are thin wrappers over provider+router | `medforge/models.py` | All legacy callers unchanged; full regression green | full suite | Low | **DONE** |
| Model-run auditing | `model_runs` table (task, provider, model, latency, success) | V13 migration | Additive + idempotent; `record_model_run` records metadata | migration tests | Low | **DONE** |

## P13 (this phase) Knowledge refresh + source update system — **IMPLEMENTED (V14)**

Documentation: `docs/KNOWLEDGE_REFRESH.md`; audit + execution results:
`docs/P13_MATRIX.md`.

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Textbook change detection | file sha256 vs stored edition hash → new P3 edition (old preserved) + chunk diff | `medforge/knowledge_refresh.py::check_textbook_updates`, `diff_textbook_editions` | Re-registration carries stored document metadata so identity survives; chunk add/remove counts reported | 18 P13 tests | Low | **DONE (`p13-refresh-v1`)** |
| PubMed / web change detection | PMID-set diff (injectable fetcher); conditional GET (ETag / body hash) | `check_pubmed_updates`, `check_web_updates` | Network sources skipped under `MEDFORGE_OFFLINE`; web fetch reuses P1 SSRF validation | yes | Low | **DONE** |
| Claim re-verification | bounded candidates from the new edition → P4 `verify_claim`; upgrade/downgrade accounting | `reverify_claims_for_document` | Zero candidates → deterministic abstention with zero model calls; P4 history append-only | yes | Low | **DONE** |
| Audit trail + notifications | one log row per check; one run row per orchestration; notifications with severity + acknowledgement | V14 tables + `run_refresh` | Skipped checks are not logged as source state; errors recorded, never hidden | yes | Low | **DONE** |
| CLI | `refresh check|status|notifications|ack|reverify` | `medforge_core.py` | Verified end-to-end on an isolated home | CLI E2E | Low | **DONE** |

Remaining honest limitations: no scheduler installer (P14 owns automation);
PubMed compares PMID sets, not revised abstracts; PDF diffs are chunk-level.

## P14 (this phase) Automation + nightly study workflow — **IMPLEMENTED (V15)**

Documentation: `docs/AUTOMATION.md`; audit + execution results: `docs/P14_MATRIX.md`.

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Nightly workflow | maintenance → P13 refresh → verified backup → P9 study prep → recorded run | `medforge/automation.py::run_nightly` | Per-step JSON results; a failing step marks the run failed with the error; later steps still run | 15 P14 tests | Low | **DONE (`p14-automation-v1`)** |
| Verified backups + retention | snapshot copy, `integrity_check` on the copy, keep newest 7 nightly snapshots | `backup_step` | Pruning only ever touches `medforge_nightly_*.db`; other backups untouched | yes | Low | **DONE** |
| Scheduling | launchd user agent (macOS) with `StartCalendarInterval`, `RunAtLoad=false`; cron line elsewhere | `install_schedule`, `schedule_status`, `uninstall_schedule` | `plutil -lint` OK; `write-only` supported; non-macOS reports honestly, no fake install | yes | Low | **DONE** |
| Audit trail | `automation_jobs` + `automation_runs` (steps JSON, status, timings) | V15 migration | Additive + idempotent | migration tests | Low | **DONE** |
| CLI | `nightly [run|dry-run|only|runs|status]`, `schedule install|status|uninstall` | `medforge_core.py` | Verified end-to-end on the demo home | CLI E2E | Low | **DONE** |

## P9 Spaced learning (original roadmap numbering)

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| SM-2/FSRS preserved | yes, both, with review_log | `learner.py` | Verified | yes | Low | **KEEP** |
| Card→curriculum/concept/source links | card → topic slug only (item_id prefix); sources string on card | `learner.py` | No curriculum node / concept / claim links | partial | Medium | **PARTIAL** |
| Leech → revised card generation | leech detection exists (3+ lapses) | `learner.py::review_analytics` | Detection only; no auto-rewrite | yes (detection) | Medium | **PARTIAL** |

## P10 Product factory / P11 PDF engine / P12 video engine

> P9 added a **canonical** product path beside the legacy one:
> `content.py` generates one evidence-cited content model and renders six
> artifact types (study guide, cheat sheet, flashcards, quiz, mind map, script)
> deterministically from that single fact list, with provenance, checksums and
> tamper detection. The legacy `product.build_product` ten-artifact path is
> unchanged and still available; the remaining gaps below are its gaps.

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| 15 output types from one snapshot | 10 generated artifacts + flashcards + 3 PDFs from one frozen source snapshot | `product.py` | Study notes/guide/cheat/workbook/MCQ/viva/case/Anki/mindmap/scripts/carousel/captions/description/refs ✔; **mechanism diagram MISSING** | product tests partial | Medium | **PARTIAL** |
| Consistent derived outputs | single source_text + checksums per artifact | `product.py` | Verified resume/consistency mechanism | partial | Low | **KEEP** |
| PDF quality (headings/tables/callouts/pages/pearls) | reportlab headings/bullets/footer; no tables/diagrams/callouts | `export.py` | Plain but clean draft PDFs | `test_export.py` | Medium | **PARTIAL** |
| OCR for scanned PDFs | none | — | Reported as limitation in-app | — | Medium | **MISSING** |
| Video pipeline | scripts only (short/3-min); no MP4 rendering | `product.py` | Honest script outputs | — | High | **PARTIAL → MISSING renderer** |

## P13 Model router / P14 Resource manager

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Provider interfaces | implicit: `models.chat/embed` direct Ollama calls behind 4 functions | `models.py` | Loopback-only guard; no provider abstraction | partial | Medium | **PARTIAL** |
| Model metadata on artifacts | `state["chat_model"]` saved per product | `product.py` | Per-pack, not per-artifact | — | Low | **PARTIAL** |
| 8 GB discipline | one-job lock (`job_lock`/`serialized`), model unload after embed phase, 3.6 GB model cap, disk-quota check (500 MB), keep_alive tuning | `utils.py`, `models.py`, `product.py` | Verified patterns; no memory *measurement* harness | partial | Low | **KEEP; measurement MISSING** |

## P15 Private vs distributable output

| Requirement | Current | Source file | Actual behavior | Tests | Risk | Verdict |
|---|---|---|---|---|---|---|
| Private review bundle | full pack zip incl. source-text.txt / evidence reports | `dashboard.py::show_pack` | Private-safe by default | — | Low | **PARTIAL** |
| Distributable bundle (no copyrighted excerpts) | none — no redaction distinction | — | — | — | High | **MISSING** |

## P16 Testing

| Requirement | Current | Files | Verdict |
|---|---|---|---|
| Unit tests | 46 passing across learner/SR, DB, export, generation, ingestion, retrieval, models | `tests/` | **KEEP**, extend |
| Integration (live Ollama) | marker defined (`integration`), unused | `conftest.py` | **PARTIAL** |
| Recovery/migration tests | backup/integrity/data-loss/rollback covered | `test_phase1_db.py` | **KEEP** |
| Claim-verification / tutor / video / OCR tests | N/A — features missing | — | **MISSING with features** |

## P17 Security

| Requirement | Current | Source file | Actual behavior | Verdict |
|---|---|---|---|---|
| SSRF / private-network fetch | HTTPS-only, allowlist, resolved-IP check, 443-only, size+time caps | `ingestion.py` | Solid | **KEEP** |
| Prompt injection | system prompt declares source data untrusted; no instruction-execution path from source text | `types.py`, `generation.py` | Reasonable for local single-user; no injection-strip filter | **PARTIAL** |
| Shell safety | subprocess list-args everywhere, no shell=True; `subprocess.run(["open", path])` only | `utils.py`, `models.py`, `product.py` | Safe | **KEEP** |
| Path traversal | PDF upload name-sanitized (`Path(name.replace("\\","/")).name`), content-type check `%PDF-` | `dashboard.py` | Good | **KEEP** |
| Secrets in logs | no API keys used at all (local-first); Ollama loopback only | `models.py` | No cloud = small surface | **KEEP** |
| Accidental cloud upload | none possible in current code (no cloud calls) | — | — | **KEEP** |

## P18 Documentation reality check

| Item | Status |
|---|---|
| `docs/V3_SCHEMA.md` | Matches schema.py incl. review_log; will need node-type update after P2 |
| `docs/CLI_USAGE.md` | Matches CLI commands incl. import-cards/due/review; verified against `medforge_core.py` |
| `docs/ARCHITECTURE.md` | Describes pipeline accurately; curriculum described as "schema ready" → now misleading (dead tables) |
| Release README (`releases/…/README.md`) | References `MEDFORGE-ONE-COMMAND.command` which is not in repo (installer artifact git-ignored) — **stale for GitHub context** |
| "OCR is not yet included" (dashboard) | Accurate |

---

## Headline audit conclusions

0. **P2 + P3 now implemented** — curriculum engine (V4) and textbook provenance (V5) are live with 74/74 tests green; the audit verdicts below are the original Phase 0 findings, kept for history.
1. **The 2.1 core loop is real and tested** — ingestion → hybrid retrieval → cited generation → exports → SR review, with resumability, backups, and genuine security hardening. **Preserve it (P1 satisfied).**
2. **The biggest documented-but-not-implemented gap is the curriculum layer**: V3 schema tables exist, are empty, and are referenced by zero lines of application code. All P2 acceptance criteria are unmet today.
3. **Citation labels ≠ verification (P4)**: resolved — V6 stores first-class claims, evidence and verification history, and the model-assisted verifier produces explicit SUPPORTED/PARTIALLY_SUPPORTED/UNSUPPORTED/CONTRADICTED/INSUFFICIENT_EVIDENCE results with deterministic aggregation. The label scan remains as an honest compatibility floor.
4. **Tutor is not interactive (P7)**; P6 now records question-level performance events (`item_type`, `correct`, `confidence`, response time) but the full assessment framework is still P8, and P7 must not start until the P6 API is stable (it now is).
5. Dead `medical_sources` table means P5's authority model is unwired.
6. **Mastery is now a recency-weighted estimate with explicit uncertainty, calibration and recomputability (P6, `p6-rwm-v1`)** — the older "running mean" description in this matrix is the P6 starting point, kept for history; the legacy mean still exists and is still written.

## Change record

| Change | Phase | Status |
|---|---|---|
| Curriculum engine (schema extension + engine + CLI + dashboard + tests) | P2 | **DONE** — see `docs/CURRICULUM.md`; 15 new tests, 61/61 passing; V4 migration applied to the live DB with a verified pre-migration backup (SR queue 119 and 47 chunks preserved) |
| Textbook provenance engine (documents/editions, chapter/section/page/chunk provenance, curriculum links, V5 migration, CLI + dashboard, tests) | P3 | **DONE** — see `docs/P3_MATRIX.md` execution results and `docs/TEXTBOOKS.md`; 13 new tests, 74/74 passing; V5 applied to the live DB with verified backup `backups/medforge_pre_v5_backup.db` (all row counts unchanged) |
| Evidence graph (claims/evidence/relationships/verification history, model-assisted verifier with abstention, contradiction representation, curriculum traceability, CLI + dashboard EVIDENCE tab, V6 migration, tests) | P4 | **DONE** — see `docs/P4_MATRIX.md` execution results and `docs/EVIDENCE.md`; 20 new tests, 94/94 passing; V6 applied to the live DB with verified backup `backups/medforge_pre_v6_backup.db` (pre-existing row counts unchanged; live model-assisted verification run recorded) |
| Recency-weighted learner model (append-only `learning_attempts`, materialized `learner_model_state`, extended weakness columns, confidence calibration, prerequisite risk API, deterministic study priority, recalculation + versioning, CLI + dashboard LEARNER tab, V7 migration, tests) | P6 | **DONE** — see `docs/P6_MATRIX.md` execution results and `docs/LEARNER_MODEL.md`; 32 new tests, 126/126 passing; V7 applied to the live DB with verified backup `backups/medforge_pre_v7_backup.db` (all pre-existing row counts unchanged, integrity + foreign-key checks clean). Two CLI defects found during end-to-end validation and fixed. |
| Interactive adaptive tutor (persistent stage machine, P6-driven target selection, evidence-first teaching with verification policy, 7 modes, deterministic + model-assisted grading, adaptation, misconceptions, learner events, session summary, spaced-repetition sync, prompt-injection containment, dashboard TUTOR tab, CLI, V8 migration, tests) | P7 | **DONE** — see `docs/P7_MATRIX.md` execution results and `docs/TUTOR.md`; 37 new tests, 163/163 passing; V8 applied to the live DB with verified backup `backups/medforge_pre_v8_backup.db` (all pre-existing row counts unchanged, integrity + foreign-key checks clean); live end-to-end session, abstention, recovery and dashboard runs recorded. Four real defects found during live validation and fixed (bare-constant crash in model-assisted teaching, `model_calls` undercount, cross-topic rubric contamination, duplicated evidence records). |
| Study intelligence + product factory integration (orchestrator over curriculum/textbook/evidence/learner/tutor/assessment/SR, persistent plans + missions, canonical content + deterministic rendering, V10 migration, CLI + dashboard, tests) | P9 | **DONE** — see `docs/P9_MATRIX.md` execution results, `docs/STUDY_INTELLIGENCE.md` and `docs/PRODUCT_FACTORY.md`; 51 new tests, 261/261 passing; V10 applied to the live DB with verified backup `backups/medforge_pre_v10_backup.db` (40 → 45 tables, 438 → 439 rows, integrity + foreign-key checks clean); isolated end-to-end study loop, learner-adaptation, product-consistency, versioning, recovery, CLI and dashboard runs recorded. Eight real defects found by running the E2E and fixed (nested curriculum node resolution, tutor objective column, tutor goal vocabulary, ungrounded assessment items, empty 10-minute day, model-mode killed by a refs-only citation validator, adaptive re-render filename collision, and the pre-existing `status` command shadowing). |
| Publication / approval workflow (who may publish what, bundles, approval gates) | P10 | **DONE** — see `docs/P10_MATRIX.md` execution results and `docs/PUBLICATION.md`; 20 new tests, 286/286 passing; V11 applied to the live DB with verified backup `backups/medforge_pre_v11_backup.db` (45 → 48 tables, integrity + foreign-key checks clean, idempotent rerun verified); isolated E2E covering generate → render → nine gates → approve → publish both modes with private/distributable separation, plus CLI wiring (`publication` command) exercised end-to-end. |
| Professional medical video production (storyboard, frames, TTS, ffmpeg, ffprobe QA, captions, V12 migration) | P11 | **DONE** — see `docs/P11_MATRIX.md` execution results and `docs/VIDEO.md`; 14 new tests, 300/300 passing; V12 applied to the live DB with verified backup `backups/medforge_pre_v12_backup.db` (48 → 49 tables, integrity + foreign-key checks clean, idempotent rerun verified); isolated E2E on `/tmp/mf-p10-demo` covering three aspects (16:9/9:16/1:1) with real `say` TTS, ffprobe-validated MP4s, evidence recorded at `/tmp/mf-p10-demo/e2e_p11_result.json`; CLI wiring (`video` command) exercised end-to-end. |
| Provider abstraction + model router (protocol, Ollama/Mock providers, task-aware router, V13 migration, backward compat) | P12 | **DONE** — see `docs/P12_MATRIX.md` execution results; 27 new tests, 326 passed + 1 skipped; V13 applied to the live DB with verified backup `backups/medforge_pre_v13_backup_20261008T194212.db` (49 → 50 tables, integrity + foreign-key checks clean, idempotent rerun verified); `medforge/models.py` rewritten as thin wrappers delegating to provider/router; CLI `provider` command added. |
| Knowledge refresh + source update system (textbook hash detection + new editions, PubMed/web checks, P4 claim re-verification, notifications, V14 migration) | P13 | **DONE** — see `docs/P13_MATRIX.md` execution results and `docs/KNOWLEDGE_REFRESH.md`; 18 new tests, 344 passed + 1 skipped; V14 applied to the live DB with verified backup `backups/medforge_pre_v14_backup_20261008T202940.db` (49 → 52 tables, integrity + foreign-key checks clean, idempotent rerun verified); isolated E2E on `/tmp/mf-p13-demo` (evidence `/tmp/mf-p13-demo/e2e_p13_result.json`) covering register → check(ok) → file change → new edition + chunk diff + notification → ack → reverify; CLI `refresh` command exercised end-to-end. Three real defects found by tests and fixed (duplicate refresh ids from second-hash uids, metadata-loss splitting a document on re-registration, and same-second tie-breaks in latest-edition/last-check selection). |
| Automation + nightly workflow (maintenance, P13 refresh, verified backup with retention, P9 study prep, launchd scheduling, V15 migration) | P14 | **DONE** — see `docs/P14_MATRIX.md` execution results and `docs/AUTOMATION.md`; 15 new tests, 359 passed + 1 skipped; V15 applied to the live DB with verified backup `backups/medforge_pre_v15_backup_20261008T204030.db` (52 → 54 tables, integrity + foreign-key checks clean, idempotent rerun verified); CLI E2E on `/tmp/mf-p13-demo` covering `nightly` (4/4 steps ok, verified snapshot) and `schedule install|status|uninstall` with a `plutil -lint`-valid plist. One real defect found and fixed: `split(';')` DDL execution broke on a semicolon inside a SQL comment (both `migrate_v14` and `migrate_v15` now use `executescript`). |
| Reliability + security + performance hardening (diagnostics/verify/restore/repair, credentials-in-URL rejection, real gated OpenAI provider, measured performance guards) | P15 | **DONE** — see `docs/P15_MATRIX.md` execution results, `docs/OPERATIONS.md` and `docs/SECURITY.md`; 34 new tests (11 doctor, 19 security, 4 perf), 393 passed + 1 skipped; **no migration** (behaviour-only hardening, schema stays at `15.0.0`); CLI E2E on `/tmp/mf-p13-demo` covering `diagnose` healthy report, `verify-backup`, `restore` to an explicit target with re-verification, and `repair` (FTS rows re-derived). Five real defects found by tests/E2E and fixed: credentials accepted in URLs despite the documented rejection, plain-FTS5 "rebuild" silently emptying the search index, lexicographic version ordering reporting `9.0.0` as latest, false-unhealthy baseline on fresh chained homes, and the OpenAI opt-in hook pointing at a non-existent module (a fake-cloud path) now replaced by a real gated client with refusal tests. |
| Source hierarchy (P5), card links + leech rewrite, PDF/OCR upgrade, distributable bundle, evaluation, packaging, final readiness | P5, P16–P18 | **NOT STARTED — planned in dependency order; each with its own WHY/WHAT/RISK/MIGRATION/TEST/ROLLBACK record at implementation time.** |
