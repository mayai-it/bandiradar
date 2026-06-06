"""Core pipeline tests (ARCHITECTURE.md §3 / Prompt 6). Offline, tmp db, fixed now."""

from datetime import UTC, datetime

import pytest

from bandiradar import core
from bandiradar.storage import Store

NOW = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "core.db"))
    yield s
    s.close()


def test_run_fetch_sample_counts_and_dedupe(store):
    first = core.run_fetch("synthetic", store, sample=True, now=NOW)
    assert (first.source, first.status) == ("synthetic", "ok")
    assert (first.fetched, first.mapped, first.new, first.amended) == (6, 6, 6, 0)
    assert first.skipped_invalid == 0
    assert first.error is None
    assert first.duration_s >= 0.0

    second = core.run_fetch("synthetic", store, sample=True, now=NOW)
    assert second.new == 0
    assert second.amended == 0
    assert second.skipped_invalid == 0
    assert len(store.list_opportunities()) == 6


def test_run_match_mayai_keepset_ranked_desc(store):
    mayai = core.load_profile("mayai")
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
    mayai = core.load_profile("mayai")
    full = core.run_match(mayai, store, sample=True, now=NOW)
    limited = core.run_match(mayai, store, sample=True, now=NOW, limit=1)
    assert len(limited) == 1
    assert limited[0][0].id == full[0][0].id  # same top result

    high = core.run_match(mayai, store, sample=True, now=NOW, min_score=101)
    assert high == []


def test_run_monitor_fetches_then_matches(store):
    mayai = core.load_profile("mayai")
    ranked = core.run_monitor(mayai, "synthetic", store, sample=True, now=NOW)
    assert {opp.id for opp, _ in ranked} == {
        "synthetic:ocds-bandi-0001",
        "synthetic:ocds-bandi-0002",
        "synthetic:ocds-bandi-0004",
    }


def test_run_fetch_quarantines_invalid_records(store, monkeypatch):
    """One dirty record that fails to map is skipped + counted, never fatal."""
    from bandiradar.models import Opportunity, RawDoc

    class _DirtySource:
        id = "dirty"
        kind = "incentive"

        def to_opportunities(self, raw, now=None):
            if raw.payload.get("bad"):
                raise ValueError("dirty record: bad value bounds")
            return [
                Opportunity(
                    id=f"dirty:{raw.id}",
                    source="dirty",
                    source_url="",
                    kind="incentive",
                    title="ok",
                    geo_scope="national",
                    status="open",
                    raw_ref=raw.id,
                )
            ]

        def load_fixture(self):
            return [
                RawDoc(id="good", source="dirty", fetched_at=NOW, payload={}),
                RawDoc(id="bad", source="dirty", fetched_at=NOW, payload={"bad": True}),
            ]

    monkeypatch.setattr(core, "get", lambda _sid: _DirtySource())
    result = core.run_fetch("dirty", store, sample=True, now=NOW)
    assert result.status == "ok"  # a quarantined record is not a failure
    assert result.fetched == 2  # both raw docs pulled
    assert result.mapped == 1  # only the good one mapped
    assert result.new == 1
    assert result.skipped_invalid == 1  # the dirty one quarantined, not fatal
    assert len(store.list_opportunities()) == 1
