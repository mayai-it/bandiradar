"""Stage-2 relevance scorer tests — OFFLINE fallback only (Prompt 4).

Zero secrets: an autouse fixture forces the provider to "none" so ``score`` uses
the deterministic heuristic, and the LLM SDKs are never imported.
"""

from datetime import UTC, datetime

import pytest
import yaml

import synthetic_source as synthetic
from bandiradar import resources
from bandiradar.matching import relevance
from bandiradar.matching.llm import get_client
from bandiradar.matching.relevance import (
    HEURISTIC,
    InMemoryScoreCache,
    cache_key,
    heuristic_fallback,
    score,
)
from bandiradar.models import Profile

NOW = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)
PROFILES = resources.profiles_dir()


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
    for raw in synthetic.load_fixture():
        for opp in synthetic.to_opportunities(raw, now=NOW):
            out[opp.id] = opp
    return out


def test_get_client_is_none_when_provider_none():
    assert get_client() is None


def test_get_client_is_none_when_provider_unset(monkeypatch):
    monkeypatch.delenv("BANDIRADAR_LLM_PROVIDER", raising=False)
    assert get_client() is None


def test_score_is_deterministic_and_bounded():
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    a = score(opp, mayai, now=NOW)
    b = score(opp, mayai, now=NOW)
    assert a == b
    assert 0 <= a.score <= 100


def test_strong_match_outranks_weak_match():
    opps = opps_by_id()
    mayai = load_profile("mayai.yaml")
    strong = score(opps["synthetic:ocds-bandi-0001"], mayai, now=NOW)  # software, Lazio
    weak = score(opps["synthetic:ocds-bandi-0006"], mayai, now=NOW)  # machinery, ER
    assert strong.score > weak.score


def test_matched_capabilities_non_empty_for_strong_match():
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    result = heuristic_fallback(opp, mayai)
    assert result.matched_capabilities


def test_match_carries_cache_key_parts():
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
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
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
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
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
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
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]  # CPV 72000000 -> division 72
    mayai = load_profile("mayai.yaml")
    cache = InMemoryScoreCache()
    benchmarks = [_bench72()]

    enriched = score(opp, mayai, cache=cache, benchmarks=benchmarks, now=NOW)
    anac_reasons = [r for r in enriched.reasons if "ANAC history" in r]
    assert len(anac_reasons) == 1  # benchmark reason appended

    # The CACHED match is bare (no enrichment).
    cached = cache.get(cache_key(opp, mayai, "heuristic"))
    assert not any("ANAC history" in r for r in cached.reasons)

    # A 2nd enriched call does NOT double-append.
    again = score(opp, mayai, cache=cache, benchmarks=benchmarks, now=NOW)
    assert again.reasons == enriched.reasons
    assert len([r for r in again.reasons if "ANAC history" in r]) == 1


def test_benchmarks_none_leaves_match_unchanged():
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    bare = score(opp, mayai, now=NOW)
    assert not any("ANAC history" in r for r in bare.reasons)


def test_heuristic_sentinel_forces_offline_even_with_a_client(monkeypatch):
    """``client=HEURISTIC`` must use the offline heuristic even when a provider is
    configured — otherwise the eval "heuristic" baseline secretly becomes the LLM."""
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    spy = _SpyClient()
    # Pretend a real client IS configured (the default fallback would pick it up).
    monkeypatch.setattr(relevance, "get_client", lambda: spy)

    forced = score(opp, mayai, client=HEURISTIC, now=NOW)
    assert spy.calls == 0  # the configured client was NOT consulted
    assert forced.score == heuristic_fallback(opp, mayai, now=NOW).score

    # client=None still falls back to the configured client (CLI/MCP behaviour).
    fell_back = score(opp, mayai, client=None, now=NOW)
    assert spy.calls == 1
    assert fell_back.score == 77  # the spy's fixed score, not the heuristic


def test_score_all_sorted_desc():
    opps = list(opps_by_id().values())
    mayai = load_profile("mayai.yaml")
    matches = relevance.score_all(opps, mayai, now=NOW)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------- #
# cache-key correctness (no false reuse across different scoring inputs)
# --------------------------------------------------------------------------- #


def test_cache_key_differs_with_vs_without_documents():
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    with_docs = opp.model_copy(update={"document_text": "requisiti dal disciplinare"})
    # Same opportunity + profile + backend, but document text folded in -> the key
    # MUST differ, so a --with-documents score never reuses a bare one.
    assert cache_key(opp, mayai, "heuristic") != cache_key(
        with_docs, mayai, "heuristic"
    )


def test_cache_key_differs_by_backend_model():
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    assert cache_key(opp, mayai, "heuristic") != cache_key(
        opp, mayai, "anthropic:claude-haiku-4-5-20251001"
    )


def test_distinct_opportunities_do_not_collide():
    mayai = load_profile("mayai.yaml")
    a = opps_by_id()["synthetic:ocds-bandi-0001"]
    b = opps_by_id()["synthetic:ocds-bandi-0002"]
    assert cache_key(a, mayai, "heuristic") != cache_key(b, mayai, "heuristic")


