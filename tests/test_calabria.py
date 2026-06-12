"""Offline tests for the Regione Calabria (Calabria Europa) LLM-scraper source.

No network, no LLM: mapper over the RECORDED extraction fixture (guardrail 5),
pure parser over the recorded WP-REST `bando` JSON cassette.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import calabria
from bandiradar.sources.base import get, list_sources
from bandiradar.sources.llm_scraper import validate_refs

# Capture (2026-06-12): deadlines span 2025-06 .. 2026-07 plus no-deadline items;
# this NOW yields a closed / closing_soon / open mix.
NOW = datetime(2026, 6, 12, 0, 0, tzinfo=UTC)

CASSETTE = Path(__file__).parent / "cassettes" / "calabria_listing.json"


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in calabria.load_fixture():
        for opp in calabria.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = calabria.load_fixture()
    assert len(raws) == 10
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("calabria:") for r in raws)
    assert all(r.source == "calabria" for r in raws)


def test_all_regional_calabria():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.geo_scope == "regional"
        assert opp.region == "Calabria"
        assert opp.issuer_name == "Regione Calabria — Calabria Europa"
        assert opp.kind in ("incentive", "tender")
        assert "calabriaeuropa.regione.calabria.it" in opp.source_url


def test_field_mapping_known_bando():
    opps = by_id()
    sicura = next(o for o in opps.values() if "Impresa Sicura" in o.title)
    assert sicura.deadline is not None
    assert (sicura.deadline.year, sicura.deadline.month, sicura.deadline.day) == (
        2026,
        5,
        11,
    )
    assert sicura.status == "closed"  # before NOW
    assert sicura.eligibility_text


def test_status_mix_present():
    statuses = {o.status for o in by_id().values()}
    assert "closed" in statuses  # the May 2026 windows
    assert "closing_soon" in statuses  # 2026-06-15 within 7 days
    assert "open" in statuses  # 2026-07-03 + no-deadline


# --------------------------------------------------------------------------- #
# Pure listing parser over the recorded WP-REST JSON cassette
# --------------------------------------------------------------------------- #


def test_parse_listing_extracts_refs_from_json():
    items = json.loads(CASSETTE.read_text(encoding="utf-8"))
    refs = calabria.parse_listing(items)
    assert len(refs) == len(items)
    assert validate_refs(refs) == "ok"
    ids = [r[0] for r in refs]
    assert len(set(ids)) == len(ids)
    assert all(
        url.startswith("https://calabriaeuropa.regione.calabria.it/bando/")
        for _, url, _ in refs
    )


def test_parse_listing_drift_is_detected_not_silent():
    assert calabria.parse_listing({"error": "html instead of json"}) == []
    assert (
        calabria.parse_listing([{"weird": 1}])
        and validate_refs(calabria.parse_listing([{"weird": 1}])) == "broken"
    )  # malformed item -> empty url/title -> broken, not a crash


def test_calabria_is_registered_and_requires_llm():
    source = get("calabria")
    assert source.id == "calabria"
    assert source.requires_llm is True
    assert "calabria" in {s.id for s in list_sources()}
