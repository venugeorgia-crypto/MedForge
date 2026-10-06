"""MedForge P9 canonical content model + deterministic product rendering.

The product factory's failure mode was N independent LLM calls per topic —
each artifact could contradict the others. P9 generates ONE canonical,
evidence-grounded content representation per (topic, source-set, config) and
then renders every artifact deterministically from it:

    gather sources → one canonical generation (LLM, strict JSON, with a
    deterministic P4/P3 fallback) → validate → render study guide / cheat
    sheet / flashcards / quiz / mind map / script from the same facts.

Provenance survives rendering: every canonical element carries evidence_refs
(P4 evidence ids + locators); every rendered artifact stores its content_id,
checksum, status and provenance chain (artifact → element → evidence →
source). Cross-artifact consistency is structural — all artifacts cite the
same fact ids — and is re-checked at render time.

Caching: content_id = sha1(topic|sources_digest|config|prompt_version|model).
Same inputs → cache hit (no model call, no duplicate content). Changed source
set or config → new content_id → new version; history is never overwritten
(content_items is append-only).

The canonical generation is learner-independent; learner adaptation happens
at render time (profile-driven section emphasis), so no learner-specific
output is ever cached or shared.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import medforge.types as T
from medforge.utils import slugify, utcnow, atomic_text
from medforge.generation import generate_text

CONTENT_VERSION = "p9-content-v1"
PROMPT_VERSION = "canonical-v1"

# Artifact types that deliberately render a curated subset of the canonical
# facts. Everything else must cover the whole fact list to be READY.
SUMMARY_ARTIFACT_TYPES = ("cheat_sheet", "mind_map", "script")

__all__ = [
    "CONTENT_VERSION", "PROMPT_VERSION", "SUMMARY_ARTIFACT_TYPES",
    "generate_canonical_content", "get_content", "list_content",
    "render_study_products", "get_product_status", "list_artifacts",
    "artifact_provenance", "content_consistency_report",
    "canonical_fallback_from_evidence",
]


def _now() -> str:
    return utcnow()


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    return con


def _json_load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _ensure_tables() -> None:
    from medforge.study import ensure_study_tables

    ensure_study_tables()


# ─── Canonical generation ───


_CANONICAL_TASK = """Return ONE canonical content model for this topic as strict JSON
(no markdown fences, no commentary) with exactly these keys:
{
 "learning_objectives": ["..."],
 "key_facts": [{"fact": "...", "refs": ["S1"]}],
 "mechanisms": [{"name": "...", "steps": ["... [S1]"]}],
 "definitions": [{"term": "...", "meaning": "...", "refs": ["S1"]}],
 "relationships": [{"a": "...", "b": "...", "relation": "...", "refs": ["S1"]}],
 "clinical_correlations": [{"point": "...", "refs": ["S1"]}],
 "misconceptions": [{"wrong": "...", "correction": "...", "refs": ["S1"]}],
 "high_yield": ["... [S1]"]
}
Rules:
- every fact/step/meaning/correlation/point/correction ends with the [S#] label(s) of the evidence that supports it
- never invent facts outside EVIDENCE; omit a key (or leave it empty) when the evidence does not support it
- 5-12 key_facts; concise medical-student language"""


def _sources_digest(source_text: str, sources: List[Dict[str, Any]]) -> str:
    canon = json.dumps([{
        "label": s.get("label"), "source": s.get("source"), "locator": s.get("locator"),
        "url": s.get("url", ""), "kind": s.get("kind"),
    } for s in sources], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1((canon + "|" + source_text).encode("utf-8")).hexdigest()[:16]


def _parse_canonical_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    text = re.sub(r"```(?:json)?", "", raw).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or "key_facts" not in data:
        return None
    return data


def _inline_labels(text: Any) -> set:
    """Labels cited inline as ``[S1]`` in prose."""
    return set(re.findall(r"\[(S\d+)\]", str(text or "")))


def _element_refs(row: Dict[str, Any], labels: set) -> List[str]:
    """Resolve an element's citations from both supported model forms.

    The canonical schema asks for a ``"refs"`` array, but models frequently
    cite inline in prose instead (``"fact": "... [S1]"``). Both are accepted:
    a row is grounded when *any* citation it carries names supplied labels, and
    the resolved labels are materialized back onto ``refs`` so rendering and
    provenance never have to re-parse prose. No valid citation -> no element.
    """
    found: set = set()
    for ref in row.get("refs") or []:
        found |= set(re.findall(r"S\d+", str(ref)))
    for value in row.values():
        if isinstance(value, str):
            found |= _inline_labels(value)
        elif isinstance(value, list):
            for item in value:
                found |= _inline_labels(item)
    if not found or not found <= labels:
        return []
    return sorted(found, key=lambda s: int(s[1:]))


def _sanitize(data: Dict[str, Any], labels: set) -> Dict[str, Any]:
    """Drop elements whose citations are missing or point outside the sources.

    Every factual element must cite supplied evidence — an element with no
    citation is dropped, never trusted. If that leaves no key facts, the
    caller falls back to deterministic evidence sentences.
    """
    out: Dict[str, Any] = {}
    # Objectives are framing, so an uncited objective is allowed; a cited one
    # must stay in range. High-yield points are facts and must cite evidence.
    out["learning_objectives"] = [
        s for s in (data.get("learning_objectives") or [])
        if _inline_labels(s) <= labels
    ]
    out["high_yield"] = [
        s for s in (data.get("high_yield") or [])
        if _inline_labels(s) and _inline_labels(s) <= labels
    ]
    # Mechanisms cite inside their steps rather than in a refs list.
    mechanisms = []
    for row in (data.get("mechanisms") or []):
        if not isinstance(row, dict):
            continue
        cited: set = set()
        for step in row.get("steps") or []:
            cited |= _inline_labels(step)
        if not cited or not cited <= labels:
            continue
        mechanisms.append(row)
    out["mechanisms"] = mechanisms
    for key in ("key_facts", "definitions", "relationships",
                "clinical_correlations", "misconceptions"):
        rows = []
        for row in (data.get(key) or []):
            if not isinstance(row, dict):
                continue
            refs = _element_refs(row, labels)
            if not refs:
                continue
            row = dict(row)
            row["refs"] = refs
            rows.append(row)
        out[key] = rows
    if not out.get("key_facts"):
        raise ValueError("canonical content had no evidence-cited key facts")
    return out


def _label_evidence_refs(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Carry P4/P3 provenance for each [S#] label into the content item."""
    from medforge.evidence import evidence_snapshot

    snapshot = evidence_snapshot() if sources else {"counts": {"evidence": 0}}
    refs = []
    for s in sources:
        refs.append({
            "label": s.get("label"), "source": s.get("source"),
            "locator": s.get("locator"), "url": s.get("url", ""),
            "kind": s.get("kind"), "chunk_id": s.get("id"),
        })
    return refs


def canonical_fallback_from_evidence(topic: str, source_text: str,
                                     sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic canonical content when the model is unavailable.

    Built from the evidence sentences themselves — never invents a fact.
    """
    facts = []
    for s in sources:
        for sentence in re.split(r"(?<=[.!?])\s+", (s.get("text") or "").strip()):
            sentence = sentence.strip()
            if len(sentence) < 40:
                continue
            facts.append({"fact": sentence, "refs": [s["label"]]})
            if len(facts) >= 10:
                break
        if len(facts) >= 10:
            break
    return {
        "learning_objectives": [f"Recall the key evidence-backed statements about {topic}"],
        "key_facts": facts,
        "mechanisms": [],
        "definitions": [],
        "relationships": [],
        "clinical_correlations": [],
        "misconceptions": [],
        "high_yield": [f"{f['fact']} {f['refs'][0]}" for f in facts[:3]],
    }


def generate_canonical_content(topic: str, sources: Optional[List[Dict[str, Any]]] = None,
                               source_text: Optional[str] = None,
                               config: Optional[Dict[str, Any]] = None,
                               model: Optional[str] = None,
                               allow_model: bool = True,
                               now: Optional[str] = None) -> Dict[str, Any]:
    """One canonical generation per (topic, sources, config); cache-aware."""
    _ensure_tables()
    now = now or _now()
    from medforge.retrieval import source_pack
    from medforge.models import ensure_models

    config = config or {}
    if sources is None or source_text is None:
        source_text, sources = source_pack(topic, 6)
    if not sources:
        raise RuntimeError("No evidence available for canonical content generation.")
    digest_sources = _sources_digest(source_text or "", sources)
    digest_config = hashlib.sha1(_json_dump(config).encode()).hexdigest()[:12]
    try:
        chat_model = model or ensure_models()
    except Exception:
        chat_model = "unavailable"
    content_id = hashlib.sha1(
        "|".join([slugify(topic), digest_sources, digest_config, PROMPT_VERSION, chat_model])
    .encode()).hexdigest()[:16]

    con = _connect()
    try:
        row = con.execute("SELECT * FROM content_items WHERE content_id=?",
                          (content_id,)).fetchone()
        if row:
            item = dict(row)
            item["content"] = _json_load(item.get("content"), {})
            item["evidence_refs"] = _json_load(item.get("evidence_refs"), [])
            item["cached"] = True
            return item
    finally:
        con.close()

    labels = {s["label"] for s in sources}
    content, mode = None, "model"
    if allow_model and chat_model not in (None, "unavailable") and not T.OFFLINE:
        try:
            raw = generate_text(chat_model, topic, source_text or "", _CANONICAL_TASK)
            data = _parse_canonical_json(raw)
            if data:
                content = _sanitize(data, labels)
        except Exception:
            content = None
    if content is None:
        content = canonical_fallback_from_evidence(topic, source_text or "", sources)
        mode = "deterministic_fallback"
        # Re-attach the pack provenance that the fallback used.
        for fact in content["key_facts"]:
            fact.setdefault("refs", [])

    evidence_refs = _label_evidence_refs(sources)

    con = _connect()
    try:
        con.execute(
            "INSERT INTO content_items (content_id, topic, mastery_key, curriculum_node_id,"
            " sources_digest, config_digest, prompt_version, model, content, evidence_refs,"
            " generation_mode, content_version, study_version, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (content_id, topic, slugify(topic), None, digest_sources, digest_config,
             PROMPT_VERSION, chat_model, _json_dump(content), _json_dump(evidence_refs),
             mode, CONTENT_VERSION, "p9-study-v1", now),
        )
        con.commit()
    finally:
        con.close()
    item = {"content_id": content_id, "topic": topic, "content": content,
            "evidence_refs": evidence_refs, "generation_mode": mode,
            "model": chat_model, "cached": False, "created_at": now}
    return item


def get_content(content_id: str) -> Optional[Dict[str, Any]]:
    _ensure_tables()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM content_items WHERE content_id=?",
                          (content_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["content"] = _json_load(item.get("content"), {})
        item["evidence_refs"] = _json_load(item.get("evidence_refs"), [])
        return item
    finally:
        con.close()


def list_content(topic: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _connect()
    try:
        if topic:
            rows = con.execute("SELECT content_id, topic, generation_mode, content_version,"
                               " created_at FROM content_items WHERE topic=? ORDER BY created_at"
                               " DESC LIMIT ?", (topic, max(1, limit))).fetchall()
        else:
            rows = con.execute("SELECT content_id, topic, generation_mode, content_version,"
                               " created_at FROM content_items ORDER BY created_at DESC"
                               " LIMIT ?", (max(1, limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ─── Deterministic rendering ───


def _cite(line: str, refs: List[str]) -> str:
    """Append only the citation labels the prose does not already carry.

    Canonical facts often arrive with inline citations (``"… [S1]"``), so
    appending the refs list unconditionally produced ``[S1] [S1]``.
    """
    present = _inline_labels(line)
    missing: List[str] = []
    for ref in refs or []:
        # Accept both the bare ("S1") and bracketed ("[S1]") ref forms.
        label = (re.findall(r"S\d+", str(ref)) or [str(ref)])[0]
        if label not in present and label not in missing:
            missing.append(label)
    labels = " ".join(f"[{m}]" for m in missing)
    return f"{line} {labels}".strip()


def _fact_lines(content: Dict[str, Any]) -> List[Tuple[str, str, List[str]]]:
    """(fact_id, text, refs) for every canonical fact element."""
    out = []
    for i, f in enumerate(content.get("key_facts") or [], 1):
        out.append((f"fact-{i:02d}", f.get("fact", ""), list(f.get("refs") or [])))
    for i, d in enumerate(content.get("definitions") or [], 1):
        out.append((f"def-{i:02d}", f"{d.get('term')}: {d.get('meaning')}", list(d.get("refs") or [])))
    for i, c in enumerate(content.get("clinical_correlations") or [], 1):
        out.append((f"clin-{i:02d}", c.get("point", ""), list(c.get("refs") or [])))
    return out


def render_study_products(content_id: str, artifact_types: Optional[List[str]] = None,
                          adaptation: Optional[Dict[str, Any]] = None,
                          outdir: Optional[Path] = None,
                          now: Optional[str] = None) -> Dict[str, Any]:
    """Render artifacts deterministically from the canonical content."""
    _ensure_tables()
    now = now or _now()
    item = get_content(content_id)
    if not item:
        raise ValueError(f"Unknown content_id {content_id!r}")
    content = item["content"]
    topic = item["topic"]
    profile = (adaptation or {}).get("profile", "developing")
    requested = artifact_types or ["study_guide", "cheat_sheet", "flashcards", "quiz"]
    facts = _fact_lines(content)
    if not facts:
        raise ValueError("canonical content has no facts to render")

    con = _connect()
    try:
        version_row = con.execute(
            "SELECT MAX(artifact_version) AS v FROM content_artifacts WHERE content_id=?",
            (content_id,)).fetchone()
        base_version = int(version_row["v"] or 0) + 1 if version_row else 1
    finally:
        con.close()

    rendered: Dict[str, str] = {}
    artifact_rows: List[Dict[str, Any]] = []
    fact_ids_used: set = set()

    if "study_guide" in requested:
        lines = [f"# Study Guide — {topic}",
                 "",
                 "## Learning objectives"]
        for o in content.get("learning_objectives") or []:
            lines.append(f"- {o}")
        lines += ["", "## Key facts"]
        for fid, text, refs in facts:
            lines.append(f"- {_cite(text, refs)}")
            fact_ids_used.add(fid)
        if content.get("mechanisms"):
            lines += ["", "## Mechanisms"]
            for m in content["mechanisms"]:
                lines.append(f"### {m.get('name', 'mechanism')}")
                for s in m.get("steps") or []:
                    lines.append(f"- {s}")
        if content.get("misconceptions") and profile in ("weak", "developing", "overconfident"):
            lines += ["", "## Common misconceptions"]
            for m in content["misconceptions"]:
                lines.append(f"- Wrong: {m.get('wrong')} → Correct: {_cite(m.get('correction', ''), m.get('refs') or [])}")
        if profile == "weak":
            lines += ["", "## Suggested first step", "- Study this with the tutor in `explain` mode before self-testing."]
        if profile == "strong":
            lines += ["", "## Going deeper", "- Attempt the quiz cold, then review only the missed items."]
        rendered["study_guide"] = "\n".join(lines)

    if "cheat_sheet" in requested:
        lines = [f"# Cheat Sheet — {topic}", "", "## Must-know"]
        for fid, text, refs in facts[:8]:
            lines.append(f"- {_cite(text, refs)}")
            fact_ids_used.add(fid)
        if content.get("high_yield"):
            lines += ["", "## High yield"]
            for h in content["high_yield"][:6]:
                lines.append(f"- {h}")
        rendered["cheat_sheet"] = "\n".join(lines)

    if "flashcards" in requested:
        rows = [["Question", "Answer", "Sources"]]
        for fid, text, refs in facts:
            rows.append([f"{fid}|{text}", _cite("Explain the fact and why it matters.", refs),
                         " ".join(f"[{r}]" for r in refs)])
            fact_ids_used.add(fid)
        for i, m in enumerate(content.get("misconceptions") or [], 1):
            rows.append([f"misc-{i:02d}|True or false: {m.get('wrong')}",
                         f"False — {_cite(m.get('correction', ''), m.get('refs') or [])}",
                         " ".join(f"[{r}]" for r in (m.get("refs") or []))])
        rendered["flashcards"] = "\n".join("\t".join(r) for r in rows)

    if "quiz" in requested:
        lines = [f"# Quiz — {topic}", ""]
        hard = profile in ("strong", "overconfident")
        for i, (fid, text, refs) in enumerate(facts, 1):
            stem = (f"Clinical application: how does this fact change management? {text}"
                    if hard and i % 2 == 0 else
                    f"Recall: which statement is correct? {text}")
            lines.append(f"**Q{i}.** {stem} {' '.join(f'[{r}]' for r in refs)}")
            lines.append(f"**A{i}.** {_cite(text, refs)}")
            lines.append("")
            fact_ids_used.add(fid)
        rendered["quiz"] = "\n".join(lines)

    if "mind_map" in requested:
        lines = [f"- {topic}"]
        for fid, text, refs in facts[:6]:
            lines.append(f"  - {_cite(text, refs)}")
            fact_ids_used.add(fid)
        for m in content.get("mechanisms") or []:
            lines.append(f"  - mechanism: {m.get('name')}")
        rendered["mind_map"] = "\n".join(lines)

    if "script" in requested:
        lines = ["HOOK: " + _cite(facts[0][1], facts[0][2]), ""]
        fact_ids_used.add(facts[0][0])
        for fid, text, refs in facts[1:4]:
            lines.append("BODY: " + _cite(text, refs))
            fact_ids_used.add(fid)
        lines += ["", "RECAP: " + _cite(facts[0][1], facts[0][2])]
        rendered["script"] = "\n".join(lines)

    outdir = Path(outdir) if outdir else (T.PRODUCTS / slugify(topic) / "p9")
    outdir.mkdir(parents=True, exist_ok=True)

    def _artifact_path(name: str) -> Path:
        """One file per (artifact type, adaptation profile).

        Profiles render different emphasis, so sharing a single filename would
        let a later profile silently overwrite an earlier one's bytes and make
        the consistency report look tampered. The default profile keeps the
        plain ``<type>.md`` name.
        """
        suffix = "" if profile == "developing" else f".{profile}"
        return outdir / f"{name}{suffix}.md"

    for name, text in rendered.items():
        atomic_text(_artifact_path(name), text)

    all_fact_ids = {fid for fid, _, _ in facts}
    # Coverage semantics: study_guide/flashcards/quiz render EVERY canonical
    # fact, so a gap there is a real defect. cheat_sheet/mind_map/script are
    # curated subsets by design, so asking them to cover everything would flag
    # perfectly good artifacts for review and train users to ignore the signal.
    full_coverage = any(t not in SUMMARY_ARTIFACT_TYPES for t in rendered)
    missing = sorted(all_fact_ids - fact_ids_used) if full_coverage else []
    consistency = {
        "facts_total": len(all_fact_ids),
        "facts_rendered": len(fact_ids_used & all_fact_ids),
        "unknown_fact_ids": 0,
        "coverage_scope": "full" if full_coverage else "summary",
        "contradiction_check": "structural (single fact source)",
    }
    for name, text in rendered.items():
        checksum = hashlib.sha256(text.encode()).hexdigest()
        artifact_id = f"art-{content_id[:8]}-{name}-{base_version}"
        con = _connect()
        try:
            con.execute(
                "INSERT OR REPLACE INTO content_artifacts (artifact_id, content_id,"
                " artifact_type, artifact_version, adaptation_profile, path, checksum,"
                " status, validation, render_mode, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, content_id, name, base_version, profile,
                 str(_artifact_path(name)), checksum,
                 "READY" if not missing else "NEEDS_REVIEW",
                 _json_dump(consistency), "deterministic", now),
            )
            con.commit()
        finally:
            con.close()
        artifact_rows.append({"artifact_id": artifact_id, "artifact_type": name,
                              "version": base_version, "checksum": checksum[:12],
                              "path": str(_artifact_path(name)),
                              "status": "READY" if not missing else "NEEDS_REVIEW"})
    return {"content_id": content_id, "topic": topic, "adaptation_profile": profile,
            "artifacts": artifact_rows, "consistency": consistency, "generated_at": now}


def get_product_status(content_id: str) -> Dict[str, Any]:
    _ensure_tables()
    item = get_content(content_id)
    if not item:
        raise ValueError(f"Unknown content_id {content_id!r}")
    con = _connect()
    try:
        rows = con.execute("SELECT * FROM content_artifacts WHERE content_id=?"
                           " ORDER BY artifact_type, artifact_version",
                           (content_id,)).fetchall()
    finally:
        con.close()
    artifacts = []
    for r in rows:
        d = dict(r)
        d["validation"] = _json_load(d.get("validation"), {})
        artifacts.append(d)
    statuses = {a["artifact_type"]: a["status"] for a in artifacts}
    overall = "READY" if statuses and all(v == "READY" for v in statuses.values()) else (
        "BLOCKED" if any(v == "BLOCKED" for v in statuses.values()) else "NEEDS_REVIEW")
    return {"content_id": content_id, "topic": item["topic"],
            "generation_mode": item["generation_mode"], "model": item["model"],
            "artifacts": artifacts, "artifact_statuses": statuses,
            "overall_status": overall, "content_version": item.get("content_version")}


def list_artifacts(content_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _connect()
    try:
        if content_id:
            rows = con.execute("SELECT * FROM content_artifacts WHERE content_id=?"
                               " ORDER BY created_at DESC LIMIT ?",
                               (content_id, max(1, limit))).fetchall()
        else:
            rows = con.execute("SELECT * FROM content_artifacts ORDER BY created_at DESC"
                               " LIMIT ?", (max(1, limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def artifact_provenance(artifact_id: str) -> Dict[str, Any]:
    """artifact → content element → evidence → source chain."""
    _ensure_tables()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM content_artifacts WHERE artifact_id=?",
                          (artifact_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown artifact_id {artifact_id!r}")
        artifact = dict(row)
        item_row = con.execute("SELECT topic, evidence_refs, model, prompt_version, sources_digest"
                               " FROM content_items WHERE content_id=?",
                               (artifact["content_id"],)).fetchone()
    finally:
        con.close()
    if not item_row:
        raise ValueError(f"content row missing for {artifact['content_id']!r}")
    return {
        "artifact_id": artifact["artifact_id"],
        "artifact_type": artifact["artifact_type"],
        "artifact_version": artifact["artifact_version"],
        "checksum": artifact["checksum"],
        "status": artifact["status"],
        "topic": item_row["topic"],
        "content_id": artifact["content_id"],
        "prompt_version": item_row["prompt_version"],
        "model": item_row["model"],
        "sources_digest": item_row["sources_digest"],
        "evidence_refs": _json_load(item_row["evidence_refs"], []),
        "chain": "artifact → content_item → evidence_refs → P3/P4 sources",
    }


def content_consistency_report(content_id: str) -> Dict[str, Any]:
    """Verify rendered artifacts all draw on the same canonical facts."""
    _ensure_tables()
    item = get_content(content_id)
    if not item:
        raise ValueError(f"Unknown content_id {content_id!r}")
    fact_ids = {fid for fid, _, _ in _fact_lines(item["content"])}
    artifact_list = list_artifacts(content_id)
    per_artifact = []
    for a in artifact_list:
        path = Path(a["path"])
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        checksum_ok = hashlib.sha256(text.encode()).hexdigest() == a["checksum"] if text else None
        per_artifact.append({
            "artifact_id": a["artifact_id"], "artifact_type": a["artifact_type"],
            "checksum_current": checksum_ok,
            "validation": _json_load(a.get("validation"), {}),
        })
    return {
        "content_id": content_id, "facts_total": len(fact_ids),
        "artifacts": per_artifact,
        "consistent": all(x["checksum_current"] for x in per_artifact) if per_artifact else None,
        "note": "all artifacts render from one canonical fact list; no independent generation",
    }
