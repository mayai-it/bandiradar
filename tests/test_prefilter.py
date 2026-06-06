"""Tests for the Stage-1 deterministic prefilter (ARCHITECTURE.md §6 / Prompt 3).

Offline, fixed ``now``. Real-profile cases are built from the bundled synthetic
OCDS fixture + the two shipped YAML profiles; edge cases use small synthetic
opportunities/profiles to isolate one gate at a time.
"""

from datetime import UTC, datetime, timedelta

import yaml

import synthetic_source as synthetic
from bandiradar import resources
from bandiradar.matching.prefilter import prefilter, prefilter_explain
from bandiradar.models import Opportunity, Profile, ValueRange

NOW = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)
PROFILES = resources.profiles_dir()


def load_profile(name: str) -> Profile:
    data = yaml.safe_load((PROFILES / name).read_text(encoding="utf-8"))
    return Profile(**data)


def all_opportunities() -> list[Opportunity]:
    opps: list[Opportunity] = []
    for raw in synthetic.load_fixture():
        opps.extend(synthetic.to_opportunities(raw, now=NOW))
    return opps


def reasons_by_id(opps, profile) -> dict[str, tuple[bool, str]]:
    return {o.id: (kept, why) for o, kept, why in prefilter_explain(opps, profile, NOW)}


def make_opp(**overrides) -> Opportunity:
    """A synthetic opportunity that passes every gate against an empty profile."""
    base = dict(
        id="x:1",
        source="x",
        source_url="https://example.invalid/x/1",
        kind="tender",
        title="Generic title",
        summary="generic summary",
        geo_scope="national",  # bypasses the geography gate by default
        region=None,
        status="open",
        raw_ref="x:1",
    )
    base.update(overrides)
    return Opportunity(**base)


# --------------------------------------------------------------------------- #
# Real profiles against the synthetic OCDS fixture
# --------------------------------------------------------------------------- #


def test_mayai_keepset():
    opps = all_opportunities()
    mayai = load_profile("mayai.yaml")
    kept = {o.id for o in prefilter(opps, mayai, now=NOW)}
    assert kept == {
        "synthetic:ocds-bandi-0001",
        "synthetic:ocds-bandi-0002",
        "synthetic:ocds-bandi-0004",
    }
    reasons = reasons_by_id(opps, mayai)
    region_drop = (False, "region not among profile regions")
    assert reasons["synthetic:ocds-bandi-0003"][0] is False
    assert "closed" in reasons["synthetic:ocds-bandi-0003"][1]  # past deadline
    assert reasons["synthetic:ocds-bandi-0005"] == region_drop
    assert reasons["synthetic:ocds-bandi-0006"] == region_drop


def test_mayai_keeps_national_0004_despite_lazio_region_via_geo_bypass():
    opps = all_opportunities()
    mayai = load_profile("mayai.yaml")
    kept = {o.id: o for o in prefilter(opps, mayai, now=NOW)}
    assert "synthetic:ocds-bandi-0004" in kept
    opp = kept["synthetic:ocds-bandi-0004"]
    # Kept because geo_scope is "national" (bypasses geo), not because Lazio
    # is in mayai.regions — mayai.regions is just ["Lazio"], and the region is
    # still populated on the opportunity.
    assert opp.geo_scope == "national"
    assert opp.region == "Lazio"


def test_manifattura_keepset_derived_from_profile():
    # Derivation under the gate rules, NOW = 2026-06-03:
    #   0001 Lazio / 0002 Lazio / 0005 Campania -> region gate (regions are
    #        ["Emilia-Romagna","Lombardia"]);
    #   0003 -> closed (deadline 2026-05-15);
    #   0004 national bypasses geo but CPV 72* doesn't match interests 42*/445*,
    #        no keywords -> relevance gate;
    #   0006 Emilia-Romagna + CPV 42000000 matches interest 42000000 -> KEPT.
    opps = all_opportunities()
    manifattura = load_profile("manifattura.yaml")
    kept = {o.id for o in prefilter(opps, manifattura, now=NOW)}
    assert kept == {"synthetic:ocds-bandi-0006"}
    reasons = reasons_by_id(opps, manifattura)
    assert reasons["synthetic:ocds-bandi-0004"] == (
        False,
        "no CPV match or keyword hit",
    )


