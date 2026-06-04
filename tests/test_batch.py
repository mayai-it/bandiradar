"""Profile-suite batch + robustness fuzz tests (offline, --sample, fixed seed)."""

import csv
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bandiradar import core
from bandiradar.cli import app
from bandiradar.matching.prefilter import prefilter
from bandiradar.matching.relevance import score_all
from bandiradar.models import Profile, ValueRange
from bandiradar.sources.base import list_sources
from bandiradar.storage import Store

NOW = datetime(2026, 6, 4, 0, 0, tzinfo=UTC)
PROFILES_DIR = Path(__file__).resolve().parents[1] / "data" / "profiles"
runner = CliRunner()


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "batch.db"))
    yield s
    s.close()


def all_profiles() -> list[Profile]:
    return [core.load_profile(p) for p in sorted(PROFILES_DIR.glob("*.yaml"))]


# --------------------------------------------------------------------------- #
# run_batch structure
# --------------------------------------------------------------------------- #


def test_run_batch_one_entry_per_profile_with_valid_scores(store):
    profiles = all_profiles()
    assert len(profiles) >= 7  # the curated suite
    results = core.run_batch(profiles, store, sample=True, now=NOW)

    assert len(results) == len(profiles)
    assert [p.name for p, _ in results] == [p.name for p in profiles]  # order kept
    for _profile, ranked in results:
        for opp, match in ranked:
            assert 0 <= match.score <= 100
            assert opp.id == match.opportunity_id
        scores = [m.score for _, m in ranked]
        assert scores == sorted(scores, reverse=True)  # ranked desc


def test_run_batch_top_limits_per_profile(store):
    profiles = all_profiles()
    results = core.run_batch(profiles, store, sample=True, now=NOW, top=1)
    assert all(len(ranked) <= 1 for _, ranked in results)


# --------------------------------------------------------------------------- #
# CLI batch
# --------------------------------------------------------------------------- #


def test_cli_batch_table_lists_every_profile(tmp_path):
    db = str(tmp_path / "b.db")
    res = runner.invoke(app, ["batch", "--sample", "--db", db])
    assert res.exit_code == 0
    for token in [
        "MayAI",
        "Manifattura",
        "MedForniture",
        "Costruzioni",
        "Trattoria",
        "Consulenza",
        "Studio",
    ]:
        assert token in res.stdout


def test_cli_batch_json(tmp_path):
    db = str(tmp_path / "bj.db")
    res = runner.invoke(app, ["batch", "--sample", "--json", "--db", db])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert len(data) == len(all_profiles())
    for row in data:
        assert {"profile", "matches", "by_source", "top", "results"} <= set(row)


def test_cli_batch_csv(tmp_path):
    db = str(tmp_path / "bc.db")
    out = str(tmp_path / "out.csv")
    res = runner.invoke(app, ["batch", "--sample", "--csv", out, "--db", db])
    assert res.exit_code == 0
    with open(out, encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header = rows[0]
    assert header[:4] == ["profile", "matches", "top_score", "top_title"]
    assert len(rows) - 1 == len(all_profiles())


# --------------------------------------------------------------------------- #
# Robustness fuzz: ~200 random valid profiles, never raise, scores in [0,100]
# --------------------------------------------------------------------------- #

_ATECO = ["62.01", "41.20", "69.20", "56.10", "70.22", "25.62", "47.11"]
_CPV = ["72000000", "45000000", "79000000", "33000000", "48000000", "42000000"]
_REGIONS = ["Lazio", "Lombardia", "Campania", "Emilia-Romagna", "Puglia"]
_KEYWORDS = [
    "digitalizzazione",
    "software",
    "lavori",
    "consulenza",
    "macchinari",
    "intelligenza artificiale",
    "turismo",
    "manutenzione",
    "dispositivi",
]
_RANGES = [
    (None, None),
    (1000, 100000),
    (50000, 5000000),
    (None, 250000),
    (10000, None),
]
_CAPS = [
    "",
    "consulenza e software per le PMI",
    "lavori edili e manutenzione",
    "dispositivi medici e forniture sanitarie",
]


def _rand_profile(rng: random.Random, i: int) -> Profile:
    lo, hi = rng.choice(_RANGES)
    return Profile(
        name=f"fuzz-{i}",
        ateco=rng.sample(_ATECO, k=rng.randint(0, 3)),
        cpv_interests=rng.sample(_CPV, k=rng.randint(0, 2)),
        keywords=rng.sample(_KEYWORDS, k=rng.randint(0, 3)),
        regions=rng.sample(_REGIONS, k=rng.randint(0, 2)),
        value_range=ValueRange(min=lo, max=hi),
        capabilities=rng.choice(_CAPS),
        exclusions=rng.sample(["construction", "catering"], k=rng.randint(0, 2)),
    )


def test_fuzz_200_profiles_never_raise_and_scores_bounded(store):
    for source in list_sources():
        core.run_fetch(source.id, store, sample=True, now=NOW)
    opportunities = store.list_opportunities()
    assert opportunities  # sample data loaded across every registered source

    rng = random.Random(20260604)  # FIXED seed — deterministic
    for i in range(200):
        profile = _rand_profile(rng, i)
        kept = prefilter(opportunities, profile, now=NOW)
        matches = score_all(kept, profile, now=NOW)
        for m in matches:
            assert 0 <= m.score <= 100
        # ranked desc invariant holds for arbitrary profiles too
        scores = [m.score for m in matches]
        assert scores == sorted(scores, reverse=True)
