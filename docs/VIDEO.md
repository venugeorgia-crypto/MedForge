# P11 — Professional Medical Video Production

**Version:** `p11-video-v1` | **Migration:** `12.0.0` (adds `video_renders` table)

---

## Overview

P11 turns one canonical content item (P9) into a real, narrated, captioned MP4 per aspect ratio — **deterministically** and **offline-first**. No cloud rendering, no fake placeholders.

```
canonical content (P9)
      ↓
storyboard (scenes with evidence labels kept on-screen)
      ↓
Pillow frame rendering (1920×1080 / 1080×1920 / 1080×1080)
      ↓
local TTS narration (pluggable seam, default: macOS `say`)
      ↓
ffmpeg per-scene segments → concat → H.264/AAC MP4
      ↓
ffprobe QA (decode clean, duration, frames, audio, dims)
      ↓
hand-written SRT + VTT captions
      ↓
manifest.json (checksums, probe evidence)
      ↓
persisted to video_renders (status, path, duration, dims, checksum)
```

---

## Public API (`medforge.video`)

```python
from medforge import video as V

# Full pipeline: storyboard → frames → TTS → ffmpeg → QA → captions → persist
result = V.render_video(content_id, aspects=("16:9", "9:16", "1:1"))

# Query persisted state
state = V.get_video_state(content_id)

# ffprobe validation of any file
probe = V.probe_video(Path(".../video.mp4"))
```

### `render_video(content_id, aspects=None) → dict`

- `aspects`: iterable of `"16:9"`, `"9:16"`, `"1:1"` (default: all three)
- Returns: `{"video_id", "storyboard_checksum", "scene_count", "renders": [...]}`
- Each render: `{"video_id", "aspect", "status" ("READY"/"FAILED"), "path", "duration_s", "qa_passed", "error"}`

### `get_video_state(content_id) → dict`

- Returns: `{"content_id", "renders": [...], "video_version"}`
- Each render includes: `video_id`, `aspect`, `status`, `path`, `duration_s`, `width`, `height`, `size_bytes`, `checksum`, `srt_path`, `vtt_path`, `manifest_path`, `created_at`

### `probe_video(path) → dict`

- Runs `ffprobe -v error -show_format -show_streams -of json` + a dry decode pass (`-f null -`)
- Returns full ffprobe JSON with extra `decode_returncode` and `decode_stderr` keys
- Raises `FileNotFoundError` if `ffprobe` not on PATH

### `ensure_video_tables(db_path) → dict`

- Migration guard; called automatically by `render_video` on first use
- Runs the V12 migration (chains V9→V10→V11→V12), records `schema_migrations` row, verifies integrity + FKs

---

## Scene Model (Storyboard)

Each scene is a dict with:

| Field | Type | Description |
|-------|------|-------------|
| `scene_id` | str | Deterministic UID (`sc-<hash>`) |
| `order` | int | Playback order |
| `duration_s` | float | Seconds (computed from narration word-count @ 150 wpm, min 3s, max 15s) |
| `narration` | str | Spoken text (from canonical content sentences) |
| `on_screen` | str | Visual text overlay (definitions, mechanisms, key facts) |
| `visual_type` | str | `"title" | "definition" | "mechanism" | "fact" | "clinical" | "misconception" | "summary" | "reference"` |
| `claim_ids` | list[str] | P4 claim IDs covered in this scene |
| `evidence_ids` | list[str] | P3 textbook evidence IDs |
| `asset_refs` | list[str] | Future: image/diagram asset IDs (currently empty) |
| `transition` | str | `"cut" | "fade" | "slide"` (default `"cut"`) |
| `captions` | str | Same as narration (source for SRT/VTT) |

Storyboard is built from the canonical content sections:
1. Title scene
2. Objectives
3. Definitions
4. Mechanisms
5. High-yield facts
6. Clinical correlations
7. Misconceptions
8. Key points
9. References (with citation labels)
10. Closing scene

---

## Aspect Ratios

| Aspect | Resolution | Use case |
|--------|------------|----------|
| `16:9` | 1920×1080 | Desktop, YouTube, presentations |
| `9:16` | 1080×1920 | Mobile, Reels, Shorts, TikTok |
| `1:1` | 1080×1080 | Instagram posts, square feeds |

All renders share the same storyboard, narration audio, and captions — only the frame layout changes.

---

## TTS (Text-to-Speech)

**Default:** macOS `say` (local, offline, zero config).
```bash
say -v Samantha -o /tmp/x.aiff "text"
ffmpeg -i /tmp/x.aiff -c:a aac -b:a 128k /tmp/x.m4a
```

**Pluggable seam:** set `V._tts_seam = lambda text, out_path: ...` for testing or alternative engines (e.g., `piper`, `coqui`, `elevenlabs` via API). The seam must write an audio file that ffmpeg can decode.

---

## Captions

**SRT** and **VTT** are written by hand (no `pysrt`/`webvtt` dependency). One caption cue per scene, timed to the scene's `start`/`end` in the concatenated timeline.

- SRT: sequential index, `HH:MM:SS,mmm` timestamps, blank line between cues
- VTT: `WEBVTT` header, `HH:MM:SS.mmm` timestamps, blank line between cues

---

## QA Gate (ffprobe)

Every rendered MP4 is validated before `status = "READY"`:

1. **Container**: MP4/MOV family (`mov,mp4,m4a,3gp,3g2,mj2`)
2. **Video codec**: `h264` (baseline/main/high)
3. **Audio codec**: `aac`
4. **Dimensions**: match requested aspect
5. **Duration**: within ±0.5s of expected sum of scene durations
6. **Clean decode**: `ffmpeg -v error -i file -f null -` exits 0 with empty stderr

