"""P11 — Professional Medical Video Production (p11-video-v1, migration 12.0.0).

Turns one P9 canonical content item into a real, narrated, captioned MP4 per
aspect ratio, deterministically:

    storyboard (scenes from canonical content, evidence labels kept on screen)
    → Pillow frames → local TTS narration (pluggable seam, default macOS `say`)
    → ffmpeg per-scene segments → concat → ffprobe-validated H.264/AAC MP4
    → hand-written SRT + VTT captions → manifest.json

A video is never reported READY until ffprobe validates container, codecs,
dimensions, audio, duration and a clean full decode. Reads canonical content
only through medforge.content public APIs; owns no domain math.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import medforge.types as T
import medforge.content as CT
from medforge.utils import slugify

VIDEO_VERSION = "p11-video-v1"
RENDERER_VERSION = "p11-video-render-v1"

ASPECT_SPECS: Dict[str, Tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}

# Caption target width (characters) and per-scene fact grouping.
CAPTION_WIDTH = 90
FACTS_PER_SCENE = 4
CORRELATIONS_PER_SCENE = 3

_LABEL_RE = re.compile(r"\s*\[[^\[\]]{1,8}\]\s*")

# ─── database access (mirrors publication.py patterns) ───

_ENSURED_DATABASES: set = set()


def ensure_video_tables() -> None:
    """Create the video_renders table via the additive V12 migration."""
    if str(T.META_DB) in _ENSURED_DATABASES and Path(T.META_DB).is_file():
        return
    here = Path(__file__).resolve()
    candidates = [Path.cwd(), T.BASE, *here.parents]
    for base in candidates:
        try:
            import sys as _sys

            if str(base) not in _sys.path:
                _sys.path.insert(0, str(base))
            from core.database.migrate_v12 import ensure_video_v12
        except ImportError:
            continue
        ensure_video_v12(T.META_DB, create_backup=False)
        _ENSURED_DATABASES.add(str(T.META_DB))
        return
    raise RuntimeError("core.database.migrate_v12 not found; cannot ensure video tables")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ─── TTS seam ───

def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _tts_say(text: str, workdir: Path) -> Tuple[Path, float]:
    """Default local TTS: macOS `say` → AIFF → ffmpeg → 44.1 kHz mono WAV."""
    say = shutil.which("say")
    ffmpeg = shutil.which("ffmpeg")
    if not say or not ffmpeg:
        raise RuntimeError("local TTS unavailable: need /usr/bin/say and ffmpeg "
                           "(or patch video.TTS_FN with another engine)")
    aiff = workdir / "narration.aiff"
    wav = workdir / "narration.wav"
    subprocess.run([say, "-o", str(aiff), text], check=True,
                   capture_output=True, timeout=120)
    subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(aiff),
                    "-ar", "44100", "-ac", "1", str(wav)],
                   check=True, capture_output=True, timeout=120)
    return wav, _wav_duration(wav)


# Pluggable engine seam — swap for another local/cloud engine without touching
# the pipeline. The default is the local, private macOS `say` engine.
TTS_FN: Callable[[str, Path], Tuple[Path, float]] = _tts_say


# ─── storyboard ───

def _scene_labels(*texts: str) -> List[str]:
    labels = set()
    for t in texts:
        labels.update(m.strip("[]") for m in re.findall(r"\[[^\[\]]{1,8}\]", t or ""))
    return sorted(labels)


def _narration_text(text: str) -> str:
    """Spoken text: drop citation labels (they stay on screen)."""
    return re.sub(r"\s+", " ", _LABEL_RE.sub(" ", text or "")).strip()


def _claim_ids_for_labels(topic: str, labels: List[str]) -> List[str]:
    """P4 claim IDs whose claim text cites any of the scene's labels."""
    if not labels:
        return []
    try:
        from medforge.evidence import claims_list

        listing = claims_list(topic=topic, limit=100) or {}
    except Exception:
        return []
    out = []
    for c in listing.get("claims", []):
        ctext = str(c.get("claim_text") or c.get("claim") or c.get("statement") or "")
        if _scene_labels(ctext):
            if set(_scene_labels(ctext)) & set(labels):
                out.append(c.get("claim_id"))
    return [c for c in out if c]


