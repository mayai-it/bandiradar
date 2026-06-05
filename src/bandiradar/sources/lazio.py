"""Regione Lazio — regional incentives source (LazioInnova bandi portal).

The Lazio CKAN portal (dati.regione.lazio.it) is unreachable and Lazio's CKAN
catalogue carries no open calls; the *live* open business incentives live on
LazioInnova, the regional development agency's bandi portal. LazioInnova is
WordPress, so we read its structured WP REST API (the portal's own JSON) rather
than scraping HTML.

``to_opportunities`` maps one WP `bandi` post to an Opportunity
(``kind="incentive"``, ``geo_scope="regional"``, ``region="Lazio"``). LazioInnova
exposes no structured deadline/value/CPV, so the scadenza date is parsed from the
free-text content and the bando body becomes ``eligibility_text``.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.sources.base import register

SOURCE_ID = "lazio"
SOURCE_KIND: Kind = "incentive"

# LazioInnova WordPress REST API (custom post type "bandi"); no auth.
LAZIO_DATA_URL = "https://www.lazioinnova.it/wp-json/wp/v2/bandi"
_PER_PAGE = 100

FIXTURE_PATH = Path(__file__).resolve().parents[3] / "data" / "fixtures" / "lazio.json"

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


def _parse_scadenza(text: str) -> datetime | None:
    """Find the deadline date in free-text bando content (None if not found).

    Only accepts a date preceded (within ~70 chars) by a deadline keyword, so the
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


def to_opportunities(raw: RawDoc, now: datetime | None = None) -> list[Opportunity]:
    """PURE mapping from one LazioInnova WP `bandi` post (``raw.payload``)."""
    post: dict[str, Any] = raw.payload
    content = _strip_html((post.get("content") or {}).get("rendered", ""))
    deadline = _parse_scadenza(content)
    class_list = post.get("class_list") or []
    keywords = _slugs(class_list, "tematiche-") + _slugs(class_list, "destinatari-")

    opportunity = Opportunity(
        id=f"lazio:{post['id']}",
        source=SOURCE_ID,
        source_url=post.get("link") or "",
        kind=SOURCE_KIND,
        title=_strip_html((post.get("title") or {}).get("rendered", ""))
        or str(post["id"]),
        summary=_strip_html((post.get("excerpt") or {}).get("rendered", "")) or None,
        issuer_name="LazioInnova",
        issuer_region="Lazio",
        cpv=[],
        keywords=keywords,
        geo_scope="regional",
        region="Lazio",
        published_at=_parse_dt(post.get("date")),
        deadline=deadline,
        status=default_status(deadline, now),
        eligibility_text=content or None,
        raw_ref=raw.id,
        # content_hash auto-fills.
    )
    return [opportunity]


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def load_fixture(path: Path | None = None) -> list[RawDoc]:
    """Read the recorded LazioInnova capture into RawDocs (offline)."""
    package = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    fetched_at = _parse_dt(package.get("_captured")) or datetime.fromisoformat(
        "1970-01-01T00:00:00+00:00"
    )
    raws: list[RawDoc] = []
    for post in package.get("posts", []):
        raws.append(
            RawDoc(
                id=f"lazio:{post['id']}",
                source=SOURCE_ID,
                fetched_at=fetched_at,
                payload=post,
                url=post.get("link"),
            )
        )
    return raws


def _wp_pages(
    client: httpx.Client, since: datetime | None
) -> Iterator[list[dict[str, Any]]]:
    """Yield successive pages of WP `bandi` posts (reusable WP-REST pagination)."""
    page = 1
    while True:
        params: dict[str, Any] = {"per_page": _PER_PAGE, "page": page}
        if since is not None:
            params["after"] = since.astimezone(UTC).isoformat()
        try:
            response = client.get(LAZIO_DATA_URL, params=params)
            if response.status_code == 400:  # WP returns 400 past the last page
                break
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"LazioInnova fetch failed: {exc}") from exc
        posts = response.json()
        if not posts:
            break
        yield posts
        if len(posts) < _PER_PAGE:
            break
        page += 1


class LazioSource:
    """Regione Lazio source via LazioInnova. Offline via load_fixture()."""

    id = SOURCE_ID
    kind: Kind = SOURCE_KIND

    def fetch(self, since: datetime | None = None) -> Iterable[RawDoc]:
        return self._fetch(since)

    def _fetch(self, since: datetime | None) -> Iterator[RawDoc]:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            for posts in _wp_pages(client, since):
                for post in posts:
                    yield RawDoc(
                        id=f"lazio:{post['id']}",
                        source=SOURCE_ID,
                        fetched_at=datetime.now(tz=UTC),
                        payload=post,
                        url=post.get("link"),
                    )

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)

    def load_fixture(self) -> list[RawDoc]:
        return load_fixture()


register(LazioSource())
