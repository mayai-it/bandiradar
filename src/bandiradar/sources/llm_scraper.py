"""Reusable LLM-assisted extraction for HTML bandi portals (no clean data API).

For regional portals whose bandi are HTML pages, an LLM reads each page and
extracts the canonical fields, so a new region configures only the *crawl*, not a
bespoke parser. The extraction is I/O (an LLM call), so it runs in a source's
``fetch()``; ``to_opportunities`` stays PURE over the recorded/extracted fields.

This module is generic — future scraper regions reuse :func:`extract_bando_fields`
and the cache protocol.
"""

from __future__ import annotations

import html
import re
from typing import Any, Protocol, runtime_checkable

from bandiradar.crawl import (  # the self-healing spine lives top-level; re-export here
    CrawlRecipe,
    DetailRef,
    Health,
    apply_recipe,
    recipe_reproduces_golden,
    validate_refs,
)
from bandiradar.matching.llm import LLMClient

__all__ = [
    "CrawlRecipe",
    "DetailRef",
    "Health",
    "apply_recipe",
    "recipe_reproduces_golden",
    "validate_refs",
    "extract_bando_fields",
    "html_to_text",
    "ExtractionCache",
    "InMemoryExtractionCache",
    "EXTRACTION_SYSTEM",
]

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG2_RE = re.compile(r"<[^>]+>")
_MAX_PAGE_CHARS = 12000  # bound the prompt; bando pages are mostly boilerplate

EXTRACTION_SYSTEM = (
    "You extract structured facts about an Italian public funding opportunity "
    "(bando/avviso) from the text of its web page. Use ONLY the page text; do not "
    "invent. Respond with STRICT JSON and nothing else, exactly this shape:\n"
    "{\n"
    '  "title": <string>,\n'
    '  "summary": <short string or null>,\n'
    '  "eligibility_text": <who can apply / requirements, a string or null>,\n'
    '  "value_amount": <total/!budget EUR as a number, or null>,\n'
    '  "value_min": <min grant/project EUR number or null>,\n'
    '  "value_max": <max grant/project EUR number or null>,\n'
    '  "deadline": <ISO date "YYYY-MM-DD" of the application deadline, or null>,\n'
    '  "keywords": [<sector/topic terms in Italian>],\n'
    '  "kind": <"incentive" or "tender">\n'
    "}\n"
    "Amounts are plain numbers (no currency symbols/thousands separators). If a "
    "field is absent, use null (or [] for keywords)."
)


@runtime_checkable
class ExtractionCache(Protocol):
    """Cache of extracted bando fields, keyed by detail URL."""

    def get(self, url: str) -> dict | None: ...

    def set(self, url: str, data: dict) -> None: ...


class InMemoryExtractionCache:
    """Default process-local extraction cache (SqliteExtractionCache persists)."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    def get(self, url: str) -> dict | None:
        return self._store.get(url)

    def set(self, url: str, data: dict) -> None:
        self._store[url] = data


def html_to_text(page_html: str) -> str:
    """Strip a detail page to readable text (drop script/style, collapse space)."""
    text = _TAG2_RE.sub(" ", _TAG_RE.sub(" ", page_html or ""))
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _as_float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.]", "", value.replace(",", "."))
        # collapse multiple dots (e.g. "1.000.000" -> drop thousands dots)
        if cleaned.count(".") > 1:
            cleaned = cleaned.replace(".", "")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_bando_fields(page_text: str, region: str, client: LLMClient) -> dict:
    """LLM-extract canonical bando fields from page text (tolerant of loose JSON).

    Returns a dict with title/summary/eligibility_text/value_*/deadline/keywords/
    kind. Never raises on a malformed model reply — missing fields become None/[].
    """
    user = (
        f"Region: {region}\n\nBando page text:\n{page_text[:_MAX_PAGE_CHARS]}\n\n"
        "Return ONLY the JSON object."
    )
    raw = client.score(EXTRACTION_SYSTEM, user)
    if not isinstance(raw, dict):
        raw = {}
    kind = str(raw.get("kind", "")).strip().lower()
    if kind not in ("incentive", "tender"):
        kind = "incentive"
    keywords = raw.get("keywords")
    keywords = [str(k) for k in keywords] if isinstance(keywords, list) else []
    deadline = _as_str(raw.get("deadline"))
    return {
        "title": _as_str(raw.get("title")),
        "summary": _as_str(raw.get("summary")),
        "eligibility_text": _as_str(raw.get("eligibility_text")),
        "value_amount": _as_float(raw.get("value_amount")),
        "value_min": _as_float(raw.get("value_min")),
        "value_max": _as_float(raw.get("value_max")),
        "deadline": deadline,  # ISO string or None; the mapper parses it
        "keywords": keywords,
        "kind": kind,
    }
