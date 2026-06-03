"""incentivi.gov.it — MIMIT national catalogue of business incentives.

The first grant/incentive source (``kind="incentive"``), exercising the canonical
superset (grants have no CPV, carry a funding range and an eligibility/benefit
text the matcher relies on). The portal publishes open data under the Italian
Open Data License v2.0 (IODL 2.0 — attribution required); the data is served by
a public Solr export endpoint.

``to_opportunities`` maps one incentive record AS IT APPEARS in the recorded
fixture (``data/fixtures/incentivi.json``, a real capture). National measures
(granted by a Ministero / national agency) map to ``geo_scope="national"``;
those granted by a Regione/Provincia/Camera di Commercio map to ``"regional"``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.sources.base import register

SOURCE_ID = "incentivi"
SOURCE_KIND: Kind = "incentive"

# Public Solr export behind incentivi.gov.it/it/open-data (index "incentivi").
INCENTIVI_DATA_URL = "https://www.incentivi.gov.it/solr/coredrupal/select"
_PAGE_ROWS = 200

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

FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "fixtures" / "incentivi.json"
)


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) else value


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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
        value_min=_to_float(_first(record.get("zs_field_cost_min"))),
        value_max=_to_float(_first(record.get("zs_field_cost_max"))),
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

    def fetch(self, since: datetime | None = None) -> Iterable[RawDoc]:
        """Live, paginated download of the public incentives dataset.

        Filters by publication (open) date >= ``since`` when provided. Raises a
        clear error on HTTP failure.
        """
        return self._fetch_pages(since)

    def _fetch_pages(self, since: datetime | None) -> Iterator[RawDoc]:
        start = 0
        with httpx.Client(timeout=60.0) as client:
            while True:
                params = {
                    "q": "*:*",
                    "fq": "index_id:incentivi",
                    "rows": _PAGE_ROWS,
                    "start": start,
                    "wt": "json",
                }
                try:
                    response = client.get(INCENTIVI_DATA_URL, params=params)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise RuntimeError(
                        f"incentivi.gov.it download failed: {exc}"
                    ) from exc

                payload = response.json().get("response", {})
                docs = payload.get("docs") or []
                if not docs:
                    break
                for doc in docs:
                    if since is not None:
                        opened = _parse_dt(_first(doc.get("zs_field_open_date")))
                        if opened is not None and opened < since:
                            continue
                    yield RawDoc(
                        id=f"incentivi:{_node_id(doc)}",
                        source=SOURCE_ID,
                        fetched_at=datetime.now().astimezone(),
                        payload=doc,
                        url=_first(doc.get("zs_field_link")),
                    )
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
