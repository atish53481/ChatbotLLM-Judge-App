"""Context for a reply whose chatbot response lists no sources of its own.

A target's optional `sources_fallback` picks where it comes from:
- "cited_links": the pages the reply links to, fetched as plain text;
- "knowledge_base": the passages of the theme's uploaded documents that best
  match the question and the reply (BM25 keyword ranking, no embeddings, so
  sentence-transformers stays unloaded).

Neither is the chatbot's own retrieval: the judge scores whether the answer is
backed by those pages or documents, so metrics that grade a retriever
(MetricSpec.grades_retriever) never use them.
"""
from __future__ import annotations

import ipaddress
import math
import re
import socket
import threading
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from backend.rag.enhance import extract_text, split_sections
from backend.rag.fetch import MAX_BYTES, TIMEOUT, html_to_text

MODES = ("", "cited_links", "knowledge_base")
SOURCE_LABELS = {"cited_links": "cited_pages", "knowledge_base": "knowledge_base"}
NOTES = {
    "cited_pages": (
        "Context came from the pages the chatbot's replies link to (fetched by the judge app), "
        "not from the chatbot's own retrieval."
    ),
    "knowledge_base": (
        "Context came from your knowledge base, the documents uploaded for this theme (the passages that best match "
        "each question and reply), not from the chatbot's own retrieval: the score says whether "
        "the answers are backed by your documents."
    ),
}
NOTHING_FOUND = {
    "cited_pages": "The reply links to no readable public page, so there is no source to score.",
    "knowledge_base": "No passage of this theme's uploaded documents matches this question and reply.",
}

DOCUMENTS_DIR = Path(__file__).resolve().parent.parent / "datasets" / "documents"
MAX_LINKS = 3
MAX_REDIRECTS = 3
MAX_PAGE_CHARS = 4000
MAX_PASSAGES = 3
CHUNK_CHARS = 1200
_DOCUMENT_SUFFIXES = {".pdf", ".txt", ".docx", ".md", ".markdown", ".mdx"}
_URL = re.compile(r"https?://[^\s<>()\[\]{}\"'`]+")
_STOPWORDS = frozenset(
    "a an the is are was were be been do does did i you me my your we our it its of to in on "
    "for at by with and or if can could would should will how what when where which who why "
    "this that there their have has had not no yes please hi hello thanks thank about get".split()
)


# --- cited links ----------------------------------------------------------------
def cited_urls(reply: str) -> list[str]:
    """The http(s) links in a reply, in order, without repeats, at most MAX_LINKS."""
    urls: list[str] = []
    for match in _URL.findall(reply or ""):
        url = match.rstrip(".,;:!?")
        if url not in urls:
            urls.append(url)
    return urls[:MAX_LINKS]


def _public_host(host: str) -> bool:
    """Whether every address the host resolves to is on the public internet, so
    a reply cannot make the app fetch the judge's own API or a private network."""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


def _allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname) and _public_host(parsed.hostname)


def _page_text(url: str) -> str | None:
    """A public page's readable text, or None; redirects are followed by hand
    so each hop is checked."""
    for _ in range(MAX_REDIRECTS + 1):
        if not _allowed(url):
            return None
        try:
            response = requests.get(url, timeout=TIMEOUT, stream=True, allow_redirects=False,
                                    headers={"User-Agent": "LLM-Judge source check"})
        except requests.RequestException:
            return None
        if response.is_redirect:
            url = urljoin(url, response.headers.get("Location", ""))
            continue
        if not response.ok:
            return None
        content_type = response.headers.get("Content-Type", "").lower()
        if not content_type.startswith(("text/html", "text/plain", "application/xhtml")):
            return None
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                return None
        raw = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        text = raw if content_type.startswith("text/plain") else html_to_text(raw)
        return text[:MAX_PAGE_CHARS].strip() or None
    return None


def cited_pages(reply: str) -> list[str]:
    """The text of each public page the reply links to, headed by its address."""
    pages = []
    for url in cited_urls(reply):
        text = _page_text(url)
        if text:
            pages.append(f"Source: {url}\n{text}")
    return pages


# --- knowledge base ---------------------------------------------------------------
def _terms(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9$][a-z0-9$\-]*", (text or "").lower())
    return [w.rstrip("s") if len(w) > 3 else w for w in words if w not in _STOPWORDS]


# Chunks per document, kept while the file is unchanged: {path: (mtime, [(text, terms)])}.
_CHUNKS: dict[str, tuple[float, list[tuple[str, list[str]]]]] = {}
_chunks_lock = threading.Lock()


def _documents(theme: str) -> list[Path]:
    """The theme's documents; one rewritten by Enhance is read as its _enhanced.txt."""
    folder = DOCUMENTS_DIR / theme
    if not theme or not folder.is_dir() or folder.resolve().parent != DOCUMENTS_DIR.resolve():
        return []
    files = [p for p in sorted(folder.iterdir()) if p.is_file() and p.suffix.lower() in _DOCUMENT_SUFFIXES]
    enhanced = {p.name for p in files if p.stem.endswith("_enhanced")}
    return [p for p in files if p.stem.endswith("_enhanced") or f"{p.stem}_enhanced.txt" not in enhanced]


def _document_chunks(path: Path) -> list[tuple[str, list[str]]]:
    mtime = path.stat().st_mtime
    with _chunks_lock:
        cached = _CHUNKS.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        text = extract_text(path)
    except Exception:  # noqa: BLE001 - an unreadable file is left out, not a run error
        text = ""
    chunks = [(f"[{path.name}]\n{c.strip()}", _terms(c)) for c in split_sections(text, CHUNK_CHARS) if c.strip()]
    with _chunks_lock:
        _CHUNKS[str(path)] = (mtime, chunks)
    return chunks


def knowledge_base(theme: str, question: str, reply: str, k: int = MAX_PASSAGES) -> list[str]:
    """The k passages of the theme's documents that best match the question and
    reply (BM25); none when no passage shares a word with them."""
    chunks = [c for path in _documents(theme) for c in _document_chunks(path)]
    query = set(_terms(f"{question} {reply}"))
    if not chunks or not query:
        return []
    avg_len = sum(len(t) for _, t in chunks) / len(chunks) or 1
    frequency = Counter(term for _, terms in chunks for term in set(terms))
    k1, b = 1.5, 0.75
    scored = []
    for text, terms in chunks:
        counts = Counter(terms)
        score = 0.0
        for term in query & counts.keys():
            idf = math.log(1 + (len(chunks) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            tf = counts[term]
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(terms) / avg_len))
        if score > 0:
            scored.append((score, text))
    scored.sort(key=lambda pair: -pair[0])
    return [text for _, text in scored[:k]]


# --- entry point ----------------------------------------------------------------
def derive(mode: str, theme: str, question: str, reply: str) -> tuple[list[str], str | None]:
    """(passages, context_source label) for a target's sources_fallback mode."""
    if mode == "cited_links":
        return cited_pages(reply), SOURCE_LABELS[mode]
    if mode == "knowledge_base":
        return knowledge_base(theme, question, reply), SOURCE_LABELS[mode]
    return [], None
