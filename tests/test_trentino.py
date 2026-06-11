"""Offline tests for the Provincia Autonoma di Trento (FEASR CKAN CSV) source.

No network: drives load_fixture() -> to_opportunities() against the recorded CKAN
open-data CSV. Exercises the structured-fields map (date/amount/link parsing) and
the open/closed lifecycle.
"""

from datetime import UTC, datetime

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import trentino as tn
from bandiradar.sources.base import get, list_sources

# Capture (2026-06-12): chiusure span 2023-11 .. 2027-05; this NOW gives a mix
# (the 2026-06-30 "aperto" bandi fall within the closing-soon window).
NOW = datetime(2026, 6, 25, 0, 0, tzinfo=UTC)


def opps() -> list[Opportunity]:
    out: list[Opportunity] = []
    for raw in tn.load_fixture():
        out += tn.to_opportunities(raw, now=NOW)
    return out


def find(title_substr: str, *, with_deadline=None) -> Opportunity:
    for o in opps():
        if title_substr.lower() in o.title.lower():
            if with_deadline is None or (
                o.deadline and o.deadline.date().isoformat() == with_deadline
            ):
                return o
    raise AssertionError(f"no bando matching {title_substr!r}")


def test_load_fixture_prefixed_rawdocs():
    raws = tn.load_fixture()
    assert len(raws) == 41
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("trentino:") for r in raws)
    assert len({r.id for r in raws}) == 41  # stable, unique row keys


def test_all_regional_trentino_incentives():
    items = opps()
    assert items
    for opp in items:
        assert opp.kind == "incentive"
        assert opp.geo_scope == "regional"
        assert opp.region == "Trentino-Alto Adige"
        assert opp.issuer_name == "Provincia Autonoma di Trento"
        assert opp.cpv == []
        assert "FEASR" in opp.keywords


def test_structured_fields_parsed():
    opp = find("SRE01 - insediamento giovani agricoltori", with_deadline="2026-02-09")
    assert opp.value_amount == 3200000.0  # "3.200.000,00" -> float
    assert "provincia.tn.it" in opp.source_url  # link extracted from the HTML cell
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 2, 9)
    assert opp.status == "closed"  # 2026-02-09 is before NOW


def test_open_closing_and_closed_status_present():
    # A currently-open FEASR bando (chiusura 2026-06-30) within the closing window.
    srb01 = find("SRB01 - sostegno zone", with_deadline="2026-06-30")
    assert srb01.status == "closing_soon"
    # A planned/future bando (chiusura 2027-05-15) reads as open.
    planned = find("SRB01 - sostegno zone", with_deadline="2027-05-15")
    assert planned.status == "open"
    # And at least one closed historical row.
    assert any(o.status == "closed" for o in opps())


def test_trentino_is_registered():
    source = get("trentino")
    assert source.id == "trentino"
    assert source.kind == "incentive"
    assert "trentino" in {s.id for s in list_sources()}
