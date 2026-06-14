"""Regione Friuli Venezia Giulia — regione.fvg.it bandi/avvisi, LLM scraper.

Recon (2026-06-12): the regional CKAN/Socrata holds only retrospective contribution
reports, but the main portal's ``MODULI/bandi_avvisi`` module is SERVER-RENDERED
and its search (``ricerca.jsp``) accepts a GET with **``onlyTagServizio=1``** —
the portal's own "Bandi contenenti misure contributive" filter — returning only
the *in corso* bandi that carry contribution measures (the slice that matters;
the unfiltered module mixes water-concession notices and other administrative
publications). Items carry title, publication date and a scadenza; detail pages
(``BANDI/<id>.html``) are server-rendered with a structured "Scadenza:" → the LLM
extracts the canonical fields. LLM scraper over
:class:`~bandiradar.sources.llm_scraper.LlmScraperSource`.

CI NOTE: ``www.regione.fvg.it`` drops GitHub-runner IPs but answers the EU-pinned
relay (fra1) — the host is routed via ``BANDIRADAR_RELAY_HOSTS`` in the monitor
workflow. Locally (residential IPs) the fetch goes direct; the per-host relay
routing in ``http.py`` makes this transparent to this adapter.
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
    apply_html_recipe,
)

SOURCE_ID = "fvg"
FVG_BASE_URL = "https://www.regione.fvg.it"
_MODULE = "/rafvg/cms/RAFVG/MODULI/bandi_avvisi"
# The portal's own "Bandi contenenti misure contributive" filter, as a GET.
FVG_LISTING_URL = f"{FVG_BASE_URL}{_MODULE}/ricerca.jsp"
FVG_LISTING_PARAMS = {"txtChiave": "", "onlyTagServizio": "1", "startsearch": "vai"}
# Current contributi bandi span ~2 result pages; 3 is a safe bound.
_LISTING_PAGES = 3

# The listing PARSE as DATA — auto-healable, golden-gated. The ``#contributi`` anchor
# IS the portal's contribution filter (baked into the regex); the title is the item's
# <h3>. Reproduces the hand parser's refs exactly.
FVG_RECIPE = HtmlCrawlRecipe(
    listing_url=FVG_LISTING_URL,
    base_url=FVG_BASE_URL,
    item_regex=(
        r'<a href="(?P<path>/rafvg/cms/RAFVG/MODULI/bandi_avvisi/BANDI/'
        r'(?P<post_id>\d+)\.html)#contributi"[^>]*>.*?<h3[^>]*>(?P<title>.*?)</h3>'
    ),
    url_template="{base}{path}",
    params=FVG_LISTING_PARAMS,
)

# One result item: <a href="…/BANDI/<id>.html#contributi" …> … <h3>title</h3> … </a>
_ITEM_RE = re.compile(
    r'<a href="(/rafvg/cms/RAFVG/MODULI/bandi_avvisi/BANDI/(\d+)\.html)#contributi"'
    r"[^>]*>(.*?)</a>",
    re.S,
)
_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_listing(page_html: str) -> list[DetailRef]:
    """PURE: filtered ricerca.jsp HTML -> DetailRefs (id, absolute URL, title).

    Only ``#contributi``-anchored items exist on the filtered page (the portal
    adds the anchor for contribution-bearing bandi). Tolerant: an item without an
    extractable ``<h3>`` title survives with "" so drift surfaces via
    ``validate_refs``.
    """
    refs: list[DetailRef] = []
    seen: set[str] = set()
    for m in _ITEM_RE.finditer(page_html or ""):
        path, bando_id, inner = m.group(1), m.group(2), m.group(3)
        if bando_id in seen:
            continue
        seen.add(bando_id)
        h = _H3_RE.search(inner)
        title = (
            html.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", h.group(1)))).strip()
            if h
            else ""
        )
        refs.append((bando_id, f"{FVG_BASE_URL}{path}", title))
    return refs


class FvgSource(LlmScraperSource):
    """LLM scraper for the FVG contributi-bearing bandi (in corso)."""

    id = SOURCE_ID
    region = "Friuli-Venezia Giulia"
    issuer_name = "Regione Autonoma Friuli Venezia Giulia"
    listing_url = f"{FVG_LISTING_URL}?txtChiave=&onlyTagServizio=1&startsearch=vai"

    html_recipe = FVG_RECIPE  # filtered HTML listing -> regex-recipe auto-heal

    def _listing_html(self, recipe: HtmlCrawlRecipe) -> str:
        """Fetch the filtered result pages and CONCATENATE their HTML; the recipe
        parses + dedups the combined markup. Stops at the first page with no items."""
        parts: list[str] = []
        with http.client(follow_redirects=True) as client:
            for page in range(1, _LISTING_PAGES + 1):
                params = dict(recipe.params)
                if page > 1:
                    params["pag"] = str(page)
                resp = http.with_retry(
                    lambda params=params: client.get(recipe.listing_url, params=params),
                    what="FVG bandi ricerca",
                )
                http.raise_for_status(resp, what="FVG bandi ricerca")
                if not apply_html_recipe(recipe, resp.text):
                    break  # past the last page of filtered results
                parts.append(resp.text)
        return "\n".join(parts)


SOURCE = FvgSource()

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
