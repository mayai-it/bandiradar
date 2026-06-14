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
import json
import logging
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from bandiradar import http, resources, trust
from bandiradar.crawl import (  # the self-healing spine lives top-level; re-export here
    CrawlRecipe,
    DetailRef,
    Health,
    apply_recipe,
    recipe_reproduces_golden,
    validate_refs,
)
from bandiradar.matching.llm import LLMClient
from bandiradar.models import (
    Opportunity,
    RawDoc,
    default_status,
    sanitize_value_bounds,
)
from bandiradar.sources.base import ProgressFn

logger = logging.getLogger(__name__)

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
    "LlmScraperSource",
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
    """Cache of extracted bando fields (+ their trust report), keyed by detail URL."""

    def get(self, url: str) -> dict | None: ...

    def set(self, url: str, data: dict) -> None: ...

    def get_trust(self, url: str) -> dict | None: ...

    def set_trust(self, url: str, report: dict) -> None: ...


class InMemoryExtractionCache:
    """Default process-local extraction cache (SqliteExtractionCache persists)."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._trust: dict[str, dict] = {}

    def get(self, url: str) -> dict | None:
        return self._store.get(url)

    def set(self, url: str, data: dict) -> None:
        self._store[url] = data
        self._trust.pop(url, None)  # a re-extraction invalidates the old report

    def get_trust(self, url: str) -> dict | None:
        return self._trust.get(url)

    def set_trust(self, url: str, report: dict) -> None:
        self._trust[url] = report


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


# --------------------------------------------------------------------------- #
# Reusable LLM-scraper SOURCE base (HTML listing + LLM detail extraction)
# --------------------------------------------------------------------------- #


class LlmScraperSource:
    """Base for a regional LLM-scraper source over an HTML portal.

    A subclass provides only the LISTING, in one of two flavours, plus its identity
    config (``id``/``region``/``issuer_name``/listing URL). Everything else is shared:
    per-URL extraction cache, LLM field extraction (:func:`extract_bando_fields`), the
    generic record→Opportunity mapper, the fixture loader, and crawl-health tracking.

    - **JSON listing** (e.g. a WP-REST portal): set ``default_recipe`` to a
      ``CrawlRecipe`` and implement :meth:`_listing_json`. The listing is DATA-parsed
      via ``apply_recipe`` (dotted paths), so on drift the LLM recipe-healer can
      re-derive the paths and a candidate is adopted ONLY if it reproduces the golden
      exactly — the SAME gated self-heal as ``toscana``.
    - **HTML listing**: leave ``default_recipe`` unset and implement
      :meth:`_listing_refs` (a pure, offline-testable parse). The parse is bespoke
      code, not re-derivable DATA, so on drift the crawl is DETECTED as broken and
      flagged for a human (``last_crawl_health`` / ``crawl_health()`` surface it in
      doctor and the monitor), never auto-healed and never silently ignored.
    """

    # Subclass identity (set as class attrs in the concrete source).
    id: str = ""
    region: str = ""
    issuer_name: str = ""
    listing_url: str = ""  # informational + the doctor probe target

    kind = "incentive"  # registry hint; per-record kind comes from the extraction
    requires_llm = True  # live fetch extracts fields with an LLM
    last_crawl_health: Health | None = None
    _MAX_ITEMS = 20

    # A JSON-listing subclass (e.g. a WP-REST portal) sets this to a CrawlRecipe to
    # opt INTO the self-healing crawl: the listing is DATA-parsed via ``apply_recipe``
    # (dotted paths), so on drift the LLM recipe-healer can re-derive the paths and a
    # candidate is adopted ONLY if it reproduces the golden exactly. Left ``None`` for
    # an HTML-listing subclass (the parse is bespoke code) — drift is DETECTED and
    # flagged for a human, never auto-healed (the parse is not re-derivable DATA).
    default_recipe: CrawlRecipe | None = None

    # ---- subclass hooks ---------------------------------------------------- #

    def _listing_refs(self) -> list[DetailRef]:
        """HTML path: fetch the listing page(s) and parse them into DetailRefs
        (I/O + pure parse). HTML subclasses implement this; the parse itself must be
        a pure, offline-testable function over the HTML."""
        raise NotImplementedError

    def _listing_json(self, recipe: CrawlRecipe) -> Any:
        """Recipe path: fetch the listing JSON for ``recipe`` (url + params).
        JSON-listing subclasses (those that set ``default_recipe``) implement this."""
        raise NotImplementedError

    def _active_recipe(self, recipe_store) -> CrawlRecipe | None:
        """The adopted override if present, else the baked ``default_recipe``."""
        override = recipe_store.get_recipe(self.id) if recipe_store else None
        return override or self.default_recipe

    # ---- shared plumbing ---------------------------------------------------- #

    def _fixture_path(self) -> Path:
        return resources.fixture(f"{self.id}.json")

    def _fetch_text(self, url: str) -> str:
        with http.client(follow_redirects=True) as client:
            resp = http.with_retry(lambda: client.get(url), what=f"{self.id} {url}")
            http.raise_for_status(resp, what=f"{self.id} {url}")
            return html_to_text(resp.text)

    def _list_details(self, recipe_store=None, client=None) -> list[DetailRef]:
        """Crawl the listing; snapshot the golden on health, act on drift.

        Recipe path (``default_recipe`` set, JSON listing): DATA-parse via
        ``apply_recipe``; on drift, with an LLM client + a golden, attempt a GATED
        self-heal (mirrors ``toscana``) — adopt a re-derived recipe only if the spine
        guard passes. HTML path (no recipe): pure-code parse; drift is DETECTED and
        flagged for a human, never auto-healed."""
        if self.default_recipe is not None:
            return self._list_details_recipe(recipe_store, client)
        refs = self._listing_refs()
        self.last_crawl_health = validate_refs(refs)
        if self.last_crawl_health == "ok":
            if recipe_store is not None:
                recipe_store.set_golden(self.id, refs)
            return refs
        logger.warning(
            "%s crawl health=%s (%d refs) — the HTML listing may have drifted; "
            "no auto-heal for an HTML parse, human review needed",
            self.id,
            self.last_crawl_health,
            len(refs),
        )
        # Keep whatever is usable; the drift stays visible via crawl health.
        return [r for r in refs if r[1].strip() and r[2].strip()]

    def _list_details_recipe(self, recipe_store, client) -> list[DetailRef]:
        """JSON-listing crawl via the active CrawlRecipe + gated self-heal on drift."""
        from bandiradar.sources.heal import heal_crawl

        recipe = self._active_recipe(recipe_store)
        listing = self._listing_json(recipe)
        refs = apply_recipe(recipe, listing)
        self.last_crawl_health = validate_refs(refs)
        if self.last_crawl_health == "ok":
            if recipe_store is not None:
                recipe_store.set_golden(self.id, refs)  # the next heal's golden
            return refs
        logger.warning(
            "%s crawl health=%s (%d refs) — JSON listing may have drifted",
            self.id,
            self.last_crawl_health,
            len(refs),
        )
        expected = recipe_store.get_golden(self.id) if recipe_store else None
        if recipe_store is not None and client is not None and expected:
            result = heal_crawl(
                self.id, listing, expected, recipe, client, recipe_store
            )
            logger.warning(
                "%s self-heal: status=%s adopted=%s — %s",
                self.id,
                result.status,
                result.adopted,
                result.reason,
            )
            if result.adopted:
                refs = apply_recipe(self._active_recipe(recipe_store), listing)
                self.last_crawl_health = validate_refs(refs)
        return refs

    def crawl_health(self) -> Health:
        """Key-less crawl probe for ``doctor`` (listing reachable + parseable).
        Uses the active recipe for a JSON listing, else the HTML parse — no heal."""
        if self.default_recipe is not None:
            from bandiradar.recipe_store import RecipeStore
            from bandiradar.storage import Store

            store = Store(None)
            try:
                recipe = self._active_recipe(RecipeStore(store))
                return validate_refs(apply_recipe(recipe, self._listing_json(recipe)))
            finally:
                store.close()
        return validate_refs(self._listing_refs())

    def fetch(
        self,
        since: datetime | None = None,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
        progress: ProgressFn | None = None,
        client: LLMClient | None = None,
        cache: ExtractionCache | None = None,
    ) -> Iterable[RawDoc]:
        """LIVE: list detail URLs, fetch each page, LLM-extract (cached per URL)."""
        from bandiradar.matching.llm import client_status, get_client

        client = client if client is not None else get_client()
        if client is None:
            raise RuntimeError(
                f"LLM scraper has no usable LLM client: {client_status()}. "
                "Configure BANDIRADAR_LLM_PROVIDER + the API key (see .env.example), "
                "or use --sample to run offline against the recorded fixture."
            )
        from bandiradar.recipe_store import RecipeStore
        from bandiradar.storage import SqliteExtractionCache, Store

        cap = limit if limit is not None else self._MAX_ITEMS
        own_store = Store(None) if cache is None else None
        if cache is None:
            cache = SqliteExtractionCache(own_store)
        recipe_store = RecipeStore(own_store) if own_store is not None else None
        return self._scrape(client, cache, recipe_store, cap, progress, own_store)

    def _scrape(
        self, client, cache, recipe_store, max_items, progress, own_store=None
    ) -> Iterator[RawDoc]:
        try:
            count = 0
            for post_id, url, listing_title in self._list_details(recipe_store, client)[
                :max_items
            ]:
                record = cache.get(url)
                report = cache.get_trust(url)
                if record is None:
                    page_text = self._fetch_text(url)
                    record = extract_bando_fields(page_text, self.region, client)
                    cache.set(url, record)
                    report = trust.assess(record, page_text).model_dump()
                    cache.set_trust(url, report)
                elif report is None:
                    # Legacy cache row (pre-trust): backfill the report ONCE —
                    # one page fetch, the LLM extraction is never re-paid.
                    report = trust.assess(record, self._fetch_text(url)).model_dump()
                    cache.set_trust(url, report)
                payload = {
                    **record,
                    "_post_id": post_id,
                    "_url": url,
                    "_listing_title": listing_title,
                    "_trust": report,
                }
                yield RawDoc(
                    id=f"{self.id}:{post_id}",
                    source=self.id,
                    fetched_at=datetime.now(tz=UTC),
                    payload=payload,
                    url=url,
                )
                count += 1
                if progress is not None:
                    progress(f"{self.id}: {count} fetched")
        finally:
            if own_store is not None:
                own_store.close()

    # ---- pure mapping + fixture --------------------------------------------- #

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        """PURE map of one EXTRACTED bando record (``raw.payload``)."""
        p: dict[str, Any] = raw.payload
        deadline = _parse_iso_date(p.get("deadline"))
        kind = (
            p.get("kind") if p.get("kind") in ("incentive", "tender") else ("incentive")
        )
        keywords = p.get("keywords")
        keywords = [str(k) for k in keywords] if isinstance(keywords, list) else []
        eligibility = " ".join(
            part for part in (p.get("eligibility_text"), " ".join(keywords)) if part
        ).strip()
        value_min, value_max = sanitize_value_bounds(
            p.get("value_min"), p.get("value_max")
        )
        # Trust-spine provenance: these fields came from an LLM extraction; the
        # deterministic report (when recorded) rides along as confidence/verdict.
        report = p.get("_trust") if isinstance(p.get("_trust"), dict) else None
        return [
            Opportunity(
                id=f"{self.id}:{p['_post_id']}",
                source=self.id,
                source_url=p.get("_url") or "",
                kind=kind,
                title=p.get("title") or p.get("_listing_title") or str(p["_post_id"]),
                summary=p.get("summary"),
                issuer_name=self.issuer_name,
                issuer_region=self.region,
                cpv=[],
                keywords=keywords,
                value_amount=p.get("value_amount"),
                value_min=value_min,
                value_max=value_max,
                geo_scope="regional",
                region=self.region,
                deadline=deadline,
                status=default_status(deadline, now),
                eligibility_text=eligibility or None,
                raw_ref=raw.id,
                provenance="llm",
                confidence=report.get("confidence") if report else None,
                trust_verdict=report.get("verdict") if report else None,
            )
        ]

    def load_fixture(self, path: Path | None = None) -> list[RawDoc]:
        """Read RECORDED extracted records into RawDocs (offline, no LLM)."""
        package = json.loads((path or self._fixture_path()).read_text(encoding="utf-8"))
        fetched_at = _parse_iso_date(package.get("_captured")) or datetime(
            1970, 1, 1, tzinfo=UTC
        )
        return [
            RawDoc(
                id=f"{self.id}:{rec['_post_id']}",
                source=self.id,
                fetched_at=fetched_at,
                payload=rec,
                url=rec.get("_url"),
            )
            for rec in package.get("records", [])
        ]


def _parse_iso_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
