"""Offline tests for the Regione Emilia-Romagna (Plone `Bando`) source.

No network: drives load_fixture() -> to_opportunities() against the recorded
plone.restapi `@search?portal_type=Bando&fullobjects` capture. Exercises the
reusable Plone base — notably the STRUCTURED `scadenza_bando` deadline (no text
parsing) and the vocabulary-derived keywords.
"""

from datetime import UTC, datetime

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import emilia_romagna as er
from bandiradar.sources.base import get, list_sources

# Capture (2026-06-12): scadenze span 2025-08 .. 2026-05 plus some open/rolling
# bandi with no deadline; this NOW yields an open/closing_soon/closed mix.
NOW = datetime(2026, 5, 15, 0, 0, tzinfo=UTC)


def opps() -> list[Opportunity]:
    out: list[Opportunity] = []
    for raw in er.load_fixture():
        out += er.to_opportunities(raw, now=NOW)
    return out


def find(title_substr: str) -> Opportunity:
    for o in opps():
        if title_substr.lower() in o.title.lower():
            return o
    raise AssertionError(f"no bando with title containing {title_substr!r}")


def test_load_fixture_prefixed_rawdocs():
    raws = er.load_fixture()
    assert len(raws) == 18
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("emilia_romagna:") for r in raws)
    assert all(r.source == "emilia_romagna" for r in raws)


def test_all_regional_er_incentives():
    items = opps()
    assert items
    for opp in items:
        assert opp.kind == "incentive"
        assert opp.geo_scope == "regional"
        assert opp.region == "Emilia-Romagna"
        assert opp.cpv == []
        assert opp.id.startswith("emilia_romagna:")


def test_structured_deadline_is_used_no_text_parsing():
    # The Plone `Bando` carries a real `scadenza_bando` -> we use it directly.
    opp = find("promozione della cittadinanza")
    assert opp.deadline is not None
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 5, 20)
    assert opp.status == "closing_soon"  # 2026-05-20 within 7 days of NOW
    assert "politicheterritoriali.regione.emilia-romagna.it" in opp.source_url
    # Keywords come from the Bando's structured vocabulary fields (tipologia/materie).
    assert any("Agevolazioni" in k for k in opp.keywords)


def test_open_closing_and_closed_status_present():
    items = {o.title: o for o in opps()}
    # A rolling/no-deadline bando reads as open.
    assert find("iscrizione all'elenco regionale").status == "open"
    assert find("promozione della cittadinanza").status == "closing_soon"  # 2026-05-20
    assert find("emergenza in Palestina e Ucraina").status == "closed"  # 2026-04-10
    assert items  # sanity


def test_emilia_romagna_is_registered():
    source = get("emilia_romagna")
    assert source.id == "emilia_romagna"
    assert source.kind == "incentive"
    assert "emilia_romagna" in {s.id for s in list_sources()}
