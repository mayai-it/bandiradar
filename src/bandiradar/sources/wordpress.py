"""Reusable WordPress-REST bandi adapter base.

Many Italian regional finanza-agevolata agencies run WordPress and expose their
bandi as a custom post type over the WP REST API (``/wp-json/wp/v2/<type>``).
This base turns "add such a region" into a CONFIG entry — a
:class:`WordPressBandiSource` instance (portal endpoint, region, issuer, kind,
which taxonomies become keywords) + a recorded fixture + a test — with no new
mapping logic. ``sources/lazio.py`` is the reference config.

Per-portal field shape is the standard WP REST: ``id``, ``title.rendered``,
``link``, ``date``, ``content.rendered``, ``excerpt.rendered``, and ``class_list``
(slug-form taxonomy classes). WP REST exposes no structured deadline/value, so the
scadenza is parsed from the free-text content (:func:`_parse_scadenza`) and the
bando body becomes ``eligibility_text``.
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

import httpx

from bandiradar import resources
from bandiradar.models import Kind, Opportunity, RawDoc, default_status

_MONTHS = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}
# A date counts as the deadline only if one of these words precedes it nearby.
_DEADLINE_KEYS = ("scadenz", "termine", "entro", "presentazione", "chiusura")
_MONTH_DATE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})")
_NUMERIC_DATE_RE = re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})")
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", value or ""))).strip()


def _slugs(class_list: list[str], prefix: str) -> list[str]:
    return [c[len(prefix) :] for c in class_list if c.startswith(prefix)]


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_scadenza(text: str) -> datetime | None:
    """Find the deadline date in free-text bando content (None if not found).

    Only accepts a date preceded (within ~70 chars) by a deadline keyword, so a
    publication date ("pubblicato sul BUR del 30 aprile 2026") is not mistaken for
    the scadenza.
    """
    low = text.lower()
    candidates: list[tuple[int, int, int, int]] = []  # (pos, year, month, day)
    for m in _MONTH_DATE_RE.finditer(low):
        candidates.append(
            (m.start(), int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
        )
    for m in _NUMERIC_DATE_RE.finditer(low):
        candidates.append(
            (m.start(), int(m.group(3)), int(m.group(2)), int(m.group(1)))
        )
    candidates.sort()
    for pos, year, month, day in candidates:
        context = low[max(0, pos - 70) : pos]
        if any(key in context for key in _DEADLINE_KEYS):
            try:
                return datetime(year, month, day, tzinfo=UTC)
            except ValueError:
                continue
    return None


def _wp_pages(
    client: httpx.Client, data_url: str, per_page: int, since: datetime | None
) -> Iterator[list[dict[str, Any]]]:
    """Yield successive pages of WP posts (reusable WP-REST pagination)."""
    page = 1
    while True:
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        if since is not None:
            params["after"] = since.astimezone(UTC).isoformat()
        try:
            response = client.get(data_url, params=params)
            if response.status_code == 400:  # WP returns 400 past the last page
                break
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"WordPress fetch failed for {data_url}: {exc}") from exc
        posts = response.json()
        if not posts:
            break
        yield posts
        if len(posts) < per_page:
            break
        page += 1


@dataclass
class WordPressBandiSource:
    """A regional bandi source backed by a WordPress REST endpoint (config-only)."""

    id: str
    region: str
    data_url: str
    issuer_name: str
    kind: Kind = "incentive"
    keyword_taxonomies: tuple[str, ...] = ("tematiche-", "destinatari-")
    per_page: int = 100
    fixture_name: str = ""

    def _fixture_path(self):
        return resources.fixture(self.fixture_name or f"{self.id}.json")

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        """PURE mapping from one WP post (``raw.payload``) to an Opportunity."""
        post: dict[str, Any] = raw.payload
        content = _strip_html((post.get("content") or {}).get("rendered", ""))
        deadline = _parse_scadenza(content)
        class_list = post.get("class_list") or []
        keywords: list[str] = []
        for prefix in self.keyword_taxonomies:
            keywords.extend(_slugs(class_list, prefix))

        title = _strip_html((post.get("title") or {}).get("rendered", ""))
        summary = _strip_html((post.get("excerpt") or {}).get("rendered", ""))
        return [
            Opportunity(
                id=f"{self.id}:{post['id']}",
                source=self.id,
                source_url=post.get("link") or "",
                kind=self.kind,
                title=title or str(post["id"]),
                summary=summary or None,
                issuer_name=self.issuer_name,
                issuer_region=self.region,
                cpv=[],
                keywords=keywords,
                geo_scope="regional",
                region=self.region,
                published_at=_parse_dt(post.get("date")),
                deadline=deadline,
                status=default_status(deadline, now),
                eligibility_text=content or None,
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
                id=f"{self.id}:{post['id']}",
                source=self.id,
                fetched_at=fetched_at,
                payload=post,
                url=post.get("link"),
            )
            for post in package.get("posts", [])
        ]

    def fetch(self, since: datetime | None = None) -> Iterable[RawDoc]:
        return self._fetch(since)

    def _fetch(self, since: datetime | None) -> Iterator[RawDoc]:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            for posts in _wp_pages(client, self.data_url, self.per_page, since):
                for post in posts:
                    yield RawDoc(
                        id=f"{self.id}:{post['id']}",
                        source=self.id,
                        fetched_at=datetime.now(tz=UTC),
                        payload=post,
                        url=post.get("link"),
                    )
