"""Unit tests for medforge.models module."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

from medforge.models import model_exists


def test_model_exists():
    """Test model existence checking with :latest suffix handling."""
    names = ["qwen3:4b-instruct", "embeddinggemma:latest", "llama3"]
    
    # Exact match
    assert model_exists("qwen3:4b-instruct", names) is True
    assert model_exists("embeddinggemma:latest", names) is True
    
    # With :latest added
    assert model_exists("embeddinggemma", names) is True  # Should match embeddinggemma:latest
    assert model_exists("llama3:latest", names) is True  # Should match llama3
    
    # Non-existent
    assert model_exists("nonexistent", names) is False
    assert model_exists("llama2", names) is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])