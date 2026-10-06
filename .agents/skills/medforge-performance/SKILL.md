---
name: medforge-performance
description: Resource discipline for MedForge on an 8 GB M1 MacBook Air — model memory, one-job locking, bounded retrieval, retrieval/query cost, disk quota and the measurement habits to use before claiming a speedup.
license: MIT
metadata:
  category: development
---

# MedForge Performance — resource discipline

Target hardware: M1 MacBook Air, **8 GB RAM**, ~256 GB storage, local Ollama.
Everything is local, so the app competes with the OS, the browser and the model
for the same memory.

## Hard budgets

| Resource | Budget | Where enforced |
| --- | --- | --- |
| Concurrent jobs | one at a time | `job_lock()` / `utils.serialized` |
| Model memory | single chat model, unloaded after the embed phase; 3.6 GB model cap | `models.py` |
| Model context | bounded source pack (≤ ~8k chars, ≤ ~1.3k per chunk) | `retrieval.source_pack` |
| Retrieval fan-out | top-k bounded (≤ 6 in the canonical path) | `retrieval.py` |
| Disk | 500 MB free-space check before heavy work | `product.py` / installer |
| Temp | demo homes under `/tmp`, cleaned deliberately | phase loop |

## Rules

1. **Never load a second model to save latency.** Reuse `ensure_models()` and
   the configured `PREFERRED_CHAT` / fallback chain.
2. **Bounded retrieval, always.** Any new query must cap results and truncate
   per-item text; a "fetch everything" path will OOM on a large textbook.
3. **No full-textbook loads.** Stream chunks; the P3 chunk table exists so you
   never need the whole PDF in memory.
4. **Keep DB access narrow.** Indexes exist for the hot paths
   (`idx_study_*`, `idx_content_*`, `idx_assess_*`); a new query that scans a
   table per topic is a defect, not a shortcut.
5. **Do not call the model in a loop.** The P9 design exists precisely because
   ten calls per topic was the old default; if you need N artifacts, render them
   deterministically from one generation.
6. **Guard interactive paths.** Dashboard actions must not kick off unbounded
   work synchronously; use the job lock and surface progress.
7. **Preserve the keep-alive / unload tuning** in `models.py`.

## Measure before claiming

- **Correctness first**: a faster wrong answer is a regression.
- Time representative inputs with `time.perf_counter()` around the real public
  call, on a realistic home (not an empty DB), and report the numbers.
- Report memory only if actually measured (e.g. `resource.getrusage` delta);
  say "not measured" otherwise.
- Never claim a speedup without a before/after measurement on the same machine
  and the same inputs.

## Known cost facts

- Canonical generation is one model call (seconds, model-dependent); rendering
  six artifacts from it is pure Python (single-digit ms).
- Planning (`build_study_plan`) is SQL + Python over a small curriculum — no
  model, no full-text.
- Embeddings are the expensive phase of the legacy product pipeline; that is
  where the unload-after-embed logic matters.

## Load order

After `medforge-core`. Required when touching `models.py`, `retrieval.py`,
`ingestion.py`, anything with a loop over topics/artifacts, or when a task
mentions speed, memory or disk.
