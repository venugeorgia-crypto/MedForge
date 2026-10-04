"""MedForge generation: LLM text generation, citation auditing, flashcard parsing."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from medforge.types import SYSTEM_EVIDENCE
from medforge.models import chat
from medforge.utils import atomic_text


def generate_text(model: str, topic: str, sources: str, task: str, temp: float = 0.12) -> str:
    """Generate text with the chat model, grounding it in the supplied evidence."""
    if not sources.strip():
        raise RuntimeError("No usable evidence. Add a relevant PDF or enable research, then retry.")
    prompt = f"""TOPIC: {topic}

EVIDENCE:
{sources}

TASK:
{task}

Important:
- end EVERY line that states a medical fact with one or more [S#] labels — paragraphs, bullets, corrections, answers and captions each count as a line
- attach labels to the line they support; a label alone on its own line does not cite the lines above it
- in quizzes, label the question line, the correct-answer line and the rationale; answer choices A-D do not need labels
- stay within the evidence
- concise, clear medical-student language
"""
    return chat(model, prompt, SYSTEM_EVIDENCE, temp)


def citation_audit(texts: Dict[str, str], sources: List[Dict[str, Any]], outdir: Path) -> Tuple[int, int]:
    """Scan generated texts for citation-label validity and write evidence reports."""
    citation_re = re.compile(r"\[S(\d+)\]")
    labels = {int(s["label"][1:]) for s in sources}
    flagged: List[str] = []
    total = 0
    cited = 0
    for fname, text in texts.items():
        for line in text.splitlines():
            s = line.strip()
            if len(s) < 20 or s.startswith("#") or s.startswith("```"):
                continue
            total += 1
            found = {int(x) for x in citation_re.findall(s)}
            valid = found & labels
            unknown = found - labels
            if valid and not unknown:
                cited += 1
            else:
                reason = "unknown source label" if unknown else "no source label"
                flagged.append(f"{fname} [{reason}]: {s}")

    (outdir / "unsupported-claims.txt").write_text(
        "Citation-label scan only; semantic support has NOT been verified.\n"
        + ("\n".join(flagged) if flagged else "No label-format issues detected. Human source review is still required."),
        encoding="utf-8",
    )
    ratio = (cited / total * 100) if total else 0
    atomic_text(outdir / "evidence-report.json", json.dumps({
        "status": "DRAFT_REQUIRES_REVIEW",
        "semantic_support": "not_verified",
        "cited_lines": cited,
        "scanned_lines": total,
        "label_issues": flagged,
    }, indent=2))
    report = f"""<!doctype html><meta charset="utf-8"><title>MedForge Evidence Report</title>
    <style>body{{font-family:system-ui;max-width:900px;margin:40px auto;padding:0 20px}}
    .ok{{color:#087f23}} .warn{{color:#b45309}} code{{background:#eee;padding:2px 5px}}</style>
    <h1>MedForge Evidence Report</h1>
    <p><b>DRAFT — REQUIRES SOURCE REVIEW</b></p>
    <p><b>Citation-label coverage:</b> {cited}/{total} scanned lines ({ratio:.1f}%). This is not an accuracy score.</p>
    <p class="warn">This scan checks label syntax, including bullet points and flashcards. It does not identify every medical claim or verify source support.
    Verify important claims against the linked primary/authoritative source before publication.</p>
    <h2>Sources</h2><ol>
    {''.join(f"<li><b>{html.escape(s['source'])}</b> — {html.escape(s['locator'])} "
             + (f"<a href='{html.escape(s.get('url', ''))}'>source</a>" if s.get('url') else "")
             + f" (retrieval weight {s.get('quality', 0):.2f})</li>" for s in sources)}
    </ol>
    <h2>Uncited lines</h2><pre>{html.escape(chr(10).join(flagged[:200]) or "None detected")}</pre>
    """
    (outdir / "evidence-report.html").write_text(report, encoding="utf-8")
    return cited, total


def parse_tsv_cards(raw: str) -> List[Tuple[str, str, str]]:
    """Parse TSV flashcards (Question/Answer/SourceLabels).

    Accepts the canonical three-column layout, plus the variants small models
    actually emit: the literal text "<TAB>" as the separator, and two columns
    with the source labels at the end of the answer. Rows without any valid
    [S#] label are dropped.
    """
    cards: List[Tuple[str, str, str]] = []
    normalized = re.sub(r"<\s*TAB\s*>", "\t", raw or "", flags=re.IGNORECASE)
    for line in normalized.splitlines():
        p = [x.strip() for x in line.split("\t")]
        if not p or not p[0] or p[0].lower() in {"question", "front"}:
            continue  # header or empty row
        if len(p) == 3 and p[1] and re.search(r"\[S\d+\]", p[2]):
            cards.append((p[0], p[1], p[2]))
        elif len(p) == 2 and p[1]:
            labels = re.findall(r"\[S\d+\]", p[1])
            answer = re.sub(r"(\s*\[S\d+\])+\s*$", "", p[1]).strip()
            if labels and answer:
                cards.append((p[0], answer, " ".join(dict.fromkeys(labels))))
    return cards


def parse_cards_lenient(raw: str) -> List[Tuple[str, str, str]]:
    """Lenient flashcard parser for small local models that ignore TSV.

    Handles the common prose format emitted by 4B-class models, e.g.::

        Question:What is X? Answer:It is Y. SourceLabels[S2]

    Blocks are separated by blank lines; markdown emphasis characters are
    stripped first. A card is kept only when a question, an answer, and at
    least one [S#] source label are all present (citation integrity is
    preserved: every label found in the block is carried into the Sources
    field). Returns an empty list when nothing parseable is found.
    """
    q_re = re.compile(r"Question\s*[:\-]\s*(.+)", re.IGNORECASE | re.DOTALL)
    a_re = re.compile(r"Answer\s*[:\-]\s*(.+)$", re.IGNORECASE | re.DOTALL)
    labels_re = re.compile(r"\[(S\d+)\]", re.IGNORECASE)
    trailing_labels_re = re.compile(r"\s*Source\s*Labels\s*:?\s*$", re.IGNORECASE)

    cards: List[Tuple[str, str, str]] = []
    for block in re.split(r"\n\s*\n", raw or ""):
        block = block.replace("*", "").replace("_", " ").strip()
        if not block:
            continue
        qm = q_re.search(block)
        if not qm:
            continue
        question = " ".join(qm.group(1).split())
        am = a_re.search(block)
        if not am:
            continue
        answer = " ".join(am.group(1).split())
        answer = trailing_labels_re.sub("", answer).strip()
        labels = sorted(
            {m.group(1).upper() for m in labels_re.finditer(block)},
            key=lambda x: int(x[1:]),
        )
        if not question or not answer or not labels:
            continue
        if question.lower().rstrip(":") in {"question", "front"}:
            continue  # header row
        cards.append((question, answer, " ".join(f"[{l}]" for l in labels)))
    return cards


__all__ = ["generate_text", "citation_audit", "parse_tsv_cards"]
