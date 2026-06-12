"""Regione Veneto — bandi.regione.veneto.it (SIU public portal), LLM scraper.

Recon (2026-06-12): the portal's listing pages are JS-driven (jQuery DataTables)
and its internal JSON endpoint (``/Public/GetListaAttiJson``) consistently answers
200 with ZERO rows to non-browser callers (cookies/headers replicated — the data
layer stonewalls bots), so there is NO usable structured API. What IS
server-rendered: the LANDING page ("IN SCADENZA" + latest published atti, with
``Dettaglio?idAtto=N`` links) and every ``Dettaglio`` page (labelled fields:
Scadenza, Stato, Oggetto…). So this is an LLM scraper over the
:class:`~bandiradar.sources.llm_scraper.LlmScraperSource` base: the landing is the
crawl seed (drift DETECTED via the golden, not auto-healed — the parse is code),
the LLM extracts the canonical fields from each detail page.

HONEST COVERAGE NOTE: one visit surfaces only the items on the landing (~7-10);
the daily monitor accumulates them over time. The full archive sits behind the
bot-stonewalled JSON — out of honest reach.
"""

from __future__ import annotations

import html
import re

from bandiradar import http
from bandiradar.sources.base import register
from bandiradar.sources.llm_scraper import DetailRef, LlmScraperSource

SOURCE_ID = "veneto"
VENETO_BASE_URL = "https://bandi.regione.veneto.it"
VENETO_LISTING_URL = f"{VENETO_BASE_URL}/Public/Index"

# Server-rendered detail anchors on the landing: href="Dettaglio?idAtto=13062".
_DETAIL_RE = re.compile(
    r'<a[^>]+href="(?:/Public/)?Dettaglio\?idAtto=(\d+)"[^>]*>(.*?)</a>',
    re.S | re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_listing(page_html: str) -> list[DetailRef]:
    """PURE: landing HTML -> DetailRefs (idAtto, absolute detail URL, title).

    Deduplicates by idAtto (the same atto can appear in both landing sections).
    Tolerant: empty titles survive as "" so drift surfaces via ``validate_refs``.
    """
    refs: list[DetailRef] = []
    seen: set[str] = set()
    for m in _DETAIL_RE.finditer(page_html or ""):
        id_atto = m.group(1)
        if id_atto in seen:
            continue
        seen.add(id_atto)
        title = html.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", m.group(2)))).strip()
        refs.append(
            (id_atto, f"{VENETO_BASE_URL}/Public/Dettaglio?idAtto={id_atto}", title)
        )
    return refs


class VenetoSource(LlmScraperSource):
    """LLM scraper for the Veneto SIU bandi portal."""

    id = SOURCE_ID
    region = "Veneto"
    issuer_name = "Regione del Veneto"
    listing_url = VENETO_LISTING_URL

    def _listing_refs(self) -> list[DetailRef]:
        with http.client(follow_redirects=True) as client:
            resp = http.with_retry(
                lambda: client.get(VENETO_LISTING_URL), what="Veneto landing"
            )
            http.raise_for_status(resp, what="Veneto landing")
            return parse_listing(resp.text)


SOURCE = VenetoSource()

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
