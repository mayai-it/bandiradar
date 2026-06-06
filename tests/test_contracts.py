"""Recorded-response CONTRACT tests (offline, in CI).

Each test drives a source's REAL ``fetch()`` against a recorded response cassette
(envelope INCLUDED — the pagination wrapper, not just inner records) via an
``httpx.MockTransport``, then asserts the envelope is parsed, RawDocs are produced,
and ``to_opportunities`` maps them to the expected canonical fields. This pins our
fetch+parse to reality and catches a parsing regression or a silently-wrong mock.

The opt-in LIVE drift check (``-m live``, network) lives in ``test_live.py``; see
CONTRIBUTING for how to re-record a cassette when an API changes.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from bandiradar import http as http_mod
from bandiradar.sources import anac, incentivi, lombardia, ted, wordpress  # noqa: F401
from bandiradar.sources.base import get
from bandiradar.sources.llm_scraper import InMemoryExtractionCache

CASSETTES = Path(__file__).parent / "cassettes"
NOW = datetime(2026, 6, 6, 0, 0, tzinfo=UTC)

# Capture the REAL httpx.Client/stream BEFORE any monkeypatch, so the mock factory
# never recurses into a patched httpx.Client.
_REAL_CLIENT = httpx.Client


def _json(name: str):
    return json.loads((CASSETTES / name).read_text(encoding="utf-8"))


def _client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        return _REAL_CLIENT(transport=transport)

    return factory


# --------------------------------------------------------------------------- #
# TED — POST search; envelope {"notices": [...], "totalNoticeCount": N}
# --------------------------------------------------------------------------- #


def test_ted_contract(monkeypatch):
    cassette = _json("ted.json")
    expected_id = f"ted:{cassette['notices'][0]['publication-number']}"

    def handler(request):
        assert request.method == "POST"  # TED search is a POST
        return httpx.Response(200, json=cassette)

    monkeypatch.setattr(ted.httpx, "Client", _client_factory(handler))
    raws = list(ted.TedSource().fetch(limit=10))

    assert [r.id for r in raws] == [expected_id]  # envelope parsed -> one RawDoc
    [opp] = ted.to_opportunities(raws[0], now=NOW)
    assert opp.id == expected_id
    assert opp.source == "ted" and opp.kind == "tender" and opp.geo_scope == "eu"
    assert opp.title and isinstance(opp.cpv, list)


# --------------------------------------------------------------------------- #
# incentivi — Solr; envelope {"response": {"numFound": N, "docs": [...]}}
# --------------------------------------------------------------------------- #


def test_incentivi_contract(monkeypatch):
    cassette = _json("incentivi.json")
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=cassette)

    monkeypatch.setattr(incentivi.httpx, "Client", _client_factory(handler))
    raws = list(incentivi.IncentiviSource().fetch(limit=10))

    assert len(raws) == 1  # response.response.docs parsed; numFound stops paging
    assert calls and calls[0].url.params["fq"] == "index_id:incentivi"
    [opp] = incentivi.to_opportunities(raws[0], now=NOW)
    assert opp.id.startswith("incentivi:")
    assert opp.source == "incentivi" and opp.kind == "incentive"
    assert opp.cpv == []  # incentives carry no CPV
    assert opp.value_currency == "EUR"


# --------------------------------------------------------------------------- #
# Lombardia — Socrata SODA; a bare JSON ARRAY of rows
# --------------------------------------------------------------------------- #


def test_lombardia_contract(monkeypatch):
    rows = _json("lombardia.json")
    assert isinstance(rows, list)

    def handler(request):
        return httpx.Response(200, json=rows)

    monkeypatch.setattr(lombardia.httpx, "Client", _client_factory(handler))
    raws = list(lombardia.LombardiaSource().fetch(limit=10))

    expected = {f"lombardia:{r['codice_bando']}" for r in rows}
    assert {r.id for r in raws} == expected  # bare-array envelope parsed + deduped
    [opp] = lombardia.to_opportunities(raws[0], now=NOW)
    assert opp.source == "lombardia" and opp.kind == "tender"
    assert opp.region == "Lombardia" and opp.geo_scope == "regional"


# --------------------------------------------------------------------------- #
# Lazio — WordPress REST; a bare JSON ARRAY of posts
# --------------------------------------------------------------------------- #


def test_lazio_contract(monkeypatch):
    posts = _json("lazio.json")
    assert isinstance(posts, list)

    def handler(request):
        # WP returns the page once; an out-of-range page would 400/empty.
        page = request.url.params.get("page")
        if page and int(page) > 1:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=posts)

    monkeypatch.setattr(wordpress.httpx, "Client", _client_factory(handler))
    src = get("lazio")
    raws = list(src.fetch(limit=10))

    assert {r.id for r in raws} == {f"lazio:{p['id']}" for p in posts}
    [opp] = src.to_opportunities(raws[0], now=NOW)
    assert opp.source == "lazio" and opp.kind == "incentive"
    assert opp.region == "Lazio" and opp.geo_scope == "regional"


# --------------------------------------------------------------------------- #
# Toscana — WP-REST listing + a detail HTML page (LLM extraction MOCKED)
# --------------------------------------------------------------------------- #


class _FakeLLM:
    """Stand-in for the LLM client — we contract-test the crawl/HTTP, not the LLM."""

    def score(self, system: str, user: str) -> dict:
        return {
            "title": "Bando estratto",
            "summary": "estratto",
            "eligibility_text": "PMI toscane",
            "value_amount": None,
            "value_min": None,
            "value_max": None,
            "deadline": "2026-06-30",
            "keywords": ["transizione digitale"],
            "kind": "incentive",
        }


def test_toscana_contract_crawl(monkeypatch):
    listing = _json("toscana_listing.json")
    detail_html = (CASSETTES / "toscana_detail.html").read_text(encoding="utf-8")
    seen_paths: list[str] = []

    def handler(request):
        seen_paths.append(request.url.path)
        if "/wp-json/" in request.url.path:
            return httpx.Response(200, json=listing)
        return httpx.Response(200, text=detail_html)  # a bando detail page

    # Patch httpx.Client globally (toscana builds its own client for list+detail).
    monkeypatch.setattr(ted.httpx, "Client", _client_factory(handler))
    src = get("toscana")
    raws = list(src.fetch(client=_FakeLLM(), cache=InMemoryExtractionCache(), limit=2))

    # The listing envelope was parsed into detail refs AND a detail page was fetched.
    assert any("/wp-json/" in p for p in seen_paths)
    assert any("/wp-json/" not in p for p in seen_paths)
    assert {r.id for r in raws} == {f"toscana:{item['id']}" for item in listing}
    assert raws[0].payload["_url"] == listing[0]["link"]  # listing link carried through
    [opp] = src.to_opportunities(raws[0], now=NOW)
    assert opp.source == "toscana" and opp.region == "Toscana"
    assert opp.geo_scope == "regional"


# --------------------------------------------------------------------------- #
# ANAC — OCP mirror; gzipped JSONL stream, one compiled release per line
# --------------------------------------------------------------------------- #


def test_anac_contract(monkeypatch):
    gz = (CASSETTES / "anac.jsonl.gz").read_bytes()

    def fake_stream(method, url, **kwargs):
        def handler(request):
            return httpx.Response(200, content=gz)

        return _REAL_CLIENT(transport=httpx.MockTransport(handler)).stream(method, url)

    monkeypatch.setattr(http_mod.httpx, "stream", fake_stream)
    raws = list(anac.AnacSource().fetch(year=2025, limit=10))

    assert len(raws) == 2  # gunzipped JSONL: two compiled releases
    assert all(r.id.startswith("anac:") for r in raws)
    [opp] = anac.to_opportunities(raws[0], now=NOW)
    assert opp.source == "anac" and opp.kind == "tender"
    assert opp.geo_scope == "national" and opp.region is None  # OCDS has no region