def _add_scene(scenes: List[Dict[str, Any]], content_id: str, title: str,
               narration: str, on_screen: List[str], visual: str,
               topic: str) -> None:
    idx = len(scenes)
    labels = _scene_labels(*(on_screen or [title]))
    scenes.append({
        "scene_id": f"{content_id}-s{idx:02d}",
        "index": idx,
        "title": title,
        "narration": _narration_text(narration),
        "on_screen": on_screen,
        "visual": visual,
        "claim_refs": _claim_ids_for_labels(topic, labels),
        "evidence_refs": labels,
        "asset_refs": [],
        "transition": "cut",
        "duration_s": None,
    })


def build_storyboard(content_id: str) -> Dict[str, Any]:
    """Deterministic scene list derived from one canonical content item."""
    item = CT.get_content(content_id)
    if not item:
        raise ValueError(f"Unknown content_id {content_id!r}")
    c = item.get("content") or {}
    topic = item.get("topic", "")
    scenes: List[Dict[str, Any]] = []

    _add_scene(scenes, content_id, topic, f"Study video: {topic}.", [topic], "title", topic)

    objectives = c.get("learning_objectives") or []
    if objectives:
        bullets = [f"• {o}" for o in objectives[:6]]
        _add_scene(scenes, content_id, "Learning objectives",
                   "In this video we cover: " + "; ".join(objectives[:6]) + ".",
                   bullets, "objectives", topic)

    for d in (c.get("definitions") or []):
        term = d.get("term", "")
        meaning = d.get("meaning", "")
        _add_scene(scenes, content_id, f"Definition: {term}",
                   f"{term} — {meaning}.", [f"{term}: {meaning}"], "definition", topic)

    for m in (c.get("mechanisms") or []):
        steps = m.get("steps") or []
        _add_scene(scenes, content_id, f"Mechanism: {m.get('name', '')}",
                   "Step by step: " + ". ".join(_narration_text(s) for s in steps) + ".",
                   [f"{i + 1}. {s}" for i, s in enumerate(steps[:6])], "mechanism", topic)

    facts = c.get("key_facts") or []
    for start in range(0, len(facts), FACTS_PER_SCENE):
        chunk = facts[start:start + FACTS_PER_SCENE]
        _add_scene(
            scenes, content_id, "Key facts" if start == 0 else "Key facts (cont.)",
            ". ".join(f.get("fact", "") for f in chunk) + ".",
            [f"• {f.get('fact', '')}" for f in chunk], "key_facts", topic)

    rels = c.get("relationships") or []
    if rels:
        lines = [f"{r.get('a', '')} → {r.get('relation', '')} → {r.get('b', '')}"
                 for r in rels[:6]]
        _add_scene(scenes, content_id, "Relationships",
                   ". ".join(_narration_text(l) for l in lines) + ".",
                   [f"• {l}" for l in lines], "relationships", topic)

    corr = c.get("clinical_correlations") or []
    for start in range(0, len(corr), CORRELATIONS_PER_SCENE):
        chunk = corr[start:start + CORRELATIONS_PER_SCENE]
        _add_scene(scenes, content_id, "Clinical correlation",
                   ". ".join(x.get("point", "") for x in chunk) + ".",
                   [f"• {x.get('point', '')}" for x in chunk], "clinical", topic)

    misc = c.get("misconceptions") or []
    for start in range(0, len(misc), CORRELATIONS_PER_SCENE):
        chunk = misc[start:start + CORRELATIONS_PER_SCENE]
        lines = [f"Wrong: {x.get('wrong', '')} → {x.get('correction', '')}"
                 for x in chunk]
        _add_scene(scenes, content_id, "Misconceptions",
                   ". ".join(_narration_text(l) for l in lines) + ".",
                   [f"• {l}" for l in lines], "misconceptions", topic)

    high = c.get("high_yield") or []
    if high:
        _add_scene(scenes, content_id, "High-yield takeaways",
                   ". ".join(high[:6]) + ".",
                   [f"★ {h}" for h in high[:6]], "high_yield", topic)

    refs = item.get("evidence_refs") or []
    if refs:
        lines = [f"[{r.get('label')}] {r.get('source', '')} {r.get('locator', '')}".strip()
                 for r in refs]
        _add_scene(scenes, content_id, "References",
                   "Evidence and sources: " + "; ".join(
                       f"{r.get('label')} {r.get('source', '')}" for r in refs) + ".",
                   lines, "references", topic)

    for i, s in enumerate(scenes):
        s["index"] = i
        s["scene_id"] = f"{content_id}-s{i:02d}"

    checksum = hashlib.sha1(json.dumps(
        {"content_id": content_id, "scenes": scenes},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"content_id": content_id, "topic": topic,
            "generation_mode": item.get("generation_mode"),
            "scenes": scenes, "storyboard_checksum": checksum,
            "video_version": VIDEO_VERSION}


# ─── captions ───

def _caption_segments(narration: str) -> List[str]:
    narration = narration.strip()
    if not narration:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", narration)
    out: List[str] = []
    cur = ""
    for sent in sentences:
        while len(sent) > CAPTION_WIDTH:
            cut = sent.rfind(" ", 0, CAPTION_WIDTH)
            cut = cut if cut > 20 else CAPTION_WIDTH
            piece, sent = sent[:cut].strip(), sent[cut:].strip()
            if cur:
                out.append(cur)
                cur = ""
            out.append(piece)
        if not sent:
            continue
        if cur and len(cur) + 1 + len(sent) <= CAPTION_WIDTH:
            cur = f"{cur} {sent}"
        else:
            if cur:
                out.append(cur)
            cur = sent
    if cur:
        out.append(cur)
    return out


def _ts(seconds: float, srt: bool) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    sep = "," if srt else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def caption_entries(scene: Dict[str, Any], start: float) -> List[Dict[str, Any]]:
    """Proportional caption timing for one scene starting at `start` seconds."""
    segs = _caption_segments(scene.get("narration", ""))
    dur = float(scene.get("duration_s") or 0.0)
    if not segs or dur <= 0:
        return []
    weights = [max(1, len(s)) for s in segs]
    total = sum(weights)
    entries, t = [], start
    for seg, w in zip(segs, weights):
        span = dur * w / total
        entries.append({"start": t, "end": t + span, "text": seg})
        t += span
    return entries


def write_captions(scenes: List[Dict[str, Any]], outdir: Path) -> Tuple[Path, Path]:
    srt_path, vtt_path = outdir / "captions.srt", outdir / "captions.vtt"
    entries: List[Dict[str, Any]] = []
    t = 0.0
    for s in scenes:
        entries.extend(caption_entries(s, t))
        t += float(s.get("duration_s") or 0.0)
    srt_lines, vtt_lines = [], ["WEBVTT", ""]
    for i, e in enumerate(entries, 1):
        srt_lines.append(str(i))
        srt_lines.append(f"{_ts(e['start'], True)} --> {_ts(e['end'], True)}")
        srt_lines.append(e["text"])
        srt_lines.append("")
        vtt_lines.append(f"{_ts(e['start'], False)} --> {_ts(e['end'], False)}")
        vtt_lines.append(e["text"])
        vtt_lines.append("")
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
    vtt_path.write_text("\n".join(vtt_lines), encoding="utf-8")
    return srt_path, vtt_path


# ─── frames ───

def _load_font(size: int):
    from PIL import ImageFont

    for cand in ("/System/Library/Fonts/Helvetica.ttc",
                 "/System/Library/Fonts/HelveticaNeue.ttc",
                 "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(cand).is_file():
            try:
                return ImageFont.truetype(cand, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_frames(storyboard: Dict[str, Any], outdir: Path,
                  aspect: str = "16:9") -> List[Path]:
    """Render one PNG slide per scene at the aspect's exact pixel size."""
    from PIL import Image, ImageDraw

    width, height = ASPECT_SPECS[aspect]
    frames_dir = outdir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    title_font = _load_font(int(height * 0.062))
    body_font = _load_font(int(height * 0.034))
    foot_font = _load_font(int(height * 0.022))

    paths: List[Path] = []
    scenes = storyboard["scenes"]
    pad = int(width * 0.06)
    max_w = width - 2 * pad
    for s in scenes:
        img = Image.new("RGB", (width, height), "#0F172A")
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, width, int(height * 0.012)], fill="#2563EB")
        # title (wrapped, up to 2 lines)
        y = int(height * 0.09)
        for line in _wrap(draw, s["title"], title_font, max_w)[:2]:
            draw.text((pad, y), line, font=title_font, fill="#F8FAFC")
            y += int(height * 0.085)
        draw.line([pad, y, width - pad, y], fill="#334155", width=2)
        y += int(height * 0.045)
        # bullets
        for bullet in s["on_screen"]:
            for line in _wrap(draw, bullet, body_font, max_w):
                if y > height * 0.90:
                    break
                draw.text((pad, y), line, font=body_font, fill="#CBD5E1")
                y += int(height * 0.058)
        # footer
        foot = (f"MedForge • {storyboard['topic']} • scene "
                f"{s['index'] + 1}/{len(scenes)}")
        draw.text((pad, height - int(height * 0.055)), foot,
                  font=foot_font, fill="#64748B")
        path = frames_dir / f"scene_{s['index']:02d}.png"
        img.save(path, "PNG")
        paths.append(path)
    return paths


# ─── ffmpeg assembly ───

def _ffmpeg_bin() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH; video rendering unavailable")
    return ffmpeg


def _segment(ffmpeg: str, frame: Path, wav: Optional[Path], duration: float,
             seg_path: Path, width: int, height: int) -> None:
    cmd = [ffmpeg, "-y", "-v", "error", "-loop", "1", "-i", str(frame)]
    if wav is not None:
        cmd += ["-i", str(wav)]
    cmd += ["-c:v", "libx264", "-tune", "stillimage", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-vf", f"scale={width}:{height}"]
    if wav is not None:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "1"]
    else:
        cmd += ["-an"]
    cmd += ["-t", f"{duration:.3f}", "-shortest", str(seg_path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)


def _concat(ffmpeg: str, segments: List[Path], out_path: Path) -> None:
    lst = out_path.parent / "concat.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in segments), encoding="utf-8")
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", "-movflags", "+faststart",
                    str(out_path)], check=True, capture_output=True, timeout=300)


