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
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from bandiradar.matching.llm import LLMClient

# --------------------------------------------------------------------------- #
# Self-healing spine — crawl recipes + drift detection + golden validation
# --------------------------------------------------------------------------- #
#
# The FRAGILE part of an LLM scraper is the CRAWL (the listing it depends on), not
# the extraction (the LLM adapts to HTML changes already). So the crawl is made a
# DATA recipe — validatable and replaceable — with a drift detector and a golden
# validator. A future agent can re-derive a broken recipe (slice 2); it must produce
# a recipe that passes ``recipe_reproduces_golden`` — the deterministic socket it
# plugs into and cannot bypass.

# One listing item -> (post_id, detail_url, listing_title).
DetailRef = tuple[Any, str, str]

# Crawl health: ok (all refs usable) / degraded (some empty) / broken (none usable).
Health = Literal["ok", "degraded", "broken"]


@dataclass(frozen=True)
class CrawlRecipe:
    """How to crawl a scraper's listing — DATA, not code, so it can be validated and
    swapped (e.g. re-derived by an agent) without touching the adapter.

    ``listing_url`` + ``params`` describe the request; the ``*_path`` fields are
    DOTTED paths into each listing item (e.g. ``"title.rendered"``) for the three
    fields the crawl needs. :func:`apply_recipe` reads them — no hardcoded parse.
    """

    listing_url: str
    params: dict[str, Any] = field(default_factory=dict)
    post_id_path: str = "id"
    detail_url_path: str = "link"
    title_path: str = "title.rendered"


def _dig(item: Any, path: str) -> Any:
    """Follow a dotted path into nested dicts; ``None`` if any step is missing."""
    cur = item
    for key in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def apply_recipe(recipe: CrawlRecipe, listing_json: Any) -> list[DetailRef]:
    """PURE: turn a listing JSON (a list of items) into DetailRefs per the recipe.

    Replaces the hardcoded listing parse. Tolerant: a missing field becomes ``""``
    (string) or ``None`` (post_id) so drift surfaces via :func:`validate_refs`
    rather than a crash."""
    if not isinstance(listing_json, list):
        return []
    refs: list[DetailRef] = []
    for item in listing_json:
        post_id = _dig(item, recipe.post_id_path)
        url = _dig(item, recipe.detail_url_path)
        title = _dig(item, recipe.title_path)
        refs.append(
            (
                post_id,
                str(url) if url is not None else "",
                str(title) if title is not None else "",
            )
        )
    return refs


def validate_refs(refs: list[DetailRef]) -> Health:
    """PURE drift detector. A ref is USABLE iff it has a non-empty url AND title.

    broken = no refs, or none usable (the listing shape/fields drifted);
    degraded = some usable, some empty; ok = all usable."""
    if not refs:
        return "broken"
    usable = sum(1 for (_pid, url, title) in refs if url.strip() and title.strip())
    if usable == 0:
        return "broken"
    if usable < len(refs):
        return "degraded"
    return "ok"


def recipe_reproduces_golden(
    recipe: CrawlRecipe,
    golden_listing: Any,
    expected_refs: list[DetailRef],
) -> bool:
    """PURE golden-sample validator: a candidate recipe is valid IFF it reproduces
    the known-good refs from a recorded golden listing. The gate an agent-derived
    recipe must pass before it can replace the current one (slice 2)."""
    return apply_recipe(recipe, golden_listing) == [tuple(r) for r in expected_refs]


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
