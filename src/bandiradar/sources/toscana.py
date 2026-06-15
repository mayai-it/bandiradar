"""Regione Toscana — LLM-assisted scraper (Sviluppo Toscana bandi).

Toscana's WP REST `bando` endpoint exposes detail-page links but **empty**
content (no API for the bando body/deadline). So this source is the first
LLM-assisted scraper: ``fetch()`` lists the detail URLs, fetches each HTML page,
and uses the LLM to extract the canonical fields (cached per URL). The extracted
records are what ``to_opportunities`` maps — keeping that mapping PURE and the
``--sample`` path fully offline (it reads recorded extractions, never the LLM).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandiradar import http, resources, trust
from bandiradar.matching.llm import LLMClient, client_status, get_client
from bandiradar.models import (
    Kind,
    Opportunity,
    RawDoc,
    default_status,
    sanitize_value_bounds,
)
from bandiradar.recipe_store import RecipeStore
from bandiradar.sources.base import ProgressFn, register
from bandiradar.sources.heal import heal_crawl
from bandiradar.sources.llm_scraper import (
    CrawlRecipe,
    DetailRef,
    ExtractionCache,
    Health,
    apply_recipe,
    extract_bando_fields,
    html_to_text,
    validate_refs,
)
from bandiradar.storage import SqliteExtractionCache, Store

logger = logging.getLogger(__name__)

SOURCE_ID = "toscana"
REGION = "Toscana"
ISSUER = "Sviluppo Toscana"
# WP REST listing (links + titles only; the body is scraped from each page).
TOSCANA_LIST_URL = "https://www.sviluppo.toscana.it/wp-json/wp/v2/bando"
_MAX_ITEMS = 20

# Default crawl recipe — the current Toscana values, now as DATA (validatable /
# replaceable) instead of a hardcoded parse. WP-REST item shape: {id, link,
# title:{rendered}}.
TOSCANA_RECIPE = CrawlRecipe(
    listing_url=TOSCANA_LIST_URL,
    params={"per_page": _MAX_ITEMS, "_fields": "id,link,title"},
    post_id_path="id",
    detail_url_path="link",
    title_path="title.rendered",
)

FIXTURE_PATH = resources.fixture("toscana.json")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def to_opportunities(raw: RawDoc, now: datetime | None = None) -> list[Opportunity]:
    """PURE map of one EXTRACTED bando record (``raw.payload``) to an Opportunity."""
    p: dict[str, Any] = raw.payload
    deadline = _parse_iso(p.get("deadline"))
    kind: Kind = "tender" if p.get("kind") == "tender" else "incentive"
    keywords = p.get("keywords")
    keywords = [str(k) for k in keywords] if isinstance(keywords, list) else []

    # The extracted sector keywords genuinely describe the bando, so fold them into
    # the matcher's text (the prefilter/heuristic read eligibility_text, not the
    # opportunity's keyword list).
    eligibility = " ".join(
        part for part in (p.get("eligibility_text"), " ".join(keywords)) if part
    ).strip()

    # LLM-extracted amounts can be transposed/garbage; sanitize the bounds so a
    # dirty extraction can't fail validation.
    value_min, value_max = sanitize_value_bounds(p.get("value_min"), p.get("value_max"))

    # Trust-spine provenance (see llm_scraper.LlmScraperSource.to_opportunities).
    report = p.get("_trust") if isinstance(p.get("_trust"), dict) else None

    return [
        Opportunity(
            id=f"{SOURCE_ID}:{p['_post_id']}",
            source=SOURCE_ID,
            source_url=p.get("_url") or "",
            kind=kind,
            title=p.get("title") or p.get("_listing_title") or str(p["_post_id"]),
            summary=p.get("summary"),
            issuer_name=ISSUER,
            issuer_region=REGION,
            cpv=[],
            keywords=keywords,
            value_amount=p.get("value_amount"),
            value_min=value_min,
            value_max=value_max,
            geo_scope="regional",
            region=REGION,
            deadline=deadline,
            status=default_status(deadline, now),
            eligibility_text=eligibility or None,
            raw_ref=raw.id,
            provenance="llm",
            confidence=report.get("confidence") if report else None,
            trust_verdict=report.get("verdict") if report else None,
        )
    ]


def load_fixture(path: Path | None = None) -> list[RawDoc]:
    """Read RECORDED extracted bando records into RawDocs (offline, no LLM)."""
    package = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    fetched_at = _parse_iso(package.get("_captured")) or datetime.fromisoformat(
        "1970-01-01T00:00:00+00:00"
    )
    return [
        RawDoc(
            id=f"{SOURCE_ID}:{rec['_post_id']}",
            source=SOURCE_ID,
            fetched_at=fetched_at,
            payload=rec,
            url=rec.get("_url"),
        )
        for rec in package.get("records", [])
    ]


class ToscanaSource:
    """LLM-assisted scraper for Sviluppo Toscana bandi."""

    id = SOURCE_ID
    kind: Kind = "incentive"
    # Live fetch needs an LLM provider+key (it extracts fields from HTML pages).
    # `doctor` reports "needs key" instead of probing when none is configured.
    requires_llm = True
    # Health of the LAST crawl (set by _list_details / crawl_health). The CRAWL is
    # key-less, so doctor can report it even without an LLM key.
    last_crawl_health: Health | None = None

    def _active_recipe(self, recipe_store: RecipeStore | None) -> CrawlRecipe:
        """The adopted override if present, else the baked default. Toscana's listing
        is JSON, so any adopted override is a CrawlRecipe (the store is generic)."""
        override = recipe_store.get_recipe(SOURCE_ID) if recipe_store else None
        if isinstance(override, CrawlRecipe):
            return override
        return TOSCANA_RECIPE

    def _listing_json(self, recipe: CrawlRecipe) -> Any:
        """Fetch the raw WP-REST listing JSON for a recipe (the key-less crawl)."""
        with http.client(follow_redirects=True) as client:
            # `or None`: an explicit empty params mapping would WIPE a query string
            # embedded in listing_url (the httpx params trap) — e.g. on a healed
            # recipe whose endpoint carries its filter in the URL itself.
            resp = http.with_retry(
                lambda: client.get(recipe.listing_url, params=recipe.params or None),
                what="Toscana listing",
            )
            http.raise_for_status(resp, what="Toscana listing")
            return resp.json()

    def _list_details(
        self,
        recipe_store: RecipeStore | None = None,
        client: LLMClient | None = None,
    ) -> list[DetailRef]:
        """Crawl via the active recipe. On a healthy crawl, snapshot the golden refs.
        On drift, log it and (with an LLM key + a golden) attempt a GATED self-heal —
        adopting a re-derived recipe only if the spine guard passes."""
        recipe = self._active_recipe(recipe_store)
        listing = self._listing_json(recipe)
        refs = apply_recipe(recipe, listing)
        self.last_crawl_health = validate_refs(refs)
        if self.last_crawl_health == "ok":
            if recipe_store is not None:
                recipe_store.set_golden(SOURCE_ID, refs)  # the next heal's golden
            return refs

        logger.warning(
            "toscana crawl health=%s (%d refs) — listing may have drifted",
            self.last_crawl_health,
            len(refs),
        )
        expected = recipe_store.get_golden(SOURCE_ID) if recipe_store else None
        if recipe_store is not None and client is not None and expected:
            result = heal_crawl(
                SOURCE_ID, listing, expected, recipe, client, recipe_store
            )
            logger.warning(
                "toscana self-heal: status=%s adopted=%s — %s",
                result.status,
                result.adopted,
                result.reason,
            )
            if result.adopted:
                healed = self._active_recipe(recipe_store)
                refs = apply_recipe(healed, listing)
                self.last_crawl_health = validate_refs(refs)
        return refs

    def crawl_health(self) -> Health:
        """Probe ONLY the crawl (active recipe -> listing -> drift), no LLM/heal. Lets
        ``doctor`` surface listing drift for this key-dependent source without a key."""
        store = Store(None)
        try:
            recipe = self._active_recipe(RecipeStore(store))
            return validate_refs(apply_recipe(recipe, self._listing_json(recipe)))
        finally:
            store.close()

    def _fetch_text(self, url: str) -> str:
        with http.client(follow_redirects=True) as client:
            resp = http.with_retry(
                lambda: client.get(url), what=f"Toscana detail {url}"
            )
            http.raise_for_status(resp, what=f"Toscana detail {url}")
            return html_to_text(resp.text)

    def fetch(
        self,
        since: datetime | None = None,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
        progress: ProgressFn | None = None,
        client: LLMClient | None = None,
        cache: ExtractionCache | None = None,
        store: Store | None = None,
        list_details=None,
        fetch_text=None,
        max_items: int = _MAX_ITEMS,
    ) -> Iterable[RawDoc]:
        """LIVE: list bando URLs, fetch each page, LLM-extract fields (cached per URL).

        Requires an LLM provider + key (live only). ``--sample`` uses
        ``load_fixture`` and never calls this. Yields LAZILY (one bando at a time).
        When ``store`` is given, the extraction cache AND recipe store bind to it (so
        ``--db`` controls all persistence); else a default Store is opened + owned.
        """
        client = client if client is not None else get_client()
        if client is None:
            # Honest reason: distinguishes "not configured" from "SDK not installed"
            # (e.g. uv sync without the anthropic extra), not always blaming the key.
            raise RuntimeError(
                f"LLM scraper has no usable LLM client: {client_status()}. "
                "Configure BANDIRADAR_LLM_PROVIDER + the API key (see .env.example), "
                "or use --sample to run offline against the recorded fixture."
            )
        cap = limit if limit is not None else max_items
        # Bind the extraction cache + recipe store to the caller's Store when given;
        # else open our OWN Store (and close it when the generator is done — avoids a
        # leaked SQLite connection).
        own_store = None
        if store is not None:
            cache = cache or SqliteExtractionCache(store)
            recipe_store = RecipeStore(store)
        elif cache is None:
            own_store = Store(None)
            cache = SqliteExtractionCache(own_store)
            recipe_store = RecipeStore(own_store)
        else:
            recipe_store = None
        list_details = list_details or (
            lambda: self._list_details(recipe_store, client)
        )
        fetch_text = fetch_text or self._fetch_text

        return self._scrape(
            client, cache, list_details, fetch_text, cap, progress, own_store
        )

    def _scrape(
        self,
        client,
        cache,
        list_details,
        fetch_text,
        max_items,
        progress,
        own_store=None,
    ) -> Iterator[RawDoc]:
        try:
            count = 0
            for post_id, url, listing_title in list_details()[:max_items]:
                record = cache.get(url)
                report = cache.get_trust(url)
                if record is None:
                    page_text = fetch_text(url)
                    record = extract_bando_fields(page_text, REGION, client)
                    cache.set(url, record)
                    report = trust.assess(record, page_text).model_dump()
                    cache.set_trust(url, report)
                elif report is None:
                    # Legacy cache row (pre-trust): backfill with one page fetch.
                    report = trust.assess(record, fetch_text(url)).model_dump()
                    cache.set_trust(url, report)
                payload = {
                    **record,
                    "_post_id": post_id,
                    "_url": url,
                    "_listing_title": listing_title,
                    "_trust": report,
                }
                yield RawDoc(
                    id=f"{SOURCE_ID}:{post_id}",
                    source=SOURCE_ID,
                    fetched_at=datetime.now(tz=UTC),
                    payload=payload,
                    url=url,
                )
                count += 1
                if progress is not None:
                    progress(f"toscana: {count} fetched")
        finally:
            if own_store is not None:
                own_store.close()

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)

    def load_fixture(self) -> list[RawDoc]:
        return load_fixture()


register(ToscanaSource())
