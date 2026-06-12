"""Offline tests for the Regione Piemonte (Drupal bandi portal) LLM-scraper source.

No network, no LLM: the mapper runs over the RECORDED extraction fixture
(guardrail 5) and the pure listing parser runs over a recorded Views-page cassette
(the stato=Aperto filtered listing the adapter actually requests).
"""

from datetime import UTC, datetime
from pathlib import Path

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import piemonte
from bandiradar.sources.base import get, list_sources
from bandiradar.sources.llm_scraper import validate_refs

# Capture (2026-06-12): deadlines span 2026-04-02 .. 2026-12-31; this NOW yields
# a closed / closing_soon / open mix.
NOW = datetime(2026, 6, 25, 0, 0, tzinfo=UTC)

CASSETTE = Path(__file__).parent / "cassettes" / "piemonte_listing.html"


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in piemonte.load_fixture():
        for opp in piemonte.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = piemonte.load_fixture()
    assert len(raws) == 10
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("piemonte:") for r in raws)
    assert all(r.source == "piemonte" for r in raws)


def test_all_regional_piemonte():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.geo_scope == "regional"
        assert opp.region == "Piemonte"
        assert opp.issuer_name == "Regione Piemonte"
        assert opp.kind in ("incentive", "tender")
        assert "bandi.regione.piemonte.it" in opp.source_url


def test_field_mapping_known_bando():
    opp = by_id()["piemonte:progetto-bandiera-piemonte-hydrogen-valley"]
    assert "hydrogen" in opp.title.lower()
    assert opp.deadline is not None
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 6, 30)
    assert opp.status == "closing_soon"  # 5 days past NOW
    assert opp.eligibility_text


def test_status_mix_present():
    statuses = {o.status for o in by_id().values()}
    assert "closed" in statuses  # 2026-04-02
    assert "closing_soon" in statuses  # 2026-06-30
    assert "open" in statuses  # 2026-07-10 / 2026-12-31


# --------------------------------------------------------------------------- #
# Pure listing parser over the recorded stato=Aperto Views cassette
# --------------------------------------------------------------------------- #


def test_parse_listing_extracts_unique_refs():
    refs = piemonte.parse_listing(CASSETTE.read_text(encoding="utf-8"))
    assert len(refs) == 9  # one Views page of open bandi
    assert validate_refs(refs) == "ok"
    slugs = [r[0] for r in refs]
    assert len(set(slugs)) == len(slugs)
    assert all(
        url.startswith("https://bandi.regione.piemonte.it/contributi-finanziamenti/")
        for _, url, _ in refs
    )
    by = {r[0]: r for r in refs}
    assert "hydrogen" in by["progetto-bandiera-piemonte-hydrogen-valley"][2].lower()


def test_parse_listing_drift_is_detected_not_silent():
    assert piemonte.parse_listing("<html><body>restyling</body></html>") == []
    assert validate_refs([]) == "broken"


def test_piemonte_is_registered_and_requires_llm():
    source = get("piemonte")
    assert source.id == "piemonte"
    assert source.requires_llm is True
    assert "piemonte" in {s.id for s in list_sources()}
