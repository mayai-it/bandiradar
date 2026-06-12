"""Offline tests for the Regione Puglia (PR 2021-2027 portal) LLM-scraper source.

No network, no LLM: the mapper runs over the RECORDED extraction fixture
(guardrail 5) and the pure listing parser runs over a recorded Liferay
news-list fragment cassette (the resource URL the adapter actually calls).
"""

from datetime import UTC, datetime
from pathlib import Path

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import puglia
from bandiradar.sources.base import get, list_sources
from bandiradar.sources.llm_scraper import validate_refs

# Capture (2026-06-12): deadlines span 2026-06-15 .. 2026-07-25; this NOW yields
# a closed / closing_soon / open mix.
NOW = datetime(2026, 6, 25, 0, 0, tzinfo=UTC)

CASSETTE = Path(__file__).parent / "cassettes" / "puglia_listing.html"


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in puglia.load_fixture():
        for opp in puglia.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = puglia.load_fixture()
    assert len(raws) == 4  # the fragment's "Bando aperto" items at capture time
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("puglia:") for r in raws)
    assert all(r.source == "puglia" for r in raws)


def test_all_regional_puglia():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.geo_scope == "regional"
        assert opp.region == "Puglia"
        assert opp.issuer_name == "Regione Puglia"
        assert opp.kind in ("incentive", "tender")
        assert "pr2127.regione.puglia.it" in opp.source_url


def test_field_mapping_known_bando():
    opps = by_id()
    buoni = next(o for o in opps.values() if "Buoni Servizio" in o.title)
    assert buoni.deadline is not None
    assert (buoni.deadline.year, buoni.deadline.month, buoni.deadline.day) == (
        2026,
        6,
        15,
    )
    assert buoni.status == "closed"  # before NOW
    assert buoni.eligibility_text


def test_status_mix_present():
    statuses = {o.status for o in by_id().values()}
    assert "closed" in statuses  # 2026-06-15
    assert "closing_soon" in statuses  # 2026-06-30 within 7 days
    assert "open" in statuses  # 2026-07-25


# --------------------------------------------------------------------------- #
# Pure listing parser over the recorded news-list fragment cassette
# --------------------------------------------------------------------------- #


def test_parse_listing_keeps_only_open_badged_bandi():
    refs = puglia.parse_listing(CASSETTE.read_text(encoding="utf-8"))
    assert len(refs) == 4  # 4 "Bando aperto" out of 10 fragment items
    assert validate_refs(refs) == "ok"
    slugs = [r[0] for r in refs]
    assert len(set(slugs)) == len(slugs)
    assert all(
        url.startswith("https://pr2127.regione.puglia.it/") for _, url, _ in refs
    )
    assert any("buoni-servizio" in s for s in slugs)
    # The unbadged news/verbali and "Bando chiuso" items are NOT crawled.
    assert not any("verbale" in s for s in slugs)


def test_parse_listing_drift_is_detected_not_silent():
    assert puglia.parse_listing("<html><body>nuovo portale</body></html>") == []
    assert validate_refs([]) == "broken"


def test_puglia_is_registered_and_requires_llm():
    source = get("puglia")
    assert source.id == "puglia"
    assert source.requires_llm is True
    assert "puglia" in {s.id for s in list_sources()}
