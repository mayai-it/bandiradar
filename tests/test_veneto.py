"""Offline tests for the Regione Veneto (SIU portal) LLM-scraper source.

No network, no LLM: the mapper runs over the RECORDED extraction fixture
(guardrail 5) and the pure listing parser runs over a recorded landing cassette.
"""

from datetime import UTC, datetime
from pathlib import Path

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import veneto
from bandiradar.sources.base import get, list_sources
from bandiradar.sources.llm_scraper import validate_refs

# Capture (2026-06-12): deadlines span 2026-06-15 .. 2026-06-30; this NOW yields
# a closed / closing_soon / open mix.
NOW = datetime(2026, 6, 22, 0, 0, tzinfo=UTC)

CASSETTE = Path(__file__).parent / "cassettes" / "veneto_listing.html"


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in veneto.load_fixture():
        for opp in veneto.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = veneto.load_fixture()
    assert len(raws) == 10
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("veneto:") for r in raws)
    assert all(r.source == "veneto" for r in raws)


def test_all_regional_veneto():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.geo_scope == "regional"
        assert opp.region == "Veneto"
        assert opp.issuer_name == "Regione del Veneto"
        assert opp.kind in ("incentive", "tender")  # extraction classifies per atto
        assert "bandi.regione.veneto.it" in opp.source_url


def test_field_mapping_known_bando():
    opp = by_id()["veneto:13079"]
    assert "protezione civile" in (opp.title + (opp.eligibility_text or "")).lower()
    assert opp.deadline is not None
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 6, 30)
    assert opp.status == "open"  # 8 days past NOW
    assert opp.eligibility_text  # the matcher's input


def test_status_mix_present():
    statuses = {o.status for o in by_id().values()}
    assert "closed" in statuses  # 2026-06-15 < NOW
    assert "closing_soon" in statuses  # 2026-06-29 within 7 days
    assert "open" in statuses  # 2026-06-30


# --------------------------------------------------------------------------- #
# Pure listing parser (the crawl seed) over a recorded landing cassette
# --------------------------------------------------------------------------- #


def test_parse_listing_extracts_unique_refs():
    refs = veneto.parse_listing(CASSETTE.read_text(encoding="utf-8"))
    assert len(refs) == 10  # landing sections, deduped by idAtto
    assert validate_refs(refs) == "ok"
    ids = [r[0] for r in refs]
    assert len(set(ids)) == len(ids)
    assert all(
        url.startswith("https://bandi.regione.veneto.it/Public/Dettaglio?idAtto=")
        for _, url, _ in refs
    )
    by = {r[0]: r for r in refs}
    assert "subappalto" in by["13062"][2].lower()


def test_parse_listing_drift_is_detected_not_silent():
    # A redesigned landing (no Dettaglio anchors) -> no refs -> broken, never a crash.
    assert veneto.parse_listing("<html><body>nuovo portale</body></html>") == []
    assert validate_refs([]) == "broken"


def test_veneto_is_registered_and_requires_llm():
    source = get("veneto")
    assert source.id == "veneto"
    assert source.requires_llm is True
    assert "veneto" in {s.id for s in list_sources()}
