"""Offline tests for the Regione Lazio (LazioInnova) source. No network."""

from datetime import UTC, datetime

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import lazio
from bandiradar.sources import wordpress as wp
from bandiradar.sources.base import get, list_sources

NOW = datetime(2026, 6, 6, 0, 0, tzinfo=UTC)


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in lazio.load_fixture():
        for opp in lazio.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = lazio.load_fixture()
    assert len(raws) == 15
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("lazio:") for r in raws)
    assert all(r.source == "lazio" for r in raws)


def test_all_regional_lazio_incentives():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.kind == "incentive"
        assert opp.geo_scope == "regional"
        assert opp.region == "Lazio"
        assert opp.cpv == []  # incentives carry no CPV
        assert opp.id.startswith("lazio:")


def test_field_mapping_known_bando():
    opp = by_id()["lazio:58887"]
    assert "Donne e Impresa" in opp.title
    assert opp.issuer_name == "LazioInnova"
    assert opp.issuer_region == "Lazio"
    assert "digitalizzazione" in opp.keywords
    assert opp.eligibility_text  # the bando body the matcher reads
    assert "lazioinnova.it" in opp.source_url
    assert opp.deadline is not None
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 6, 10)
    assert opp.status == "closing_soon"  # 2026-06-10 is within 7 days of NOW


def test_open_and_closed_status_present():
    opps = by_id()
    assert opps["lazio:59390"].status == "open"  # scadenza 2026-06-14 (future)
    assert opps["lazio:55402"].status == "closed"  # scadenza 2025-11-24 (past)


# --------------------------------------------------------------------------- #
# scadenza parser
# --------------------------------------------------------------------------- #


def test_parse_scadenza_month_name():
    text = "Il bando è pubblicato; la scadenza è alle ore 17:00 del 14 giugno 2026."
    d = lazio._parse_scadenza(text)
    assert d is not None and (d.year, d.month, d.day) == (2026, 6, 14)


def test_parse_scadenza_numeric_with_keyword():
    assert lazio._parse_scadenza("Domande entro il 10/06/2026.").day == 10


def test_parse_scadenza_ignores_non_deadline_dates():
    # A publication date with no deadline keyword nearby -> not treated as scadenza.
    assert lazio._parse_scadenza("Pubblicato sul BUR n. 35 del 30 aprile 2026.") is None


def test_parse_scadenza_none_when_absent():
    assert lazio._parse_scadenza("Bando a sportello senza data indicata.") is None


def test_lazio_registered():
    source = get("lazio")
    assert source.id == "lazio"
    assert source.kind == "incentive"
    assert "lazio" in {s.id for s in list_sources()}


# --------------------------------------------------------------------------- #
# fetch() via mocked WP REST client (no network)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, posts, calls):
        self._posts = posts
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        self._calls.append((url, params))
        # One page of posts (< per_page so pagination stops), then nothing.
        return _FakeResponse(self._posts if len(self._calls) == 1 else [])


def test_fetch_yields_rawdocs_via_mocked_client(monkeypatch):
    posts = [r.payload for r in lazio.load_fixture()]
    calls: list = []
    # fetch/pagination live in the shared WordPress base now.
    monkeypatch.setattr(wp.httpx, "Client", lambda *a, **k: _FakeClient(posts, calls))
    raws = list(get("lazio").fetch())
    assert len(raws) == len(posts)
    assert all(r.id.startswith("lazio:") for r in raws)
    url, _params = calls[0]
    assert url == lazio.LAZIO_DATA_URL
