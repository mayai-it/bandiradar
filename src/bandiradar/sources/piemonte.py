"""Regione Piemonte — bandi.regione.piemonte.it (Drupal 10), LLM scraper.

Recon (2026-06-12): the region runs a dedicated bandi portal on Drupal 10. No
jsonapi is exposed (404) and the RSS holds only ~10 mostly "pre-informazione"
items — but the ``/contributi-finanziamenti`` Views listing is SERVER-RENDERED
with semantic Drupal field markup AND an exposed stato filter
(``?field_stato_target_id=19`` = "Aperto"), so the crawl can ask the server for
currently-open bandi only. Detail pages are server-rendered with labelled fields
(Scadenza, Stato, Dotazione finanziaria, Rivolto a…) → the LLM extracts the
canonical fields from each. LLM scraper over the
:class:`~bandiradar.sources.llm_scraper.LlmScraperSource` base; listing drift is
DETECTED via the golden (the HTML parse is code — no auto-heal), never silent.
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

SOURCE_ID = "piemonte"
PIEMONTE_BASE_URL = "https://bandi.regione.piemonte.it"
# The Views listing filtered server-side to stato "Aperto" (target_id 19).
PIEMONTE_LISTING_URL = f"{PIEMONTE_BASE_URL}/contributi-finanziamenti"
PIEMONTE_STATO_APERTO = "19"
# Open bandi span a couple of Views pages at most; 3 pages × ~9 items covers them.
_LISTING_PAGES = 3

# The listing PARSE as DATA (a regex-template) — auto-healable, golden-gated.
# ``post_id`` is the node slug (the ``about`` path tail); the URL is built from the
# captured ``path``. Reproduces the hand parser's refs exactly (per page).
PIEMONTE_RECIPE = HtmlCrawlRecipe(
    listing_url=PIEMONTE_LISTING_URL,
    base_url=PIEMONTE_BASE_URL,
    item_regex=(
        r'<article\s+about="(?P<path>/contributi-finanziamenti/'
        r'(?:[^"]*/)?(?P<post_id>[^"/]+))".*?<h2>(?P<title>.*?)</h2>'
    ),
    url_template="{base}{path}",
)

# One Views row: <article about="/contributi-finanziamenti/<slug>" ...> ... with the
# display title inside <h2>. The `about` attribute is Drupal's stable node URI.
_ROW_RE = re.compile(
    r'<article\s+about="(/contributi-finanziamenti/[^"]+)".*?<h2>(.*?)</h2>',
    re.S | re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_listing(page_html: str) -> list[DetailRef]:
    """PURE: Views listing HTML -> DetailRefs (slug, absolute URL, title).

    The node slug (the ``about`` path tail) is the stable id. Tolerant: empty
    titles survive as "" so drift surfaces via ``validate_refs``.
    """
    refs: list[DetailRef] = []
    seen: set[str] = set()
    for m in _ROW_RE.finditer(page_html or ""):
        path = m.group(1)
        slug = path.rsplit("/", 1)[-1]
        if slug in seen:
            continue
        seen.add(slug)
        title = html.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", m.group(2)))).strip()
        refs.append((slug, f"{PIEMONTE_BASE_URL}{path}", title))
    return refs


class PiemonteSource(LlmScraperSource):
    """LLM scraper for the Piemonte bandi portal (open bandi only)."""

    id = SOURCE_ID
    region = "Piemonte"
    issuer_name = "Regione Piemonte"
    listing_url = (
        f"{PIEMONTE_LISTING_URL}?field_stato_target_id={PIEMONTE_STATO_APERTO}"
    )
    html_recipe = PIEMONTE_RECIPE  # HTML listing -> regex-recipe auto-heal

    def _listing_html(self, recipe: HtmlCrawlRecipe) -> str:
        """Fetch the open-bandi Views pages and CONCATENATE their HTML; the recipe
        parses + dedups the combined markup. Stops at the first page with no items
        (past the last page of open bandi)."""
        parts: list[str] = []
        with http.client(follow_redirects=True) as client:
            for page in range(_LISTING_PAGES):
                resp = http.with_retry(
                    lambda page=page: client.get(
                        PIEMONTE_LISTING_URL,
                        params={
                            "field_stato_target_id": PIEMONTE_STATO_APERTO,
                            "page": page,
                        },
                    ),
                    what="Piemonte listing",
                )
                http.raise_for_status(resp, what="Piemonte listing")
                if not apply_html_recipe(recipe, resp.text):
                    break  # past the last page of open bandi
                parts.append(resp.text)
        return "\n".join(parts)


SOURCE = PiemonteSource()

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
