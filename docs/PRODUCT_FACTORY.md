# Product Factory — Canonical Content & Deterministic Rendering (P9, V10)

`medforge.content` turns one evidence-grounded **canonical content model** into
many products. The point is to stop several independent LLM calls from producing
contradictory artifacts: every product is rendered from the same fact list.

Versions: `CONTENT_VERSION = "p9-content-v1"`, `PROMPT_VERSION = "canonical-v1"`.

```
EVIDENCE (P3 chunks / P4 graph)
        |
        v
  generate_canonical_content()        <- ONE model call (or deterministic fallback)
        |
        v
   content_items  (canonical, content-addressed)
        |
        v
  render_study_products()             <- pure Python, deterministic
        |
        +--> study_guide  cheat_sheet  flashcards  quiz  mind_map  script
        |
        v
   content_artifacts (version, checksum, status, provenance)
```

## 1. Public interface

| Function | Purpose |
| --- | --- |
| `generate_canonical_content(topic, sources=None, source_text=None, config=None, model=None, allow_model=True, now=None)` | Produce (or fetch from cache) the canonical content for one topic. |
| `get_content(content_id)` / `list_content(topic=None, limit=20)` | Read canonical items. |
| `render_study_products(content_id, artifact_types=None, adaptation=None, outdir=None, now=None)` | Render artifacts to disk + DB. |
| `regenerate_product(content_id, artifact_types=None, adaptation=None, outdir=None, now=None)` | The contract interface for "render again": re-renders from the **stored** canonical item (never re-generates facts) and reports `regenerated`, `generation_mode`, `prompt_version`, `content_version`. |
| `get_product_status(content_id)` | Per-artifact status + overall status. |
| `list_artifacts(content_id=None, limit=50)` | Artifact rows. |
| `artifact_provenance(artifact_id)` | artifact → content item → evidence → source. |
| `content_consistency_report(content_id)` | Re-verify every artifact's on-disk checksum. |
| `canonical_fallback_from_evidence(topic, source_text, sources)` | Deterministic evidence-sentence fallback. |

## 2. The canonical content model

One JSON object per topic:

```json
{
  "learning_objectives": ["..."],
  "key_facts":           [{"fact": "... [S1]", "refs": ["S1"]}],
  "mechanisms":          [{"name": "...", "steps": ["... [S1]"]}],
  "definitions":         [{"term": "...", "meaning": "...", "refs": ["S1"]}],
  "relationships":       [{"a": "...", "b": "...", "relation": "...", "refs": ["S1"]}],
  "clinical_correlations": [{"point": "...", "refs": ["S1"]}],
  "misconceptions":      [{"wrong": "...", "correction": "...", "refs": ["S1"]}],
  "high_yield":          ["... [S1]"]
}
```

Each `[S#]` label maps to a retrieved source block (P3 chunk with locator, or a
P4 evidence record). The labels, their provenance and the sources digest are
stored alongside the content, so an artifact can always be traced back to the
passage that supports it.

### Citation validation (`_sanitize`)

Model output is **never trusted uncited**. `_sanitize` walks every element and
drops any element that carries no citation resolving to a supplied `[S#]`
label:

- `learning_objectives` may be uncited framing, but a cited objective must be in
  range.
- `high_yield` entries are facts and must cite inline.
- `mechanisms` are validated by citations inside their `steps`.
- `key_facts`, `definitions`, `relationships`, `clinical_correlations` and
  `misconceptions` are validated per element by `_element_refs()`.

`_element_refs()` accepts **both** citation forms the model produces — an
explicit `"refs": ["S1"]` array and inline `[S1]` markers in the prose — and
materializes the resolved labels back onto `refs`, so rendering and provenance
never re-parse prose. A bogus label (e.g. `[S-1]`) or a missing citation drops
only that element.

If nothing survives, `_sanitize` raises and the caller falls back to
`canonical_fallback_from_evidence()`: deterministic sentences split out of the
evidence itself (≥ 40 chars, at most 10 facts). The fallback **cannot invent a
fact** — every sentence is a substring of the supplied source text.

Materializing both citation forms matters in practice: the local model cites
inline and omits the `refs` array, so a refs-only validator silently discarded
every real generation and replaced it with the fallback. That defect was found
and fixed during the P9 E2E run.

## 3. Content identity and caching

```
content_id = sha1( slug(topic) | sources_digest | config_digest | PROMPT_VERSION | model )[:16]
```

- `sources_digest` covers every source block (label, source, locator, url,
  kind) plus the assembled source text.
- `config_digest` covers the caller's config object.
- The model name is part of the key, so switching models produces new content
  instead of silently reusing another model's output.

Consequences:

- **Cache hit** → the stored row is returned with `cached: true` and **no model
  call**.
- **Changed sources or config** → a *new* `content_id` and a *new* row. The
  previous item is never mutated, so content history is append-only.
- The canonical layer is learner-independent: adaptation happens at render
  time, so a learner's profile never fragments the generation cache.

## 4. Rendering

`render_study_products()` is pure Python over the canonical item — no model
calls. Artifact types: `study_guide`, `cheat_sheet`, `flashcards`, `quiz`,
`mind_map`, `script`.

Every render produces the same structural consistency block:

