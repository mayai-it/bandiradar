"""Regione Calabria — calabriaeuropa.regione.calabria.it, LLM scraper.

Recon (2026-06-12): the institutional ``regione.calabria.it`` (WP) keeps its REST
API permission-locked, but **calabriaeuropa** (the PR 2021-2027 portal, WordPress)
exposes its ``bando`` custom post type over WP-REST (``/wp-json/wp/v2/bando`` is
open while generic ``posts`` are 401). The REST records carry only id/link/title
(empty ``content``/ACF), so the JSON listing seeds the crawl and the LLM extracts
the canonical fields from each rich, server-rendered detail page (Beneficiari,
Dotazione Finanziaria, scadenze — ~17k chars of text). LLM scraper over
:class:`~bandiradar.sources.llm_scraper.LlmScraperSource` with a JSON listing
(same situation as ``toscana``).
"""

from __future__ import annotations

import html
import re
from typing import Any

from bandiradar import http
from bandiradar.sources.base import register
from bandiradar.sources.llm_scraper import CrawlRecipe, DetailRef, LlmScraperSource

SOURCE_ID = "calabria"
CALABRIA_BASE_URL = "https://calabriaeuropa.regione.calabria.it"
CALABRIA_LISTING_URL = f"{CALABRIA_BASE_URL}/wp-json/wp/v2/bando"

# The crawl as DATA (validatable / re-derivable) — WP-REST item shape is
# {id, link, title:{rendered}}, identical to toscana, so the self-healing spine
# applies. ``_fields`` trims the live payload to just what the crawl reads.
CALABRIA_RECIPE = CrawlRecipe(
    listing_url=CALABRIA_LISTING_URL,
    params={
        "per_page": 20,
        "orderby": "date",
        "order": "desc",
        "_fields": "id,link,title",
    },
    post_id_path="id",
    detail_url_path="link",
    title_path="title.rendered",
)

_TAG_RE = re.compile(r"<[^>]+>")


def parse_listing(items: Any) -> list[DetailRef]:
    """PURE: WP-REST ``bando`` JSON (a list of posts) -> DetailRefs.

    Tolerant: a malformed item yields empty url/title so drift surfaces via
    ``validate_refs`` instead of a crash.
    """
    if not isinstance(items, list):
        return []
    refs: list[DetailRef] = []
    seen: set = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        post_id = item.get("id")
        if post_id in seen:
            continue
        seen.add(post_id)
        title_raw = (item.get("title") or {}).get("rendered", "")
        title = html.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", title_raw))).strip()
        refs.append((post_id, str(item.get("link") or ""), title))
    return refs


class CalabriaSource(LlmScraperSource):
    """LLM scraper for the Calabria Europa bandi (PR 2021-2027 portal)."""

    id = SOURCE_ID
    region = "Calabria"
    issuer_name = "Regione Calabria — Calabria Europa"
    listing_url = f"{CALABRIA_LISTING_URL}?per_page=20&orderby=date&order=desc"
    # JSON listing -> opt into the self-healing crawl (recipe + golden + heal).
    default_recipe = CALABRIA_RECIPE

    def _listing_json(self, recipe: CrawlRecipe):
        with http.client(follow_redirects=True) as client:
            resp = http.with_retry(
                lambda: client.get(recipe.listing_url, params=recipe.params or None),
                what="Calabria bando listing",
            )
            http.raise_for_status(resp, what="Calabria bando listing")
            return resp.json()


SOURCE = CalabriaSource()

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
