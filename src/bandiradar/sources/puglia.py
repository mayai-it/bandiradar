"""Regione Puglia — pr2127.regione.puglia.it (PR Puglia 2021-2027), LLM scraper.

Recon (2026-06-12): the historic ``sistema.puglia.it`` is an Oracle-Portal service
registry — its "Bandi Aperti" mixes fresh entries with standing services from 2010+
and carries NO scadenza; its per-bando mini-sites are framesets with no content in
the DOM. ``por.regione.puglia.it`` only holds the closed 2014-2020 cycle. The
CURRENT programming-period portal ``pr2127.regione.puglia.it`` (Liferay) is the
viable surface: its avvisi list is served by a parameterless **Liferay resource
URL** (``p_p_lifecycle=2`` + ``/ricerca/news/news-list``) returning a clean
server-rendered fragment of ``news-list-item`` rows — detail URL + title + a
per-item **"Bando aperto"/"Bando chiuso" badge** — and supports ``_…_delta=30``
for one-call pagination. Detail pages are server-rendered and rich (dotazione,
beneficiari, scadenze in prose) → the LLM extracts the canonical fields.

The crawl keeps ONLY items badged "Bando aperto" (precision; news/verbali and
closed bandi are skipped). NOTE: the portlet instance id inside the resource URL
is part of the portal deployment — if it ever changes, the crawl breaks loudly
(drift → ``validate_refs`` broken → human-flagged), never silently.
"""

from __future__ import annotations

import html
import re

from bandiradar import http
from bandiradar.sources.base import register
from bandiradar.sources.llm_scraper import DetailRef, LlmScraperSource

SOURCE_ID = "puglia"
PUGLIA_BASE_URL = "https://pr2127.regione.puglia.it"
_PORTLET = "RicercaNews_INSTANCE_HR72zDg9uWt0"
# The Liferay resource URL serving the avvisi fragment (see module docstring).
PUGLIA_LISTING_URL = (
    f"{PUGLIA_BASE_URL}/elenco-avvisi-pubblicati"
    f"?p_p_id={_PORTLET}&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
    "&p_p_resource_id=%2Fricerca%2Fnews%2Fnews-list&p_p_cacheability=cacheLevelPage"
    f"&_{_PORTLET}_delta=30"
)

_ITEM_SPLIT_RE = re.compile(r'class="row news-list-item"')
_DETAIL_RE = re.compile(
    r'<a[^>]+href="(https://pr2127\.regione\.puglia\.it/[^"]*?/-/[^"?]+)[^"]*"'
)
_TITLE_RE = re.compile(r"<h\d[^>]*>(.*?)</h\d>", re.S)
_OPEN_BADGE_RE = re.compile(r"Bando\s+aperto", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_listing(fragment_html: str) -> list[DetailRef]:
    """PURE: news-list fragment HTML -> DetailRefs for OPEN bandi only.

    One ``news-list-item`` block per avviso; keep blocks badged "Bando aperto"
    (news/verbali carry no badge, closed bandi are badged "Bando chiuso"). The id
    is the detail URL's slug tail. Tolerant: a block without an extractable URL or
    title is skipped silently only if unbadged; a badged block missing fields
    yields an empty ref so drift surfaces via ``validate_refs``.
    """
    refs: list[DetailRef] = []
    seen: set[str] = set()
    for block in _ITEM_SPLIT_RE.split(fragment_html or "")[1:]:
        if not _OPEN_BADGE_RE.search(block):
            continue  # news, verbali, pre-informazione, or closed bandi
        m_url = _DETAIL_RE.search(block)
        url = m_url.group(1) if m_url else ""
        slug = url.rsplit("/", 1)[-1] if url else ""
        m_title = _TITLE_RE.search(block)
        title = (
            html.unescape(
                re.sub(r"\s+", " ", _TAG_RE.sub(" ", m_title.group(1)))
            ).strip()
            if m_title
            else ""
        )
        if slug in seen:
            continue
        if slug:
            seen.add(slug)
        refs.append((slug, url, title))
    return refs


class PugliaSource(LlmScraperSource):
    """LLM scraper for the PR Puglia 2021-2027 avvisi (open bandi only)."""

    id = SOURCE_ID
    region = "Puglia"
    issuer_name = "Regione Puglia"
    listing_url = PUGLIA_LISTING_URL

    def _listing_refs(self) -> list[DetailRef]:
        with http.client(follow_redirects=True) as client:
            resp = http.with_retry(
                lambda: client.get(PUGLIA_LISTING_URL), what="Puglia avvisi fragment"
            )
            http.raise_for_status(resp, what="Puglia avvisi fragment")
            return parse_listing(resp.text)


SOURCE = PugliaSource()

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
