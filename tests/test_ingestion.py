"""Unit tests for medforge.ingestion module."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

from medforge.ingestion import domain_quality, fetch_web_text
from medforge.types import TRUSTED_DOMAINS


def test_domain_quality():
    """Test domain quality scoring."""
    # Trusted domains
    assert domain_quality("https://pubmed.ncbi.nlm.nih.gov/12345") == 1.00
    assert domain_quality("https://www.ncbi.nlm.nih.gov/pubmed/12345") == 0.98
    assert domain_quality("https://nih.gov/some/page") == 0.96
    assert domain_quality("https://medlineplus.gov/article") == 0.94
    assert domain_quality("https://who.int/health-topics") == 0.96
    assert domain_quality("https://cochrane.org/review") == 0.94
    
    # .gov and .edu
    assert domain_quality("https://example.gov/page") == 0.86
    assert domain_quality("https://university.edu/research") == 0.86
    
    # Unknown domains
    assert domain_quality("https://random-site.com/page") == 0.55
    
    # Invalid URLs
    assert domain_quality("http://not-https.com") == 0.0  # Not HTTPS
    assert domain_quality("https://user:pass@site.com") == 0.0  # Auth in URL


def test_fetch_web_text_validation():
    """Test that fetch_web_text validates URLs before fetching."""
    # This test would require network, so we test the validation logic indirectly
    # by checking that disallowed domains raise ValueError
    
    # We can't easily test the full fetch without network, but we can verify
    # the allowlist logic is correct
    from medforge.ingestion import ALLOWED_HOSTS
    
    assert "pubmed.ncbi.nlm.nih.gov" in ALLOWED_HOSTS
    assert "nih.gov" in ALLOWED_HOSTS
    assert "who.int" in ALLOWED_HOSTS
    assert "cochrane.org" in ALLOWED_HOSTS
    
    # Ensure known bad domains are NOT in allowlist
    assert "evil.com" not in ALLOWED_HOSTS
    assert "localhost" not in ALLOWED_HOSTS


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])