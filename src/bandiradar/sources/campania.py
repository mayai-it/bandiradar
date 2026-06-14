"""Regione Campania — sviluppocampania.it (in-house agency), LLM scraper.

Recon (2026-06-12): the FESR portal (``fesr.regione.campania.it``) drops
datacenter IPs INCLUDING the EU-pinned relay (500) — unusable from CI. The main
``www.regione.campania.it`` has no bandi listing (bandi appear as generic news).
The viable surface is **Sviluppo Campania** (the region's in-house company):
WordPress with the REST API auth-locked (401, restx plugin) and a broken
``/feed`` (500), BUT a SERVER-RENDERED ``/bandi-aperti/`` page whose curated
media-image widget boxes link the currently-OPEN business bandi (~6: Fondo
Regionale Crescita II, Sostegno al lavoro autonomo, Fondo Rotativo PMI, Distretti
del Commercio, Basket Eque, Garanzia Campania Bond), each a dated post page with
the full bando content → the LLM extracts the canonical fields. LLM scraper over
:class:`~bandiradar.sources.llm_scraper.LlmScraperSource`. Honest scope: the
curated open set is small (~6) and accumulated daily by the monitor; the page's
nav submenu (closed bandi) and its archive (the agency's own selection/albo
notices) are deliberately NOT crawled.

CI NOTE: runner reachability is probed in the monitor pre-flight (direct AND via
relay) BEFORE any routing decision; the host is in the relay worker's allowlist
so routing is a one-line `BANDIRADAR_RELAY_HOSTS` change if the probes show a
block (lesson: euroinfosicilia started blocking runners after we shipped).
"""

from __future__ import annotations

import html
import re

from bandiradar import http
from bandiradar.sources.base import register
from bandiradar.sources.llm_scraper import (
    DetailRef,
    HtmlCrawlRecipe,
    LlmScraperSource,
)

SOURCE_ID = "campania"
CAMPANIA_BASE_URL = "https://www.sviluppocampania.it"
CAMPANIA_LISTING_URL = f"{CAMPANIA_BASE_URL}/bandi-aperti/"

# The listing PARSE as DATA — auto-healable, golden-gated. The widget anchors are
# IMAGE links (no text), so the crawl label is SYNTHESIZED from the slug
# (``title_template`` humanizes ``-`` -> space); the real title comes from the LLM
# extraction. Reproduces the hand parser's refs exactly.
CAMPANIA_RECIPE = HtmlCrawlRecipe(
    listing_url=CAMPANIA_LISTING_URL,
    item_regex=(
        r'class="widget widget_media_image"[^>]*>\s*<a href="'
        r"(?P<url>https://www\.sviluppocampania\.it/"
        r'(?P<path>20\d\d/\d\d/\d\d/(?:[^"#?]*/)?(?P<post_id>[^"#?/]+?)))/?"'
    ),
    url_template="{url}",
    title_template="{post_id}",
)

# The OPEN bandi are the page's curated media-image widget boxes, each linking a
# DATED post (/YYYY/MM/DD/slug/). This hook is deliberate: the page also carries a
# nav submenu of CLOSED bandi and a <main> archive dominated by the agency's own
# selection/albo notices — plain dated-post anchors would sweep those in.
# ``widget_media_image`` is core-WordPress widget markup, the stable marker here.
_ITEM_RE = re.compile(
    r'class="widget widget_media_image"[^>]*>\s*'
    r'<a href="(https://www\.sviluppocampania\.it/(20\d\d/\d\d/\d\d/[^"#?]+?))/?"',
    re.S,
)


def parse_listing(page_html: str) -> list[DetailRef]:
    """PURE: /bandi-aperti/ HTML -> DetailRefs (slug, absolute URL, label).

    The id is the dated post path's slug tail. The widget anchors are IMAGE links
    (no text), so the listing label is derived from the slug — it is only a crawl
    label; the real title comes from the LLM extraction of the detail page.
    Tolerant: no matching widgets -> [] so drift surfaces via ``validate_refs``.
    """
    refs: list[DetailRef] = []
    seen: set[str] = set()
    for m in _ITEM_RE.finditer(page_html or ""):
        url, path = m.group(1), m.group(2)
        slug = path.rsplit("/", 1)[-1]
        if slug in seen:
            continue
        seen.add(slug)
        label = html.unescape(slug.replace("-", " ")).strip()
        refs.append((slug, url, label))
    return refs


class CampaniaSource(LlmScraperSource):
    """LLM scraper for the Sviluppo Campania open bandi."""

    id = SOURCE_ID
    region = "Campania"
    issuer_name = "Regione Campania — Sviluppo Campania"
    listing_url = CAMPANIA_LISTING_URL

    html_recipe = CAMPANIA_RECIPE  # HTML listing -> regex-recipe auto-heal

    def _listing_html(self, recipe: HtmlCrawlRecipe) -> str:
        with http.client(follow_redirects=True) as client:
            resp = http.with_retry(
                lambda: client.get(recipe.listing_url), what="Campania bandi aperti"
            )
            http.raise_for_status(resp, what="Campania bandi aperti")
            return resp.text


SOURCE = CampaniaSource()

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
