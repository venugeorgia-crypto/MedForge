"""MedForge utilities: filesystem, locking, hashing, time, chunking.

Imports ONLY from medforge.types (bottom of the dependency DAG).
"""

from __future__ import annotations

import fcntl
import functools
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Tuple, TypeVar

from medforge.types import BASE, APP, DOCS, PRODUCTS, DBDIR, CHROMA_DIR, TMP, LOGS

F = TypeVar("F", bound=Callable[..., Any])


def mkdirs() -> None:
    """Create all required MedForge directories."""
    for p in [BASE, APP, DOCS, PRODUCTS, DBDIR, CHROMA_DIR, TMP, LOGS]:
        p.mkdir(parents=True, exist_ok=True)


def sh(cmd: List[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess command."""
    kwargs: Dict[str, Any] = {"text": True, "check": check}
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return subprocess.run([str(x) for x in cmd], **kwargs)


def slugify(s: str) -> str:
    """Convert a string to a filesystem-safe slug."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", s.strip()).strip("-").lower()
    return (s[:70] or "topic")


def utcnow() -> str:
    """Current UTC time as ISO-8601 with Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_text(path: Path, text: str) -> None:
    """Atomically write text to a file (temp file + os.replace + fsync)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".medforge-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def job_lock() -> Iterator[None]:
    """Exclusive non-blocking file lock serializing long-running MedForge jobs."""
    mkdirs()
    with open(BASE / ".job.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another MedForge job is running. Let it finish, then try again.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def serialized(fn: F) -> F:
    """Decorator that serializes a function with the global job lock."""

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with job_lock():
            return fn(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


def chunks(text: str, max_words: int = 300, overlap: int = 50) -> List[str]:
    """Split text into overlapping word chunks (min 20 words each)."""
    words = re.sub(r"\s+", " ", text or "").strip().split()
    out: List[str] = []
    if not words:
        return out
    step = max(1, max_words - overlap)
    for i in range(0, len(words), step):
        piece = " ".join(words[i:i + max_words]).strip()
        if len(piece.split()) >= 20:
            out.append(piece)
    return out


def batch(items: List[Any], n: int = 10) -> Iterator[List[Any]]:
    """Yield successive n-sized batches from items."""
    for i in range(0, len(items), n):
        yield items[i:i + n]


__all__ = [
    "mkdirs", "sh", "slugify", "utcnow", "atomic_text", "job_lock", "serialized",
    "chunks", "batch",
]
