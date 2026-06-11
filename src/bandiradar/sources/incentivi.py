"""incentivi.gov.it — MIMIT national catalogue of business incentives.

The first grant/incentive source (``kind="incentive"``), exercising the canonical
superset (grants have no CPV, carry a funding range and an eligibility/benefit
text the matcher relies on). incentivi.gov.it publishes its catalogue as open
data under the Italian Open Data License v2.0 (IODL 2.0 — attribution required:
Ministero delle Imprese e del Made in Italy).

Live source = the OFFICIAL open-data export. Note (verified by tracing the
open-data page's own download button in its theme JS): incentivi.gov.it does NOT
serve a separate static IODL file — its "Scarica dataset" button builds the
JSON/CSV download client-side from the export endpoint below (Solr index
``incentivi``, ``fl=*``, ``fq=index_id:incentivi``). So this endpoint IS the
official open-data export, not an internal search hack; we query it the same way
the page does. (dati.gov.it carries no MIMIT incentivi resource to point at
instead.)

``to_opportunities`` maps one incentive record AS IT APPEARS in the recorded
fixture (``data/fixtures/incentivi.json``, a real capture from this export).
National measures (granted by a Ministero / national agency) map to
``geo_scope="national"``; those granted by a Regione/Provincia/Camera di
Commercio map to ``"regional"``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandiradar import http, resources
from bandiradar.models import (
    Kind,
    Opportunity,
    RawDoc,
    default_status,
    sanitize_value_bounds,
)
from bandiradar.sources.base import ProgressFn, register

SOURCE_ID = "incentivi"
SOURCE_KIND: Kind = "incentive"

# Official incentivi.gov.it open-data export endpoint (IODL 2.0). This is exactly
# what the open-data page's download button queries (index "incentivi", fl=*);
# there is no separate static file to fetch instead.
INCENTIVI_DATA_URL = "https://www.incentivi.gov.it/solr/coredrupal/select"
_PAGE_ROWS = 200
_MAX_RECORDS = 20000  # safety ceiling when no explicit limit is given

# Granting bodies that make a measure national in scope (geo bypass), even though
# every record is also tagged to a region.
_NATIONAL_GRANTOR_MARKERS = (
    "minister",
    "invitalia",
    "mimit",
    "agenzia nazionale",
    "cybersicurezza nazionale",
    "presidenza del consiglio",
)

FIXTURE_PATH = resources.fixture("incentivi.json")


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) else value


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO timestamp; naive values are assumed to be UTC (tz-aware out)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _node_id(record: dict[str, Any]) -> str:
    match = re.search(r"node/(\d+)", str(record.get("ss_search_api_id", "")))
    return match.group(1) if match else str(record.get("id") or record.get("hash"))


def _is_national(grantor: str | None) -> bool:
    g = (grantor or "").lower()
    return any(marker in g for marker in _NATIONAL_GRANTOR_MARKERS)


def to_opportunities(raw: RawDoc, now: datetime | None = None) -> list[Opportunity]:
    """PURE map of one incentive record (``raw.payload``) to an Opportunity."""
    record: dict[str, Any] = raw.payload

    grantor = _first(record.get("zs_field_subject_grant"))
    region_value = _first(record.get("zs_field_regions_value"))
    national = _is_national(grantor)

    body = _first(record.get("zs_body")) or _first(record.get("tum_X3b_it_body_ft"))
    deadline = _parse_dt(_first(record.get("zs_field_close_date")))

    # Tags/sector become keywords; ATECO here is free-text prose, not codes.
    keywords = [
        v
        for v in (
            _first(record.get("zs_field_scopes_value")),
            _first(record.get("zs_field_activity_sector_value")),
            _first(record.get("zs_field_dimensions_value")),
        )
        if v
    ]
    summary_bits = [
        _first(record.get("zs_field_scopes_value")),
        _first(record.get("zs_field_support_form_value")),
        _first(record.get("zs_field_dimensions_value")),
    ]
    summary = "; ".join(b for b in summary_bits if b) or None

    # Real records sometimes carry a transposed cost_min/cost_max; sanitize so one
    # dirty row can't fail the whole ingestion (the mapper stays pure).
    value_min, value_max = sanitize_value_bounds(
        _to_float(_first(record.get("zs_field_cost_min"))),
        _to_float(_first(record.get("zs_field_cost_max"))),
    )

    opportunity = Opportunity(
        id=f"incentivi:{_node_id(record)}",
        source=SOURCE_ID,
        source_url=_first(record.get("zs_field_link")) or "",
        kind=SOURCE_KIND,
        title=_first(record.get("tum_X3b_it_title_ft")) or _node_id(record),
        summary=summary,
        issuer_name=grantor,
        issuer_region=region_value,
        cpv=[],  # incentives carry no CPV
        keywords=keywords,
        value_min=value_min,
        value_max=value_max,
        value_currency="EUR",
        geo_scope="national" if national else "regional",
        region=None if national else region_value,
        published_at=_parse_dt(_first(record.get("zs_field_open_date"))),
        deadline=deadline,
        status=default_status(deadline, now),
        eligibility_text=body,  # requirements/benefit text — the matcher uses it
        raw_ref=raw.id,
        # content_hash auto-fills.
    )
    return [opportunity]


def load_fixture(path: Path | None = None) -> list[RawDoc]:
    """Read the recorded incentivi capture into RawDocs (offline)."""
    package = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    fetched_at = _parse_dt(package.get("_captured")) or datetime.fromisoformat(
        "1970-01-01T00:00:00+00:00"
    )
    raws: list[RawDoc] = []
    for doc in package.get("docs", []):
        raws.append(
            RawDoc(
                id=f"incentivi:{_node_id(doc)}",
                source=SOURCE_ID,
                fetched_at=fetched_at,
                payload=doc,
                url=_first(doc.get("zs_field_link")),
            )
        )
    return raws


class IncentiviSource:
    """incentivi.gov.it source. Offline via load_fixture(); live Solr download."""

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
        """Live, paginated download of the public incentives dataset, LAZILY.

        Filters by publication (open) date >= ``since`` when provided. Retries
        transient HTTP failures; raises a clear error if a page still fails.
        """
        return self._fetch_pages(since, limit, max_pages, progress)

    def _fetch_pages(
        self,
        since: datetime | None,
        limit: int | None,
        max_pages: int | None,
        progress: ProgressFn | None,
    ) -> Iterator[RawDoc]:
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        cap = limit if limit is not None else _MAX_RECORDS
        start = 0
        page = 0
        seen = 0
        with http.client() as client:
            while seen < cap and (max_pages is None or page < max_pages):
                page += 1
                # Same parameters the open-data page uses for its official export
                # (fl=* full records, fq restricts to the incentives index).
                params = {
                    "q": "*:*",
                    "q.op": "OR",
                    "fq": "index_id:incentivi",
                    "fl": "*",
                    "rows": _PAGE_ROWS,
                    "start": start,
                    "wt": "json",
                }
                response = http.with_retry(
                    lambda params=params: client.get(INCENTIVI_DATA_URL, params=params),
                    what="incentivi.gov.it download",
                )
                http.raise_for_status(response, what="incentivi.gov.it download")
                payload = response.json().get("response", {})
                docs = payload.get("docs") or []
                if not docs:
                    break
                for doc in docs:
                    if seen >= cap:
                        break
                    if since is not None:
                        opened = _parse_dt(_first(doc.get("zs_field_open_date")))
                        if opened is not None and opened < since:
                            continue
                    seen += 1
                    yield RawDoc(
                        id=f"incentivi:{_node_id(doc)}",
                        source=SOURCE_ID,
                        fetched_at=datetime.now().astimezone(),
                        payload=doc,
                        url=_first(doc.get("zs_field_link")),
                    )
                if progress is not None:
                    progress(f"incentivi: page {page}, {seen} fetched")
                start += _PAGE_ROWS
                if start >= payload.get("numFound", 0):
                    break

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)

    def load_fixture(self) -> list[RawDoc]:
        return load_fixture()


register(IncentiviSource())
