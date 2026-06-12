"""Offline tests for the Friuli Venezia Giulia LLM-scraper source.

No network, no LLM: the mapper runs over the RECORDED extraction fixture
(guardrail 5) and the pure listing parser runs over a recorded ricerca.jsp
cassette (the contributi-filtered search the adapter actually requests).
"""

from datetime import UTC, datetime
from pathlib import Path

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import fvg
from bandiradar.sources.base import get, list_sources
from bandiradar.sources.llm_scraper import validate_refs

# Capture (2026-06-12): deadlines are mostly year-end sportello dates (2026-12-31)
# plus 2026-07-31 and two already-passed windows; this NOW yields a mix.
NOW = datetime(2026, 7, 28, 0, 0, tzinfo=UTC)

CASSETTE = Path(__file__).parent / "cassettes" / "fvg_listing.html"


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in fvg.load_fixture():
        for opp in fvg.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = fvg.load_fixture()
    assert len(raws) == 12
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("fvg:") for r in raws)
    assert all(r.source == "fvg" for r in raws)


def test_all_regional_fvg():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.geo_scope == "regional"
        assert opp.region == "Friuli-Venezia Giulia"
        assert opp.issuer_name == "Regione Autonoma Friuli Venezia Giulia"
        assert opp.kind in ("incentive", "tender")
        assert "regione.fvg.it" in opp.source_url


def test_field_mapping_known_bando():
    opps = by_id()
    pmi = next(o for o in opps.values() if "micro e piccole imprese" in o.title)
    assert pmi.deadline is not None
    assert (pmi.deadline.year, pmi.deadline.month, pmi.deadline.day) == (2026, 7, 31)
    assert pmi.status == "closing_soon"  # 3 days past NOW
    assert pmi.eligibility_text


def test_status_mix_present():
    statuses = {o.status for o in by_id().values()}
    assert "closed" in statuses  # 2026-01-31 / 2026-05-31
    assert "closing_soon" in statuses  # 2026-07-31
    assert "open" in statuses  # 2026-12-31 sportello + no-deadline


# --------------------------------------------------------------------------- #
# Pure listing parser over the recorded contributi-filtered cassette
# --------------------------------------------------------------------------- #


def test_parse_listing_extracts_contributi_refs():
    refs = fvg.parse_listing(CASSETTE.read_text(encoding="utf-8"))
    assert len(refs) == 10  # page 1 of the filtered results
    assert validate_refs(refs) == "ok"
    ids = [r[0] for r in refs]
    assert len(set(ids)) == len(ids)
    assert all(
        url.startswith(
            "https://www.regione.fvg.it/rafvg/cms/RAFVG/MODULI/bandi_avvisi/BANDI/"
        )
        for _, url, _ in refs
    )
    by = {r[0]: r for r in refs}
    assert "micro e piccole imprese" in by["8992"][2]


def test_parse_listing_drift_is_detected_not_silent():
    assert fvg.parse_listing("<html><body>nuovo portale</body></html>") == []
    assert validate_refs([]) == "broken"


def test_fvg_is_registered_and_requires_llm():
    source = get("fvg")
    assert source.id == "fvg"
    assert source.requires_llm is True
    assert "fvg" in {s.id for s in list_sources()}
