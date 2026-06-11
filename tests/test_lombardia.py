"""Offline tests for the Regione Lombardia adapter (Socrata SODA). No network."""

from datetime import UTC, datetime

from bandiradar import http
from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import lombardia
from bandiradar.sources.base import get, list_sources

NOW = datetime(2026, 6, 4, 0, 0, tzinfo=UTC)


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in lombardia.load_fixture():
        for opp in lombardia.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = lombardia.load_fixture()
    assert len(raws) == 15
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("lombardia:") for r in raws)
    assert all(r.source == "lombardia" for r in raws)


def test_all_regional_lombardia_tenders():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.kind == "tender"
        assert opp.geo_scope == "regional"
        assert opp.region == "Lombardia"
        assert opp.id.startswith("lombardia:")


def test_field_mapping_known_record():
    opp = by_id()["lombardia:141141"]
    assert opp.title == "ServBiblioteca 2026"
    assert opp.issuer_name == "COMUNE DI ROSATE"
    assert opp.issuer_region == "MILANO"  # province
    assert opp.cpv == ["92511000"]
    assert opp.value_amount == 245310.5
    assert opp.value_currency == "EUR"
    assert opp.deadline is not None
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 7, 5)
    assert opp.status == "open"
    assert "k6cb-4hbm" in opp.source_url and "141141" in opp.source_url


def test_open_and_closed_status_present():
    opps = by_id()
    assert opps["lombardia:140997"].status == "open"  # endoscopy, deadline 2026-06-15
    assert opps["lombardia:141340"].status == "closed"  # CENSIMENTO PONTI, past


def test_lombardia_registered():
    source = get("lombardia")
    assert source.id == "lombardia"
    assert source.kind == "tender"
    assert "lombardia" in {s.id for s in list_sources()}


# --------------------------------------------------------------------------- #
# fetch() with a mocked SODA client (no network) — dedupes lotti by bando
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, rows):
        self._rows = rows

    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


class _FakeClient:
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        self._calls.append((url, params))
        # Return the page once, then empty to stop pagination.
        page = self._rows if not self._calls[:-1] else []
        return _FakeResponse(page)


def test_fetch_dedupes_lotti_and_hits_soda(monkeypatch):
    # Two lotti rows of the same bando + one other bando -> 2 distinct RawDocs.
    rows = [
        {"codice_bando": "A", "n_lotto": "0", "oggetto_dell_appalto": "x"},
        {"codice_bando": "A", "n_lotto": "1", "oggetto_dell_appalto": "x"},
        {"codice_bando": "B", "n_lotto": "0", "oggetto_dell_appalto": "y"},
    ]
    calls: list = []
    monkeypatch.setattr(
        http.httpx, "Client", lambda *a, **k: _FakeClient(rows, calls)
    )
    raws = list(lombardia.LombardiaSource().fetch())
    assert {r.id for r in raws} == {"lombardia:A", "lombardia:B"}
    url, params = calls[0]
    assert url == lombardia.LOMBARDIA_DATA_URL
    assert params["$order"].startswith("data_pubblicazione")
