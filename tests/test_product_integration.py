"""P9 canonical content + product factory tests (offline, deterministic).

P9 replaced N independent per-artifact LLM calls with ONE canonical,
evidence-grounded content model per (topic, sources, config), from which every
artifact renders deterministically. These tests pin that contract:

* one generation per source set, with a stable content-addressed id (cache hit)
* changed sources/config → NEW id; history is append-only, never rewritten
* every canonical element must cite a supplied [S#] label
* unsupported content is refused or falls back to evidence sentences (never
  invented), and refusal is explicit
* multiple artifacts render from the same facts, so they cannot contradict
* provenance survives rendering (artifact → content → evidence → source)
* learner adaptation happens at RENDER time (canonical content is shared)
* artifact versions accumulate rather than overwrite

No live Ollama: the model seam is a canned JSON responder and the retrieval
seam is synthetic, source-labelled evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

import medforge as mf
import medforge.types as T
from medforge import content as CT

from test_study_intelligence import (  # noqa: F401  (fixtures reused on purpose)
    TOPIC, env, _hermetic, _seed_textbook, SENT_A, SENT_B,
)

SOURCES = [
    {"label": "S1", "source": "Textbook A", "locator": "p.1", "kind": "textbook",
     "id": "chunk-1", "text": SENT_A},
    {"label": "S2", "source": "Textbook A", "locator": "p.2", "kind": "textbook",
     "id": "chunk-2", "text": SENT_B},
]
SOURCE_TEXT = SENT_A + "\n" + SENT_B

CANNED = json.dumps({
    "learning_objectives": ["Explain the growth hormone axis"],
    "key_facts": [
        {"fact": "Growth hormone release is driven by hypothalamic GHRH.",
         "refs": ["S1"]},
        {"fact": "Growth hormone stimulates hepatic IGF-1 production.",
         "refs": ["S2"]},
    ],
    "mechanisms": [{"name": "GH axis", "steps": ["Hypothalamus releases GHRH [S1]"]}],
    "definitions": [{"term": "IGF-1", "meaning": "Mediator of GH growth effects",
                     "refs": ["S2"]}],
    "relationships": [{"a": "GH", "b": "IGF-1", "relation": "stimulates",
                       "refs": ["S2"]}],
    "clinical_correlations": [{"point": "GH excess causes gigantism before closure.",
                               "refs": ["S1"]}],
    "misconceptions": [{"wrong": "GH acts only on bone.",
                        "correction": "GH also drives hepatic IGF-1.",
                        "refs": ["S2"]}],
    "high_yield": ["Pituitary GH release is GHRH-dependent [S1]"],
})


@pytest.fixture(autouse=True)
def _model_seam(monkeypatch):
    """Canned model seam + bounded counters; no Ollama, no network."""
    calls = {"generate_text": 0, "payload": CANNED}

    def fake_generate_text(model, topic, sources_text, task, **kw):
        calls["generate_text"] += 1
        return calls["payload"]

    monkeypatch.setattr(CT, "generate_text", fake_generate_text)
    monkeypatch.setattr("medforge.models.ensure_models", lambda: "test-model")
    monkeypatch.setattr(T, "OFFLINE", False)
    return calls


def _gen(**kw):
    kw.setdefault("sources", SOURCES)
    kw.setdefault("source_text", SOURCE_TEXT)
    return CT.generate_canonical_content(TOPIC, **kw)


# ─── generation, identity, caching ───


class TestCanonicalGeneration:
    def test_no_evidence_is_refused(self, env):
        with pytest.raises(RuntimeError):
            CT.generate_canonical_content("Totally Unlinked Topic",
                                          sources=[], source_text="")

    def test_model_generation_cites_supplied_labels_only(self, env):
        item = _gen()
        assert item["generation_mode"] == "model"
        facts = item["content"]["key_facts"]
        assert facts
        for f in facts:
            assert set(f.get("refs") or []) <= {"S1", "S2"}

    def test_unknown_citation_is_dropped_and_falls_back(self, env, monkeypatch):
        bad = json.loads(CANNED)
        bad["key_facts"] = [{"fact": "Unsupported claim.", "refs": ["S9"]}]
        monkeypatch.setitem(CT.generate_text.__dict__, "payload", bad)
        calls = {"payload": json.dumps(bad)}
        monkeypatch.setattr(CT, "generate_text", lambda *a, **k: calls["payload"])
        item = _gen()
        assert item["generation_mode"] == "deterministic_fallback"
        for f in item["content"]["key_facts"]:
            assert set(f.get("refs") or []) <= {"S1", "S2"}

    def test_inline_citations_are_accepted_and_materialized(self, env, monkeypatch):
        # Models often cite in prose ("... [S1]") instead of a refs array; that
        # is still grounded output and must not be thrown away wholesale.
        inline = json.loads(CANNED)
        inline["key_facts"] = [{"fact": "Metabolism rises with thyroid hormone [S1]"},
                               {"fact": "Thyroxine is the main circulating hormone [S2]"}]
        monkeypatch.setattr(CT, "generate_text", lambda *a, **k: json.dumps(inline))
        item = _gen(config={"variant": "inline-citations"})
        assert item["generation_mode"] == "model"
        facts = item["content"]["key_facts"]
        assert [f["refs"] for f in facts] == [["S1"], ["S2"]]

    def test_partially_cited_model_output_keeps_valid_elements(self, env, monkeypatch):
        mixed = json.loads(CANNED)
        mixed["key_facts"] = [{"fact": "Grounded claim [S1]"},
                              {"fact": "Bogus label claim [S-1]"},
                              {"fact": "Uncited claim"}]
        monkeypatch.setattr(CT, "generate_text", lambda *a, **k: json.dumps(mixed))
        item = _gen(config={"variant": "mixed-citations"})
        assert item["generation_mode"] == "model"
        facts = item["content"]["key_facts"]
        assert len(facts) == 1 and facts[0]["refs"] == ["S1"]

    def test_deterministic_fallback_never_invents(self, env):
        item = _gen(allow_model=False)
        assert item["generation_mode"] == "deterministic_fallback"
        content = item["content"]
        for f in content["key_facts"]:
            assert f["refs"] == ["S1"] or f["refs"] == ["S2"]
            assert f["fact"] in SOURCE_TEXT

    def test_fenced_json_is_parsed(self, env, monkeypatch):
        monkeypatch.setattr(CT, "generate_text", lambda *a, **k: "```json\n" + CANNED + "\n```")
        item = _gen(config={"variant": "fenced"})
        assert item["generation_mode"] == "model"

    def test_same_inputs_are_cached_not_regenerated(self, env, _model_seam):
        first = _gen()
        assert first["cached"] is False
        second = _gen()
        assert second["cached"] is True
        assert second["content_id"] == first["content_id"]
        assert _model_seam["generate_text"] == 1, "cache hit must not call the model"
        con = sqlite3.connect(T.META_DB)
        try:
            n = con.execute("SELECT COUNT(*) FROM content_items").fetchone()[0]
        finally:
            con.close()
        assert n == 1

    def test_changed_sources_produce_a_new_version(self, env):
        first = _gen()
        other = [dict(SOURCES[0]), dict(SOURCES[1], text="Different wording entirely about IGF-1 signalling.")]
        second = _gen(sources=other, source_text=SOURCE_TEXT + " extra")
        assert second["content_id"] != first["content_id"]
        # Append-only history: the original content item survives untouched.
        assert CT.get_content(first["content_id"]) is not None
        assert len(CT.list_content(topic=TOPIC)) == 2

    def test_changed_config_produces_a_new_id(self, env):
        first = _gen()
        second = _gen(config={"depth": "deeper"})
        assert second["content_id"] != first["content_id"]

    def test_retrieval_seam_used_when_sources_omitted(self, env, monkeypatch):
        monkeypatch.setattr("medforge.retrieval.source_pack",
                            lambda topic, k: (SOURCE_TEXT, SOURCES))
        item = CT.generate_canonical_content(TOPIC)
        assert item["content"]["key_facts"]
        assert item["cached"] is False


# ─── rendering ───


class TestRendering:
    def test_multiple_artifacts_render_from_one_canonical(self, env, tmp_path):
        item = _gen()
        out = CT.render_study_products(item["content_id"], outdir=tmp_path)
        types = {a["artifact_type"] for a in out["artifacts"]}
        assert types == {"study_guide", "cheat_sheet", "flashcards", "quiz"}
        for name in types:
            assert (tmp_path / f"{name}.md").is_file()
        assert out["consistency"]["facts_rendered"] == out["consistency"]["facts_total"]
        assert out["consistency"]["unknown_fact_ids"] == 0

    def test_render_is_deterministic_for_same_profile(self, env, tmp_path):
        item = _gen()
        a = CT.render_study_products(item["content_id"], outdir=tmp_path / "a")
        b = CT.render_study_products(item["content_id"], outdir=tmp_path / "b")
        guide_a = (tmp_path / "a" / "study_guide.md").read_text()
        guide_b = (tmp_path / "b" / "study_guide.md").read_text()
        assert guide_a == guide_b, "canonical content must render identically"
        assert a["consistency"] == b["consistency"]

    def test_adaptation_changes_rendering_not_canonical(self, env, tmp_path):
        item = _gen()
        weak = CT.render_study_products(item["content_id"], adaptation={"profile": "weak"},
                                        outdir=tmp_path / "weak")
        strong = CT.render_study_products(item["content_id"], adaptation={"profile": "strong"},
                                          outdir=tmp_path / "strong")
        weak_guide = (tmp_path / "weak" / "study_guide.weak.md").read_text()
        strong_guide = (tmp_path / "strong" / "study_guide.strong.md").read_text()
        assert weak_guide != strong_guide, "profile must change emphasis"
        assert "Suggested first step" in weak_guide
        assert "Going deeper" in strong_guide
        # Canonical content itself is learner-independent and unchanged.
        assert CT.get_content(item["content_id"])["content"] == item["content"]

    def test_every_artifact_type_renders_and_passes_the_gate(self, env, tmp_path):
        item = _gen()
        types = ["study_guide", "cheat_sheet", "flashcards", "quiz",
                 "mind_map", "script"]
        out = CT.render_study_products(item["content_id"], artifact_types=types,
                                       outdir=tmp_path)
        statuses = {a["artifact_type"]: a["status"] for a in out["artifacts"]}
        assert set(statuses) == set(types)
        assert set(statuses.values()) == {"READY"}, statuses
        assert out["consistency"]["facts_rendered"] == out["consistency"]["facts_total"]
        assert out["consistency"]["coverage_scope"] == "full"
        assert CT.content_consistency_report(item["content_id"])["consistent"] is True

    def test_summary_artifacts_are_ready_despite_partial_coverage(self, env, tmp_path):
        # cheat_sheet/mind_map/script are curated subsets by design; flagging
        # them for review would make the status signal meaningless.
        item = _gen()
        for atype in CT.SUMMARY_ARTIFACT_TYPES:
            out = CT.render_study_products(item["content_id"], artifact_types=[atype],
                                           outdir=tmp_path / atype)
            assert out["artifacts"][0]["status"] == "READY", atype
            assert out["consistency"]["coverage_scope"] == "summary"

    def test_rendered_citations_are_not_duplicated(self, env, tmp_path):
        # A canonical fact may already carry an inline [S1]; _cite must not
        # append a second copy of a label the prose already shows.
        item = _gen()
        out = CT.render_study_products(item["content_id"], outdir=tmp_path)
        for a in out["artifacts"]:
            text = (tmp_path / f"{a['artifact_type']}.md").read_text(encoding="utf-8")
            assert "[S1] [S1]" not in text, a["artifact_type"]
            assert "[S2] [S2]" not in text, a["artifact_type"]

    def test_adaptive_rerenders_do_not_shadow_each_other(self, env, tmp_path):
        # All profiles share one outdir, so each needs its own file; otherwise a
        # later profile overwrites an earlier one's bytes and the consistency
        # report misreads a normal re-render as tampering.
        item = _gen()
        CT.render_study_products(item["content_id"], artifact_types=["study_guide"],
                                 outdir=tmp_path)
        for profile in ("weak", "strong"):
            CT.render_study_products(item["content_id"], artifact_types=["study_guide"],
                                     adaptation={"profile": profile}, outdir=tmp_path)
        assert (tmp_path / "study_guide.md").is_file()
        assert (tmp_path / "study_guide.weak.md").is_file()
        assert (tmp_path / "study_guide.strong.md").is_file()
        assert CT.content_consistency_report(item["content_id"])["consistent"] is True

    def test_artifact_versions_accumulate(self, env, tmp_path):
        item = _gen()
        CT.render_study_products(item["content_id"], outdir=tmp_path / "v1")
        CT.render_study_products(item["content_id"], outdir=tmp_path / "v2")
        versions = sorted({a["artifact_version"]
                           for a in CT.list_artifacts(item["content_id"])})
        assert versions == [1, 2], "re-rendering must not overwrite history"

    def test_artifact_identity_is_stable_and_content_addressed(self, env):
        item = _gen()
        CT.render_study_products(item["content_id"])
        arts = CT.list_artifacts(item["content_id"])
        assert arts
        for a in arts:
            assert a["artifact_id"].startswith(f"art-{item['content_id'][:8]}-")
            assert a["content_id"] == item["content_id"]
            assert a["checksum"]


# ─── provenance, status, consistency ───


class TestProvenance:
    def test_provenance_chain_survives_rendering(self, env):
        item = _gen()
        CT.render_study_products(item["content_id"])
        artifact = CT.list_artifacts(item["content_id"])[0]
        prov = CT.artifact_provenance(artifact["artifact_id"])
        assert prov["content_id"] == item["content_id"]
        assert prov["topic"] == TOPIC
        assert prov["model"] == "test-model"
        assert prov["prompt_version"] == CT.PROMPT_VERSION
        assert prov["sources_digest"]
        labels = {r["label"] for r in prov["evidence_refs"]}
        assert labels <= {"S1", "S2"}
        for r in prov["evidence_refs"]:
            assert r["kind"] and r["locator"], "each ref keeps source + locator"
        assert "→" in prov["chain"]

    def test_product_status_reports_ready(self, env):
        item = _gen()
        CT.render_study_products(item["content_id"])
        status = CT.get_product_status(item["content_id"])
        assert status["overall_status"] == "READY"
        assert set(status["artifact_statuses"]) == {"study_guide", "cheat_sheet",
                                                    "flashcards", "quiz"}
        assert status["content_version"] == CT.CONTENT_VERSION

    def test_product_status_unknown_id_raises(self, env):
        with pytest.raises(ValueError):
            CT.get_product_status("does-not-exist")

    def test_consistency_report_detects_tampering(self, env, tmp_path):
        item = _gen()
        CT.render_study_products(item["content_id"], outdir=tmp_path)
        assert CT.content_consistency_report(item["content_id"])["consistent"] is True
        (tmp_path / "study_guide.md").write_text("tampered", encoding="utf-8")
        report = CT.content_consistency_report(item["content_id"])
        assert report["consistent"] is False

    def test_single_generation_backs_every_artifact(self, env, _model_seam):
        item = _gen()
        CT.render_study_products(item["content_id"])
        assert _model_seam["generate_text"] == 1, (
            "four artifacts must share ONE model generation")

    def test_flashcard_rows_are_stable_and_cited(self, env, tmp_path):
        item = _gen()
        CT.render_study_products(item["content_id"], artifact_types=["flashcards"],
                                 outdir=tmp_path / "a")
        CT.render_study_products(item["content_id"], artifact_types=["flashcards"],
                                 outdir=tmp_path / "b")
        first = (tmp_path / "a" / "flashcards.md").read_text()
        second = (tmp_path / "b" / "flashcards.md").read_text()
        assert first == second, "card identity must be stable across renders"
        for line in first.splitlines()[1:]:
            assert "[S1]" in line or "[S2]" in line
