"""Core pipeline tests (ARCHITECTURE.md §3 / Prompt 6). Offline, tmp db, fixed now."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bandiradar import core
from bandiradar.storage import Store

NOW = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)
PROFILES = Path(__file__).resolve().parents[1] / "data" / "profiles"


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "core.db"))
    yield s
    s.close()


def test_run_fetch_sample_counts_and_dedupe(store):
    first = core.run_fetch("synthetic", store, sample=True, now=NOW)
    assert first == {"fetched": 6, "new": 6, "amended": 0}

    second = core.run_fetch("synthetic", store, sample=True, now=NOW)
    assert second["new"] == 0
    assert second["amended"] == 0
    assert len(store.list_opportunities()) == 6


def test_run_match_mayai_keepset_ranked_desc(store):
    mayai = core.load_profile(PROFILES / "mayai.yaml")
    # Scope to synthetic: with TED also registered, an unscoped match would span both.
    ranked = core.run_match(mayai, store, source_id="synthetic", sample=True, now=NOW)

    ids = {opp.id for opp, _ in ranked}
    assert ids == {
        "synthetic:ocds-bandi-0001",
        "synthetic:ocds-bandi-0002",
        "synthetic:ocds-bandi-0004",
    }
    scores = [m.score for _, m in ranked]
    assert scores == sorted(scores, reverse=True)
    # Each pair is consistent.
    for opp, m in ranked:
        assert m.opportunity_id == opp.id
        assert 0 <= m.score <= 100


def test_run_match_min_score_and_limit(store):
    mayai = core.load_profile(PROFILES / "mayai.yaml")
    full = core.run_match(mayai, store, sample=True, now=NOW)
    limited = core.run_match(mayai, store, sample=True, now=NOW, limit=1)
    assert len(limited) == 1
    assert limited[0][0].id == full[0][0].id  # same top result

    high = core.run_match(mayai, store, sample=True, now=NOW, min_score=101)
    assert high == []


def test_run_monitor_fetches_then_matches(store):
    mayai = core.load_profile(PROFILES / "mayai.yaml")
    ranked = core.run_monitor(mayai, "synthetic", store, sample=True, now=NOW)
    assert {opp.id for opp, _ in ranked} == {
        "synthetic:ocds-bandi-0001",
        "synthetic:ocds-bandi-0002",
        "synthetic:ocds-bandi-0004",
    }
