"""Unit tests for medforge.export module."""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

from medforge.export import make_anki, slugify, write_pdf


def test_slugify():
    """Test slugify function."""
    assert slugify("Cardiac Cycle") == "cardiac-cycle"
    assert slugify("  Hello World!  ") == "hello-world"
    assert slugify("Special@#$%Chars") == "special-chars"
    assert slugify("") == "topic"
    assert slugify("a" * 100) == "a" * 70


def test_make_anki():
    """Test Anki deck creation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "test.apkg"
        cards = [
            ("Q1", "A1", "[S1] Source 1"),
            ("Q2", "A2", "[S2] Source 2"),
        ]
        make_anki("Test Topic", cards, out)
        assert out.exists()
        assert out.stat().st_size > 0


def test_write_pdf():
    """Test PDF generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "test.pdf"
        text = """# Heading 1
        
Some body text with **bold** and *italic*.

## Heading 2

- Bullet point 1
- Bullet point 2

### Heading 3

More text."""
        write_pdf("Test Title", text, out)
        assert out.exists()
        assert out.stat().st_size > 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])