"""Regione Liguria — regione.liguria.it publiccompetition, LLM scraper.

Recon (2026-06-12): ``filse.it`` (Joomla) has no API, but the institutional
portal's Joomla ``com_publiccompetition`` component has a quicksearch with TWO
server-side filters that matter: **tipologia 6 = "contributi"** and **stato 1 =
"Attivi"** — returning only the currently-active contribution calls (Nidi gratis,
bonus assunzionali turismo, dote sport, …). The search is a POST guarded by a
session cookie + a per-session Joomla CSRF token, so the crawl is two requests on
ONE client: GET the search page (cookie + token), then POST the filtered query.
Detail pages are server-rendered with labelled fields (Data apertura/chiusura,
**Beneficiari: imprese**, dotazione) → the LLM extracts the canonical fields.
LLM scraper over :class:`~bandiradar.sources.llm_scraper.LlmScraperSource`.
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

SOURCE_ID = "liguria"
LIGURIA_BASE_URL = "https://www.regione.liguria.it"
LIGURIA_SEARCH_PAGE = f"{LIGURIA_BASE_URL}/bandi-e-avvisi.html"
LIGURIA_RESULTS_URL = f"{LIGURIA_BASE_URL}/homepage-bandi-e-avvisi.html"
LIGURIA_TIPOLOGIA_CONTRIBUTI = "6"
LIGURIA_STATO_ATTIVI = "1"

# The listing PARSE as DATA — auto-healable, golden-gated. Only the PARSE is the
# recipe; the bespoke FETCH (warm GET for the CSRF token, then the filtered POST)
# stays in ``_listing_html``. Reproduces the hand parser's refs exactly.
LIGURIA_RECIPE = HtmlCrawlRecipe(
    listing_url=LIGURIA_SEARCH_PAGE,
    base_url=LIGURIA_BASE_URL,
    item_regex=(
        r'<a[^>]+href="(?P<path>/homepage-bandi-e-avvisi/publiccompetition/'
        r'(?P<post_id>\d+):[^"]+\.html)"[^>]*>(?P<title>.*?)</a>'
    ),
    url_template="{base}{path}",
)

# One result item: <a class='bando_link' href="/homepage-bandi-e-avvisi/
# publiccompetition/<id>:<slug>.html"> title </a>
_ITEM_RE = re.compile(
    r'<a[^>]+href="(/homepage-bandi-e-avvisi/publiccompetition/(\d+):[^"]+\.html)"'
    r"[^>]*>(.*?)</a>",
    re.S,
)
# The Joomla per-session CSRF token: a 32-hex input inside the quicksearch form.
_TOKEN_RE = re.compile(r'name="([a-f0-9]{32})"')
_TAG_RE = re.compile(r"<[^>]+>")


def parse_listing(page_html: str) -> list[DetailRef]:
    """PURE: filtered results HTML -> DetailRefs (numeric id, absolute URL, title).

    Tolerant: an item without anchor text survives with "" so drift surfaces via
    ``validate_refs``.
    """
    refs: list[DetailRef] = []
    seen: set[str] = set()
    for m in _ITEM_RE.finditer(page_html or ""):
        path, item_id, inner = m.group(1), m.group(2), m.group(3)
        if item_id in seen:
            continue
        seen.add(item_id)
        title = html.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", inner))).strip()
        refs.append((item_id, f"{LIGURIA_BASE_URL}{path}", title))
    return refs


def parse_csrf_token(page_html: str) -> str | None:
    """PURE: the quicksearch form's per-session 32-hex Joomla token, or None."""
    form = next(
        (f for f in re.split(r"<form", page_html or "") if "quickTipologia" in f),
        "",
    )
    m = _TOKEN_RE.search(form)
    return m.group(1) if m else None


class LiguriaSource(LlmScraperSource):
    """LLM scraper for the Liguria active contributi (publiccompetition)."""

    id = SOURCE_ID
    region = "Liguria"
    issuer_name = "Regione Liguria"
    listing_url = LIGURIA_SEARCH_PAGE

    html_recipe = LIGURIA_RECIPE  # parse is a recipe; the POST+CSRF fetch is below

    def _listing_html(self, recipe: HtmlCrawlRecipe) -> str:
        """Bespoke FETCH (the PARSE is the recipe): warm GET for the per-session CSRF
        token, then the filtered POST. Returns "" if the form drifted (no token) ->
        the recipe yields no refs -> broken, human-flagged."""
        with http.client(follow_redirects=True) as client:
            # 1) Warm up: session cookie + the per-session CSRF token.
            warm = http.with_retry(
                lambda: client.get(recipe.listing_url), what="Liguria search page"
            )
            http.raise_for_status(warm, what="Liguria search page")
            token = parse_csrf_token(warm.text)
            if token is None:
                return ""  # form drifted -> no token -> broken, human-flagged
            # 2) The filtered query: tipologia=contributi, stato=Attivi.
            data = {
                "quicksearch": "",
                "quickTipologia[]": LIGURIA_TIPOLOGIA_CONTRIBUTI,
                "quicksearch_stato": LIGURIA_STATO_ATTIVI,
                "quicksearch_submit": "1",
                "quicksearch_action": "search",
                "boxchecked": "0",
                "limitstart": "0",
                token: "1",
            }
            resp = http.with_retry(
                lambda: client.post(LIGURIA_RESULTS_URL, data=data),
                what="Liguria contributi search",
            )
            http.raise_for_status(resp, what="Liguria contributi search")
            return resp.text


SOURCE = LiguriaSource()

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
