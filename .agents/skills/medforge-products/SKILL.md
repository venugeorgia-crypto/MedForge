---
name: medforge-products
description: Guard the P9 canonical content model and product rendering — one evidence-cited generation per (topic, sources, config), deterministic rendering, caching, versioning, checksums and provenance.
license: MIT
metadata:
  category: development
---

# MedForge Products — canonical content and rendering

`medforge/content.py` turns one evidence-grounded canonical content model into
many artifacts. `medforge/product.py` is the **legacy** ten-artifact pipeline —
kept for compatibility, not extended.

## The model

One row in `content_items` per `(topic, sources_digest, config_digest,
prompt_version, model)`:

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

## Invariants to preserve

1. **At most one model call per (topic, sources, config).** Cache key is
   `sha1(slug | sources_digest | config_digest | PROMPT_VERSION | model)`.
   The model name is in the key so switching models cannot silently reuse
   another model's output.
2. **Never trust uncited model output.** `_sanitize` / `_element_refs` accept a
   `"refs"` array *or* inline `[S#]` in prose, materialise the labels onto
   `refs`, and drop any element without a valid citation. An empty result falls
   back to `canonical_fallback_from_evidence` (verbatim sentences, cannot invent).
3. **Rendering is deterministic and model-free.** Same canonical content +
   same profile ⇒ byte-identical artifact.
4. **Learner adaptation happens at render time only.** The canonical item is
   learner-independent; `adaptation={"profile": …}` changes emphasis, never
   facts, and never fragments the cache.
5. **One file per (artifact type, adaptation profile).** `<type>.md` for
   `developing`, `<type>.<profile>.md` otherwise — profiles must not overwrite
   each other.
6. **Versions accumulate.** Each render bumps `artifact_version`; superseded
   rows stay in `content_artifacts` with their checksums.
7. **Changed sources/config ⇒ new `content_id`.** History is append-only;
   regeneration never rewrites a canonical row.
8. **`SUMMARY_ARTIFACT_TYPES`** (`cheat_sheet`, `mind_map`, `script`) are
   curated subsets by design, so the completeness gate only demands full
   coverage from `study_guide`, `flashcards`, `quiz`; `coverage_scope` records
   which case applied.
9. **Checksums are the integrity signal.** `content_consistency_report`
   re-reads files from disk and compares sha256. Tampering must flip it to
   `false`.
10. **Provenance chain intact**: artifact → content item → `evidence_refs` →
    P3/P4 source, with `sources_digest`, `prompt_version`, `model`.
11. **`_cite` is idempotent** — it must not append a label the prose already
    carries (no `[S1] [S1]`).

## Privacy

Learner data never enters canonical content or artifacts. Artifacts carry
citations and locators, not long excerpts. Nothing is published by this module;
publication and approval belong to a later phase.

## CLI

```
product-intel build <topic>[|types|outdir]
product-intel status <content_id>        # READY / NEEDS_REVIEW / BLOCKED
product-intel inspect <content_id>
product-intel artifacts [content_id]
product-intel provenance <artifact_id>
product-intel consistency <content_id>
product-intel regenerate <content_id>[|outdir]
```

Files land in `PRODUCTS/<topic-slug>/p9/`.

## Load order

After `medforge-core` and `medforge-phase`, with `medforge-evidence` and
`medforge-medical-qa` (the gates it depends on). Required for any change to
`content.py`, the canonical schema, artifact types/statuses, or export formats.
