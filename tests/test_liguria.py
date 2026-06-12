"""Offline tests for the Regione Liguria (publiccompetition) LLM-scraper source.

No network, no LLM: mapper over the RECORDED extraction fixture (guardrail 5);
pure parsers over recorded cassettes — the contributi-filtered results page and
the quicksearch form (CSRF-token extraction).
"""

from datetime import UTC, datetime
from pathlib import Path

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import liguria
from bandiradar.sources.base import get, list_sources
from bandiradar.sources.llm_scraper import validate_refs

# Capture (2026-06-12): deadlines span 2026-06-22 .. 2026-07-03 plus rolling
# no-deadline items; this NOW yields a closed / closing_soon / open mix.
NOW = datetime(2026, 6, 26, 0, 0, tzinfo=UTC)

CASSETTE = Path(__file__).parent / "cassettes" / "liguria_listing.html"
FORM_CASSETTE = Path(__file__).parent / "cassettes" / "liguria_search_form.html"


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in liguria.load_fixture():
        for opp in liguria.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = liguria.load_fixture()
    assert len(raws) == 10
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("liguria:") for r in raws)
    assert all(r.source == "liguria" for r in raws)


def test_all_regional_liguria():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.geo_scope == "regional"
        assert opp.region == "Liguria"
        assert opp.issuer_name == "Regione Liguria"
        assert "regione.liguria.it" in opp.source_url


def test_field_mapping_known_bando():
    opps = by_id()
    bonus = next(o for o in opps.values() if "assunzionali nel turismo" in o.title)
    assert bonus.kind == "incentive"
    assert bonus.deadline is not None
    assert (bonus.deadline.year, bonus.deadline.month, bonus.deadline.day) == (
        2026,
        6,
        30,
    )
    assert bonus.status == "closing_soon"  # 4 days past NOW
    assert bonus.eligibility_text


def test_status_mix_present():
    statuses = {o.status for o in by_id().values()}
    assert "closed" in statuses  # 2026-06-22
    assert "closing_soon" in statuses  # 2026-06-30 / 07-01 / 07-03
    assert "open" in statuses  # the rolling no-deadline contributi


# --------------------------------------------------------------------------- #
# Pure parsers: filtered results + CSRF token
# --------------------------------------------------------------------------- #


def test_parse_listing_extracts_active_contributi():
    refs = liguria.parse_listing(CASSETTE.read_text(encoding="utf-8"))
    assert len(refs) == 10  # tipologia=contributi & stato=Attivi at capture time
    assert validate_refs(refs) == "ok"
    ids = [r[0] for r in refs]
    assert len(set(ids)) == len(ids)
    assert all(
        url.startswith(
            "https://www.regione.liguria.it/homepage-bandi-e-avvisi/publiccompetition/"
        )
        for _, url, _ in refs
    )
    by = {r[0]: r for r in refs}
    assert "assunzionali" in by["4606"][2].lower()


def test_parse_csrf_token_from_the_quicksearch_form():
    token = liguria.parse_csrf_token(FORM_CASSETTE.read_text(encoding="utf-8"))
    assert token is not None
    assert len(token) == 32 and all(c in "0123456789abcdef" for c in token)


def test_parsers_drift_is_detected_not_silent():
    assert liguria.parse_listing("<html><body>restyling</body></html>") == []
    assert validate_refs([]) == "broken"
    assert liguria.parse_csrf_token("<html>no form</html>") is None


def test_liguria_is_registered_and_requires_llm():
    source = get("liguria")
    assert source.id == "liguria"
    assert source.requires_llm is True
    assert "liguria" in {s.id for s in list_sources()}
