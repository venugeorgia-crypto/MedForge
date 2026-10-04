# MedForge Evidence Graph (P4, schema V6)

**Purpose:** turn generated text that *contains* `[S#]` labels into explicit
claims with inspectable, verifiable evidence relationships:

```
Curriculum Topic → Claim → Verification status → Evidence excerpt
                 → Source → Edition → Chapter → Section → Page
```

P3 provenance (document → edition → chapter → section → page → chunk) is
consumed, never redesigned. `[S#]` labels stay for backward compatibility and
resolve to real evidence records (`pack_label_map()` maps `S1 → evidence_id`).

## Data model (migration 6.0.0, purely additive)

| Table | What it stores | Key rules |
|---|---|---|
| `claims` | one row per distinct extracted statement: `claim_id` (sha1 of normalized text), `claim_text` (original wording), `normalized_text` (UNIQUE), `claim_type`, `topic`, `curriculum_node_id`, `source_labels`, `source_file`, `generation_run`, `verification_status`, `verification_confidence`, `review_status` | content-addressed; same text across runs = one claim; near-duplicates are **not** merged |
| `evidence` | `evidence_id` = sha1(chunk_id \| excerpt_hash), `source_id`, `chunk_id`, `evidence_type`, `document_id`, `edition_id`, `textbook_node_id`, `chapter_title`, `section_title`, `page_number`, `locator`, `url`, `quality`, bounded `excerpt` + `excerpt_hash` | provenance is joined from `textbook_chunks`/`textbook_nodes`; no invented or approximate locators; idempotent inserts |
| `claim_evidence` | one deduplicated edge per (claim, evidence): `relationship` (`supports`, `partially_supports`, `contradicts`, `insufficient`, `related`), `support_confidence`, `verification_method`, `notes` | `UNIQUE(claim_id, evidence_id)` prevents accidental duplicates |
| `verification_runs` | append-only history: verification id, claim, evidence, method, result, confidence, notes, verifier, verifier version, timestamp | attempts are never overwritten; a later run never erases an earlier verdict |

Applied by `core/database/migrate_v6.py::ensure_evidence_v6()` (idempotent,
self-healing, runs V5 → V4 first, optional verified backup at
`backups/medforge_pre_v6_backup.db`). Rollback = drop the four new tables; no
existing table is altered.

## Claim extraction (deterministic, `medforge/evidence.py`)

Generated text becomes claims line by line. **Created:** explanatory prose,
bullet/numbered list items, simple table data rows. **Skipped (typed
`question`/`instruction`/`non_factual`, dropped unless explicitly requested):**
markdown headings, table header rows, code blocks, prompt/meta lines
(`Task:`, `Citation rules:`, …), citation-only fragments, questions, and
imperative instructions to the student. The original generated wording is
preserved in `claim_text`; nothing is rephrased.

Deterministic claim typing: `definition`, `causation`, `mechanism`,
`association`, `epidemiology`, `clinical`, `fact` — surface cues only, in
specific→general order. Non-factual claims are never sent to verification
(they get `NOT_FACTUAL` with a recorded gate run).

### Normalization (conservative by design)

`normalize_claim_text()` lowercases, collapses whitespace, strips markdown
emphasis/list markers/`[S#]` labels/trailing punctuation and thousands
separators, then `claim_id = sha1(normalized)[:24]`. It never removes content
words, so:

> "Growth hormone promotes linear growth." ≠
> "Growth hormone promotes linear growth mainly through IGF-1."

are two distinct claims with distinct ids. Duplicate detection is exact-match on
`normalized_text`; near-duplicates stay separate.

## Evidence linking + source hierarchy

`verify_claim()` takes candidate sources two ways: the product path reuses the
pack's already-retrieved sources (no new embedding, no textbook loading); the
standalone path runs bounded `hybrid_retrieve(claim_text, 8)`. Candidates are
stored via `store_evidence()` (idempotent) and ranked by `_rank_candidates()`:

1. textbooks linked to the claim's curriculum node (`curriculum_text_links`)
2. other textbook chunks (SOURCE_PRIORITY 0.95 via `quality`)
3. other authoritative sources by retrieval quality (pubmed 0.96, guideline
   0.92, course_pdf 0.90, web 0.84 — **ranking hints, not accuracy scores**)

Bounded work on an 8 GB M1: excerpt ≤ 900 chars, ≤ 3 candidates per claim,
≤ 25 claims per run (the product hook uses ≤ 12 claims × 2 candidates), one
model call per (claim, evidence) pair, none at all when no candidate exists.

## Model-assisted verification (the core of P4)

Per (claim, evidence) pair the verifier receives **delimited data only**:

```
<<<CLAIM>>> … <<<END CLAIM>>>
<<<EVIDENCE_EXCERPT>>> … <<<END EVIDENCE_EXCERPT>>>
<<<SOURCE_METADATA>>>{json}<<<END SOURCE_METADATA>>>
```

The system prompt states the excerpt is untrusted reference data, never
instructions, and embedded `<<<`/`>>>` tokens are neutralized. The model returns
strict JSON: `relationship` (SUPPORTED / PARTIALLY_SUPPORTED / UNSUPPORTED /
CONTRADICTED / INSUFFICIENT_EVIDENCE), `confidence`, `reason`,
`context_mismatch`. Anything malformed, empty, or mislabeled — and any model
error or missing model — degrades to `INSUFFICIENT_EVIDENCE`. It never upgrades.

**No self-verification:** evidence always comes from retrieved source chunks;
the generated answer is never evidence for itself. **No keyword-overlap
verdicts:** only the verifier's explicit claim/evidence comparison produces a
relationship.

### Deterministic aggregation (no silent upgrades)

| Observed pair results | Claim status |
|---|---|
| any CONTRADICTED | `CONTRADICTED` (disagreement represented; no auto-winner) |
| ≥1 SUPPORTED, remainder SUPPORTED/PARTIALLY_SUPPORTED | `SUPPORTED` (confidence = min of SUPPORTED runs) |
| SUPPORTED mixed with UNSUPPORTED/INSUFFICIENT | `PARTIALLY_SUPPORTED` |
| any PARTIALLY_SUPPORTED (no support) | `PARTIALLY_SUPPORTED` |
| any UNSUPPORTED (no support) | `UNSUPPORTED` |
| none usable | `INSUFFICIENT_EVIDENCE` |
| no candidate evidence at all | `INSUFFICIENT_EVIDENCE`, **zero model calls** |

`UNSUPPORTED`/`CONTRADICTED`/`INSUFFICIENT_EVIDENCE` set
`review_status='needs_review'`; human review is never downgraded by later runs.
Every pair attempt is appended to `verification_runs`, so history shows exactly
how a status was reached (e.g. PARTIALLY_SUPPORTED after one SUPPORTED and one
UNSUPPORTED candidate).

## Integration

- `product.py::build_product` runs citation audit first (compatibility floor),
  then a **guarded** evidence-graph pass (`verify_product_claims`, ≤12 claims ×
  2 candidates) that can never block pack creation and writes an excerpt-free
  `evidence-graph.json` summary into the pack.
- CLI: `claims [STATUS]`, `claim-info <claim_id>`, `verify-pack [pack_dir]`,
  `evidence-status` (see `docs/CLI_USAGE.md`).
- Dashboard: **EVIDENCE** tab — metrics, status filter, expandable
  claim → relationship → evidence id → source → private excerpt → verification
  history drill-down.
- Curriculum: `store_claims(..., curriculum_node_id=…)` and
  `curriculum_node_id_for_topic()` link claims to P2 nodes when the topic
  exists; P2 behavior is unchanged.

## What P4 verifies — and what it does NOT verify

**Verifies:** whether a retrieved source excerpt supports, partially supports,
contradicts, or fails to support a specific extracted claim; keeps the exact
locator and every verification attempt; abstains when no evidence was
retrieved.

**Does NOT verify:** medical truth, currency of guidelines, correctness of the
source itself, statistical calibration of a "SUPPORTED" verdict, or
population/context match beyond what the verifier reports
(`context_mismatch` is recorded, not adjudicated). Model-assisted means
fallible; deterministic representation makes results inspectable, not
infallible. Contradiction is representable — resolving which side is right is
deliberately deferred to a later phase.

## Known failure modes

- The 4B-class local verifier can under- or over-call support; that is why
  mixed evidence aggregates *downward* (SUPPORTED + UNSUPPORTED →
  PARTIALLY_SUPPORTED) and every run is historical.
- Extraction can miss a tersely worded claim (min ~4 words / 12 letters) or
  keep an awkward table row as a claim; both are visible and reviewable in the
  EVIDENCE tab.
- Claims are deduplicated by exact normalized text only; medically identical
  paraphrases remain separate claims (deliberate, to avoid over-merging).
- No candidate is retrieved → abstention, so a claim is not "unsupported", it
  is "not checked yet" (`INSUFFICIENT_EVIDENCE`).

## Copyright boundaries

Evidence excerpts are internal verification data stored in the private SQLite
database (and shown in the local-only UI). They are **not** written into
distributable artifacts: `evidence-graph.json` contains statuses, confidences
and evidence ids — no excerpt text. Having a citation is not a redistribution
right; pack outputs keep only the bounded `[S#]` evidence blocks that the
existing pipeline already exposed.
