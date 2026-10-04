"""MedForge export: PDF generation (reportlab) and Anki deck creation (genanki)."""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path
from typing import List, Tuple

from medforge.utils import slugify

__all__ = ["write_pdf", "make_anki", "slugify"]


def write_pdf(title: str, text: str, path: Path) -> None:
    """Write a styled PDF document via reportlab."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    styles = getSampleStyleSheet()
    font = "Helvetica"
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            if "MedForgeBody" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("MedForgeBody", candidate))
            font = "MedForgeBody"
            break
    for style in styles.byName.values():
        style.fontName = font
    body = ParagraphStyle("Body", parent=styles["BodyText"], leading=14, spaceAfter=5)
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
    )
    story = [
        Paragraph(html.escape(title), styles["Title"]),
        Spacer(1, 8),
        Paragraph("Educational draft - source review required", styles["BodyText"]),
        Spacer(1, 10),
    ]
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            story.append(Spacer(1, 5))
            continue
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        if line.startswith("### "):
            story.append(Paragraph(html.escape(line[4:]), styles["Heading3"]))
        elif line.startswith("## "):
            story.append(Paragraph(html.escape(line[3:]), styles["Heading2"]))
        elif line.startswith("# "):
            story.append(Paragraph(html.escape(line[2:]), styles["Heading1"]))
        elif line.startswith("- "):
            story.append(Paragraph("• " + html.escape(line[2:]), body))
        else:
            story.append(Paragraph(html.escape(line), body))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.drawString(18 * mm, 9 * mm, "MedForge | Draft for source review")
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, str(document.page))
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def make_anki(topic: str, cards: List[Tuple[str, str, str]], out: Path) -> None:
    """Create an Anki .apkg deck from (question, answer, sources) triples."""
    import genanki

    model_id = int(hashlib.sha1(b"medforge-model-v2").hexdigest()[:8], 16)
    deck_id = int(hashlib.sha1(f"medforge-{topic}".encode()).hexdigest()[:8], 16)
    note_model = genanki.Model(
        model_id, "MedForge Evidence Card",
        fields=[{"name": "Question"}, {"name": "Answer"}, {"name": "Sources"}],
        templates=[{
            "name": "Card 1",
            "qfmt": "{{Question}}",
            "afmt": "{{FrontSide}}<hr id=answer>{{Answer}}<br><br><small>{{Sources}}</small>",
        }],
        css=".card { font-family: Arial; font-size: 22px; text-align:left; padding:18px; }",
    )
    deck = genanki.Deck(deck_id, f"MedForge::{topic}")
    for q, a, s in cards:
        deck.add_note(genanki.Note(
            model=note_model,
            fields=[html.escape(x).replace("\n", "<br>") for x in [q, a, s]],
            tags=["MedForge", slugify(topic), "NeedsReview"],
        ))
    genanki.Package(deck).write_to_file(str(out))
