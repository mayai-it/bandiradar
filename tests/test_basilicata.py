"""Offline tests for the Regione Basilicata (portalebandi) LLM-scraper source.

No network, no LLM: mapper over the RECORDED extraction fixture (guardrail 5),
pure parser over the recorded WP-REST `avvisi-e-bandi` JSON cassette.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import basilicata
from bandiradar.sources.base import get, list_sources
from bandiradar.sources.llm_scraper import validate_refs

# Capture (2026-06-12): deadlines span 2026-06-17 .. 2027-06-30; this NOW yields
# a closed / closing_soon / open mix.
NOW = datetime(2026, 6, 26, 0, 0, tzinfo=UTC)

CASSETTE = Path(__file__).parent / "cassettes" / "basilicata_listing.json"


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in basilicata.load_fixture():
        for opp in basilicata.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = basilicata.load_fixture()
    assert len(raws) == 10
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("basilicata:") for r in raws)
    assert all(r.source == "basilicata" for r in raws)


def test_all_regional_basilicata():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.geo_scope == "regional"
        assert opp.region == "Basilicata"
        assert opp.issuer_name == "Regione Basilicata"
        assert opp.kind in ("incentive", "tender")  # the portal carries both
        assert "portalebandi.regione.basilicata.it" in opp.source_url


def test_field_mapping_known_bando():
    opps = by_id()
    basilavoro = next(o for o in opps.values() if "Basilavoro" in o.title)
    assert basilavoro.kind == "incentive"  # business hiring incentives
    assert basilavoro.deadline is not None
    assert (
        basilavoro.deadline.year,
        basilavoro.deadline.month,
        basilavoro.deadline.day,
    ) == (2027, 6, 30)
    assert basilavoro.status == "open"
    assert basilavoro.eligibility_text


def test_mixed_kinds_classified():
    # The portal publishes EVERYTHING (aste, concessioni, incentives); the LLM
    # classification keeps them honest (tender vs incentive).
    kinds = {o.kind for o in by_id().values()}
    assert kinds == {"incentive", "tender"}


def test_status_mix_present():
    statuses = {o.status for o in by_id().values()}
    assert "closed" in statuses  # 2026-06-17 / 06-19 / 06-25
    assert "closing_soon" in statuses  # 2026-06-30 within 7 days
    assert "open" in statuses  # 2026-08+ and 2027


# --------------------------------------------------------------------------- #
# Pure listing parser over the recorded WP-REST JSON cassette
# --------------------------------------------------------------------------- #


def test_parse_listing_extracts_refs_from_json():
    items = json.loads(CASSETTE.read_text(encoding="utf-8"))
    refs = basilicata.parse_listing(items)
    assert len(refs) == len(items)
    assert validate_refs(refs) == "ok"
    ids = [r[0] for r in refs]
    assert len(set(ids)) == len(ids)
    assert all(
        url.startswith("https://portalebandi.regione.basilicata.it/avvisi-e-bandi/")
        for _, url, _ in refs
    )


def test_parse_listing_drift_is_detected_not_silent():
    assert basilicata.parse_listing("not a list") == []
    refs = basilicata.parse_listing([{"weird": 1}])
    assert refs and validate_refs(refs) == "broken"


def test_basilicata_is_registered_and_requires_llm():
    source = get("basilicata")
    assert source.id == "basilicata"
    assert source.requires_llm is True
    assert "basilicata" in {s.id for s in list_sources()}
