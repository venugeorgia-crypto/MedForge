# P11_MATRIX.md — Professional Medical Video Production

Status: **IMPLEMENTED + TESTED** (`p11-video-v1`, migration `12.0.0`)

## 1. Audit (entry state)

- No video subsystem exists. Closest assets: `content.py` renders a text
  `script` artifact (scene narration source) and legacy `product.py` writes
  text captions/carousel. No `.mp4` rendering anywhere.
- Capabilities probed: `ffmpeg` + `ffprobe` (Homebrew), `say` (/usr/bin/say,
  local TTS), Pillow, genanki, reportlab present. No matplotlib, no srt module
  (captions must be hand-written).
- Ownership: video reads canonical content through `CT.get_content()` only;
  it must not duplicate factory or evidence logic. Publication (P10) owns
  lifecycle gates for `content_artifacts`; video renders are recorded in their
  own table.

## 2. Design

**Scene model** (deterministic, derived from canonical content):

| Field | Meaning |
| --- | --- |
| `scene_id` | `<content_id>-sNN` stable identity |
| `index` | 0-based order |
| `title` | slide heading |
| `narration` | spoken text (citation labels stripped for TTS) |
| `on_screen` | visible bullets (citation labels KEPT — provenance survives) |
| `visual` | slide type: title/objectives/definition/mechanism/relationships/key_facts/clinical/misconceptions/high_yield/references |
| `claim_refs` | P4 claim IDs whose text cites labels used in this scene |
| `evidence_refs` | labels cited in this scene |
| `asset_refs` | external assets (empty by default) |
| `transition` | `cut` |
| `duration_s` | narration duration (from TTS wav) |

**Storyboard**: title → objectives → definitions → mechanisms → key facts
(groups of 4) → relationships → clinical correlations (groups of 3) →
misconceptions (groups of 3) → high-yield → references. Reproducible:
same canonical item ⇒ same scenes; checksum recorded.

**Pipeline** (`medforge/video.py`):

1. `build_storyboard(content_id)` → scenes + checksum.
2. `render_frames(storyboard, outdir, aspect)` — Pillow slides at
   1920×1080 (16:9), 1080×1920 (9:16), 1080×1080 (1:1).
3. `synthesize_narration(scene, workdir)` — pluggable `TTS_FN` seam; default
   local macOS `say` → AIFF → ffmpeg → 44.1 kHz mono WAV. Tests patch the seam.
4. Captions — narration split into ≤90-char segments, timing proportional
   within each scene; hand-written `.srt` + `.vtt` (monotonic, non-overlapping).
5. `assemble` — per-scene `ffmpeg -loop 1` still + audio → H.264/AAC segment,
   concat demuxer → `-movflags +faststart` MP4.
6. `render_video(content_id, aspects)` — orchestrates, then QA via ffprobe:
   container mp4, video codec h264, exact width/height, audio codec aac,
   duration ≈ Σ scene durations (±0.5 s), nb_frames > 0, and a full
   `ffmpeg -f null` decode pass with empty stderr. A render is only marked
   READY if every probe passes; failures are recorded, never hidden.

**Persistence (V12)**: `video_renders` table (video_id PK =
`vid-<cid12>-<aspect>`, content_id FK CASCADE, aspect, status
queued/rendering/READY/FAILED, path, manifest_path, duration_s, width, height,
size_bytes, scene_count, renderer_version, storyboard_checksum, error,
created_at, updated_at). Additive and idempotent, chains the full migration
path (v9 → v10 → v11 → v12), verified backup `backups/medforge_pre_v12_backup.db`.

**Output dirs** (deterministic): `products/<slug>/video/<aspect>/` containing
`storyboard.json`, `frames/`, `segments/`, `captions.srt`, `captions.vtt`,
`video.mp4`, `manifest.json` (checksums + probe results).

## 3. Acceptance criteria (tests must prove)

1. Unknown content_id → ValueError (honest failure, no fake video).
2. Storyboard deterministic; scene ids stable; on_screen keeps `[Sx]` labels.
3. Frames exist on disk with exact per-aspect pixel dimensions.
4. Captions: SRT/VTT monotonic, cover all scenes, parse cleanly.
5. Full render produces a REAL MP4 validated by ffprobe (container/codec/dims/
   audio/duration/frames) + clean null-decode — no placeholder presented as video.
6. Every render recorded in `video_renders` with status and probe results.
7. Whole P11 suite runs hermetic (patched TTS seam, no Ollama).

## 4. Execution results

- **Migration V12**: Fresh run OK (chained V9→V10→V11→V12), 3 tables created
  (`video_renders`), `schema_migrations` row `12.0.0` recorded, idempotent
  rerun OK, backup written to `backups/medforge_pre_v12_backup.db`
  (integrity_check=ok, foreign_key_check=empty).
- **Unit tests**: 14/14 passing (~60s on M1 Air, all hermetic with patched TTS).
- **Full regression**: 300/300 passing (286 P2–P10 + 14 P11).
- **E2E** on isolated demo home (`/tmp/mf-p10-demo`):
  - content_id: `664acfaadea7032a` (growth hormone physiology)
  - storyboard: 10 scenes, checksum `2cc24a1cacaed1d2dd62c1118b1b0f2ab887185c`
  - 3 renders: 16:9 (1920×1080), 9:16 (1080×1920), 1:1 (1080×1080)
  - All `READY`, QA passed, ffprobe: h264/aac, clean decode, ~40.2s duration
  - File sizes: 864–977 KB each
  - Captions SRT/VTT valid (monotonic cues = scene count)
  - Evidence recorded at `/tmp/mf-p10-demo/e2e_p11_result.json`
- **CLI**: `medforge_core video render|state|probe` wired and tested.
- **Docs**: `docs/VIDEO.md` created.
- **P11 gate**: **PASS** — real MP4 rendering verified, no feature theater.
