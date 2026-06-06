"""Offline tests for the ANAC/OCDS adapter (ARCHITECTURE.md §5).

No network, no secrets: drives ``load_fixture()`` -> ``to_opportunities()`` over
RECORDED REAL OCDS releases, and exercises ``fetch()`` with a MOCKED release
stream (no network). The data is retrospective (awarded contracts), so every
opportunity maps to ``status="closed"`` — that's correct.
"""

from datetime import UTC, datetime

import pytest

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import anac
from bandiradar.sources.base import get, list_sources

NOW = datetime(2026, 6, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def opportunities_by_id() -> dict[str, Opportunity]:
    opps: dict[str, Opportunity] = {}
    for raw in anac.load_fixture():
        for opp in anac.to_opportunities(raw, now=NOW):
            opps[opp.id] = opp
    return opps


def test_load_fixture_returns_prefixed_rawdocs():
    raws = anac.load_fixture()
    assert len(raws) == 12
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("anac:") for r in raws)
    assert all(r.source == "anac" for r in raws)
    assert raws[0].fetched_at.tzinfo is not None


def test_field_mapping_for_first_release(opportunities_by_id):
    opp = opportunities_by_id["anac:ocds-hu01ve-8114164"]
    assert opp.source == "anac"
    assert opp.kind == "tender"
    assert opp.issuer_name == "COMUNE DI TRANI"
    # No region/NUTS in OCP/ANAC data -> region None, national scope.
    assert opp.region is None
    assert opp.issuer_region is None
    assert opp.geo_scope == "national"
    assert opp.cpv == ["72322000-8"]
    assert opp.value_amount == 196721.31
    assert opp.value_currency == "EUR"
    # tender.title is null in the real data -> falls back to the description.
    assert opp.title.startswith("SERVIZIO TRIENNALE")
    assert opp.summary and "SERVIZIO TRIENNALE" in opp.summary
    # endDate 2022-02-18T12:00:00Z -> deadline in the past.
    assert opp.deadline == datetime(2022, 2, 18, 12, 0, tzinfo=UTC)
    # Malformed compiled `date` -> parsed to its leading date at UTC midnight.
    assert opp.published_at == datetime(2025, 1, 8, 0, 0, tzinfo=UTC)
    assert opp.raw_ref == "anac:ocds-hu01ve-8114164"
    assert opp.content_hash
    assert opp.status == "closed"


def test_value_prefers_award_over_tender(opportunities_by_id):
    # ocds-hu01ve-8315069: award value 811537.41 (tender value is 820417.76).
    assert opportunities_by_id["anac:ocds-hu01ve-8315069"].value_amount == 811537.41


def test_all_releases_are_historical_closed(opportunities_by_id):
    assert opportunities_by_id  # non-empty
    for opp in opportunities_by_id.values():
        assert opp.status == "closed"  # retrospective awarded contracts
        assert opp.region is None
        assert opp.geo_scope == "national"
        assert opp.id.startswith("anac:")


def test_to_opportunities_is_deterministic():
    raw = anac.load_fixture()[0]
    a = anac.to_opportunities(raw, now=NOW)[0]
    b = anac.to_opportunities(raw, now=NOW)[0]
    assert a.content_hash == b.content_hash


# --------------------------------------------------------------------------- #
# fetch() — mocked release stream (no network)
# --------------------------------------------------------------------------- #


def _fake_releases(n: int, date: str = "2025-03-01T00:00:00Z"):
    return [{"ocid": f"ocds-test-{i}", "date": date, "tender": {}} for i in range(n)]


def test_fetch_caps_at_max_items():
    raws = list(
        anac.AnacSource().fetch(
            max_items=3,
            year=2025,
            streamer=lambda _year: _fake_releases(10),
        )
    )
    assert len(raws) == 3
    assert [r.id for r in raws] == [
        "anac:ocds-test-0",
        "anac:ocds-test-1",
        "anac:ocds-test-2",
    ]


def test_fetch_filters_by_since():
    releases = [
        {"ocid": "old", "date": "2025-01-01T00:00:00Z", "tender": {}},
        {"ocid": "new", "date": "2025-12-01T00:00:00Z", "tender": {}},
    ]
    raws = list(
        anac.AnacSource().fetch(
            since=datetime(2025, 6, 1, tzinfo=UTC),
            year=2025,
            streamer=lambda _year: list(releases),
        )
    )
    assert [r.id for r in raws] == ["anac:new"]


def test_fetch_falls_back_to_previous_year_on_error():
    def streamer(year: int):
        if year == 2026:
            raise RuntimeError("ANAC OCDS download failed (2026): 404")
        return _fake_releases(2)

    raws = list(anac.AnacSource().fetch(year=2026, streamer=streamer))
    assert len(raws) == 2  # fell back to 2025


def test_fetch_is_memory_safe_lazy():
    # The streamer yields lazily; fetch must not exhaust it beyond max_items.
    pulled = []

    def streamer(_year: int):
        for i in range(1000):
            pulled.append(i)
            yield {"ocid": f"ocds-{i}", "date": "2025-03-01T00:00:00Z", "tender": {}}

    list(anac.AnacSource().fetch(max_items=5, year=2025, streamer=streamer))
    assert len(pulled) == 5  # only 5 releases were ever generated


def test_anac_is_registered():
    source = get("anac")
    assert source.id == "anac"
    assert source.kind == "tender"
    assert "anac" in {s.id for s in list_sources()}
