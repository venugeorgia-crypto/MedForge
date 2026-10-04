"""Unit tests for medforge.generation module."""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

from medforge.generation import citation_audit, parse_tsv_cards, parse_cards_lenient


def test_parse_tsv_cards():
    """Test TSV flashcard parsing."""
    raw = "What is the cardiac cycle?\tThe sequence of systole and diastole\t[S1]\nWhat happens during systole?\tVentricular contraction\t[S1] [S2]"
    cards = parse_tsv_cards(raw)
    assert len(cards) == 2
    assert cards[0][0] == "What is the cardiac cycle?"
    assert cards[0][1] == "The sequence of systole and diastole"
    assert cards[0][2] == "[S1]"
    assert cards[1][2] == "[S1] [S2]"

    # Test invalid rows are skipped
    raw_bad = "Question\tAnswer\tSources\nHeader row\t\t\nValid question\tValid answer\t[S1]"
    cards = parse_tsv_cards(raw_bad)
    assert len(cards) == 1
    assert cards[0][0] == "Valid question"

    # Test missing source labels
    raw_no_labels = "Question\tAnswer\tNo labels here"
    cards = parse_tsv_cards(raw_no_labels)
    assert len(cards) == 0


def test_parse_tsv_cards_two_column_and_literal_tab_variants():
    """Small models emit two columns (labels inside the answer) or the literal
    text '<TAB>'; both must parse with citation labels preserved."""
    two_col = (
        "What causes acromegaly?\tA pituitary adenoma. [S3][S4]\n"
        "No labels on this row\tplain answer"
    )
    assert parse_tsv_cards(two_col) == [
        ("What causes acromegaly?", "A pituitary adenoma.", "[S3] [S4]")
    ]

    literal = "Question<TAB>Answer<TAB>SourceLabels\nWhat is X?<TAB>It is Y. [S1]"
    assert parse_tsv_cards(literal) == [("What is X?", "It is Y.", "[S1]")]

    # Canonical three-column rows still keep labels out of the answer text
    assert parse_tsv_cards("Q?\tA text\t[S2] [S3]") == [("Q?", "A text", "[S2] [S3]")]


def test_parse_cards_lenient_prose_format():
    """Lenient parser handles the prose format small models actually emit."""
    raw = (
        "Question:What is the location of the pituitary gland? Answer:The pituitary gland is "
        "located in the bony sella turcica at the base of the brain. SourceLabels[S2]\n\n"
        "Question:What are the two main anatomical regions? Answer:The anterior lobe and the "
        "posterior lobe. SourceLabels[S2]\n\n"
        "Question:This one has no label? Answer:It must be dropped.\n"
    )
    cards = parse_cards_lenient(raw)
    assert len(cards) == 2
    assert cards[0][0].startswith("What is the location")
    assert "sella turcica" in cards[0][1]
    assert cards[0][2] == "[S2]"
    assert cards[1][2] == "[S2]"


def test_parse_cards_lenient_collects_all_labels_and_skips_headers():
    """Labels anywhere in a block are collected; headers are skipped."""
    raw = (
        "Question:Which hormones include GH [S2] and TSH [S3]? Answer:The anterior "
        "pituitary secretes them. SourceLabels[S4]\n\n"
        "Question Answer SourceLabels\n"
    )
    cards = parse_cards_lenient(raw)
    assert len(cards) == 1
    assert cards[0][2] == "[S2] [S3] [S4]"

    # Strict TSV still parses its own format
    tsv = "Q?\tA.\t[S1]"
    assert parse_tsv_cards(tsv) == [("Q?", "A.", "[S1]")]
    # Both parsers return [] on garbage
    assert parse_cards_lenient("no cards here at all") == []


def test_citation_audit():
    """Test citation audit report generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        outdir = Path(tmpdir)
        
        texts = {
            "study-guide.md": "# Guide\n\nThe heart has four chambers. [S1]\nThis is an uncited claim.\nAnother claim [S2].",
            "quiz.md": "Q1: What? A: Answer [S1]",
        }
        sources = [
            {"label": "S1", "source": "Source 1", "locator": "page 1", "url": "http://example.com/1", "quality": 0.9},
            {"label": "S2", "source": "Source 2", "locator": "page 2", "url": "", "quality": 0.8},
        ]
        
        cited, total = citation_audit(texts, sources, outdir)
        
        # Check reports exist
        assert (outdir / "unsupported-claims.txt").exists()
        assert (outdir / "evidence-report.json").exists()
        assert (outdir / "evidence-report.html").exists()
        
        # Check counts: 3 lines scanned (2 from study-guide, 1 from quiz)
        # study-guide: line 1 has [S1] - valid, line 2 no label, line 3 has [S2] - valid
        # quiz: line 1 has [S1] - valid
        # Total scanned = 3 (short lines filtered out)
        # Actually lines with len < 20 are filtered
        assert cited >= 2  # At least the valid citations
        assert total >= 3


def test_citation_audit_unknown_label():
    """Test citation audit catches unknown labels."""
    with tempfile.TemporaryDirectory() as tmpdir:
        outdir = Path(tmpdir)
        
        texts = {
            "test.md": "Claim with unknown label [S99].",
        }
        sources = [
            {"label": "S1", "source": "Source 1", "locator": "page 1"},
        ]
        
        cited, total = citation_audit(texts, sources, outdir)
        
        claims = (outdir / "unsupported-claims.txt").read_text()
        assert "unknown source label" in claims
        assert "S99" in claims


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])