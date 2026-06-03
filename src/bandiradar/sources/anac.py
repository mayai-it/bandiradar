"""ANAC / PNCP (OCDS) reference adapter (ARCHITECTURE.md §5).

The backbone source: real, structured, free open-contracting data. Maps an
ANAC/OCDS *release* (the shape in ``data/fixtures/anac_sample.json``) to
``Opportunity`` objects with a PURE :func:`to_opportunities`, and exposes
:func:`load_fixture` so offline use and tests need no network or secrets.

The live :meth:`AnacSource.fetch` is intentionally not wired: the real PNCP/ANAC
open-data endpoint must be confirmed against current docs first (see
:data:`ANAC_OPENDATA_URL`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.sources.base import register

SOURCE_ID = "anac"
SOURCE_KIND: Kind = "tender"

# TODO: confirm the current PNCP/ANAC open-data endpoint against live docs before
# wiring the live fetch. Do NOT invent a URL — leave empty until verified.
ANAC_OPENDATA_URL = ""

# Bundled offline fixture (an OCDS release package). Resolved relative to the
# repo layout (src/bandiradar/sources/anac.py -> <repo>/data/fixtures/...).
FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "fixtures" / "anac_sample.json"
)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an OCDS ISO-8601 timestamp; ``None``/empty -> ``None``.

    The model coerces any naive result to UTC, so we keep parsing simple here.
    """
    if not value:
        return None
    return datetime.fromisoformat(value)


def _buyer_region(release: dict[str, Any]) -> str | None:
    """Region from the buyer party's address (None if absent)."""
    buyer_id = release.get("buyer", {}).get("id")
    for party in release.get("parties", []):
        if party.get("id") == buyer_id or "buyer" in party.get("roles", []):
            return party.get("address", {}).get("region")
    return None


def _cpv_codes(tender: dict[str, Any]) -> list[str]:
    """CPV ids from each item's classification (scheme == CPV)."""
    codes: list[str] = []
    for item in tender.get("items", []):
        classification = item.get("classification", {})
        is_cpv = str(classification.get("scheme", "")).upper() == "CPV"
        if is_cpv and classification.get("id"):
            codes.append(str(classification["id"]))
    return codes


def to_opportunities(raw: RawDoc, now: datetime | None = None) -> list[Opportunity]:
    """PURE mapping from one OCDS release (``raw.payload``) to ``Opportunity``.

    No I/O. ``now`` is forwarded to :func:`default_status` so tests can pin the
    derived status deterministically.
    """
    release: dict[str, Any] = raw.payload
    ocid = release["ocid"]
    tender: dict[str, Any] = release.get("tender", {})
    value: dict[str, Any] = tender.get("value") or {}

    region = _buyer_region(release)
    deadline = _parse_dt(tender.get("tenderPeriod", {}).get("endDate"))
    description = tender.get("description")

    opportunity = Opportunity(
        id=f"anac:{ocid}",
        source=SOURCE_ID,
        source_url=release.get("url") or raw.url or "",
        kind=SOURCE_KIND,
        title=tender.get("title") or ocid,
        summary=description,
        issuer_name=release.get("buyer", {}).get("name"),
        issuer_region=region,
        cpv=_cpv_codes(tender),
        value_amount=value.get("amount"),
        value_currency=value.get("currency") or "EUR",
        geo_scope="national" if region is None else "regional",
        region=region,
        published_at=_parse_dt(release.get("date")),
        deadline=deadline,
        status=default_status(deadline, now),
        eligibility_text=tender.get("eligibilityCriteria") or description,
        raw_ref=raw.id,
        # content_hash auto-fills from the canonical fields.
    )
    return [opportunity]


def load_fixture(path: Path | None = None) -> list[RawDoc]:
    """Read the bundled OCDS release package into ``RawDoc``s (offline).

    Each OCDS release becomes one ``RawDoc`` whose ``id`` is the source-prefixed
    ocid (mirroring the Opportunity ``raw_ref``) and whose ``payload`` is the
    untouched release.
    """
    fixture_path = path or FIXTURE_PATH
    package = json.loads(fixture_path.read_text(encoding="utf-8"))
    fetched_at = _parse_dt(package.get("publishedDate")) or datetime.fromisoformat(
        "1970-01-01T00:00:00+00:00"
    )
    raws: list[RawDoc] = []
    for release in package.get("releases", []):
        ocid = release["ocid"]
        raws.append(
            RawDoc(
                id=f"anac:{ocid}",
                source=SOURCE_ID,
                fetched_at=fetched_at,
                payload=release,
                url=release.get("url"),
            )
        )
    return raws


class AnacSource:
    """ANAC/PNCP source. Offline via :func:`load_fixture`; live fetch is TODO."""

    id = SOURCE_ID
    kind: Kind = SOURCE_KIND

    def fetch(self, since: datetime | None = None) -> Iterable[RawDoc]:
        """Live fetch from PNCP/ANAC open data — not yet wired.

        The endpoint must be confirmed against current docs (see
        :data:`ANAC_OPENDATA_URL`). For offline use, call :func:`load_fixture`
        (the CLI ``--sample`` mode does this).
        """
        if not ANAC_OPENDATA_URL:
            raise NotImplementedError(
                "Live ANAC/PNCP fetch is not wired yet: confirm the current "
                "open-data endpoint against PNCP/ANAC docs and set "
                "ANAC_OPENDATA_URL. For offline use call load_fixture() "
                "(CLI: `bandiradar fetch --source anac --sample`)."
            )
        # TODO: implement the live OCDS fetch (httpx) against ANAC_OPENDATA_URL,
        # honoring `since`, once the endpoint is verified.
        raise NotImplementedError("Live ANAC/PNCP fetch not implemented yet.")

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)


register(AnacSource())
