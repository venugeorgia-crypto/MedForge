"""P11 video production tests — a video is only READY if ffprobe says so."""
import hashlib
import json
import sqlite3
import wave as wavemod
from pathlib import Path

import pytest

sys_path_parent = Path(__file__).resolve().parent.parent
sys_path_current = sys_path_parent / "current"
for p in (str(sys_path_parent), str(sys_path_current)):
    if p not in __import__("sys").path:
        __import__("sys").path.insert(0, p)

import medforge.types as T                # noqa: E402
from medforge import video as V           # noqa: E402
from medforge import content as CT        # noqa: E402

from test_study_intelligence import (      # noqa: F401,E402  (fixtures reused)
    TOPIC, env, _hermetic, _seed_textbook, SENT_A, SENT_B,
)
from test_product_integration import (     # noqa: F401,E402  (seam data reused)
    SOURCES, SOURCE_TEXT, CANNED, _model_seam,
)
import medforge.assessment as A            # noqa: E402
import medforge.tutor as TU                # noqa: E402
import medforge.learner_model as LM        # noqa: E402


@pytest.fixture(autouse=True)
def _video_env_cache(monkeypatch):
    """Never let the module-level ensure cache leak between test databases."""
    yield
    V._ENSURED_DATABASES.clear()


@pytest.fixture()
def _tts_seam(monkeypatch):
    """Deterministic local TTS seam: real 1.0 s silent WAV (no `say`, no Ollama).

    The ffmpeg pipeline itself runs for real — only the voice engine is swapped.
    """
    def fake_tts(text, workdir):
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / ("n-" + hashlib.md5(text.encode()).hexdigest()[:10] + ".wav")
        with wavemod.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00" * 44100)  # exactly 1.0 s
        return path, 1.0

    monkeypatch.setattr(V, "TTS_FN", fake_tts)
    return fake_tts


def _gen(**kw):
    kw.setdefault("sources", SOURCES)
    kw.setdefault("source_text", SOURCE_TEXT)
    return CT.generate_canonical_content(TOPIC, **kw)


# ─── V12 schema ───


class TestV12Schema:
    def test_additive_and_idempotent(self, env):
        db_path, _ = env
        V.ensure_video_tables()
        con = sqlite3.connect(db_path)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert "video_renders" in tables
            version = con.execute(
                "SELECT version FROM schema_migrations WHERE version='12.0.0'"
            ).fetchone()[0]
            assert version == "12.0.0"
        finally:
            con.close()
        V.ensure_video_tables()  # idempotent rerun

    def test_existing_content_rows_untouched(self, env):
        item = _gen()
        V.ensure_video_tables()
        again = CT.get_content(item["content_id"])
        assert again["content"] == item["content"]


# ─── storyboard ───


class TestStoryboard:
    def test_unknown_content_is_refused(self, env):
        with pytest.raises(ValueError):
            V.build_storyboard("nope")

    def test_deterministic_and_stable_ids(self, env):
        item = _gen()
        sb1 = V.build_storyboard(item["content_id"])
        sb2 = V.build_storyboard(item["content_id"])
        assert sb1 == sb2
        ids = [s["scene_id"] for s in sb1["scenes"]]
        assert ids[0].endswith("-s00") and len(ids) == len(set(ids))
        assert sb1["storyboard_checksum"] == sb2["storyboard_checksum"]

    def test_provenance_survives_into_scenes(self, env):
        item = _gen()
        sb = V.build_storyboard(item["content_id"])
        on_screen = [b for s in sb["scenes"] for b in s["on_screen"]]
        # citation labels KEPT on screen
        assert any("[S1]" in b or "[S2]" in b for b in on_screen)
        # labels stripped from narration
        assert all("[S" not in s["narration"] for s in sb["scenes"])
        known = {r["label"] for r in item["evidence_refs"]}
        for s in sb["scenes"]:
            assert set(s["evidence_refs"]) <= known
        # required scene kinds present
        visuals = {s["visual"] for s in sb["scenes"]}
        assert {"title", "key_facts", "references"} <= visuals

    def test_claim_refs_resolve_to_p4(self, env):
        item = _gen()
        sb = V.build_storyboard(item["content_id"])
        from medforge.evidence import claims_list
        topic_claims = {c["claim_id"] for c in
                        (claims_list(topic=TOPIC, limit=100).get("claims") or [])}
        for s in sb["scenes"]:
            assert set(s["claim_refs"]) <= topic_claims


# ─── captions ───


