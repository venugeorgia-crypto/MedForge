"""MedForge product pipeline: build_product, ask, study, status, state management."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import medforge.types as T
from medforge.utils import mkdirs, slugify, utcnow, atomic_text, job_lock, serialized
from medforge.storage import init_db
from medforge.learner import start_session, import_flashcards, due_items
from medforge.ingestion import ingest_pdfs, pubmed_import, web_research
from medforge.retrieval import source_pack
from medforge.generation import generate_text, citation_audit, parse_tsv_cards, parse_cards_lenient
from medforge.export import write_pdf, make_anki
from medforge.models import ensure_models, stop_model, ollama_alive, model_names

__all__ = ["product_dir", "save_state", "build_product", "ask", "study", "status"]


def product_dir(topic: str) -> Tuple[Path, Dict[str, Any]]:
    """Return the resumable product directory and its state for a topic."""
    root = T.PRODUCTS / slugify(topic)
    root.mkdir(parents=True, exist_ok=True)
    versions = sorted(
        [p for p in root.glob("v*") if p.is_dir() and re.fullmatch(r"v\d+", p.name)],
        key=lambda p: int(p.name[1:]),
    )
    if versions:
        latest = versions[-1]
        sf = latest / "state.json"
        if sf.exists():
            try:
                state = json.loads(sf.read_text())
                if state.get("topic", "").strip().casefold() != topic.strip().casefold():
                    root = T.PRODUCTS / (slugify(topic) + "-" + hashlib.sha256(topic.encode()).hexdigest()[:8])
                    root.mkdir(parents=True, exist_ok=True)
                    versions = sorted(
                        [p for p in root.glob("v*") if p.is_dir() and re.fullmatch(r"v\d+", p.name)],
                        key=lambda p: int(p.name[1:]),
                    )
                    latest = versions[-1] if versions else None
                    state = json.loads((latest / "state.json").read_text()) if latest else {}
                if state.get("pipeline_version") == T.VERSION and not state.get("complete") and latest:
                    return latest, state
            except Exception:
                pass
    n = (int(versions[-1].name[1:]) + 1) if versions else 1
    out = root / f"v{n:03d}"
    out.mkdir()
    state = {
        "topic": topic, "version": n, "created_at": utcnow(), "steps": {},
        "complete": False, "pipeline_version": T.VERSION,
        "publication_status": "DRAFT_REQUIRES_REVIEW",
    }
    atomic_text(out / "state.json", json.dumps(state, indent=2))
    return out, state


def save_state(out: Path, state: Dict[str, Any], step: str, value: Any = True) -> None:
    """Record a completed pipeline step in the product state file."""
    state["steps"][step] = value
    atomic_text(out / "state.json", json.dumps(state, indent=2))


GENERATION_TASKS: Dict[str, str] = {
    "study-guide.md": """Create a high-yield study guide. Start with a single # title line, then:
# Big picture
# Core mechanism
# Anatomy/physiology/pathology/pharmacology connections when supported
# Exam traps
# Common confusions
# 10 retrieval questions
# Five-minute recap
Citation rules:
- end EVERY paragraph, bullet and correction line with at least one [S#] label on the same line
- retrieval questions: append the label of the evidence each question tests
- never put a citation on a line by itself
End with: Educational study aid — verify important claims against the cited sources.""",

    "quiz.md": """Create 20 medical-school MCQs. For each item use exactly this format:
**Question:** <question text> [S#]
**A)** <choice>
**B)** <choice>
**C)** <choice>
**D)** <choice>
**Correct Answer:** <letter> [S#]
**Rationale:** <one sentence> [S#]
Citation rules:
- the [S#] label must appear on the Question, Correct Answer and Rationale lines (same line, not a separate one)
- answer choices A-D do not need labels
Do not invent facts outside the evidence.""",

    "viva.md": """Create 15 viva/oral-exam questions with concise model answers and citations.
Order from recall -> mechanism -> application.
Citation rules: end every question line and every model-answer line with its [S#] label; never put a citation on a line by itself.""",

    "case-exercises.md": """Create 5 fictional educational mini-cases that test this topic.
Do not give patient-specific advice. Each case needs questions, teaching answer, and citations.
Citation rules: end each question, teaching-answer line and discussion bullet with [S#] labels on the same line.""",

    "cheat-sheet.md": """Create a one-page-style cheat sheet with:
definition, mechanism chain, must-know facts, red-flag confusions, and quick recall prompts.
Keep it extremely compact and cited.
Citation rules: end every factual line (definition, chain, each bullet, each confusion, each recall prompt) with [S#] on the same line.""",

    "mindmap.md": """Create a clean text mind map using indented bullets. Center = topic.
Branches should cover core concepts, mechanisms, related systems, and exam-relevant links.
Cite factual branch statements.
Citation rules: append [S#] to each factual branch line itself (not to a parent line); never put a citation on a line by itself.""",

    "short-script.txt": """Write a 45-60 second faceless educational video script (110-140 words).
Use HOOK / BODY / RECAP. Keep spoken text natural. Put citations at the end of each factual line.
Citation rules: every factual spoken line ends with [S#] on the same line; never put a citation on a line by itself.""",

    "explainer-3min.txt": """Write a ~3-minute faceless explainer script with a strong opening,
clear mechanism, one memorable analogy, exam relevance, and final recap. Cite factual lines.
Citation rules: every factual line ends with [S#] on the same line; never put a citation on a line by itself.""",

    "instagram-carousel.md": """Create an 8-slide educational carousel:
Slide 1 hook, Slides 2-7 teaching, Slide 8 recap. Add a concise caption and source labels.
Use a markdown heading for each slide title (e.g. ## Slide 2: Anatomy), then body lines.
Citation rules: end every body line (bullets included) with its [S#] on the same line; never put a citation on its own line inside a slide.""",

    "product-description.md": """Write a factual store-ready description for this educational study pack.
Describe what is included, who it is for, and clearly state it is an educational aid, not medical advice.
Do not make guaranteed-results claims.
Citation rules: end factual lines with [S#] labels on the same line.""",
}


@serialized
def build_product(topic: str, include_web: bool = True, open_folder: bool = True) -> Path:
    """Full product pipeline: ingest → retrieve → generate → export → audit."""
    mkdirs()
    init_db()
    print("=" * 72)
    print("MEDFORGE V2.1 — ONE-COMMAND PRODUCT")
    print("Topic:", topic)
    print("=" * 72)

    out, state = product_dir(topic)
    print("  Product folder:", out, flush=True)
    if shutil.disk_usage(T.BASE).free < 500 * 1024 ** 2:
        raise RuntimeError("Less than 500 MB free disk space; free some space before generating.")
    model = ensure_models()
    state["chat_model"] = model
    save_state(out, state, "preflight")
    print(f"✓ AI ready: {model}")

    # ─── Ingestion ───
    if not state["steps"].get("source_snapshot") and not state["steps"].get("pdfs"):
        n = ingest_pdfs()
        save_state(out, state, "pdfs", {"chunks": n})
        print(f"✓ PDFs: {n} chunks")

    if not state["steps"].get("source_snapshot") and not state["steps"].get("pubmed") and not T.OFFLINE:
        try:
            n = pubmed_import(topic, 8)
        except Exception as e:
            n = 0
            print("! PubMed unavailable:", e)
        if n:
            save_state(out, state, "pubmed", {"chunks": n})
        print(f"✓ PubMed: {n} chunks")

    if include_web and not state["steps"].get("source_snapshot") and not state["steps"].get("web") and not T.OFFLINE:
        try:
            n = web_research(topic, 8)
        except Exception as e:
            n = 0
            print("! Web research unavailable:", e)
        if n:
            save_state(out, state, "web", {"chunks": n})
        print(f"✓ Web: {n} chunks")

    # ─── Source snapshot ───
    if state["steps"].get("source_snapshot"):
        source_json = (out / "source-map.json").read_text(encoding="utf-8")
        source_text = (out / "source-text.txt").read_text(encoding="utf-8")
        digest = hashlib.sha256((source_json + source_text).encode()).hexdigest()
        if state["steps"]["source_snapshot"] != digest:
            raise RuntimeError("This product's saved evidence has changed; resume stopped to preserve its citation mapping.")
        sources = json.loads(source_json)
    else:
        source_text, sources = source_pack(topic, 6)
        if not sources:
            raise RuntimeError("No relevant evidence could be retrieved. Add a relevant PDF or reconnect, then rerun the same command.")
        source_json = json.dumps(sources, indent=2, ensure_ascii=False)
        atomic_text(out / "source-map.json", source_json)
        atomic_text(out / "source-text.txt", source_text)
        save_state(out, state, "source_snapshot",
                   hashlib.sha256((source_json + source_text).encode()).hexdigest())

    # Free RAM before generation on an 8GB machine.
    stop_model(T.EMBED_MODEL)

    generated: Dict[str, str] = {}
    for fname, task in GENERATION_TASKS.items():
        if (
            state["steps"].get(fname)
            and (out / fname).is_file()
            and hashlib.sha256((out / fname).read_bytes()).hexdigest() == state["steps"][fname]
        ):
            text = (out / fname).read_text(encoding="utf-8")
        else:
            print("→", fname)
            text = generate_text(model, topic, source_text, task)
            atomic_text(out / fname, text)
            state["steps"].pop("pdfs_output", None)
            save_state(out, state, fname, hashlib.sha256(text.encode()).hexdigest())
        generated[fname] = text

    # ─── Flashcards ───
    if not state["steps"].get("flashcards") or not all(
        (out / f).is_file() for f in ["flashcards.csv", "flashcards.apkg"]
    ):
        print("→ flashcards + Anki deck")
        flashcard_task = """Return exactly 30 flashcards, one per line, as three columns
separated by actual tab characters: Question [tab] Answer [tab] SourceLabels.
Never write the text <TAB>. No markdown, no header row.
Keep each card atomic and concise."""
        flashcard_example = (
            "Output format example (use a real TAB character between columns):\n"
            "What hormones does the anterior pituitary secrete?\t"
            "GH, TSH, ACTH, FSH and LH, and prolactin.\t[S2]"
        )

        def extract_cards(raw: str) -> List[Tuple[str, str, str]]:
            # Strict TSV first; lenient prose fallback for small local models.
            return parse_tsv_cards(raw) or parse_cards_lenient(raw)

        raw = generate_text(model, topic, source_text, flashcard_task)
        cards = extract_cards(raw)
        if not cards:
            # One retry with an example-anchored prompt before giving up.
            print("  flashcard parse empty; retrying with example format...")
            raw = generate_text(model, topic, source_text,
                                flashcard_task + "\n\n" + flashcard_example)
            cards = extract_cards(raw)
        labels = {s["label"] for s in sources}
        cards = [
            (q, a, s) for q, a, s in cards
            if set(re.findall(r"\[(S\d+)\]", q + " " + a + " " + s)) <= labels
        ]
        if not cards:
            raise RuntimeError("Flashcard generation could not be parsed; rerun product to resume.")
        with open(out / "flashcards.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Question", "Answer", "Sources"])
            w.writerows(cards)
        expanded = [
            (q, a, s + " | " + "; ".join(
                f"[{ref['label']}] {ref['source']} — {ref['locator']} {ref.get('url', '')}"
                for ref in sources if f"[{ref['label']}]" in s
            ))
            for q, a, s in cards
        ]
        make_anki(topic, expanded, out / "flashcards.apkg")
        save_state(out, state, "flashcards", {"count": len(cards)})

    # ─── Spaced repetition scheduling ───
    # Runs even on resumed builds where the cards step was already complete.
    try:
        sr = import_flashcards(topic, out / "flashcards.csv")
        print(f"✓ Spaced repetition: {sr['imported']} new, {sr['scheduled']} cards scheduled")
    except Exception as e:
        print("! Spaced repetition import skipped:", e)

    # ─── PDFs ───
    if not state["steps"].get("pdfs_output") or not all(
        (out / f).is_file() for f in ["study-guide.pdf", "workbook.pdf", "cheat-sheet.pdf"]
    ):
        print("→ PDFs")
        reference_text = "\n\n# References\n" + "\n".join(
            f"[{s['label']}] {s['source']} — {s['locator']} {s.get('url', '')}" for s in sources
        )
        write_pdf(f"MedForge Study Guide — {topic}",
                  generated["study-guide.md"] + reference_text, out / "study-guide.pdf")
        workbook = "\n\n".join([
            generated["study-guide.md"],
            "\n# Practice Questions\n" + generated["quiz.md"],
            "\n# Viva\n" + generated["viva.md"],
            "\n# Case Exercises\n" + generated["case-exercises.md"],
        ])
        write_pdf(f"MedForge Workbook — {topic}",
                  workbook + reference_text, out / "workbook.pdf")
        write_pdf(f"MedForge Cheat Sheet — {topic}",
                  generated["cheat-sheet.md"] + reference_text, out / "cheat-sheet.pdf")
        save_state(out, state, "pdfs_output")

    # ─── References ───
    refs = ["# Sources\n"]
    for s in sources:
        line = f"- [{s['label']}] {s['source']} — {s['locator']}"
        if s.get("url"):
            line += f" — {s['url']}"
        refs.append(line)
    (out / "references.md").write_text("\n".join(refs), encoding="utf-8")

    # ─── Citation audit ───
    with open(out / "flashcards.csv", newline="", encoding="utf-8") as f:
        generated["flashcards.csv"] = "\n".join(" | ".join(row) for row in list(csv.reader(f))[1:])
    cited, total = citation_audit(generated, sources, out)
    save_state(out, state, "evidence_audit", {"cited_lines": cited, "total_lines": total})

    state["complete"] = True
    state["finished_at"] = utcnow()
    atomic_text(out / "state.json", json.dumps(state, indent=2))

    print("\n✓ DRAFT PACK COMPLETE — SOURCE REVIEW REQUIRED")
    print("  Folder:", out)
    print(f"  Label scan: {cited}/{total} scanned lines have valid source labels (not an accuracy score)")
    print("  Review evidence-report.html before publishing medical material.")
    if open_folder and sys.platform == "darwin":
        subprocess.run(["open", str(out)], check=False)
    return out


@serialized
def ask(topic_or_question: str) -> str:
    """Answer a question as a Socratic tutor using the evidence library."""
    model = ensure_models()
    pack, sources = source_pack(topic_or_question, 8)
    if not sources:
        return "No evidence in the local knowledge base yet."
    stop_model(T.EMBED_MODEL)
    return generate_text(model, topic_or_question, pack,
                         "Answer the question as a Socratic medical tutor. Explain mechanism, then ask 3 retrieval questions.")


def _study_task(topic: str, due_cards: List[Dict[str, Any]]) -> str:
    """Study-mission prompt, anchored on due flashcards when the queue has any."""
    task = (
        "Create a 20-minute interactive-style study mission:\n"
        "1) 3-minute blind recall prompt\n"
        "2) 7-minute mechanism/build task\n"
        "3) 5 escalating Socratic questions\n"
        "4) one fictional clinical application question\n"
        "5) 5 flash review prompts\n"
        "6) a final self-score rubric out of 10."
    )
    questions = [
        f"- [{c.get('topic_id', '')}] {c['question']}"
        for c in due_cards[:15] if (c.get("question") or "").strip()
    ]
    if questions:
        task += (
            "\n\nOpen the mission with a 'Due card recall' round on these cards from"
            " the learner's spaced repetition queue. Ask the questions only — never"
            " print their answers, and reveal them one at a time:"
            "\n" + "\n".join(questions)
            + "\n\nThen continue with the standard mission structure."
        )
    return task


@serialized
def study(topic: str) -> str:
    """Generate a 20-minute interactive study mission for a topic.

    When the spaced repetition queue has due cards, the mission opens with a
    blind-recall round on them (this topic's cards first, then up to 10 others).
    """
    model = ensure_models()
    pack, sources = source_pack(topic, 8)
    if not sources:
        try:
            pubmed_import(topic, 5)
        except Exception:
            pass
        try:
            web_research(topic, 5)
        except Exception:
            pass
        pack, sources = source_pack(topic, 8)
    # Due flashcards first so the mission starts from real recall practice.
    try:
        due = due_items(topic=topic, limit=15) or due_items(limit=10)
    except Exception as e:
        due = []
        print("! Due-card lookup unavailable:", e)
    stop_model(T.EMBED_MODEL)
    mission = generate_text(model, topic, pack, _study_task(topic, due))
    # Open a V3 session row so the self-score can complete it later
    # (log_study_result). Tracking failures never block the mission itself.
    try:
        start_session(topic, session_type="Learn",
                      notes=f"20-minute study mission ({len(due)} due cards)")
    except Exception as e:
        print("! Study session tracking unavailable:", e)
    return mission


def status() -> Dict[str, Any]:
    """Collect system status for the CLI and dashboard."""
    mkdirs()
    init_db()
    con = sqlite3.connect(T.META_DB)
    try:
        n = con.execute("select count(*) from chunks").fetchone()[0]
    finally:
        con.close()
    return {
        "version": T.VERSION,
        "base": str(T.BASE),
        "chunks": n,
        "pdfs": len(list(T.DOCS.glob("*.pdf"))),
        "products": len(list(T.PRODUCTS.glob("*/*"))),
        "ollama": ollama_alive(),
        "models": model_names() if ollama_alive() else [],
    }
