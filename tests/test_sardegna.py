"""Offline tests for the Regione Sardegna (Sardegna Impresa) LLM-scraper source.

No network, no LLM: the mapper runs over the RECORDED extraction fixture
(guardrail 5) and the pure listing parser runs over a recorded /it/agevolazioni
Views cassette (trimmed to <main>).
"""

from datetime import UTC, datetime
from pathlib import Path

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import sardegna
from bandiradar.sources.base import get, list_sources
from bandiradar.sources.llm_scraper import validate_refs

# Capture (2026-06-12): deadlines span 2026-06-15 .. 2026-09-30 plus two
# no-deadline standing measures; this NOW yields a closed/closing_soon/open mix.
NOW = datetime(2026, 6, 18, 0, 0, tzinfo=UTC)

CASSETTE = Path(__file__).parent / "cassettes" / "sardegna_listing.html"


def by_title() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in sardegna.load_fixture():
        for opp in sardegna.to_opportunities(raw, now=NOW):
            out[opp.title] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = sardegna.load_fixture()
    assert len(raws) == 10
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("sardegna:") for r in raws)
    assert all(r.source == "sardegna" for r in raws)


def test_all_regional_sardegna():
    opps = by_title()
    assert opps
    for opp in opps.values():
        assert opp.geo_scope == "regional"
        assert opp.region == "Sardegna"
        assert "sardegnaimpresa.eu" in opp.source_url
        assert opp.kind in ("incentive", "tender")


def test_field_mapping_known_bando():
    taxi = next(o for o in by_title().values() if "taxi" in o.title.lower())
    assert taxi.deadline is not None
    assert (taxi.deadline.year, taxi.deadline.month, taxi.deadline.day) == (
        2026,
        9,
        29,
    )
    assert taxi.status == "open"
    assert taxi.eligibility_text


def test_status_mix_and_no_deadline_open():
    opps = by_title()
    statuses = {o.status for o in opps.values()}
    assert "closed" in statuses  # 2026-06-15
    assert "closing_soon" in statuses  # 2026-06-20/22 within 7 days
    assert "open" in statuses
    # Standing measures without a deadline read as open.
    standing = next(o for o in opps.values() if o.deadline is None)
    assert standing.status == "open"


# --------------------------------------------------------------------------- #
# Pure listing parser over the recorded Views cassette
# --------------------------------------------------------------------------- #


def test_parse_listing_extracts_unique_refs_with_full_titles():
    refs = sardegna.parse_listing(CASSETTE.read_text(encoding="utf-8"))
    assert len(refs) == 10
    assert validate_refs(refs) == "ok"
    slugs = [r[0] for r in refs]
    assert len(set(slugs)) == len(slugs)
    assert all(
        url.startswith("https://www.sardegnaimpresa.eu/it/agevolazioni/")
        for _, url, _ in refs
    )
    by = {r[0]: r for r in refs}
    key = "bando-regionale-2026-azioni-di-sostegno-allattivita-di-impresa-servizio-taxi"
    assert "taxi" in by[key][2].lower()


def test_parse_listing_drift_is_detected_not_silent():
    assert sardegna.parse_listing("<html><body>restyling</body></html>") == []
    assert validate_refs([]) == "broken"


def test_sardegna_is_registered_and_requires_llm():
    source = get("sardegna")
    assert source.id == "sardegna"
    assert source.requires_llm is True
    assert "sardegna" in {s.id for s in list_sources()}
