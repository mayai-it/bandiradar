"""Offline tests for the Regione Campania (Sviluppo Campania) LLM-scraper source.

No network, no LLM: the mapper runs over the RECORDED extraction fixture
(guardrail 5) and the pure listing parser runs over a recorded /bandi-aperti/
cassette that contains BOTH kinds of noise the widget hook must skip (the
closed-bandi nav submenu and the agency's own selection-notice archive).
"""

from datetime import UTC, datetime
from pathlib import Path

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import campania
from bandiradar.sources.base import get, list_sources
from bandiradar.sources.llm_scraper import validate_refs

# Capture (2026-06-12): the curated open bandi are rolling/sportello measures —
# the prose deadlines on their pages are PAST windows (honest data), the open-ended
# ones have no deadline. This NOW yields closed + open.
NOW = datetime(2026, 6, 12, 0, 0, tzinfo=UTC)

CASSETTE = Path(__file__).parent / "cassettes" / "campania_listing.html"


def by_id() -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for raw in campania.load_fixture():
        for opp in campania.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_load_fixture_prefixed_rawdocs():
    raws = campania.load_fixture()
    assert len(raws) == 6  # the curated open-bandi widgets at capture time
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("campania:") for r in raws)
    assert all(r.source == "campania" for r in raws)


def test_all_regional_campania():
    opps = by_id()
    assert opps
    for opp in opps.values():
        assert opp.geo_scope == "regional"
        assert opp.region == "Campania"
        assert opp.issuer_name == "Regione Campania — Sviluppo Campania"
        assert "sviluppocampania.it" in opp.source_url


def test_field_mapping_known_bando():
    opps = by_id()
    bond = next(o for o in opps.values() if "Garanzia Campania Bond" in o.title)
    assert bond.deadline is None  # open-ended guarantee instrument
    assert bond.status == "open"
    assert bond.eligibility_text


def test_extraction_title_wins_over_slug_label():
    # The listing label is slug-derived (image widgets carry no text); the mapper
    # must prefer the LLM-extracted title.
    opps = by_id()
    frc = next(o for o in opps.values() if "FONDO REGIONALE PER LA CRESCITA" in o.title)
    assert "frc fondo regionale" not in frc.title  # not the slug label


def test_status_closed_and_open_present():
    statuses = {o.status for o in by_id().values()}
    assert "closed" in statuses  # past prose windows (honest extraction)
    assert "open" in statuses  # no-deadline rolling measures


# --------------------------------------------------------------------------- #
# Pure listing parser over the noisy /bandi-aperti/ cassette
# --------------------------------------------------------------------------- #


def test_parse_listing_keeps_only_open_widget_bandi():
    refs = campania.parse_listing(CASSETTE.read_text(encoding="utf-8"))
    assert len(refs) == 6
    assert validate_refs(refs) == "ok"
    slugs = [r[0] for r in refs]
    assert len(set(slugs)) == len(slugs)
    assert "sostegno-al-lavoro-autonomo" in slugs
    # The closed-bandi nav submenu is in the cassette but must NOT be crawled:
    assert "io-ho-un-sogno-il-futuro-e-donna" not in slugs  # Voucher Donne (chiuso)
    assert "artigianato-campano" not in slugs  # Fondo Artigianato (chiuso)
    # Nor the agency's own selection notices from the archive:
    assert not any("avviso-pubblico-di-selezione" in s for s in slugs)


def test_parse_listing_drift_is_detected_not_silent():
    assert campania.parse_listing("<html><body>restyling</body></html>") == []
    assert validate_refs([]) == "broken"


def test_campania_is_registered_and_requires_llm():
    source = get("campania")
    assert source.id == "campania"
    assert source.requires_llm is True
    assert "campania" in {s.id for s in list_sources()}
