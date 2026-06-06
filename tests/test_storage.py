"""SQLite storage tests (ARCHITECTURE.md §8 / Prompt 5). Offline, tmp db."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import yaml

import synthetic_source as synthetic
from bandiradar import resources
from bandiradar.matching import relevance
from bandiradar.models import Match, Opportunity, Profile
from bandiradar.storage import SqliteScoreCache, Store

NOW = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)
PROFILES = resources.profiles_dir()


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


# --------------------------------------------------------------------------- #
# schema upgrade path (pre-credibility-batch DB -> current schema)
# --------------------------------------------------------------------------- #


# The PREVIOUS on-disk schema: `matches` WITHOUT cache_key (and its old index),
# `runs` WITHOUT status/error. Opening such a DB used to crash with
# "no such column: cache_key" because the cache_key index ran before _migrate.
_OLD_SCHEMA = """
CREATE TABLE opportunities (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, content_hash TEXT NOT NULL,
    version INTEGER NOT NULL, status TEXT NOT NULL, deadline TEXT,
    updated_at TEXT, inserted_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE matches (
    opportunity_id TEXT NOT NULL, profile_version TEXT NOT NULL,
    opportunity_hash TEXT NOT NULL, score INTEGER NOT NULL,
    data TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, profile_version)
);
CREATE INDEX idx_matches_cache ON matches (profile_version, opportunity_hash);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, started_at TEXT,
    finished_at TEXT, fetched INTEGER, "new" INTEGER, amended INTEGER
);
"""


def _legacy_db(path: str) -> tuple[Opportunity, Match]:
    """Create a DB with the OLD schema + a couple of rows; return the seeded objects."""
    opp = Opportunity(
        id="anac:legacy-1",
        source="anac",
        source_url="https://example.invalid/legacy-1",
        kind="tender",
        title="Legacy bando",
        geo_scope="national",
        status="open",
        raw_ref="anac:legacy-1",
    )
    match = Match(
        opportunity_id=opp.id,
        opportunity_hash=opp.content_hash,
        profile_version="pv-legacy",
        score=42,
    )
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO opportunities (id, source, content_hash, version, status, "
        "deadline, updated_at, inserted_at, data) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            opp.id,
            opp.source,
            opp.content_hash,
            opp.version,
            opp.status,
            None,
            None,
            NOW.isoformat(),
            opp.model_dump_json(),
        ),
    )
    conn.execute(
        "INSERT INTO matches (opportunity_id, profile_version, opportunity_hash, "
        "score, data, created_at) VALUES (?,?,?,?,?,?)",
        (
            match.opportunity_id,
            match.profile_version,
            match.opportunity_hash,
            match.score,
            match.model_dump_json(),
            NOW.isoformat(),
        ),
    )
    conn.execute(
        'INSERT INTO runs (source, started_at, fetched, "new", amended) '
        "VALUES (?,?,?,?,?)",
        ("anac", NOW.isoformat(), 1, 1, 0),
    )
    conn.commit()
    conn.close()
    return opp, match


def test_opening_old_schema_db_upgrades_cleanly(tmp_path):
    db = str(tmp_path / "legacy.db")
    opp, match = _legacy_db(db)

    # Used to raise OperationalError: no such column: cache_key.
    store = Store(db)
    try:
        # New columns now exist.
        match_cols = {
            r["name"] for r in store.conn.execute("PRAGMA table_info(matches)")
        }
        run_cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(runs)")}
        assert "cache_key" in match_cols
        assert {"status", "error"} <= run_cols

        # Existing rows survived the upgrade and read back.
        assert store.get_opportunity(opp.id) == opp
        assert store.get_match(opp.id, "pv-legacy") == match

        # New inserts/queries work on the upgraded DB.
        opp2 = opp.model_copy(update={"id": "anac:legacy-2"})
        opp2.content_hash = opp2.compute_content_hash()
        assert store.upsert_opportunity(opp2, now=NOW) == "new"

        cache = SqliteScoreCache(store)
        key = ("pv-new", "opp-2", opp2.content_hash, "heuristic:-")
        new_match = Match(
            opportunity_id=opp2.id,
            opportunity_hash=opp2.content_hash,
            profile_version="pv-new",
            score=77,
        )
        cache.set(key, new_match)
        assert cache.get(key) == new_match

        run_id = store.start_run("anac")
        store.finish_run(
            run_id, fetched=2, new=1, amended=0, status="partial", error="boom"
        )
        run = store.get_run(run_id)
        assert run["status"] == "partial" and run["error"] == "boom"
    finally:
        store.close()


def test_migrate_is_idempotent(tmp_path):
    db = str(tmp_path / "idem.db")
    _legacy_db(db)
    Store(db).close()  # first upgrade
    # Re-opening (now current-schema) must not raise or duplicate-add columns.
    store = Store(db)
    try:
        cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(matches)")}
        assert "cache_key" in cols
    finally:
        store.close()
