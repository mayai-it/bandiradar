"""TED — Tenders Electronic Daily (EU) source adapter (ARCHITECTURE.md §5).

TED is the EU's portal for above-threshold public tenders — OPEN, biddable
notices, including large Italian public tenders. The search API is anonymous for
published notices (no key/auth).

``to_opportunities`` maps one TED notice AS IT APPEARS in the recorded fixture
(see ``data/fixtures/ted.json``, a real capture). ``fetch`` performs the live,
paginated, anonymous search; ``load_fixture`` keeps --sample fully offline.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from bandiradar import http, resources
from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.sources.base import ProgressFn, register

SOURCE_ID = "ted"
SOURCE_KIND: Kind = "tender"

# Confirmed: anonymous POST search endpoint (no auth) for published notices.
TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"

# eForms field names verified against the live OpenAPI spec + a real response.
TED_FIELDS = [
    "publication-number",
    "notice-title",
    "buyer-name",
    "buyer-country",
    "buyer-city",
    "place-of-performance-country-lot",
    "classification-cpv",
    "estimated-value-proc",
    "estimated-value-cur-proc",
    "estimated-value-lot",
    "estimated-value-cur-lot",
    "deadline-receipt-tender-date-lot",
    "deadline-receipt-tender-time-lot",
    "publication-date",
    "links",
]
# Italian notices; the live fetch adds a publication-date floor when `since` is set.
TED_BASE_QUERY = "buyer-country=ITA"
_PAGE_LIMIT = 100  # notices per page (API allows up to 250)
_MAX_NOTICES = 15000  # PAGE_NUMBER mode ceiling

FIXTURE_PATH = resources.fixture("ted.json")


# --------------------------------------------------------------------------- #
# Pure mapping helpers (operate on the real notice shape)
# --------------------------------------------------------------------------- #


def _multilang_text(value: Any, prefs: tuple[str, ...] = ("ita", "eng")) -> str | None:
    """Pick a string from a multilingual field ({lang: str} or {lang: [str]})."""
    if isinstance(value, dict):
        for lang in (*prefs, *value.keys()):
            picked = value.get(lang)
            if picked:
                return picked[0] if isinstance(picked, list) else picked
        return None
    if isinstance(value, list):
        return value[0] if value else None
    if isinstance(value, str):
        return value
    return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_deadline(record: dict[str, Any]) -> datetime | None:
    """Combine TED's split date + time deadline fields into one datetime.

    Date looks like "2026-07-07+01:00", time like "13:00:00+01:00". Either may be
    absent (some notices carry no tender deadline) -> returns None.
    """
    dates = record.get("deadline-receipt-tender-date-lot")
    if not dates:
        return None
    date_raw = dates[0]
    date_part = date_raw[:10]
    times = record.get("deadline-receipt-tender-time-lot")
    if times:
        iso = f"{date_part}T{times[0]}"
    else:
        offset = date_raw[10:] or "+00:00"
        offset = "+00:00" if offset == "Z" else offset
        iso = f"{date_part}T00:00:00{offset}"
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _parse_date(value: Any) -> datetime | None:
    """Parse a TED date like "2026-06-04+02:00" into a datetime."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    offset = value[10:] or "+00:00"
    offset = "+00:00" if offset == "Z" else offset
    try:
        return datetime.fromisoformat(f"{value[:10]}T00:00:00{offset}")
    except ValueError:
        return None


def _notice_url(record: dict[str, Any], pub_number: str) -> str:
    html = record.get("links", {}).get("html", {})
    if isinstance(html, dict) and html:
        return html.get("ENG") or next(iter(html.values()))
    return f"https://ted.europa.eu/en/notice/-/detail/{pub_number}"


def _document_urls(record: dict[str, Any]) -> list[str]:
    """The notice PDF link TED exposes (English preferred) — fed to enrichment."""
    pdf = record.get("links", {}).get("pdf", {})
    if isinstance(pdf, dict) and pdf:
        return [pdf.get("ENG") or next(iter(pdf.values()))]
    return []