# --------------------------------------------------------------------------- #
# Edge cases (one gate at a time)
# --------------------------------------------------------------------------- #


def test_gate1_missing_deadline_passes():
    empty = Profile(name="p")
    assert prefilter([make_opp(deadline=None)], empty, now=NOW)


def test_gate1_past_deadline_drops():
    empty = Profile(name="p")
    opp = make_opp(deadline=NOW - timedelta(days=1))
    results = prefilter_explain([opp], empty, now=NOW)
    assert results[0][1] is False and "closed" in results[0][2]


def test_gate2_empty_profile_regions_disables_geo():
    empty = Profile(name="p")  # no regions
    opp = make_opp(geo_scope="regional", region="Sicilia")
    assert prefilter([opp], empty, now=NOW)


def test_gate2_regional_mismatch_drops_when_profile_lists_regions():
    profile = Profile(name="p", regions=["Lazio"])
    opp = make_opp(geo_scope="regional", region="Sicilia")
    results = prefilter_explain([opp], profile, now=NOW)
    assert results[0][1] is False and "region" in results[0][2]


def test_gate3_no_value_info_passes():
    profile = Profile(name="p", value_range=ValueRange(min=10000, max=20000))
    opp = make_opp(value_amount=None)  # no value info at all
    assert prefilter([opp], profile, now=NOW)


def test_gate3_value_outside_range_drops():
    profile = Profile(name="p", value_range=ValueRange(min=10000, max=20000))
    opp = make_opp(value_amount=5.0)
    results = prefilter_explain([opp], profile, now=NOW)
    assert results[0][1] is False and "value" in results[0][2]


def test_gate5_cpv_prefix_match_keeps():
    profile = Profile(name="p", cpv_interests=["72000000"])
    opp = make_opp(cpv=["72212000"])  # "72212" starts with "72" -> match
    assert prefilter([opp], profile, now=NOW)


def test_gate5_no_cpv_or_keyword_drops():
    profile = Profile(name="p", cpv_interests=["72000000"])
    opp = make_opp(cpv=["45000000"])
    results = prefilter_explain([opp], profile, now=NOW)
    assert results[0][1] is False and "CPV" in results[0][2]


def test_gate5_keyword_hit_keeps():
    profile = Profile(name="p", keywords=["machine learning"])
    opp = make_opp(title="Servizi di Machine Learning", cpv=[])
    assert prefilter([opp], profile, now=NOW)


def test_gate5_keyword_hit_in_eligibility_text_keeps():
    # Incentives have no CPV and a generic title; their relevance signal lives in
    # the eligibility/requirements text. Gate 5 must scan it (Prompt 11).
    profile = Profile(name="p", keywords=["digitalizzazione"])
    opp = make_opp(
        cpv=[],
        title="Bando generico",
        summary="nessun dettaglio rilevante",
        eligibility_text="Interventi per la digitalizzazione delle microimprese.",
    )
    assert prefilter([opp], profile, now=NOW)


def test_gate5_no_signal_anywhere_drops():
    profile = Profile(name="p", keywords=["digitalizzazione"])
    opp = make_opp(cpv=[], title="Bando", summary="nulla", eligibility_text="nulla")
    results = prefilter_explain([opp], profile, now=NOW)
    assert results[0][1] is False and "CPV" in results[0][2]


def test_gate5_skipped_when_profile_has_no_signal_sources():
    # No cpv_interests and no keywords -> relevance gate does not run.
    profile = Profile(name="p")
    opp = make_opp(cpv=["99999999"], title="Totally unrelated")
    assert prefilter([opp], profile, now=NOW)
