"""Reusable Plone-REST `Bando` adapter base.

A large share of Italian PAs run **Plone** with the AGID/"Design Italia" content
types (``design.plone.contenttypes``), which ship a dedicated **``Bando``** type
exposed over the standard plone.restapi: ``GET <site>/++api++/@search?portal_type=
Bando&fullobjects=true``. Unlike the WordPress base (which scrapes the deadline from
free text), a ``Bando`` carries STRUCTURED fields — ``scadenza_bando`` (the actual
application deadline), ``apertura_bando``, ``bando_state`` (open/closed),
``tipologia_bando``, ``ente_bando``, ``description`` and a ``text`` body.

So "add such a PA" is a CONFIG entry — a :class:`PloneBandoSource` (site base URL,
region, issuer, kind) + a recorded fixture + a test — with no new mapping logic.
``sources/emilia_romagna.py`` is the reference config.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandiradar import http, resources
from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.sources.base import ProgressFn

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", value or ""))).strip()


def _parse_dt(value: Any) -> datetime | None:
    """Parse a Plone ISO date/datetime (date-only or full) to tz-aware UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _vocab_title(value: Any) -> str | None:
    """A plone.restapi vocabulary value -> its human title (``{title, token}``)."""
    if isinstance(value, dict):
        return value.get("title")
    if isinstance(value, str):
        return value
    return None


def _keywords(bando: dict[str, Any]) -> list[str]:
    """Descriptive metadata keywords from the Bando's structured vocab fields."""
    out: list[str] = []
    tip = _vocab_title(bando.get("tipologia_bando"))
    if tip:
        out.append(tip)
    for field_name in ("materie", "destinatari"):
        value = bando.get(field_name)
        if isinstance(value, list):
            out.extend(t for t in (_vocab_title(v) for v in value) if t)
        else:
            title = _vocab_title(value)
            if title:
                out.append(title)
    return out


@dataclass
class PloneBandoSource:
    """A regional/PA bandi source backed by a Plone-REST ``Bando`` listing (config)."""

    id: str
    region: str
    issuer_name: str
    base_url: str  # site root; the API lives at ``<base_url>/++api++/@search``
    kind: Kind = "incentive"
    b_size: int = 50
    fixture_name: str = ""

    def _fixture_path(self):
        return resources.fixture(self.fixture_name or f"{self.id}.json")

    @property
    def _search_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/++api++/@search"

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        """PURE mapping from one Plone ``Bando`` object (``raw.payload``)."""
        b: dict[str, Any] = raw.payload
        deadline = _parse_dt(b.get("scadenza_bando"))
        body = ""
        text = b.get("text")
        if isinstance(text, dict):
            body = _strip_html(text.get("data", ""))
        summary = (b.get("description") or "").strip() or None
        return [
            Opportunity(
                id=f"{self.id}:{b['UID']}",
                source=self.id,
                source_url=b.get("@id") or "",
                kind=self.kind,
                title=(b.get("title") or b["UID"]).strip(),
                summary=summary,
                issuer_name=_vocab_title(b.get("ente_bando")) or self.issuer_name,
                issuer_region=self.region,
                cpv=[],
                keywords=_keywords(b),
                geo_scope="regional",
                region=self.region,
                published_at=_parse_dt(b.get("effective")),
                deadline=deadline,
                status=default_status(deadline, now),
                # The matcher reads the body; fall back to the description.
                eligibility_text=(body or summary) or None,
                raw_ref=raw.id,
            )
        ]

    def load_fixture(self, path: Path | None = None) -> list[RawDoc]:
        package = json.loads((path or self._fixture_path()).read_text(encoding="utf-8"))
        fetched_at = _parse_dt(package.get("_captured")) or datetime.fromisoformat(
            "1970-01-01T00:00:00+00:00"
        )
        return [
            RawDoc(
                id=f"{self.id}:{item['UID']}",
                source=self.id,
                fetched_at=fetched_at,
                payload=item,
                url=item.get("@id"),
            )
            for item in package.get("items", [])
        ]

    _MAX_RECORDS = 5000  # safety ceiling when no explicit limit is given

    def fetch(
        self,
        since: datetime | None = None,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
        progress: ProgressFn | None = None,
    ) -> Iterable[RawDoc]:
        return self._fetch(since, limit, max_pages, progress)

    def _fetch(
        self,
        since: datetime | None,
        limit: int | None,
        max_pages: int | None,
        progress: ProgressFn | None,
    ) -> Iterator[RawDoc]:
        cap = limit if limit is not None else self._MAX_RECORDS
        seen = 0
        params: dict[str, Any] | None = {
            "portal_type": "Bando",
            "fullobjects": "true",
            "b_size": self.b_size,
            "sort_on": "modified",
            "sort_order": "descending",
        }
        if since is not None:
            params["modified.query"] = since.astimezone(UTC).isoformat()
            params["modified.range"] = "min"
        url: str | None = self._search_url
        page = 0
        with http.client(follow_redirects=True) as client:
            while url is not None and (max_pages is None or page < max_pages):
                response = http.with_retry(
                    lambda u=url, p=params: client.get(u, params=p),
                    what=f"Plone @search for {self.id}",
                )
                http.raise_for_status(response, what=f"Plone @search for {self.id}")
                body = response.json()
                for item in body.get("items", []):
                    if seen >= cap:
                        return
                    seen += 1
                    yield RawDoc(
                        id=f"{self.id}:{item['UID']}",
                        source=self.id,
                        fetched_at=datetime.now(tz=UTC),
                        payload=item,
                        url=item.get("@id"),
                    )
                page += 1
                if progress is not None:
                    progress(f"{self.id}: batch {page}, {seen} fetched")
                # Follow plone.restapi batching: the `next` link already carries the
                # FULL query (portal_type, b_start, …), so subsequent requests must
                # pass params=None — `params={}` would REPLACE (wipe) the link's
                # query string in httpx, refetching an unfiltered first page forever.
                url = (body.get("batching") or {}).get("next")
                params = None