def to_opportunities(raw: RawDoc, now: datetime | None = None) -> list[Opportunity]:
    """PURE mapping from one TED notice (``raw.payload``) to an Opportunity."""
    record: dict[str, Any] = raw.payload
    pub_number = record["publication-number"]

    deadline = _parse_deadline(record)
    value_amount = _to_float(record.get("estimated-value-proc"))
    currency = record.get("estimated-value-cur-proc")
    if value_amount is None:
        lots = record.get("estimated-value-lot") or []
        value_amount = _to_float(lots[0]) if lots else None
        cur_lots = record.get("estimated-value-cur-lot") or []
        currency = currency or (cur_lots[0] if cur_lots else None)

    cpv = sorted({str(c) for c in record.get("classification-cpv", []) if c})

    opportunity = Opportunity(
        id=f"ted:{pub_number}",
        source=SOURCE_ID,
        source_url=_notice_url(record, pub_number),
        kind=SOURCE_KIND,
        title=_multilang_text(record.get("notice-title")) or pub_number,
        summary=None,  # the search API does not return a description
        issuer_name=_multilang_text(record.get("buyer-name")),
        issuer_region=None,  # no NUTS/region field in the search response
        cpv=cpv,
        value_amount=value_amount,
        value_currency=currency or "EUR",
        geo_scope="eu",
        region=None,
        published_at=_parse_date(record.get("publication-date")),
        deadline=deadline,
        status=default_status(deadline, now),
        eligibility_text=None,
        document_urls=_document_urls(record),
        raw_ref=raw.id,
        # content_hash auto-fills.
    )
    return [opportunity]


# --------------------------------------------------------------------------- #
# Offline fixture + live fetch
# --------------------------------------------------------------------------- #


def load_fixture(path: Path | None = None) -> list[RawDoc]:
    """Read the recorded TED capture into RawDocs (offline, no network)."""
    package = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    fetched_at = _parse_date(package.get("_captured")) or datetime.fromisoformat(
        "1970-01-01T00:00:00+00:00"
    )
    raws: list[RawDoc] = []
    for notice in package.get("notices", []):
        pub_number = notice["publication-number"]
        raws.append(
            RawDoc(
                id=f"ted:{pub_number}",
                source=SOURCE_ID,
                fetched_at=fetched_at,
                payload=notice,
                url=_notice_url(notice, pub_number),
            )
        )
    return raws


def _build_query(since: datetime | None) -> str:
    query = TED_BASE_QUERY
    if since is not None:
        query = f"{query} AND publication-date>={since.date().isoformat()}"
    return f"{query} SORT BY publication-date DESC"


class TedSource:
    """TED source. Offline via load_fixture(); live anonymous paginated search."""

    id = SOURCE_ID
    kind: Kind = SOURCE_KIND

    def fetch(
        self,
        since: datetime | None = None,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
        progress: ProgressFn | None = None,
    ) -> Iterable[RawDoc]:
        """Live, anonymous, paginated search of TED for Italian notices.

        Yields one RawDoc per notice, LAZILY (a page at a time). Retries transient
        HTTP failures; raises a clear error if a page still fails.
        """
        return self._fetch_pages(since, limit, max_pages, progress)

    def _fetch_pages(
        self,
        since: datetime | None,
        limit: int | None,
        max_pages: int | None,
        progress: ProgressFn | None,
    ) -> Iterator[RawDoc]:
        query = _build_query(since)
        cap = limit if limit is not None else _MAX_NOTICES
        page = 1
        seen = 0
        with http.client() as client:
            while seen < cap and (max_pages is None or page <= max_pages):
                body = {
                    "query": query,
                    "fields": TED_FIELDS,
                    "page": page,
                    "limit": _PAGE_LIMIT,
                    "scope": "ACTIVE",
                    "paginationMode": "PAGE_NUMBER",
                }
                response = http.with_retry(
                    lambda body=body: client.post(
                        TED_SEARCH_URL,
                        json=body,
                        headers={"Accept": "application/json"},
                    ),
                    what="TED search",
                )
                # Classified raise: a 403 (UA/IP block) -> kind "blocked", not
                # "unknown", so the monitor can tell a block from an outage.
                http.raise_for_status(response, what="TED search")
                data = response.json()
                notices = data.get("notices") or []
                if not notices:
                    break
                for notice in notices:
                    if seen >= cap:
                        break
                    pub_number = notice["publication-number"]
                    seen += 1
                    yield RawDoc(
                        id=f"ted:{pub_number}",
                        source=SOURCE_ID,
                        fetched_at=datetime.now(tz=None).astimezone(),
                        payload=notice,
                        url=_notice_url(notice, pub_number),
                    )
                if progress is not None:
                    progress(f"ted: page {page}, {seen} fetched")
                if len(notices) < _PAGE_LIMIT:
                    break
                page += 1

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)

    def load_fixture(self) -> list[RawDoc]:
        return load_fixture()


register(TedSource())
