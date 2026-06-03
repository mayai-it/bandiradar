"""Tests for the canonical model (ARCHITECTURE.md §4 / Prompt 1).

Covers:
- model validation (good construction + rejected bad values),
- content_hash stability (deterministic; sensitive to meaningful fields;
  INSENSITIVE to version and updated_at),
- default_status for the open / closing_soon / closed cases.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from bandiradar.models import (
    CLOSING_SOON_DAYS,
    Match,
    Opportunity,
    Profile,
    RawDoc,
    ValueRange,
    default_status,
)

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)


def make_opportunity(**overrides) -> Opportunity:
    """Build a valid Opportunity, overriding individual fields per test."""
    base = dict(
        id="anac:ocds-bandi-0001",
        source="anac",
        source_url="https://example.invalid/anac/ocds-bandi-0001",
        kind="tender",
        title="Servizi di sviluppo software gestionale",
        summary="Sviluppo e manutenzione di una piattaforma gestionale.",
        issuer_name="Comune di Roma Capitale",
        issuer_region="Lazio",
        cpv=["72000000"],
        value_amount=120000.0,
        value_min=None,
        value_max=None,
        geo_scope="regional",
        region="Lazio",
        deadline=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        eligibility_text="Operatori economici iscritti al MEPA.",
        status="open",
        raw_ref="anac:ocds-bandi-0001",
    )
    base.update(overrides)
    return Opportunity(**base)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_valid_opportunity_constructs_and_autofills_content_hash():
    opp = make_opportunity()
    assert opp.version == 1
    assert opp.value_currency == "EUR"  # default
    assert opp.ateco_hints == [] and opp.keywords == []  # default factories
    # content_hash auto-populated when not supplied.
    assert opp.content_hash
    assert opp.content_hash == opp.compute_content_hash()


def test_explicit_content_hash_is_preserved():
    opp = make_opportunity(content_hash="preset")
    assert opp.content_hash == "preset"


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "subsidy"},
        {"status": "expired"},
        {"geo_scope": "galactic"},
    ],
)
def test_invalid_literals_are_rejected(bad):
    with pytest.raises(ValidationError):
        make_opportunity(**bad)


def test_value_min_greater_than_max_is_rejected():
    with pytest.raises(ValidationError):
        make_opportunity(value_min=500.0, value_max=100.0)


def test_match_score_must_be_within_0_100():
    Match(opportunity_id="anac:x", opportunity_hash="h", profile_version="v", score=87)
    with pytest.raises(ValidationError):
        Match(
            opportunity_id="anac:x",
            opportunity_hash="h",
            profile_version="v",
            score=150,
        )
    with pytest.raises(ValidationError):
        Match(
            opportunity_id="anac:x",
            opportunity_hash="h",
            profile_version="v",
            score=-1,
        )


def test_match_carries_cache_key_parts():
    opp = make_opportunity()
    match = Match(
        opportunity_id=opp.id,
        opportunity_hash=opp.content_hash,
        profile_version=make_profile().version,
        score=72,
    )
    # The cache key is (profile_version, opportunity_hash): both are present and
    # opportunity_hash tracks the opportunity's content_hash at scoring time.
    assert match.opportunity_hash == opp.content_hash
    assert match.profile_version


def test_rawdoc_constructs():
    raw = RawDoc(
        id="anac:ocds-bandi-0001",
        source="anac",
        fetched_at=NOW,
        payload={"ocid": "ocds-bandi-0001"},
    )
    assert raw.payload["ocid"] == "ocds-bandi-0001"


# --------------------------------------------------------------------------- #
# content_hash stability
# --------------------------------------------------------------------------- #


def test_content_hash_same_input_same_hash():
    assert make_opportunity().compute_content_hash() == (
        make_opportunity().compute_content_hash()
    )


def test_content_hash_changes_when_meaningful_field_changes():
    base = make_opportunity().compute_content_hash()
    assert make_opportunity(title="Titolo diverso").compute_content_hash() != base
    assert make_opportunity(value_amount=999.0).compute_content_hash() != base
    assert (
        make_opportunity(
            deadline=datetime(2027, 1, 1, tzinfo=UTC)
        ).compute_content_hash()
        != base
    )
    assert (
        make_opportunity(eligibility_text="Altri requisiti.").compute_content_hash()
        != base
    )
    # Change-detection coverage extended to kind / cpv / region / geo_scope.
    assert make_opportunity(kind="grant").compute_content_hash() != base
    assert make_opportunity(cpv=["48000000"]).compute_content_hash() != base
    assert make_opportunity(region="Lombardia").compute_content_hash() != base
    assert make_opportunity(geo_scope="national").compute_content_hash() != base


def test_content_hash_ignores_keywords_and_ateco_hints():
    base = make_opportunity().compute_content_hash()
    assert make_opportunity(keywords=["ai", "cloud"]).compute_content_hash() == base
    assert make_opportunity(ateco_hints=["62.01"]).compute_content_hash() == base


def test_content_hash_ignores_version_and_updated_at():
    base = make_opportunity().compute_content_hash()
    assert make_opportunity(version=99).compute_content_hash() == base
    assert (
        make_opportunity(
            updated_at=datetime(2030, 1, 1, tzinfo=UTC)
        ).compute_content_hash()
        == base
    )


# --------------------------------------------------------------------------- #
# Timezone safety
# --------------------------------------------------------------------------- #


def test_naive_datetimes_are_coerced_to_utc_on_opportunity():
    naive = datetime(2026, 9, 15, 10, 0)  # no tzinfo
    opp = make_opportunity(
        deadline=naive,
        published_at=naive,
        updated_at=naive,
    )
    assert opp.deadline.tzinfo is UTC
    assert opp.published_at.tzinfo is UTC
    assert opp.updated_at.tzinfo is UTC
    assert opp.deadline == datetime(2026, 9, 15, 10, 0, tzinfo=UTC)


def test_naive_fetched_at_is_coerced_to_utc_on_rawdoc():
    raw = RawDoc(
        id="anac:x",
        source="anac",
        fetched_at=datetime(2026, 6, 3, 9, 0),  # naive
        payload={},
    )
    assert raw.fetched_at.tzinfo is UTC


def test_default_status_does_not_crash_on_naive_inputs():
    # Mixing a naive deadline with an aware now must NOT raise (defensive coerce).
    naive_soon = datetime(2026, 6, 5, 12, 0)  # no tzinfo, within 7 days of NOW
    assert default_status(naive_soon, now=NOW) == "closing_soon"
    # Naive `now` is coerced too.
    assert default_status(NOW + timedelta(days=30), now=NOW.replace(tzinfo=None)) == (
        "open"
    )


# --------------------------------------------------------------------------- #
# default_status
# --------------------------------------------------------------------------- #


def test_default_status_open_for_far_future():
    assert default_status(NOW + timedelta(days=30), now=NOW) == "open"


def test_default_status_closing_soon_within_threshold():
    assert default_status(NOW + timedelta(days=CLOSING_SOON_DAYS - 4), now=NOW) == (
        "closing_soon"
    )


def test_default_status_closed_in_the_past():
    assert default_status(NOW - timedelta(days=1), now=NOW) == "closed"


def test_default_status_open_when_no_deadline():
    assert default_status(None, now=NOW) == "open"


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


def make_profile(**overrides) -> Profile:
    base = dict(
        name="MayAI",
        language="it",
        ateco=["62.01", "62.02"],
        cpv_interests=["72000000", "48000000"],
        regions=["Lazio", "national"],
        value_range=ValueRange(min=5000, max=250000),
        capabilities="AI consulting and vertical software for Italian SMEs.",
        exclusions=["construction", "catering"],
    )
    base.update(overrides)
    return Profile(**base)


def test_profile_version_is_stable_and_content_sensitive():
    assert make_profile().version == make_profile().version
    assert make_profile().version != make_profile(name="Other").version
    assert make_profile().version != make_profile(cpv_interests=["72000000"]).version


def test_profile_value_range_order_is_validated():
    with pytest.raises(ValidationError):
        make_profile(value_range=ValueRange(min=100, max=10))
