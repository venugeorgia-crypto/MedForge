"""MedForge ingestion: PDF processing, PubMed import, hardened web research.

All paths and the OFFLINE flag are read dynamically from medforge.types so
that the dashboard toggle and test fixtures take effect at call time.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
from urllib.parse import urlparse

import requests
import trafilatura

import medforge.types as T
from medforge.utils import utcnow, atomic_text, chunks, batch
from medforge.storage import upsert_records, get_collection
from medforge.textbook import registered_source_paths

__all__ = [
    "domain_quality", "ingest_pdfs", "pubmed_import", "web_research",
    "fetch_web_text", "ALLOWED_HOSTS",
]

# ─── Hardened Web Fetch Settings ───
ALLOWED_HOSTS: Set[str] = {
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "nih.gov",
    "medlineplus.gov", "fda.gov", "open.fda.gov", "cdc.gov",
    "who.int", "nice.org.uk", "ema.europa.eu", "ecdc.europa.eu",
    "nhs.uk", "cochrane.org", "bmj.com", "thelancet.com",
    "nejm.org", "jamanetwork.com", "nature.com", "sciencedirect.com",
    "wiley.com", "springer.com", "oxfordmedicine.com",
}
MAX_PAGE_SIZE = 2_000_000  # bytes
REQUEST_TIMEOUT = (5, 12)  # (connect, read) seconds
MAX_REDIRECTS = 5


def domain_quality(url: str) -> float:
    """Score a URL's trustworthiness based on its domain."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return 0.0
    host = (parsed.hostname or "").lower().removeprefix("www.")
    for domain, score in T.TRUSTED_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return score
    if host.endswith(".gov") or host.endswith(".edu"):
        return 0.86
    return 0.55


def ingest_pdfs() -> int:
    """Index PDFs from ~/MedForge/docs, re-indexing only changed files."""
    from pypdf import PdfReader

    files = sorted(p for p in T.DOCS.rglob("*") if p.suffix.lower() == ".pdf" and p.is_file())
    if not files:
        return 0
    # PDFs already ingested as registered textbooks (structural provenance,
    # chunk_count > 0) are skipped here so their content is not indexed twice
    # under two different id schemes. registered_source_paths() is defensive:
    # it returns an empty set when the V5 tables do not exist yet.
    try:
        registered = registered_source_paths()
        if registered:
            files = [p for p in files if str(p.resolve()) not in registered]
    except Exception:
        pass
    if not files:
        return 0
    cache_path = T.DBDIR / "pdf-index.json"
    try:
        cache: Dict[str, Any] = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    total = 0
    for pdf in files:
        key = str(pdf.relative_to(T.DOCS))
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        previous = cache.get(key, {})
        if previous.get("sha256") == digest and previous.get("ids"):
            continue
        try:
            reader = PdfReader(str(pdf))
        except Exception as e:
            print(f"  PDF skipped {pdf.name}: {e}")
            continue
        recs: List[Dict[str, Any]] = []
        for page_no, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            for idx, piece in enumerate(chunks(text), 1):
                rid = hashlib.sha256(f"pdf|{key}|{digest}|{page_no}|{idx}".encode()).hexdigest()
                recs.append({
                    "id": rid,
                    "text": piece,
                    "metadata": {
                        "source": key, "locator": f"page {page_no}",
                        "kind": "course_pdf", "url": "", "quality": 0.90,
                    },
                })
        if not recs:
            print(f"  No readable text in {key}; this PDF may need OCR.")
            continue
        total += upsert_records(recs)
        old_ids = previous.get("ids", [])
        # Retire records for this exact source that are no longer present.
        with sqlite3.connect(T.META_DB) as con:
            legacy = [r[0] for r in con.execute(
                "SELECT id FROM chunks WHERE kind='course_pdf' AND source=?", (key,)
            )]
        old_ids = sorted(set(old_ids + legacy) - {r["id"] for r in recs})
        if old_ids:
            get_collection().delete(ids=old_ids)
            with sqlite3.connect(T.META_DB) as con:
                con.executemany("DELETE FROM chunks WHERE id=?", [(x,) for x in old_ids])
                con.executemany("DELETE FROM chunks_fts WHERE id=?", [(x,) for x in old_ids])
        cache[key] = {"sha256": digest, "ids": [r["id"] for r in recs], "indexed_at": utcnow()}
        atomic_text(cache_path, json.dumps(cache, indent=2))
    return total


