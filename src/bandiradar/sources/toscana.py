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
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from bandiradar.matching.llm import LLMClient, get_client
from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.sources.base import register
from bandiradar.sources.llm_scraper import (
    ExtractionCache,
    extract_bando_fields,
    html_to_text,
)
from bandiradar.storage import SqliteExtractionCache, Store

SOURCE_ID = "toscana"
REGION = "Toscana"
ISSUER = "Sviluppo Toscana"
# WP REST listing (links + titles only; the body is scraped from each page).
TOSCANA_LIST_URL = "https://www.sviluppo.toscana.it/wp-json/wp/v2/bando"
_MAX_ITEMS = 20

FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "fixtures" / "toscana.json"
)

DetailRef = tuple[Any, str, str]  # (post_id, detail_url, listing_title)


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
    kind: Kind = (
        p.get("kind") if p.get("kind") in ("incentive", "tender") else "incentive"
    )
    keywords = p.get("keywords")
    keywords = [str(k) for k in keywords] if isinstance(keywords, list) else []

    # The extracted sector keywords genuinely describe the bando, so fold them into
    # the matcher's text (the prefilter/heuristic read eligibility_text, not the
    # opportunity's keyword list).
    eligibility = " ".join(
        part for part in (p.get("eligibility_text"), " ".join(keywords)) if part
    ).strip()

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
            value_min=p.get("value_min"),
            value_max=p.get("value_max"),
            geo_scope="regional",
            region=REGION,
            deadline=deadline,
            status=default_status(deadline, now),
            eligibility_text=eligibility or None,
            raw_ref=raw.id,
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

    def _list_details(self) -> list[DetailRef]:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(
                TOSCANA_LIST_URL,
                params={"per_page": _MAX_ITEMS, "_fields": "id,link,title"},
            )
            resp.raise_for_status()
            out: list[DetailRef] = []
            for post in resp.json():
                title = (post.get("title") or {}).get("rendered", "")
                out.append((post["id"], post.get("link") or "", title))
            return out

    def _fetch_text(self, url: str) -> str:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return html_to_text(resp.text)

    def fetch(
        self,
        since: datetime | None = None,
        *,
        client: LLMClient | None = None,
        cache: ExtractionCache | None = None,
        list_details=None,
        fetch_text=None,
        max_items: int = _MAX_ITEMS,
    ) -> Iterable[RawDoc]:
        """LIVE: list bando URLs, fetch each page, LLM-extract fields (cached per URL).

        Requires an LLM provider + key (live only). ``--sample`` uses
        ``load_fixture`` and never calls this.
        """
        client = client if client is not None else get_client()
        if client is None:
            raise RuntimeError(
                "LLM scraper requires an LLM provider + key — set "
                "BANDIRADAR_LLM_PROVIDER and the API key (see .env.example). "
                "Use --sample to run offline against the recorded fixture."
            )
        if cache is None:
            cache = SqliteExtractionCache(Store(None))  # persist on the default DB
        list_details = list_details or self._list_details
        fetch_text = fetch_text or self._fetch_text

        return self._scrape(client, cache, list_details, fetch_text, max_items)

    def _scrape(
        self, client, cache, list_details, fetch_text, max_items
    ) -> Iterator[RawDoc]:
        for post_id, url, listing_title in list_details()[:max_items]:
            record = cache.get(url)
            if record is None:
                record = extract_bando_fields(fetch_text(url), REGION, client)
                cache.set(url, record)
            payload = {
                **record,
                "_post_id": post_id,
                "_url": url,
                "_listing_title": listing_title,
            }
            yield RawDoc(
                id=f"{SOURCE_ID}:{post_id}",
                source=SOURCE_ID,
                fetched_at=datetime.now(tz=UTC),
                payload=payload,
                url=url,
            )

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)

    def load_fixture(self) -> list[RawDoc]:
        return load_fixture()


register(ToscanaSource())