def probe_video(path: Path) -> Dict[str, Any]:
    """ffprobe container/stream facts + full-decode verification."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found on PATH")
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr[:400]}")
    data = json.loads(proc.stdout or "{}")
    dec = subprocess.run([_ffmpeg_bin(), "-v", "error", "-i", str(path),
                          "-f", "null", "-"],
                         capture_output=True, text=True, timeout=300)
    data["decode_returncode"] = dec.returncode
    data["decode_stderr"] = dec.stderr[:800]
    return data


def qa_video(path: Path, aspect: str, expected_duration: float) -> Dict[str, Any]:
    """Real quality gate: only a fully valid MP4 passes."""
    width, height = ASPECT_SPECS[aspect]
    probe = probe_video(path)
    fmt = probe.get("format", {})
    streams = probe.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration = float(fmt.get("duration") or v.get("duration") or 0.0)
    checks = {
        "container_mp4": "mp4" in str(fmt.get("format_name", "")),
        "video_codec_h264": v.get("codec_name") == "h264",
        "dimensions": (v.get("width"), v.get("height")) == (width, height),
        "audio_codec_aac": a.get("codec_name") == "aac",
        "duration_matches": abs(duration - expected_duration) <= 0.5,
        "frames_present": int(v.get("nb_frames") or 0) > 0,
        "clean_decode": probe.get("decode_returncode") == 0
        and not probe.get("decode_stderr"),
    }
    return {"passed": all(checks.values()), "checks": checks, "probe": probe}


# ─── orchestration ───

def render_video(content_id: str, aspects: Tuple[str, ...] = ("16:9",),
                 tts_fn: Optional[Callable[[str, Path], Tuple[Path, float]]] = None
                 ) -> Dict[str, Any]:
    """Render captioned, narrated MP4s for the requested aspects.

    Every render is ffprobe-validated; failures are recorded, never hidden.
    """
    ensure_video_tables()
    tts = tts_fn or TTS_FN
    storyboard = build_storyboard(content_id)
    topic = storyboard["topic"]
    out_root = T.PRODUCTS / slugify(topic) / "video"
    out_root.mkdir(parents=True, exist_ok=True)
    ffmpeg = _ffmpeg_bin()

    renders: List[Dict[str, Any]] = []
    for aspect in aspects:
        if aspect not in ASPECT_SPECS:
            raise ValueError(f"aspect must be one of {sorted(ASPECT_SPECS)}")
        width, height = ASPECT_SPECS[aspect]
        outdir = out_root / aspect.replace(":", "x")
        outdir.mkdir(parents=True, exist_ok=True)
        seg_dir = outdir / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        video_id = "vid-" + hashlib.sha1(
            f"{content_id}|{aspect}".encode()).hexdigest()[:12]
        now = _now()
        con = _connect()
        try:
            con.execute(
                "INSERT INTO video_renders (video_id, content_id, aspect, status,"
                " scene_count, renderer_version, storyboard_checksum, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(video_id) DO UPDATE SET status='rendering',"
                " error=NULL, updated_at=excluded.updated_at",
                (video_id, content_id, aspect, "rendering", len(storyboard["scenes"]),
                 RENDERER_VERSION, storyboard["storyboard_checksum"], now, now))
            con.commit()
        finally:
            con.close()

        try:
            frames = render_frames(storyboard, outdir, aspect)
            scenes = [dict(s) for s in storyboard["scenes"]]
            work = outdir / "tts"
            work.mkdir(parents=True, exist_ok=True)
            t = 0.0
            segments: List[Path] = []
            for s, frame in zip(scenes, frames):
                narration = s["narration"]
                if narration and tts is not None:
                    wav, dur = tts(narration, work)
                else:
                    wav, dur = None, 1.5
                s["duration_s"] = round(dur, 3)
                s["start_s"] = round(t, 3)
                seg = seg_dir / f"seg_{s['index']:02d}.mp4"
                _segment(ffmpeg, frame, wav, dur, seg, width, height)
                segments.append(seg)
                t += dur
            final = outdir / "video.mp4"
            _concat(ffmpeg, segments, final)
            srt, vtt = write_captions(scenes, outdir)
            qa = qa_video(final, aspect, expected_duration=t)
            sb_path = outdir / "storyboard.json"
            sb_path.write_text(json.dumps(
                {**storyboard, "scenes": scenes}, indent=2, ensure_ascii=False,
                default=str), encoding="utf-8")
            manifest = {
                "video_id": video_id, "content_id": content_id, "topic": topic,
                "aspect": aspect, "width": width, "height": height,
                "duration_s": round(t, 3), "scene_count": len(scenes),
                "renderer_version": RENDERER_VERSION,
                "storyboard_checksum": storyboard["storyboard_checksum"],
                "files": [{"file": p.name, "sha256": _sha256(p)}
                          for p in (final, srt, vtt, sb_path)],
                "qa": {k: v for k, v in qa["checks"].items()},
                "video_version": VIDEO_VERSION,
                "created_at": now,
            }
            manifest_path = outdir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2,
                                                ensure_ascii=False, default=str),
                                     encoding="utf-8")
            status = "READY" if qa["passed"] else "FAILED"
            error = None if qa["passed"] else "ffprobe QA failed: " + ", ".join(
                k for k, v in qa["checks"].items() if not v)
            con = _connect()
            try:
                con.execute(
                    "UPDATE video_renders SET status=?, path=?, manifest_path=?,"
                    " duration_s=?, width=?, height=?, size_bytes=?, error=?,"
                    " updated_at=? WHERE video_id=?",
                    (status, str(final), str(manifest_path), round(t, 3),
                     width, height, final.stat().st_size if final.is_file() else None,
                     error, _now(), video_id))
                con.commit()
            finally:
                con.close()
            renders.append({"video_id": video_id, "aspect": aspect, "status": status,
                            "path": str(final), "duration_s": round(t, 3),
                            "qa_passed": qa["passed"], "error": error})
        except Exception as exc:
            con = _connect()
            try:
                con.execute(
                    "UPDATE video_renders SET status='FAILED', error=?, updated_at=?"
                    " WHERE video_id=?", (str(exc)[:500], _now(), video_id))
                con.commit()
            finally:
                con.close()
            renders.append({"video_id": video_id, "aspect": aspect,
                            "status": "FAILED", "error": str(exc)[:500]})
    return {"content_id": content_id, "topic": topic,
            "storyboard_checksum": storyboard["storyboard_checksum"],
            "scene_count": len(storyboard["scenes"]),
            "renders": renders, "video_version": VIDEO_VERSION}


def get_video_state(content_id: str) -> Dict[str, Any]:
    """All video_renders rows for a content item."""
    ensure_video_tables()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM video_renders WHERE content_id=? ORDER BY aspect",
            (content_id,)).fetchall()
    finally:
        con.close()
    return {"content_id": content_id,
            "renders": [dict(r) for r in rows],
            "video_version": VIDEO_VERSION}