def pubmed_import(query: str, max_results: int = 8) -> int:
    """Import PubMed abstracts via NCBI E-utilities."""
    if T.OFFLINE:
        return 0
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    common: Dict[str, str] = {"tool": "MedForge"}
    if T.NCBI_EMAIL:
        common["email"] = T.NCBI_EMAIL
    r = requests.get(base + "/esearch.fcgi", params={
        **common, "db": "pubmed", "term": query, "retmode": "json",
        "retmax": max_results, "sort": "relevance",
    }, timeout=30)
    r.raise_for_status()
    ids = r.json().get("esearchresult", {}).get("idlist", [])
    if not ids:
        return 0
    time.sleep(0.4)
    r = requests.get(base + "/efetch.fcgi", params={
        **common, "db": "pubmed", "id": ",".join(ids), "retmode": "xml",
    }, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    recs: List[Dict[str, Any]] = []
    for art in root.findall(".//PubmedArticle"):
        pmid_el = art.find(".//PMID")
        pmid = "".join(pmid_el.itertext()).strip() if pmid_el is not None else "unknown"
        title_el = art.find(".//ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""
        date_text = " ".join(
            "".join(x.itertext()).strip()
            for x in art.findall(".//PubDate/*") if x is not None
        ).strip()
        abs_parts: List[str] = []
        for a in art.findall(".//Abstract/AbstractText"):
            t = "".join(a.itertext()).strip()
            if t:
                label = a.attrib.get("Label", "")
                abs_parts.append((label + ": " if label else "") + t)
        if not abs_parts:
            continue
        full = f"Title: {title}\nDate: {date_text}\nPMID: {pmid}\nAbstract: {' '.join(abs_parts)}"
        for idx, piece in enumerate(chunks(full, 280, 40), 1):
            recs.append({
                "id": hashlib.sha256(f"pubmed|{pmid}|{idx}".encode()).hexdigest(),
                "text": piece,
                "metadata": {
                    "source": f"PubMed PMID {pmid}",
                    "locator": title[:180], "kind": "pubmed",
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "quality": 0.96,
                },
            })
    if recs:
        upsert_records(recs)
    return len(recs)


def _resolve_and_validate(url: str) -> str:
    """Validate scheme, allowlist the host, and confirm every resolved IP is global.

    Raises ValueError before any request is made when the URL is unsafe.
    Returns the canonical lowercase host.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Only HTTPS URLs are allowed")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL has no hostname")
    if parsed.port not in (None, 443):
        raise ValueError("Only port 443 is allowed")
    host_lower = host.lower().removeprefix("www.")

    allowed = any(
        host_lower == d or host_lower.endswith("." + d) for d in ALLOWED_HOSTS
    )
    if not allowed:
        raise ValueError(f"Host {host_lower} is not in the trusted-domain allowlist")

    addresses = socket.getaddrinfo(host_lower, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError(f"DNS resolution failed for {host_lower}")
    for addr in addresses:
        ip = ipaddress.ip_address(addr[4][0])
        if not ip.is_global:
            raise ValueError(f"Host {host_lower} resolves to a non-global IP ({ip}); refusing to fetch")
    return host_lower


def fetch_web_text(url: str) -> Tuple[str, str]:
    """Fetch readable text from a validated URL with size/redirect limits.

    Every redirect hop is re-validated against the allowlist before fetching.
    Returns (extracted_text, final_url); text is empty when the page is unusable.
    """
    current_url = url
    for _ in range(MAX_REDIRECTS):
        _resolve_and_validate(current_url)
        with requests.get(
            current_url,
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=False,
            headers={"User-Agent": "MedForge/2.1 educational research"},
        ) as response:
            if response.is_redirect:
                location = response.headers.get("Location", "")
                if not location:
                    return "", current_url
                current_url = requests.compat.urljoin(current_url, location)
                continue
            response.raise_for_status()
            if "text/html" not in response.headers.get("content-type", ""):
                return "", current_url
            data = bytearray()
            for part in response.iter_content(65536):
                data.extend(part)
                if len(data) > MAX_PAGE_SIZE:
                    return "", current_url
            text = trafilatura.extract(
                bytes(data).decode(response.encoding or "utf-8", errors="replace"),
                url=current_url,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
            ) or ""
            return text, current_url
    return "", current_url


def web_research(query: str, max_results: int = 8) -> int:
    """Zero-key web research restricted to trusted medical domains."""
    if T.OFFLINE:
        return 0
    from ddgs import DDGS

    search_queries = [query + " medicine", query + " guideline", query + " review"]
    raw: List[Dict[str, str]] = []
    seen: Set[str] = set()
    ddgs = DDGS(timeout=12)
    for sq in search_queries:
        try:
            results = list(ddgs.text(sq, max_results=max_results))
        except Exception as e:
            print(f"  web search warning: {e}")
            continue
        for x in results:
            url = x.get("href") or x.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            raw.append({"title": x.get("title", ""), "url": url, "snippet": x.get("body", "")})

    raw = [x for x in raw if domain_quality(x["url"]) >= 0.85]
    raw.sort(key=lambda x: domain_quality(x["url"]), reverse=True)

    recs: List[Dict[str, Any]] = []
    for item in raw[:max_results]:
        url = item["url"]
        try:
            text, url = fetch_web_text(url)
        except Exception as e:
            print(f"  fetch rejected {url}: {e}")
            continue
        # A search snippet is a discovery hint, never evidence; require real page text.
        if len(text.split()) < 80:
            continue
        q = domain_quality(url)
        host = urlparse(url).netloc.lower().replace("www.", "")
        for idx, piece in enumerate(chunks(text, 240, 30)[:3], 1):
            recs.append({
                "id": hashlib.sha256(f"web|{url}|{idx}|{piece[:70]}".encode()).hexdigest(),
                "text": piece,
                "metadata": {
                    "source": item["title"] or host,
                    "locator": host + " (page retrieved " + utcnow() + ")",
                    "kind": "web",
                    "url": url,
                    "quality": q,
                },
            })
    if recs:
        upsert_records(recs)
    return len(recs)
