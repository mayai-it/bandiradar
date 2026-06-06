"""SQLite storage tests (ARCHITECTURE.md §8 / Prompt 5). Offline, tmp db."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import synthetic_source as synthetic
from bandiradar.matching import relevance
from bandiradar.models import Match, Profile
from bandiradar.storage import SqliteScoreCache, Store

NOW = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)
PROFILES = Path(__file__).resolve().parents[1] / "data" / "profiles"


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def load_profile(name: str) -> Profile:
    return Profile(**yaml.safe_load((PROFILES / name).read_text(encoding="utf-8")))


def first_opp():
    raw = synthetic.load_fixture()[0]  # ocds-bandi-0001
    return synthetic.to_opportunities(raw, now=NOW)[0]


# --------------------------------------------------------------------------- #
# dedupe / change detection
# --------------------------------------------------------------------------- #


def test_insert_then_unchanged_then_amended(store):
    opp = first_opp()

    assert store.upsert_opportunity(opp, now=NOW) == "new"

    # Identical re-insert: no-op, no duplicate row, version unchanged.
    assert store.upsert_opportunity(opp, now=NOW) == "unchanged"
    assert len(store.list_opportunities()) == 1
    assert store.get_opportunity(opp.id).version == 1

    # Changed meaningful field -> content_hash changes -> amended.
    amended_in = opp.model_copy(update={"title": "Titolo rettificato"})
    amended_in.content_hash = amended_in.compute_content_hash()
    result = store.upsert_opportunity(amended_in, now=NOW + timedelta(days=1))
    assert result == "amended"

    stored = store.get_opportunity(opp.id)
    assert stored.version == 2
    assert stored.status == "amended"
    assert stored.title == "Titolo rettificato"
    assert len(store.list_opportunities()) == 1  # still one row


def test_get_and_list_roundtrip(store):
    opp = first_opp()
    store.upsert_opportunity(opp, now=NOW)
    got = store.get_opportunity(opp.id)
    assert got == opp  # faithful round-trip

    assert store.list_opportunities(source="synthetic")
    assert store.list_opportunities(status="open") == [opp]
    assert store.list_opportunities(status="closed") == []
    assert store.get_opportunity("missing") is None


def test_save_and_get_raw_doc_roundtrip(store):
    raw = synthetic.load_fixture()[0]
    store.save_raw_doc(raw)
    got = store.get_raw_doc(raw.id)
    assert got.id == raw.id
    assert got.payload == raw.payload


def test_list_new_filters_by_since(store):
    a = first_opp()
    b = synthetic.to_opportunities(synthetic.load_fixture()[3], now=NOW)[0]  # 0004

    store.upsert_opportunity(a, now=NOW)
    store.upsert_opportunity(b, now=NOW + timedelta(days=2))

    assert {o.id for o in store.list_new(None)} == {a.id, b.id}
    later = store.list_new(NOW + timedelta(days=1))
    assert [o.id for o in later] == [b.id]


# --------------------------------------------------------------------------- #
# matches + SqliteScoreCache
# --------------------------------------------------------------------------- #


def make_match(opp, profile, score=80, opp_hash=None) -> Match:
    return Match(
        opportunity_id=opp.id,
        opportunity_hash=opp_hash or opp.content_hash,
        profile_version=profile.version,
        score=score,
    )


def test_match_save_get_and_cache_hit_miss(store):
    opp = first_opp()
    mayai = load_profile("mayai.yaml")
    cache = SqliteScoreCache(store)
    match = make_match(opp, mayai)

    cache.set((mayai.version, opp.content_hash), match)
    assert store.get_match(opp.id, mayai.version) == match
    assert cache.get((mayai.version, opp.content_hash)) == match

    # Different opportunity_hash -> miss.
    assert cache.get((mayai.version, "different-hash")) is None


class _SpyClient:
    def __init__(self) -> None:
        self.calls = 0

    def score(self, system: str, user: str) -> dict:
        self.calls += 1
        return {"score": 55, "reasons": [], "matched_capabilities": []}


def test_relevance_score_uses_sqlite_cache(store):
    opp = first_opp()
    mayai = load_profile("mayai.yaml")
    cache = SqliteScoreCache(store)
    spy = _SpyClient()

    first = relevance.score(opp, mayai, client=spy, cache=cache, now=NOW)
    second = relevance.score(opp, mayai, client=spy, cache=cache, now=NOW)
    assert spy.calls == 1  # second served from SQLite
    assert first == second

    # Amended opportunity (new content_hash) misses the cache -> re-scored.
    amended = opp.model_copy(update={"title": "Rettifica"})
    amended.content_hash = amended.compute_content_hash()
    relevance.score(amended, mayai, client=spy, cache=cache, now=NOW)
    assert spy.calls == 2


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #


def test_run_lifecycle(store):
    run_id = store.start_run("anac", started_at=NOW)
    store.finish_run(run_id, fetched=6, new=5, amended=1, finished_at=NOW)
    run = store.get_run(run_id)
    assert run["source"] == "anac"
    assert (run["fetched"], run["new"], run["amended"]) == (6, 5, 1)
    assert run["finished_at"] is not None
