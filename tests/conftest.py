"""Pytest configuration for MedForge tests."""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# `current/` hosts the medforge package and medforge_core; the project root
# hosts the `core` (V3 database schema) package. Both must be importable.
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / "current")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test requiring Ollama"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )
