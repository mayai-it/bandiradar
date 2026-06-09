"""Offline tests for the ANAC PVL adapter (open public tenders). No network.

Fixed ``now`` so the future-deadline filter is deterministic against the recorded
capture (captured 2026-06-08; scadenze span 2026-06-05 .. 2026-07-06)."""

from datetime import UTC, datetime

from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import anac_pvl as pvl
from bandiradar.sources.base import get, list_sources

NOW = datetime(2026, 6, 9, 0, 0, tzinfo=UTC)


def mapped() -> list[Opportunity]:
    out: list[Opportunity] = []
    for raw in pvl.load_fixture():
        out += pvl.to_opportunities(raw, now=NOW)
    return out


def one(opps: list[Opportunity], prefix: str) -> Opportunity:
    return next(o for o in opps if prefix in o.id)


# --------------------------------------------------------------------------- #
# fixture + the open-tender filter
# --------------------------------------------------------------------------- #


def test_load_fixture_prefixed_rawdocs():
    raws = pvl.load_fixture()
    assert len(raws) == 11
    assert all(isinstance(r, RawDoc) for r in raws)
    assert all(r.id.startswith("anac_pvl:") and r.source == "anac_pvl" for r in raws)


def test_filter_keeps_only_open_gare():
    opps = mapped()
    # 11 fixture records -> 7 open gare; 4 dropped (rettifica, past, esito, oscurato)
    assert len(opps) == 7
    dropped_substr = ["e6f9fc50", "a99784e3", "cbdcf7ce", "00000000"]
    ids = " ".join(o.id for o in opps)
    for sub in dropped_substr:
        assert sub not in ids


def test_is_open_tender_predicate():
    base = {
        "tipo": "avviso",
        "attivo": True,
        "oscurato": False,
        "dataScadenza": "2026-06-22T00:00:00Z",
    }
    assert pvl._is_open_tender(base, NOW) is True
    assert pvl._is_open_tender({**base, "tipo": "rettifica"}, NOW) is False
    assert pvl._is_open_tender({**base, "attivo": False}, NOW) is False
    assert pvl._is_open_tender({**base, "oscurato": True}, NOW) is False
    assert pvl._is_open_tender({**base, "dataScadenza": None}, NOW) is False
    # a past deadline relative to NOW is not "open"
    assert (
        pvl._is_open_tender({**base, "dataScadenza": "2026-05-01T00:00:00Z"}, NOW)
        is False
    )


def test_deadline_in_the_past_drops_a_previously_open_gara():
    raw = next(r for r in pvl.load_fixture() if "67f7b37c" in r.id)  # scad 2026-06-16
    assert pvl.to_opportunities(raw, now=NOW)  # open on 2026-06-09
    later = datetime(2026, 7, 1, tzinfo=UTC)
    assert pvl.to_opportunities(raw, now=later) == []  # closed by 2026-07-01


def test_sample_pinned_to_capture_not_wallclock(monkeypatch, tmp_path):
    """Anti time-bomb: --sample must show the captured 7 gare even when the wall
    clock is far past the fixture deadlines — else the offline demo silently → 0
    (breaking the "--sample always runs offline" guarantee). The offline mapper
    references raw.fetched_at (== fixture _captured), never the wall clock."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    monkeypatch.setattr(pvl, "_now", lambda: _dt(2027, 1, 1, tzinfo=_UTC))

    # mapper path: now=None -> raw.fetched_at, so the future wall clock is ignored
    opps = [o for r in pvl.load_fixture() for o in pvl.to_opportunities(r)]
    assert len(opps) == 7

    # full offline path (what `fetch --source anac_pvl --sample` runs)
    from bandiradar import core

    store = core.Store(str(tmp_path / "pvl.db"))
    try:
        result = core.run_fetch("anac_pvl", store, sample=True)
        assert result.new == 7
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# field mapping
# --------------------------------------------------------------------------- #


def test_open_gara_field_mapping():
    opp = one(mapped(), "67f7b37c")  # Liguria Digitale, Genova
    assert opp.kind == "tender"
    assert opp.id.startswith("anac_pvl:")
    assert opp.source == "anac_pvl"
    assert "/bandi/" in opp.source_url and "67f7b37c" in opp.source_url
    assert opp.issuer_name  # buyer (soggetti_sa denominazione)
    assert opp.region == "Liguria" and opp.geo_scope == "regional"
    assert opp.issuer_region == "Genova"  # province kept when resolved
    assert opp.deadline is not None
    assert (opp.deadline.year, opp.deadline.month, opp.deadline.day) == (2026, 6, 16)
    assert opp.status == "open"
    assert opp.value_amount == 5148.0 and opp.value_currency == "EUR"
    # CPV from PVL is a LABEL, not a code: numeric cpv stays empty, label -> text
    assert opp.cpv == []
    assert opp.eligibility_text and "CIG" in opp.eligibility_text


def test_value_is_summed_and_present_for_p2():
    opp = one(mapped(), "f306ec52")  # P2_20, Ercolano, procedura aperta
    assert opp.value_amount == 454440.3
    assert (
        opp.eligibility_text
        and "ASSISTENZA DOMICILIARE" in opp.eligibility_text.upper()
    )


def test_region_mapping_including_anac_spelling_variants():
    opps = mapped()
    assert one(opps, "9c1a1fc2").region == "Calabria"  # "Reggio di Calabria"
    assert one(opps, "8c6d1a52").region == "Emilia-Romagna"  # "Reggio nell'Emilia"
    assert one(opps, "84faaad0").region == "Veneto"  # Padova


def test_italia_and_country_nuts_map_to_national():
    opps = mapped()
    italia = one(opps, "79eb639e")  # luogo_nuts == "ITALIA"
    germania = one(opps, "835fb425")  # luogo_nuts == "GERMANIA"
    for opp in (italia, germania):
        assert opp.geo_scope == "national" and opp.region is None


def test_region_for_nuts_unit():
    assert pvl.region_for_nuts("Genova") == "Liguria"
    assert pvl.region_for_nuts("Reggio di Calabria") == "Calabria"
    assert pvl.region_for_nuts("Milano") == "Lombardia"
    assert pvl.region_for_nuts("ITALIA") is None
    assert pvl.region_for_nuts(None) is None
    assert pvl.region_for_nuts("Springfield") is None  # unmapped -> None (national)


# --------------------------------------------------------------------------- #
# registration — does NOT shadow the existing OCDS `anac` source
# --------------------------------------------------------------------------- #


def test_registered_alongside_anac():
    source = get("anac_pvl")
    assert source.id == "anac_pvl" and source.kind == "tender"
    ids = {s.id for s in list_sources()}
    assert {"anac", "anac_pvl"} <= ids  # both present, distinct sources
