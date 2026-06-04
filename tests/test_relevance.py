"""Stage-2 relevance scorer tests — OFFLINE fallback only (Prompt 4).

Zero secrets: an autouse fixture forces the provider to "none" so ``score`` uses
the deterministic heuristic, and the LLM SDKs are never imported.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from bandiradar.matching import relevance
from bandiradar.matching.llm import get_client
from bandiradar.matching.relevance import (
    InMemoryScoreCache,
    heuristic_fallback,
    score,
)
from bandiradar.models import Profile
from bandiradar.sources import anac

NOW = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)
PROFILES = Path(__file__).resolve().parents[1] / "data" / "profiles"


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    """Guarantee the offline path regardless of the developer's environment."""
    monkeypatch.setenv("BANDIRADAR_LLM_PROVIDER", "none")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def load_profile(name: str) -> Profile:
    return Profile(**yaml.safe_load((PROFILES / name).read_text(encoding="utf-8")))


def opps_by_id() -> dict:
    out = {}
    for raw in anac.load_fixture():
        for opp in anac.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_get_client_is_none_when_provider_none():
    assert get_client() is None


def test_get_client_is_none_when_provider_unset(monkeypatch):
    monkeypatch.delenv("BANDIRADAR_LLM_PROVIDER", raising=False)
    assert get_client() is None


def test_score_is_deterministic_and_bounded():
    opp = opps_by_id()["anac:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    a = score(opp, mayai, now=NOW)
    b = score(opp, mayai, now=NOW)
    assert a == b
    assert 0 <= a.score <= 100


def test_strong_match_outranks_weak_match():
    opps = opps_by_id()
    mayai = load_profile("mayai.yaml")
    strong = score(opps["anac:ocds-bandi-0001"], mayai, now=NOW)  # software, Lazio
    weak = score(opps["anac:ocds-bandi-0006"], mayai, now=NOW)  # machinery, ER
    assert strong.score > weak.score


def test_matched_capabilities_non_empty_for_strong_match():
    opp = opps_by_id()["anac:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    result = heuristic_fallback(opp, mayai)
    assert result.matched_capabilities


def test_match_carries_cache_key_parts():
    opp = opps_by_id()["anac:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    match = score(opp, mayai, now=NOW)
    assert match.opportunity_id == opp.id
    assert match.opportunity_hash == opp.content_hash
    assert match.profile_version == mayai.version


class _SpyClient:
    """Counts .score calls; returns a fixed relevance dict."""

    def __init__(self) -> None:
        self.calls = 0

    def score(self, system: str, user: str) -> dict:
        self.calls += 1
        return {
            "score": 77,
            "reasons": ["spy"],
            "matched_capabilities": ["software"],
            "eligibility_flags": [],
            "risk_notes": [],
        }


def test_cache_avoids_recompute_and_returns_equal_match():
    opp = opps_by_id()["anac:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    cache = InMemoryScoreCache()
    spy = _SpyClient()

    first = score(opp, mayai, client=spy, cache=cache, now=NOW)
    second = score(opp, mayai, client=spy, cache=cache, now=NOW)

    assert spy.calls == 1  # second call served from cache, no recompute
    assert first == second
    assert first.score == 77


def test_amended_opportunity_misses_cache(monkeypatch):
    # Changing a meaningful field changes content_hash -> new cache key -> recompute.
    opp = opps_by_id()["anac:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    cache = InMemoryScoreCache()
    spy = _SpyClient()

    score(opp, mayai, client=spy, cache=cache, now=NOW)
    amended = opp.model_copy(update={"title": "Nuovo titolo rettificato"})
    amended.content_hash = amended.compute_content_hash()
    score(amended, mayai, client=spy, cache=cache, now=NOW)

    assert spy.calls == 2


def _bench72():
    from bandiradar.intelligence.benchmarks import Benchmark

    return Benchmark(
        cpv_division="72",
        region=None,
        count=8,
        value_median=104326.0,
        value_p25=71619.0,
        value_p75=183410.0,
        value_min=39532.0,
        value_max=283142.0,
        by_year={2025: 8},
        distinct_suppliers=8,
    )


def test_benchmarks_append_enrichment_but_cache_stays_bare():
    opp = opps_by_id()["anac:ocds-bandi-0001"]  # CPV 72000000 -> division 72
    mayai = load_profile("mayai.yaml")
    cache = InMemoryScoreCache()
    benchmarks = [_bench72()]

    enriched = score(opp, mayai, cache=cache, benchmarks=benchmarks, now=NOW)
    anac_reasons = [r for r in enriched.reasons if "ANAC history" in r]
    assert len(anac_reasons) == 1  # benchmark reason appended

    # The CACHED match is bare (no enrichment).
    cached = cache.get((mayai.version, opp.content_hash))
    assert not any("ANAC history" in r for r in cached.reasons)

    # A 2nd enriched call does NOT double-append.
    again = score(opp, mayai, cache=cache, benchmarks=benchmarks, now=NOW)
    assert again.reasons == enriched.reasons
    assert len([r for r in again.reasons if "ANAC history" in r]) == 1


def test_benchmarks_none_leaves_match_unchanged():
    opp = opps_by_id()["anac:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    bare = score(opp, mayai, now=NOW)
    assert not any("ANAC history" in r for r in bare.reasons)


def test_score_all_sorted_desc():
    opps = list(opps_by_id().values())
    mayai = load_profile("mayai.yaml")
    matches = relevance.score_all(opps, mayai, now=NOW)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)
