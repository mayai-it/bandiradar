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


def test_raw_stream_threads_store_to_llm_sources_only():
    """An LLM scraper gets the caller's ``store`` (so its extraction cache + recipe
    store persist on the same DB); a keyless source does not take it."""

    class _Src:
        def __init__(self, requires_llm):
            self.id = "x"
            self.requires_llm = requires_llm
            self.got: dict = {}

        def fetch(self, since=None, **kw):
            self.got = kw
            return iter([])

    sentinel = object()
    llm = _Src(requires_llm=True)
    list(core._raw_stream(llm, False, None, 10, None, None, sentinel))
    assert llm.got.get("store") is sentinel

    keyless = _Src(requires_llm=False)
    list(core._raw_stream(keyless, False, None, 10, None, None, sentinel))
    assert "store" not in keyless.got


def test_llm_fetch_persists_extractions_and_golden_to_passed_store(monkeypatch):
    """Regression: an LLM scraper must persist its extractions + crawl golden on the
    STORE the caller passed (so ``--db`` controls all persistence), not a default DB."""
    from pathlib import Path

    from bandiradar.recipe_store import RecipeStore
    from bandiradar.sources import veneto
    from bandiradar.storage import SqliteExtractionCache

    page = (Path(__file__).parent / "cassettes" / "veneto_listing.html").read_text(
        encoding="utf-8"
    )
    src = veneto.VenetoSource()
    monkeypatch.setattr(src, "_listing_html", lambda recipe: page)
    monkeypatch.setattr(src, "_fetch_text", lambda url: "Testo del bando di prova.")

    class _FakeClient:
        def score(self, system, user):
            return {"title": "Bando di prova", "kind": "incentive"}

    store = Store(":memory:")
    try:
        raws = list(src.fetch(client=_FakeClient(), store=store, limit=2))
        assert raws  # extracted at least one
        cache = SqliteExtractionCache(store)
        assert all(cache.get(r.url) is not None for r in raws)  # cached in THIS store
        assert RecipeStore(store).get_golden("veneto")  # golden too
    finally:
        store.close()


def test_sample_match_is_reproducible_and_pinned():
    """`--sample` pins ``now`` to ``SAMPLE_NOW`` so the Quickstart reproduces forever
    regardless of the calendar date. Guards the README's documented output (3 matches
    for `mayai` at the balanced cutoff)."""
    profile = core.load_profile("mayai")
    s1 = Store(":memory:")
    s2 = Store(":memory:")
    try:
        a = core.run_match(profile, s1, sample=True, mode="balanced")
        b = core.run_match(profile, s2, sample=True, mode="balanced")
    finally:
        s1.close()
        s2.close()
    assert [o.id for o, _ in a] == [o.id for o, _ in b]  # deterministic
    # The README's documented matches are present (the test suite also registers a
    # `synthetic` source, so don't assert an exact count — assert the real ones).
    ids = {o.id for o, _ in a}
    assert {"incentivi:3400", "lazio:48841", "lazio:58887"} <= ids


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


def test_run_match_excludes_quarantined_upstream_of_prefilter(store):
    # Two identical incentives, one quarantined by the trust spine: it stays in
    # the DB (audit) but never reaches the matcher; the dedicated flag re-adds it.
    from bandiradar.models import Opportunity

    mayai = core.load_profile("mayai")
    common = dict(
        source="x",
        kind="incentive",
        title="Piattaforma software per la PA",
        summary="software e dati",
        cpv=["72000000"],
        geo_scope="national",
        region=None,
        status="open",
    )
    store.upsert_opportunity(
        Opportunity(
            id="x:clean",
            source_url="https://example.invalid/clean",
            raw_ref="x:c",
            provenance="llm",
            confidence=1.0,
            trust_verdict="ok",
            **common,
        ),
        now=NOW,
    )
    store.upsert_opportunity(
        Opportunity(
            id="x:quarantined",
            source_url="https://example.invalid/quarantined",
            raw_ref="x:q",
            provenance="llm",
            confidence=0.1,
            trust_verdict="quarantine",
            **common,
        ),
        now=NOW,
    )

    ranked = core.run_match(mayai, store, source_id="x", now=NOW)
    assert {opp.id for opp, _ in ranked} == {"x:clean"}
    # Still in the DB — excluded from matching, not deleted.
    assert {o.id for o in store.list_opportunities(source="x")} == {
        "x:clean",
        "x:quarantined",
    }
    # The dedicated audit/debug flag re-includes it.
    included = core.run_match(
        mayai, store, source_id="x", now=NOW, include_quarantined=True
    )
    assert {opp.id for opp, _ in included} == {"x:clean", "x:quarantined"}


def test_exclude_quarantined_keeps_suspect_and_unassessed():
    from bandiradar.models import Opportunity

    def _opp(i, verdict):
        return Opportunity(
            id=f"x:{i}",
            source="x",
            source_url=f"https://example.invalid/{i}",
            kind="incentive",
            title=f"Bando {i}",
            geo_scope="national",
            status="open",
            raw_ref=f"x:{i}",
            provenance="llm" if verdict else "structured",
            trust_verdict=verdict,
        )

    opps = [_opp(1, "ok"), _opp(2, "suspect"), _opp(3, "quarantine"), _opp(4, None)]
    kept = core.exclude_quarantined(opps)
    assert [o.id for o in kept] == ["x:1", "x:2", "x:4"]


def test_run_trust_backfill_targets_llm_sources_only(store):
    # core derives the allowed set from the registry (requires_llm): toscana is
    # an LLM scraper, incentivi is structured — even when both rows share the
    # same detail URL (the national hub lists regional bandi), only the LLM
    # row receives the cached report.
    from bandiradar.models import Opportunity
    from bandiradar.storage import SqliteExtractionCache

    url = "https://x/bando/shared"
    for source in ("toscana", "incentivi"):
        store.upsert_opportunity(
            Opportunity(
                id=f"{source}:s1",
                source=source,
                source_url=url,
                kind="incentive",
                title="Bando condiviso",
                geo_scope="regional",
                status="open",
                raw_ref=f"{source}:s1",
            ),
            now=NOW,
        )
    cache = SqliteExtractionCache(store)
    cache.set(url, {"title": "Bando condiviso"})
    cache.set_trust(url, {"checks": {}, "confidence": 0.2, "verdict": "quarantine"})

    assert core.run_trust_backfill(store) == 1
    assert store.trust_counts() == {"toscana": {"quarantine": 1}}
    assert core.run_trust_backfill(store) == 0  # idempotent
