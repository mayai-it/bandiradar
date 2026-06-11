"""Offline tests for the Regione Emilia-Romagna (Plone `Bando`) source.

No network: drives load_fixture() -> to_opportunities() against the recorded
plone.restapi `@search?portal_type=Bando&fullobjects` capture. Exercises the
reusable Plone base — notably the STRUCTURED `scadenza_bando` deadline (no text
parsing), the vocabulary-derived keywords, and batching-link pagination (via a
REAL httpx client over a MockTransport, since the params-wipe bug lived in
httpx's request building, not in our parsing).
"""

import functools
import json
from datetime import UTC, datetime

import httpx

from bandiradar import http
from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import emilia_romagna as er
from bandiradar.sources.base import get, list_sources
from bandiradar.sources.plone import PloneBandoSource

# Capture (2026-06-12): scadenze span 2025-08 .. 2026-05 plus some open/rolling
# bandi with no deadline; this NOW yields an open/closing_soon/closed mix.
NOW = datetime(2026, 5, 15, 0, 0, tzinfo=UTC)


def opps() -> list[Opportunity]:
    out: list[Opportunity] = []
    for raw in er.load_fixture():
        out += er.to_opportunities(raw, now=NOW)
    return out


def find(title_substr: str) -> Opportunity:
    for o in opps():
        if title_substr.lower() in o.title.lower():
            return o
    raise AssertionError(f"no bando with title containing {title_substr!r}")


def test_load_fixture_prefixed_rawdocs():
    raws = er.load_fixture()
    assert len(raws) == 18
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("emilia_romagna:") for r in raws)
    assert all(r.source == "emilia_romagna" for r in raws)


def test_all_regional_er_incentives():
    items = opps()
    assert items
    for opp in items:
        assert opp.kind == "incentive"
        assert opp.geo_scope == "regional"
        assert opp.region == "Emilia-Romagna"
        assert opp.cpv == []
        assert opp.id.startswith("emilia_romagna:")


def test_structured_deadline_is_used_no_text_parsing():
    # The Plone `Bando` carries a real `scadenza_bando` -> we use it directly.
    opp = find("promozione della cittadinanza")
    assert opp.deadline is not None
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 5, 20)
    assert opp.status == "closing_soon"  # 2026-05-20 within 7 days of NOW
    assert "politicheterritoriali.regione.emilia-romagna.it" in opp.source_url
    # Keywords come from the Bando's structured vocabulary fields (tipologia/materie).
    assert any("Agevolazioni" in k for k in opp.keywords)


def test_open_closing_and_closed_status_present():
    items = {o.title: o for o in opps()}
    # A rolling/no-deadline bando reads as open.
    assert find("iscrizione all'elenco regionale").status == "open"
    assert find("promozione della cittadinanza").status == "closing_soon"  # 2026-05-20
    assert find("emergenza in Palestina e Ucraina").status == "closed"  # 2026-04-10
    assert items  # sanity


def test_emilia_romagna_is_registered():
    source = get("emilia_romagna")
    assert source.id == "emilia_romagna"
    assert source.kind == "incentive"
    assert "emilia_romagna" in {s.id for s in list_sources()}


# --------------------------------------------------------------------------- #
# Pagination regression — the params={} wipe bug.
#
# After page 1 the loop follows `batching.next`, whose URL already carries the
# FULL query (portal_type, b_start, …). Passing params={} to httpx REPLACES the
# URL's query string with nothing, so page 2 was requested unfiltered, the server
# answered with a listing whose `next` was always b_start=25, and the loop reread
# the same page up to the cap (prod: fetched=2000 vs 73 real Bando). These tests
# run a REAL httpx client over a MockTransport so the request-building semantics
# (where the bug lived) are exercised, not faked.
# --------------------------------------------------------------------------- #

_BASE = "https://plone.example.test"


def _bando(uid: str) -> dict:
    return {"UID": uid, "@id": f"{_BASE}/bandi/{uid}", "title": uid}


def _paged_handler(requests: list[httpx.Request]) -> httpx.MockTransport:
    """Two filtered pages; an UNFILTERED request (lost query) loops forever."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        q = dict(request.url.params)
        if q.get("portal_type") != "Bando":
            # The exact prod failure mode: query wiped -> unfiltered listing whose
            # `next` never advances. (The fix means we never land here.)
            payload = {
                "items": [_bando("unfiltered")],
                "batching": {"next": f"{_BASE}/++api++/@search?b_start=25"},
            }
        elif "b_start" not in q:
            payload = {
                "items": [_bando("page1-a"), _bando("page1-b")],
                "batching": {
                    "next": f"{_BASE}/++api++/@search?portal_type=Bando"
                    "&fullobjects=true&b_size=2&b_start=2"
                },
            }
        else:
            payload = {"items": [_bando("page2-a")]}  # no batching.next -> stop
        return httpx.Response(
            200, json=payload, headers={"content-type": "application/json"}
        )

    return httpx.MockTransport(handler)


def _patch_real_client_with_transport(monkeypatch, transport: httpx.MockTransport):
    """Route http.client() through REAL httpx with a mock transport injected."""
    real_client = httpx.Client
    monkeypatch.setattr(
        http.httpx, "Client", functools.partial(real_client, transport=transport)
    )


def test_pagination_preserves_next_link_query_and_terminates(monkeypatch):
    requests: list[httpx.Request] = []
    _patch_real_client_with_transport(monkeypatch, _paged_handler(requests))
    source = PloneBandoSource(
        id="plone_test", region="X", issuer_name="X", base_url=_BASE, b_size=2
    )

    raws = list(source.fetch(limit=50))

    # All three filtered items, no rereads, and the loop STOPPED without `next`.
    assert [r.id for r in raws] == [
        "plone_test:page1-a",
        "plone_test:page1-b",
        "plone_test:page2-a",
    ]
    assert len(requests) == 2
    # THE regression: page 2's request must keep the next-link query intact —
    # params={} must never again wipe portal_type/b_start off the URL.
    page2 = dict(requests[1].url.params)
    assert page2.get("portal_type") == "Bando"
    assert page2.get("b_start") == "2"
    assert page2.get("fullobjects") == "true"
    # And no request ever went out unfiltered.
    assert all(dict(r.url.params).get("portal_type") == "Bando" for r in requests)


def test_pagination_unfiltered_loop_is_bounded_by_limit_not_infinite(monkeypatch):
    # Belt-and-braces: even IF a future change loses the query again, the handler
    # above simulates the never-advancing listing; assert the fixed code never
    # fetches the 'unfiltered' marker at all.
    requests: list[httpx.Request] = []
    _patch_real_client_with_transport(monkeypatch, _paged_handler(requests))
    source = PloneBandoSource(
        id="plone_test", region="X", issuer_name="X", base_url=_BASE, b_size=2
    )
    raws = list(source.fetch(limit=10))
    assert all(
        json.loads(r.model_dump_json())["id"] != "plone_test:unfiltered" for r in raws
    )
    assert len(raws) == 3  # exactly the two real pages, then stop