class TestCaptions:
    def test_segments_and_timing_are_monotonic(self, env):
        item = _gen()
        sb = V.build_storyboard(item["content_id"])
        for s in sb["scenes"]:
            s["duration_s"] = 2.0
        entries = [e for s in sb["scenes"] for e in V.caption_entries(s, s["index"] * 2.0)]
        assert entries
        starts = [e["start"] for e in entries]
        ends = [e["end"] for e in entries]
        assert starts == sorted(starts)
        assert all(s < e for s, e in zip(starts, ends))
        assert all(len(e["text"]) <= V.CAPTION_WIDTH or
                   len(e["text"]) <= V.CAPTION_WIDTH + 2 for e in entries)

    def test_srt_and_vtt_written(self, env, tmp_path):
        item = _gen()
        sb = V.build_storyboard(item["content_id"])
        for s in sb["scenes"]:
            s["duration_s"] = 1.0
        srt, vtt = V.write_captions(sb["scenes"], tmp_path)
        srt_text = srt.read_text(encoding="utf-8")
        assert srt_text.lstrip().startswith("1\n")
        assert "-->" in srt_text
        assert vtt.read_text(encoding="utf-8").startswith("WEBVTT")


# ─── frames ───


class TestFrames:
    def test_exact_dimensions_per_aspect(self, env, tmp_path):
        item = _gen()
        sb = V.build_storyboard(item["content_id"])
        from PIL import Image
        for aspect in ("16:9", "9:16", "1:1"):
            paths = V.render_frames(sb, tmp_path / aspect.replace(":", "x"), aspect)
            assert paths
            with Image.open(paths[0]) as img:
                assert img.size == V.ASPECT_SPECS[aspect]


# ─── full render + ffprobe QA ───


class TestRenderAndQA:
    def test_real_mp4_passes_ffprobe(self, env, _tts_seam):
        item = _gen()
        result = V.render_video(item["content_id"], aspects=("16:9",))
        assert result["renders"], result
        r = result["renders"][0]
        assert r["status"] == "READY", r
        assert r["qa_passed"] is True
        video_path = Path(r["path"])
        assert video_path.is_file() and video_path.stat().st_size > 1000

        probe = V.probe_video(video_path)
        fmt, streams = probe["format"], probe["streams"]
        v = next(s for s in streams if s["codec_type"] == "video")
        a = next(s for s in streams if s["codec_type"] == "audio")
        assert "mp4" in fmt["format_name"]
        assert v["codec_name"] == "h264"
        assert (v["width"], v["height"]) == (1920, 1080)
        assert a["codec_name"] == "aac"
        duration = float(fmt["duration"])
        assert abs(duration - result["scene_count"] * 1.0) <= 0.5
        assert probe["decode_returncode"] == 0 and not probe["decode_stderr"]

    def test_every_render_persisted(self, env, _tts_seam):
        item = _gen()
        V.render_video(item["content_id"], aspects=("16:9",))
        state = V.get_video_state(item["content_id"])
        assert state["renders"]
        row = state["renders"][0]
        assert row["status"] == "READY"
        assert (row["width"], row["height"]) == (1920, 1080)
        assert row["renderer_version"] == V.RENDERER_VERSION

    def test_invalid_aspect_raises(self, env):
        item = _gen()
        with pytest.raises(ValueError):
            V.render_video(item["content_id"], aspects=("4:3",))

    def test_failure_is_recorded_not_hidden(self, env, _tts_seam, monkeypatch):
        item = _gen()

        def boom(*a, **k):
            raise RuntimeError("encoder exploded")

        monkeypatch.setattr(V, "_segment", boom)
        result = V.render_video(item["content_id"], aspects=("16:9",))
        r = result["renders"][0]
        assert r["status"] == "FAILED"
        assert "encoder exploded" in (r.get("error") or "")
        state = V.get_video_state(item["content_id"])
        assert state["renders"][0]["status"] == "FAILED"
        assert state["renders"][0]["error"]
        # and it must be restart-safe: a fresh attempt can succeed
        monkeypatch.undo()
        result2 = V.render_video(item["content_id"], aspects=("16:9",))
        assert result2["renders"][0]["status"] == "READY"

    def test_manifest_and_captions_on_disk(self, env, _tts_seam):
        item = _gen()
        result = V.render_video(item["content_id"], aspects=("16:9",))
        outdir = Path(result["renders"][0]["path"]).parent
        manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["qa"]["container_mp4"] is True
        assert (outdir / "captions.srt").is_file()
        assert (outdir / "captions.vtt").is_file()
        assert (outdir / "storyboard.json").is_file()
        files = {f["file"] for f in manifest["files"]}
        assert {"video.mp4", "captions.srt", "captions.vtt"} <= files
