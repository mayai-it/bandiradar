"""Regione Basilicata — portalebandi.regione.basilicata.it, LLM scraper.

Recon (2026-06-12): the institutional ``regione.basilicata.it`` (WP) has no bandi
type, but the region runs a DEDICATED **portalebandi** (WordPress) whose
``avvisi-e-bandi`` custom post type is open over WP-REST (generic ``posts`` is an
empty collection; the CPT carries the data). The REST records hold only a short
teaser, while the server-rendered detail pages are STRUCTURED (Tipo, Ente,
**Destinatari: IMPRESA**, Importo, "Giorni alla scadenza") → the JSON listing
seeds the crawl and the LLM extracts the canonical fields per page. The portal
publishes ALL regional avvisi (also aste/concessioni/selections) — the extraction
classifies each and the matcher's gates do the rest. LLM scraper over
:class:`~bandiradar.sources.llm_scraper.LlmScraperSource`.
"""

from __future__ import annotations

import html
import re
from typing import Any

from bandiradar import http
from bandiradar.sources.base import register
from bandiradar.sources.llm_scraper import CrawlRecipe, DetailRef, LlmScraperSource

SOURCE_ID = "basilicata"
BASILICATA_BASE_URL = "https://portalebandi.regione.basilicata.it"
BASILICATA_LISTING_URL = f"{BASILICATA_BASE_URL}/wp-json/wp/v2/avvisi-e-bandi"

# The crawl as DATA (validatable / re-derivable) — WP-REST item shape
# {id, link, title:{rendered}}, identical to toscana, so the self-healing spine
# applies. ``_fields`` trims the live payload to just what the crawl reads.
BASILICATA_RECIPE = CrawlRecipe(
    listing_url=BASILICATA_LISTING_URL,
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
    """PURE: WP-REST ``avvisi-e-bandi`` JSON (a list of posts) -> DetailRefs.

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


class BasilicataSource(LlmScraperSource):
    """LLM scraper for the Basilicata portalebandi avvisi."""

    id = SOURCE_ID
    region = "Basilicata"
    issuer_name = "Regione Basilicata"
    listing_url = f"{BASILICATA_LISTING_URL}?per_page=20&orderby=date&order=desc"
    # JSON listing -> opt into the self-healing crawl (recipe + golden + heal).
    default_recipe = BASILICATA_RECIPE

    def _listing_json(self, recipe: CrawlRecipe):
        with http.client(follow_redirects=True) as client:
            resp = http.with_retry(
                lambda: client.get(recipe.listing_url, params=recipe.params or None),
                what="Basilicata avvisi listing",
            )
            http.raise_for_status(resp, what="Basilicata avvisi listing")
            return resp.json()


SOURCE = BasilicataSource()

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