def test_cache_key_differs_by_full_text():
    # A full-text score must never reuse the capped-brief score for the same
    # opportunity/backend (eval full-text experiment correctness).
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    assert cache_key(opp, mayai, "anthropic:m") != cache_key(
        opp, mayai, "anthropic:m", full_text=True
    )
    # The default (brief) key is unchanged — no suffix appended.
    assert cache_key(opp, mayai, "anthropic:m") == cache_key(
        opp, mayai, "anthropic:m", full_text=False
    )


def test_opportunity_brief_full_text_toggle():
    from bandiradar.matching import prompts

    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
    long_text = "requisiti " * 2000  # > _MAX_DOC_CHARS
    opp = opp.model_copy(update={"eligibility_text": long_text})

    brief = prompts.opportunity_brief(opp)
    full = prompts.opportunity_brief(opp, full_text=True)
    assert "…[truncated]" in brief
    assert "…[truncated]" not in full
    assert len(full) > len(brief)


def test_documents_run_does_not_reuse_bare_score():
    # A bare score is cached; a later --with-documents score of the SAME opp must
    # MISS (different input) and recompute, not reuse the bare result.
    opp = opps_by_id()["synthetic:ocds-bandi-0001"]
    mayai = load_profile("mayai.yaml")
    cache = InMemoryScoreCache()
    spy = _SpyClient()

    score(opp, mayai, client=spy, cache=cache, now=NOW)
    assert spy.calls == 1
    with_docs = opp.model_copy(update={"document_text": "extra requirements text"})
    score(with_docs, mayai, client=spy, cache=cache, now=NOW)
    assert spy.calls == 2  # no false reuse of the bare score


# --------------------------------------------------------------------------- #
# LLM budget — cap NEW scorings (cache misses) per run; defer the rest.
# --------------------------------------------------------------------------- #


def test_llm_budget_caps_new_scorings_and_defers_rest():
    opps = list(opps_by_id().values())
    assert len(opps) >= 4
    mayai = load_profile("mayai.yaml")
    cache = InMemoryScoreCache()
    spy = _SpyClient()
    budget = relevance.LLMBudget(limit=2)

    matches = relevance.score_all(
        opps, mayai, client=spy, cache=cache, now=NOW, budget=budget
    )

    assert spy.calls == 2  # only 2 real LLM calls
    assert budget.scored == 2
    assert budget.deferred == len(opps) - 2
    assert len(matches) == 2  # deferred items get NO match this run...
    assert all(m.score == 77 for m in matches)  # ...and are never heuristic-mixed


def test_llm_budget_amortizes_over_runs():
    opps = list(opps_by_id().values())
    mayai = load_profile("mayai.yaml")
    cache = InMemoryScoreCache()
    spy = _SpyClient()

    # Run 1: budget 2 -> 2 scored + cached, the rest deferred.
    relevance.score_all(
        opps, mayai, client=spy, cache=cache, now=NOW, budget=relevance.LLMBudget(2)
    )
    # Run 2: the 2 cached are FREE hits; the budget scores 2 NEW ones.
    b2 = relevance.LLMBudget(limit=2)
    matches2 = relevance.score_all(
        opps, mayai, client=spy, cache=cache, now=NOW, budget=b2
    )
    assert b2.scored == 2  # 2 new this run
    assert spy.calls == 4  # 2 (run1) + 2 (run2); cached hits cost nothing
    assert len(matches2) == 4  # 2 cache hits + 2 new


def test_llm_budget_none_scores_everything():
    opps = list(opps_by_id().values())
    mayai = load_profile("mayai.yaml")
    spy = _SpyClient()
    matches = relevance.score_all(
        opps, mayai, client=spy, cache=InMemoryScoreCache(), now=NOW, budget=None
    )
    assert len(matches) == len(opps)
    assert spy.calls == len(opps)


def test_llm_budget_does_not_constrain_heuristic():
    # The heuristic backend has no per-call cost, so the budget never defers it.
    opps = list(opps_by_id().values())
    mayai = load_profile("mayai.yaml")
    budget = relevance.LLMBudget(limit=1)
    matches = relevance.score_all(
        opps,
        mayai,
        client=HEURISTIC,
        cache=InMemoryScoreCache(),
        now=NOW,
        budget=budget,
    )
    assert len(matches) == len(opps)  # all scored by the heuristic
    assert budget.scored == 0 and budget.deferred == 0


def test_llm_budget_from_env(monkeypatch):
    monkeypatch.setenv("BANDIRADAR_LLM_BUDGET", "1500")
    assert relevance.LLMBudget.from_env().limit == 1500
    monkeypatch.delenv("BANDIRADAR_LLM_BUDGET", raising=False)
    assert relevance.LLMBudget.from_env().limit is None  # unset => unlimited
    monkeypatch.setenv("BANDIRADAR_LLM_BUDGET", "0")
    assert relevance.LLMBudget.from_env().limit is None  # non-positive => unlimited
