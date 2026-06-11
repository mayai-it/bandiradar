"""Offline tests for the Regione Siciliana (EuroInfoSicilia FESR/FSC) source.

No network: drives load_fixture() -> to_opportunities() against the recorded WP REST
capture (category 321 "Bandi e Avvisi"). Verifies the standard-posts + categories
config path of the shared WordPress base maps cleanly.
"""

from datetime import UTC, datetime

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import sicilia
from bandiradar.sources.base import get, list_sources

# The capture (2026-06-12) holds bandi with deadlines spanning 2025-09 .. 2026-02; a
# fixed NOW in that window gives a deterministic open/closing_soon/closed mix.
NOW = datetime(2025, 12, 10, 0, 0, tzinfo=UTC)


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in sicilia.load_fixture():
        for opp in sicilia.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = sicilia.load_fixture()
    assert len(raws) == 15
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("sicilia:") for r in raws)
    assert all(r.source == "sicilia" for r in raws)


def test_all_regional_sicilia_incentives():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.kind == "incentive"
        assert opp.geo_scope == "regional"
        assert opp.region == "Sicilia"
        assert opp.cpv == []  # incentives carry no CPV
        assert opp.id.startswith("sicilia:")
        assert opp.issuer_name == "Regione Siciliana — EuroInfoSicilia"


def test_field_mapping_known_bando():
    opp = by_id()["sicilia:159821"]
    assert "FESR" in opp.title and "Azione 4.6.2" in opp.title
    assert opp.issuer_region == "Sicilia"
    assert "euroinfosicilia.it" in opp.source_url
    assert opp.eligibility_text  # the bando body the matcher reads
    # Keywords come from the rich post taxonomies (tag-/programmi-/destinatari-/...).
    assert "pr-fesr-sicilia-2021-2027" in opp.keywords
    assert opp.deadline is not None
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 2, 27)
    assert opp.status == "open"  # 2026-02-27 is far past NOW + 7 days


def test_open_closing_and_closed_status_present():
    opps = by_id()
    assert opps["sicilia:159821"].status == "open"  # 2026-02-27
    assert opps["sicilia:154744"].status == "closing_soon"  # 2025-12-12 (within 7d)
    assert opps["sicilia:149911"].status == "closed"  # 2025-09-25 (past)


def test_generic_category_slugs_excluded_from_keywords():
    # `category-bandi` / `category-decreti` are too generic to be useful keywords;
    # the config's keyword_taxonomies deliberately omit the `category-` prefix.
    opps = by_id()
    all_keywords = {k for o in opps.values() for k in o.keywords}
    assert "bandi" not in all_keywords
    assert "decreti" not in all_keywords


def test_sicilia_is_registered():
    source = get("sicilia")
    assert source.id == "sicilia"
    assert source.kind == "incentive"
    assert "sicilia" in {s.id for s in list_sources()}
