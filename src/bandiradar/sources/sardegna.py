"""Regione Sardegna — sardegnaimpresa.eu (Drupal 10), LLM scraper.

Recon (2026-06-12): no ``/jsonapi`` exposed and no usable RSS, but the
``/it/agevolazioni`` Views listing is SERVER-RENDERED with one anchor per
agevolazione (the anchor's ``<span>`` text is the full official title) and even a
structured per-item ``field-data-scadenza-agevolazione`` datetime. Detail pages
are server-rendered and labelled (Soggetti ammissibili, Data di scadenza,
Macrosettore, contributo) → the LLM extracts the canonical fields. LLM scraper
over :class:`~bandiradar.sources.llm_scraper.LlmScraperSource`; the listing shows
the CURRENT agevolazioni (~10), which the daily monitor accumulates over time.
"""

from __future__ import annotations

import html
import re

from bandiradar import http
from bandiradar.sources.base import register
from bandiradar.sources.llm_scraper import DetailRef, LlmScraperSource

SOURCE_ID = "sardegna"
SARDEGNA_BASE_URL = "https://www.sardegnaimpresa.eu"
SARDEGNA_LISTING_URL = f"{SARDEGNA_BASE_URL}/it/agevolazioni"

# One listing anchor per agevolazione: <a href="/it/agevolazioni/<slug>"><span>title…
_DETAIL_RE = re.compile(r'<a href="(/it/agevolazioni/[^"#?]+)"[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_listing(page_html: str) -> list[DetailRef]:
    """PURE: /it/agevolazioni HTML -> DetailRefs (slug, absolute URL, title).

    Keeps the first (title-bearing) anchor per slug — image/teaser anchors for the
    same slug are dropped by the dedup. Tolerant: an empty title survives as ""
    so drift surfaces via ``validate_refs``.
    """
    refs: list[DetailRef] = []
    best_title: dict[str, str] = {}
    order: list[tuple[str, str]] = []  # (slug, path) in first-seen order
    for m in _DETAIL_RE.finditer(page_html or ""):
        path = m.group(1)
        slug = path.rsplit("/", 1)[-1]
        title = html.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", m.group(2)))).strip()
        if slug not in best_title:
            best_title[slug] = title
            order.append((slug, path))
        elif title and not best_title[slug]:
            best_title[slug] = title  # a later anchor carried the text
    for slug, path in order:
        refs.append((slug, f"{SARDEGNA_BASE_URL}{path}", best_title[slug]))
    return refs


class SardegnaSource(LlmScraperSource):
    """LLM scraper for the Sardegna Impresa agevolazioni listing."""

    id = SOURCE_ID
    region = "Sardegna"
    issuer_name = "Regione Autonoma della Sardegna — Sardegna Impresa"
    listing_url = SARDEGNA_LISTING_URL

    def _listing_refs(self) -> list[DetailRef]:
        with http.client(follow_redirects=True) as client:
            resp = http.with_retry(
                lambda: client.get(SARDEGNA_LISTING_URL), what="Sardegna listing"
            )
            http.raise_for_status(resp, what="Sardegna listing")
            return parse_listing(resp.text)


SOURCE = SardegnaSource()

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
