"""Offline tests for the Toscana LLM-assisted scraper. No network, no LLM."""

from datetime import UTC, datetime

import pytest

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import toscana
from bandiradar.sources.base import get, list_sources
from bandiradar.sources.llm_scraper import (
    InMemoryExtractionCache,
    extract_bando_fields,
)

NOW = datetime(2026, 6, 6, 0, 0, tzinfo=UTC)


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in toscana.load_fixture():
        for opp in toscana.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


# --------------------------------------------------------------------------- #
# to_opportunities over the recorded extraction fixture (PURE, no LLM)
# --------------------------------------------------------------------------- #


def test_load_fixture_prefixed_rawdocs():
    raws = toscana.load_fixture()
    assert len(raws) == 12
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("toscana:") for r in raws)


def test_all_regional_toscana():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.source == "toscana"
        assert opp.geo_scope == "regional"
        assert opp.region == "Toscana"
        assert opp.kind in ("incentive", "tender")


def test_field_mapping_known_record():
    opp = by_id()["toscana:12084"]
    assert opp.kind == "incentive"
    assert opp.issuer_name == "Sviluppo Toscana"
    assert opp.issuer_region == "Toscana"
    assert "sviluppo.toscana.it" in opp.source_url
    assert opp.eligibility_text  # extracted body + folded keywords
    assert opp.deadline is not None
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 6, 27)
    assert opp.status == "open"


def test_open_and_closed_status():
    opps = by_id()
    assert opps["toscana:12084"].status == "open"  # deadline 2026-06-27
    assert opps["toscana:11456"].status == "closed"  # deadline 2026-05-11 (past)


# --------------------------------------------------------------------------- #
# extract_bando_fields (mocked LLM client)
# --------------------------------------------------------------------------- #


class _FakeClient:
    def __init__(self, reply: dict):
        self.reply = reply
        self.calls = 0

    def score(self, system: str, user: str) -> dict:
        self.calls += 1
        return self.reply


def test_extract_bando_fields_coerces():
    client = _FakeClient(
        {
            "title": "Bando X",
            "summary": "breve",
            "eligibility_text": "PMI toscane",
            "value_amount": "€ 1.000.000",  # currency + thousands separators
            "value_min": None,
            "value_max": 50000,
            "deadline": "2026-06-30",
            "keywords": ["innovazione", "digitale"],
            "kind": "tender",
        }
    )
    out = extract_bando_fields("some page text", "Toscana", client)
    assert out["title"] == "Bando X"
    assert out["value_amount"] == 1000000.0  # cleaned from "€ 1.000.000"
    assert out["value_max"] == 50000.0
    assert out["deadline"] == "2026-06-30"
    assert out["kind"] == "tender"
    assert out["keywords"] == ["innovazione", "digitale"]


def test_extract_bando_fields_tolerant_of_junk():
    out = extract_bando_fields("x", "Toscana", _FakeClient({"unexpected": 1}))
    assert out["title"] is None
    assert out["kind"] == "incentive"  # default
    assert out["keywords"] == []
    assert out["value_amount"] is None


# --------------------------------------------------------------------------- #
# fetch() — mocked client + fetcher + listing (no network, no real LLM)
# --------------------------------------------------------------------------- #


def _details():
    return [(1, "https://x/bando/a", "A"), (2, "https://x/bando/b", "B")]


def test_fetch_extracts_caches_and_caps():
    client = _FakeClient({"title": "T", "kind": "incentive", "keywords": []})
    cache = InMemoryExtractionCache()
    pages = {"https://x/bando/a": "page A", "https://x/bando/b": "page B"}

    raws = list(
        toscana.ToscanaSource().fetch(
            client=client,
            cache=cache,
            list_details=_details,
            fetch_text=lambda u: pages[u],
        )
    )
    assert {r.id for r in raws} == {"toscana:1", "toscana:2"}
    assert client.calls == 2  # one LLM extraction per URL
    assert raws[0].payload["_url"] == "https://x/bando/a"

    # 2nd run with the same cache -> no further LLM calls.
    list(
        toscana.ToscanaSource().fetch(
            client=client,
            cache=cache,
            list_details=_details,
            fetch_text=lambda u: pages[u],
        )
    )
    assert client.calls == 2


def test_fetch_respects_max_items():
    client = _FakeClient({"title": "T", "kind": "incentive", "keywords": []})
    raws = list(
        toscana.ToscanaSource().fetch(
            client=client,
            cache=InMemoryExtractionCache(),
            list_details=_details,
            fetch_text=lambda u: "p",
            max_items=1,
        )
    )
    assert len(raws) == 1


def test_fetch_without_llm_key_raises():
    # conftest forces provider=none -> get_client() is None -> clear, honest error
    # that names the reason (here: no provider configured).
    with pytest.raises(
        RuntimeError, match="no usable LLM client: no LLM provider configured"
    ):
        toscana.ToscanaSource().fetch()


def test_toscana_registered():
    source = get("toscana")
    assert source.id == "toscana"
    assert "toscana" in {s.id for s in list_sources()}
