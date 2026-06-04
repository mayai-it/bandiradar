"""Offline tests for the incentivi.gov.it adapter (Prompt 11).

Drives load_fixture() -> to_opportunities() with a fixed now against the recorded
real capture; no network.
"""

from datetime import UTC, datetime

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import incentivi
from bandiradar.sources.base import get, list_sources

# Fixture captured 2026-06-04 (see incentivi.json _captured).
NOW = datetime(2026, 6, 4, 0, 0, tzinfo=UTC)


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in incentivi.load_fixture():
        for opp in incentivi.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_returns_prefixed_rawdocs():
    raws = incentivi.load_fixture()
    assert len(raws) == 13
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("incentivi:") for r in raws)
    assert all(r.source == "incentivi" for r in raws)


def test_all_are_incentives_without_cpv():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.kind == "incentive"
        assert opp.id.startswith("incentivi:")
        assert opp.cpv == []  # incentives carry no CPV


def test_national_measure_mapping():
    # node/3400: granted by MIMIT -> national scope; open; digital services.
    opp = by_id()["incentivi:3400"]
    assert opp.kind == "incentive"
    assert opp.geo_scope == "national"  # MIMIT grantor
    assert opp.issuer_name == "Ministero delle Imprese e del Made in Italy"
    assert opp.value_min == 0.0
    assert opp.value_max == 1000000.0
    assert opp.value_currency == "EUR"
    assert opp.deadline is not None
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 6, 30)
    assert opp.status == "open"
    assert opp.eligibility_text  # the matcher relies on this text
    assert "digital" in opp.eligibility_text.lower()


def test_regional_measure_mapping():
    # node/5525: granted by a Provincia -> regional scope, region preserved.
    opp = by_id()["incentivi:5525"]
    assert opp.geo_scope == "regional"
    assert opp.region == "Trentino-Alto Adige/Südtirol"
    assert opp.value_min == 2000.0 and opp.value_max == 15000.0
    assert opp.status == "open"  # closes 2028-12-31


def test_closed_incentive_status():
    # node/1053 (Marche) closed well before NOW.
    assert by_id()["incentivi:1053"].status == "closed"


def test_open_and_closed_mix_present():
    statuses = [o.status for o in by_id().values()]
    assert statuses.count("open") == 6
    assert statuses.count("closed") == 7


def test_incentivi_is_registered():
    source = get("incentivi")
    assert source.id == "incentivi"
    assert source.kind == "incentive"
    assert "incentivi" in {s.id for s in list_sources()}


# --------------------------------------------------------------------------- #
# fetch() against the official export — mocked HTTP client (no network)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload, calls):
        self._payload = payload
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        self._calls.append((url, params))
        return _FakeResponse(self._payload)


def test_fetch_queries_official_export_endpoint(monkeypatch):
    docs = [r.payload for r in incentivi.load_fixture()[:2]]
    payload = {"response": {"docs": docs, "numFound": len(docs)}}
    calls: list = []
    monkeypatch.setattr(
        incentivi.httpx, "Client", lambda *a, **k: _FakeClient(payload, calls)
    )

    raws = list(incentivi.IncentiviSource().fetch())
    assert len(raws) == 2
    assert all(r.id.startswith("incentivi:") for r in raws)

    url, params = calls[0]
    assert url == incentivi.INCENTIVI_DATA_URL  # the official open-data export
    assert params["fq"] == "index_id:incentivi"
    assert params["fl"] == "*"


def test_fetch_since_filters_by_open_date(monkeypatch):
    docs = [r.payload for r in incentivi.load_fixture()[:1]]
    payload = {"response": {"docs": docs, "numFound": 1}}
    monkeypatch.setattr(
        incentivi.httpx, "Client", lambda *a, **k: _FakeClient(payload, [])
    )
    future = datetime(2099, 1, 1, tzinfo=UTC)
    assert list(incentivi.IncentiviSource().fetch(since=future)) == []
