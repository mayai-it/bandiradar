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


def test_run_match_grant_only_profile_drops_tenders(store):
    # End-to-end: a grant-only profile (mayai, seeks=["grant"]) must drop public
    # tenders at Stage 1 even when they'd otherwise match (same fields, differ only
    # in kind). The incentive survives; the tender does not.
    from bandiradar.models import Opportunity

    mayai = core.load_profile("mayai")
    common = dict(
        source="x",
        source_url="https://example.invalid/x",
        title="Piattaforma software per la PA",
        summary="software e dati",
        cpv=["72000000"],  # matches mayai cpv_interests -> passes the relevance gate
        geo_scope="national",  # bypasses the geography gate
        region=None,
        status="open",
    )
    store.upsert_opportunity(
        Opportunity(id="x:tender", kind="tender", raw_ref="x:t", **common), now=NOW
    )
    store.upsert_opportunity(
        Opportunity(id="x:incentive", kind="incentive", raw_ref="x:i", **common),
        now=NOW,
    )
    ranked = core.run_match(mayai, store, source_id="x", now=NOW)
    assert {opp.id for opp, _ in ranked} == {"x:incentive"}


def test_min_score_for_mode_mapping():
    assert core.min_score_for_mode("precision") == 40
    assert core.min_score_for_mode("balanced") == 20
    assert core.min_score_for_mode("recall") == 0
    assert core.DEFAULT_MODE == "balanced"
    import pytest

    with pytest.raises(ValueError):
        core.min_score_for_mode("nope")


def test_run_match_mode_maps_and_overrides_min_score(store):
    mayai = core.load_profile("mayai")
    kw = dict(source_id="synthetic", sample=True, now=NOW)
    # mode == the equivalent explicit cutoff
    for mode in ("precision", "balanced", "recall"):
        by_mode = core.run_match(mayai, store, mode=mode, **kw)
        by_score = core.run_match(
            mayai, store, min_score=core.min_score_for_mode(mode), **kw
        )
        assert [o.id for o, _ in by_mode] == [o.id for o, _ in by_score]
    # mode takes precedence over an explicit min_score
    precedence = core.run_match(mayai, store, mode="precision", min_score=0, **kw)
    expected = core.run_match(mayai, store, min_score=40, **kw)
    assert [o.id for o, _ in precedence] == [o.id for o, _ in expected]


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