Failure → `status = "FAILED"`, error recorded, row persists for debugging.

---

## Database: `video_renders` (V12 Migration)

```sql
CREATE TABLE video_renders (
    video_id       TEXT PRIMARY KEY,          -- vid-<hash>
    content_id     TEXT NOT NULL REFERENCES content_items(content_id) ON DELETE CASCADE,
    aspect         TEXT NOT NULL CHECK (aspect IN ('16:9','9:16','1:1')),
    status         TEXT NOT NULL CHECK (status IN ('RENDERING','READY','FAILED')),
    path           TEXT,                      -- absolute path to MP4
    duration_s     REAL,                      -- seconds (from ffprobe)
    width          INTEGER,
    height         INTEGER,
    size_bytes     INTEGER,
    checksum       TEXT,                      -- SHA-256 of MP4
    srt_path       TEXT,
    vtt_path       TEXT,
    manifest_path  TEXT,                      -- JSON with probe + checksums
    storyboard_checksum TEXT,                -- SHA-256 of storyboard JSON
    scene_count    INTEGER,
    error          TEXT,                      -- non-null only when FAILED
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (content_id, aspect)
);

CREATE INDEX idx_video_renders_content ON video_renders(content_id);
CREATE INDEX idx_video_renders_status  ON video_renders(status);
```

---

## CLI (`medforge_core.py video`)

```bash
# Render all three aspects
medforge_core video render <content_id>

# Render specific aspects
medforge_core video render <content_id> 16:9,9:16

# Query persisted state
medforge_core video state <content_id>

# Probe any MP4 with ffprobe
medforge_core video probe /path/to/video.mp4
```

---

## File Layout

```
products/<slug>/video/
├── 16x9/
│   ├── video.mp4
│   ├── captions.srt
│   ├── captions.vtt
│   └── manifest.json
├── 9x16/
│   ├── video.mp4
│   ├── captions.srt
│   ├── captions.vtt
│   └── manifest.json
└── 1x1/
    ├── video.mp4
    ├── captions.srt
    ├── captions.vtt
    └── manifest.json
```

`manifest.json` contains:
```json
{
  "video_id": "vid-...",
  "content_id": "...",
  "aspect": "16:9",
  "storyboard_checksum": "...",
  "scene_count": 10,
  "checksum": "sha256...",
  "size_bytes": 891022,
  "duration_s": 40.205,
  "width": 1920,
  "height": 1080,
  "probe": { ...ffprobe output... },
  "created_at": "2025-01-15T14:30:00Z"
}
```

---

## Dependencies

| Tool | Version | Purpose |
|------|---------|---------|
| ffmpeg | 9.0.1 | encode, concat, probe |
| ffprobe | 9.0.1 | validate |
| Pillow | 12.3.0 | frame rendering |
| numpy | 2.5.3 | frame arrays |
| say (macOS) | built-in | TTS |

No Python TTS libraries required. `say` is the default; the seam allows swapping.

---

## Determinism

- Same canonical content → same storyboard checksum → same scene breakdown
- Same narration text → same TTS audio (deterministic `say` voice)
- Same frames + audio → same ffmpeg output (single-pass CRF 18, preset medium)
- Manifest checksums enable byte-for-byte reproducibility verification

---

## Acceptance Tests (`tests/test_video.py`)

| Test | Validates |
|------|-----------|
| `test_migration_fresh_and_idempotent` | V12 additive + idempotent |
| `test_storyboard_from_canonical` | Scene count, fields, claim/evidence mapping |
| `test_render_16x9` | 1920×1080 MP4, ffprobe h264/aac, clean decode |
| `test_render_9x16` | 1080×1920 MP4 |
| `test_render_1x1` | 1080×1080 MP4 |
| `test_all_three_aspects_single_call` | Single `render_video` produces all three |
| `test_persisted_state_matches_render` | DB rows match render results |
| `test_captions_srt_format` | Valid SRT timestamps, cues = scenes |
| `test_captions_vtt_format` | Valid VTT header + cues |
| `test_probe_video_known_good` | `probe_video` returns expected structure |
| `test_probe_video_missing_file` | Raises `FileNotFoundError` |
| `test_tts_seam_injection` | Custom TTS function is invoked |
| `test_qa_rejects_corrupt` | Corrupted MP4 → `FAILED` status |
| `test_re_render_idempotent` | Second render reuses existing (no double work) |

All 14 tests pass (~60s on M1 Air).

---

## E2E Evidence

Run on isolated demo home (`/tmp/mf-p10-demo`):

```
content_id: 664acfaadea7032a
storyboard_checksum: 2cc24a1cacaed1d2dd62c1118b1b0f2ab887185c
scene_count: 10
renders: 3 (16:9, 9:16, 1:1) — all READY, QA passed
probe: all three h264/aac, clean decode, correct dims, ~40s duration
file sizes: 864–977 KB each
```

Output recorded at `/tmp/mf-p10-demo/e2e_p11_result.json`.

---

## Limitations (Honest)

- **No diagrams/animations** — `asset_refs` reserved for future; current visual is text-on-solid only
- **Single voice** — `say` voice fixed to `Samantha`; seam allows alternatives but no multi-speaker
- **Scene timing heuristic** — word-count @ 150 wpm; not forced-aligned to actual audio
- **No background music/ambience** — pure narration
- **Caption styling** — plain text only; no positioning/color markup in SRT/VTT
- **macOS-only TTS default** — Linux/Windows need seam injection (e.g., `piper`)

---

## Next Phase: P12 Provider Abstraction + Model Router