"""Offline tests for the TED adapter (ARCHITECTURE.md §5 / Prompt 10).

No network: drives load_fixture() -> to_opportunities() with a fixed now against
the recorded real capture, and exercises fetch() with a mocked HTTP client.
"""

from datetime import UTC, datetime

import pytest

from bandiradar import http
from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import ted
from bandiradar.sources.base import get, list_sources

# The fixture was captured 2026-06-04 (see ted.json _captured).
NOW = datetime(2026, 6, 4, 0, 0, tzinfo=UTC)


@pytest.fixture
def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in ted.load_fixture():
        for opp in ted.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_returns_prefixed_rawdocs():
    raws = ted.load_fixture()
    assert len(raws) == 12
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("ted:") for r in raws)
    assert all(r.source == "ted" for r in raws)


def test_all_ids_prefixed_and_eu_scope(by_id):
    assert by_id
    for opp in by_id.values():
        assert opp.id.startswith("ted:")
        assert opp.source == "ted"
        assert opp.kind == "tender"
        assert opp.geo_scope == "eu"
        assert opp.region is None  # no NUTS field in the search response


def test_field_mapping_for_known_notice(by_id):
    opp = by_id["ted:382630-2026"]
    assert opp.title  # multilingual title resolved to a string
    assert opp.issuer_name == "Comune di Siracusa"
    assert opp.cpv == ["72512000"]  # deduped from the repeated list
    assert opp.value_amount == 1362509.46  # estimated-value-proc
    assert opp.value_currency == "EUR"
    assert opp.deadline is not None
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 7, 20)
    assert opp.deadline.tzinfo is not None  # tz-aware (normalized to UTC)
    assert "ted.europa.eu" in opp.source_url
    assert "382630-2026" in opp.source_url
    assert opp.status == "open"  # deadline in the future


def test_missing_deadline_maps_to_closed(by_id):
    # 383667-2026 carries no tender deadline in the fixture. A biddable TED call always
    # states a deadline, so a notice WITHOUT one (award/result/PIN) maps to CLOSED — it
    # is not an open, biddable call and must not surface as one.
    opp = by_id["ted:383667-2026"]
    assert opp.deadline is None
    assert opp.status == "closed"


def test_missing_value_is_none(by_id):
    # 376324-2026 has no estimated value in the fixture.
    assert by_id["ted:376324-2026"].value_amount is None


def test_deadlined_notices_are_active_undated_are_closed(by_id):
    # The capture is of ACTIVE notices, so any WITH a (future) deadline is not closed;
    # any WITHOUT a deadline is closed (the no-deadline = not-biddable rule).
    for o in by_id.values():
        if o.deadline is None:
            assert o.status == "closed"
        else:
            assert o.status != "closed"


def test_ted_is_registered():
    source = get("ted")
    assert source.id == "ted"
    assert source.kind == "tender"
    assert "ted" in {s.id for s in list_sources()}


# --------------------------------------------------------------------------- #
# fetch() with a mocked HTTP client (no network)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """One page of results, then the loop stops (len < page limit)."""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        return _FakeResponse(self._payload)


def test_fetch_yields_rawdocs_with_mocked_client(monkeypatch):
    notice = ted.load_fixture()[0].payload
    payload = {"notices": [notice], "totalNoticeCount": 1}
    monkeypatch.setattr(http.httpx, "Client", lambda *a, **k: _FakeClient(payload))

    raws = list(ted.TedSource().fetch())
    assert len(raws) == 1
    assert raws[0].id == f"ted:{notice['publication-number']}"
    assert raws[0].source == "ted"


def test_fetch_raises_clear_error_on_http_failure(monkeypatch):
    import httpx

    class _BoomClient(_FakeClient):
        def post(self, url, json=None, headers=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(http.httpx, "Client", lambda *a, **k: _BoomClient(None))
    with pytest.raises(RuntimeError, match="TED search failed"):
        list(ted.TedSource().fetch())
