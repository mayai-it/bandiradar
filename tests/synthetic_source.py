"""Test-only synthetic OCDS-like source (NOT shipped).

The matcher/storage/CLI/MCP tests need a small, hand-crafted corpus that exercises
region-drop, CPV/keyword overlap, value gating, and a mix of open / closing-soon /
closed statuses. The real ``anac`` source is now wired to live OCDS data, which is
**regionless and all-historical**, so it can't serve that role.

This source owns that synthetic corpus (``tests/data/synthetic_ocds.json``) and
maps it with a region-aware OCDS mapping (buyer-party region + ``coveredBy`` scope)
— exactly the fixture the matcher tests were designed against. It registers under
id ``"synthetic"`` so ``--sample`` / ``get("synthetic")`` work in-process.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.sources.base import register

SOURCE_ID = "synthetic"
SOURCE_KIND: Kind = "tender"

FIXTURE_PATH = Path(__file__).resolve().parent / "data" / "synthetic_ocds.json"


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _buyer_region(release: dict[str, Any]) -> str | None:
    buyer_id = release.get("buyer", {}).get("id")
    for party in release.get("parties", []):
        if party.get("id") == buyer_id or "buyer" in party.get("roles", []):
            return party.get("address", {}).get("region")
    return None


def _cpv_codes(tender: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    for item in tender.get("items", []):
        classification = item.get("classification", {})
        scheme = str(classification.get("scheme", "")).upper()
        if scheme == "CPV" and classification.get("id"):
            codes.append(str(classification["id"]))
    return codes


def _geo_scope(tender: dict[str, Any], region: str | None) -> str:
    covered = {str(c).lower() for c in tender.get("coveredBy", [])}
    if "national" in covered:
        return "national"
    if covered & {"eu", "european", "europe"}:
        return "eu"
    if region is not None:
        return "regional"
    return "national"


def to_opportunities(raw: RawDoc, now: datetime | None = None) -> list[Opportunity]:
    release: dict[str, Any] = raw.payload
    ocid = release["ocid"]
    tender: dict[str, Any] = release.get("tender", {})
    value: dict[str, Any] = tender.get("value") or {}
    region = _buyer_region(release)
    deadline = _parse_dt(tender.get("tenderPeriod", {}).get("endDate"))
    description = tender.get("description")
    return [
        Opportunity(
            id=f"{SOURCE_ID}:{ocid}",
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
            geo_scope=_geo_scope(tender, region),
            region=region,
            published_at=_parse_dt(release.get("date")),
            deadline=deadline,
            status=default_status(deadline, now),
            eligibility_text=tender.get("eligibilityCriteria") or description,
            raw_ref=raw.id,
        )
    ]


def load_fixture(path: Path | None = None) -> list[RawDoc]:
    package = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    fetched_at = _parse_dt(package.get("publishedDate")) or datetime.fromisoformat(
        "1970-01-01T00:00:00+00:00"
    )
    return [
        RawDoc(
            id=f"{SOURCE_ID}:{release['ocid']}",
            source=SOURCE_ID,
            fetched_at=fetched_at,
            payload=release,
            url=release.get("url"),
        )
        for release in package.get("releases", [])
    ]


class SyntheticSource:
    """Test-only synthetic OCDS source (region-aware)."""

    id = SOURCE_ID
    kind: Kind = SOURCE_KIND

    def fetch(self, since: datetime | None = None) -> Iterable[RawDoc]:
        raise NotImplementedError("synthetic source is offline-only (load_fixture)")

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)

    def load_fixture(self) -> list[RawDoc]:
        return load_fixture()


register(SyntheticSource())
