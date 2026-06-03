"""Offline tests for the ANAC/OCDS adapter (ARCHITECTURE.md §5 / Prompt 2).

No network, no secrets: drives ``load_fixture()`` -> ``to_opportunities()`` with
a FIXED ``now`` and asserts the field mapping, the derived status for the
closed / closing-soon fixtures, and registry wiring.
"""

from datetime import UTC, datetime

import pytest

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import anac
from bandiradar.sources.base import get, list_sources

# The fixture is a snapshot as of ~2026-06-03 (see anac_sample.json _note).
NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def opportunities_by_id() -> dict[str, Opportunity]:
    opps: dict[str, Opportunity] = {}
    for raw in anac.load_fixture():
        for opp in anac.to_opportunities(raw, now=NOW):
            opps[opp.id] = opp
    return opps


def test_load_fixture_returns_prefixed_rawdocs():
    raws = anac.load_fixture()
    assert len(raws) == 6
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("anac:") for r in raws)
    assert all(r.source == "anac" for r in raws)
    # tz-aware (model coerces naive -> UTC, but keeps already-aware zones as-is).
    assert raws[0].fetched_at.tzinfo is not None


def test_field_mapping_for_first_release(opportunities_by_id):
    opp = opportunities_by_id["anac:ocds-bandi-0001"]
    assert opp.source == "anac"
    assert opp.kind == "tender"
    assert opp.source_url == "https://example.invalid/anac/notice/ocds-bandi-0001"
    assert opp.title.startswith("Servizi di sviluppo")
    assert opp.summary and "piattaforma software" in opp.summary
    assert opp.issuer_name == "Comune di Roma Capitale"
    assert opp.issuer_region == "Lazio"
    assert opp.region == "Lazio"
    assert opp.geo_scope == "regional"
    assert opp.cpv == ["72000000"]
    assert opp.value_amount == 120000
    assert opp.value_currency == "EUR"
    assert opp.deadline == datetime(2026, 9, 15, 10, 0, tzinfo=UTC)  # 12:00+02:00
    assert opp.published_at == datetime(2026, 5, 20, 7, 0, tzinfo=UTC)  # 09:00+02:00
    assert opp.raw_ref == "anac:ocds-bandi-0001"
    assert opp.content_hash  # auto-filled
    assert opp.status == "open"


def test_all_ids_are_anac_prefixed(opportunities_by_id):
    assert opportunities_by_id  # non-empty
    assert all(oid.startswith("anac:") for oid in opportunities_by_id)


def test_national_tender_uses_coveredby_not_buyer_region(opportunities_by_id):
    # ocds-bandi-0004 (MEF) has tender.coveredBy == ["national"] with a Lazio buyer.
    opp = opportunities_by_id["anac:ocds-bandi-0004"]
    assert opp.geo_scope == "national"
    assert opp.region == "Lazio"  # region stays populated regardless of scope
    assert opp.issuer_region == "Lazio"


def test_other_releases_are_regional(opportunities_by_id):
    regional = {
        "anac:ocds-bandi-0001",
        "anac:ocds-bandi-0002",
        "anac:ocds-bandi-0003",
        "anac:ocds-bandi-0005",
        "anac:ocds-bandi-0006",
    }
    for oid in regional:
        assert opportunities_by_id[oid].geo_scope == "regional", oid


def test_closed_fixture_maps_to_closed_status(opportunities_by_id):
    # ocds-bandi-0003 deadline 2026-05-15 is in the past relative to NOW.
    assert opportunities_by_id["anac:ocds-bandi-0003"].status == "closed"


def test_closing_soon_fixture_maps_to_closing_soon_status(opportunities_by_id):
    # ocds-bandi-0002 deadline 2026-06-08 is within 7 days of NOW.
    assert opportunities_by_id["anac:ocds-bandi-0002"].status == "closing_soon"


def test_to_opportunities_is_deterministic():
    raw = anac.load_fixture()[0]
    a = anac.to_opportunities(raw, now=NOW)[0]
    b = anac.to_opportunities(raw, now=NOW)[0]
    assert a.content_hash == b.content_hash


def test_anac_is_registered():
    source = get("anac")
    assert source.id == "anac"
    assert source.kind == "tender"
    assert "anac" in {s.id for s in list_sources()}


def test_live_fetch_raises_until_endpoint_confirmed():
    with pytest.raises(NotImplementedError):
        list(anac.AnacSource().fetch())