```json
{"facts_total": 10, "facts_rendered": 10, "unknown_fact_ids": 0,
 "coverage_scope": "full", "contradiction_check": "structural (single fact source)"}
```

### Coverage semantics

`study_guide`, `flashcards` and `quiz` render **every** canonical fact, so a gap
in one of them is a real defect. `cheat_sheet`, `mind_map` and `script` are
curated subsets **by design** (`SUMMARY_ARTIFACT_TYPES`), so demanding full
coverage from them would mark good artifacts `NEEDS_REVIEW` and make the status
signal worthless. `missing` is therefore computed only when a full-coverage
type was requested, and `coverage_scope` records which case applied.

### Citations

`_cite()` appends only the labels a line does not already carry. Canonical facts
frequently arrive with inline `[S1]` markers, and appending the refs list
unconditionally produced `[S1] [S1]` in every artifact.

### Files and versioning

- Default output dir: `PRODUCTS/<topic-slug>/p9/`.
- One file per (artifact **type**, adaptation **profile**):
  `<type>.md` for the default `developing` profile, `<type>.<profile>.md`
  otherwise. Distinct profiles therefore never overwrite each other's bytes —
  without that, a later adaptive render made the consistency report look like
  tampering.
- Each render bumps `artifact_version` (`1, 2, 3 …`) and stores
  `artifact_id = art-<content_id[:8]>-<type>-<version>`, a sha256 `checksum` of
  the bytes, and a `validation` block.
- Artifacts are written atomically (`atomic_text`).

### Status

Per artifact: `READY` when every canonical fact was rendered, otherwise
`NEEDS_REVIEW`. V10 also defines `DRAFT`, `VALIDATING` and `BLOCKED`. Overall
status is `READY` only when every artifact is `READY`, `BLOCKED` if any is
`BLOCKED`, else `NEEDS_REVIEW`.

## 5. Provenance and consistency

`artifact_provenance(artifact_id)` returns the full chain:

```
artifact → content_item → evidence_refs → P3/P4 sources
```

with `prompt_version`, `model`, `sources_digest` and the labelled evidence list
(label, source, locator, url, kind, chunk id).

`content_consistency_report(content_id)` re-reads every artifact **from disk**
and recomputes its sha256 against the stored checksum. `consistent` is `true`
only when all match, which makes the report a genuine integrity check: editing
a rendered file by hand flips it to `false`, and restoring the file flips it
back.

## 6. Learner adaptation at render time

`render_study_products(..., adaptation={"profile": ...})` changes **emphasis
only**:

| Profile | Rendering effect |
| --- | --- |
| `weak` | Adds `## Suggested first step` (use the tutor in `explain` mode first). |
| *(all)* | Written to `<type>.<profile>.md`, so profiles never overwrite each other. |
| `developing` | Baseline sections. |
| `strong` | Adds `## Going deeper` (attempt cold, review only missed items). |
| `overconfident` | Includes the misconceptions section prominently. |

The canonical item is untouched: same `content_id`, same facts, same citations.
Verified: the same content rendered as `study_guide.weak.md` and
`study_guide.strong.md` produced different text and identical canonical bytes.

## 7. CLI

```
product-intel build <topic>[|types|outdir]   # one canonical generation + render
product-intel status <content_id>
product-intel inspect <content_id>
product-intel artifacts [content_id]
product-intel provenance <artifact_id>
product-intel consistency <content_id>
product-intel regenerate <content_id>[|outdir]
```

`build` refuses topics with no evidence with an honest object
(`{"created": false, "refused": ...}`) instead of crashing; `regenerate`
delegates to `regenerate_product()`, so regeneration consumes the stored
canonical item and cannot silently change an artifact's facts. The legacy
`product <topic>` command is untouched.

Products are not written to any remote location and are not published;
`build` only writes local files under `PRODUCTS/`.

## 8. Privacy

The canonical item contains bounded evidence excerpts; the artifacts contain
citations and locators. Learner data (mastery, history, transcripts) is **not**
embedded in the canonical content and is never sent anywhere by this module.
Rendered artifacts are the distributable surface; the canonical item, the
excerpts and any learner context stay local.

Note the licensing boundary: rendering redistributes textbook sentences as
facts; `product`-level publishing decisions (what may be shared, and with which
excerpts) belong to the later publication/approval phase, not to this module.

## 9. Limitations

- **One fact list, one truth per topic.** Consistency is structural: artifacts
  cannot contradict each other because they share facts, but a wrong fact in
  the evidence is reproduced faithfully in every artifact.
- **Facts are quoted sentences.** The deterministic fallback and model facts
  reuse source sentences (truncated at source-block granularity); this is
  attribution-preserving but not paraphrase-quality prose.
- **Local model variance.** Model output may be unsanitizable on a given run
  and fall back to deterministic sentences; the fallback is always cited and
  always safe, but the artifact then has less synthesis.
- **Renders are per-(type, profile), not per-version files.** Superseded
  versions share a path with their successor; the DB keeps the version history
  and checksums, the filesystem keeps the current bytes.
- **No export formats here.** PDF/Anki exports remain in `export.py` and are
  invoked by the legacy product pipeline; this module writes Markdown.
- **No publication workflow.** Approval, bundling and distribution are out of
  scope for P9.
