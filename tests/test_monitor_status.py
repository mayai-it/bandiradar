"""Offline tests for the live-monitor status generator (scripts/monitor_status.py).

Pure composition over a temp DB + JSON files — no network, no secrets. Builds a
minimal fixture (a couple of source runs, an adopted crawl recipe, feed JSONs, a
doctor report) and asserts the rendered STATUS.md + the all-failed verdict.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bandiradar.recipe_store import RecipeStore
from bandiradar.storage import Store

# scripts/ is not on sys.path (pythonpath = ["src"]); load the module by path.
# Register it in sys.modules BEFORE exec so dataclasses can resolve annotations.
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "monitor_status.py"
_spec = importlib.util.spec_from_file_location("monitor_status", _SCRIPT)
ms = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
sys.modules["monitor_status"] = ms
_spec.loader.exec_module(ms)

PAST = datetime(2000, 1, 1, tzinfo=UTC)


def _finished_run(store: Store, source: str, **kw) -> None:
    rid = store.start_run(source)
    store.finish_run(
        rid, kw.pop("fetched", 0), kw.pop("new", 0), kw.pop("amended", 0), **kw
    )


@pytest.fixture
def db(tmp_path):
    s = Store(str(tmp_path / "monitor.db"))
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# DB readers
# --------------------------------------------------------------------------- #


def test_latest_runs_reads_per_source_outcomes(db):
    _finished_run(db, "incentivi", fetched=12, new=3, amended=1, status="ok")
    _finished_run(
        db, "toscana", status="failed", error="boom", error_kind="unavailable"
    )
    runs = ms.latest_runs(db.conn)
    assert set(runs) == {"incentivi", "toscana"}
    assert runs["incentivi"].new == 3
    assert runs["incentivi"].status == "ok"
    assert runs["toscana"].failed
    assert runs["toscana"].error_kind == "unavailable"


def test_latest_runs_keeps_only_most_recent_per_source(db):
    _finished_run(db, "lazio", status="failed", error="first")
    _finished_run(db, "lazio", fetched=5, new=2, status="ok")  # newer wins
    runs = ms.latest_runs(db.conn)
    assert runs["lazio"].status == "ok"
    assert runs["lazio"].new == 2


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #


def test_all_failed_only_when_every_source_failed(db):
    _finished_run(db, "a", status="failed", error="x")
    _finished_run(db, "b", status="failed", error="y")
    assert ms.all_failed(ms.latest_runs(db.conn)) is True


def test_partial_failure_is_not_all_failed(db):
    _finished_run(db, "a", status="failed", error="x")
    _finished_run(db, "b", fetched=3, new=1, status="ok")
    assert ms.all_failed(ms.latest_runs(db.conn)) is False


def test_no_runs_is_not_all_failed(db):
    assert ms.all_failed(ms.latest_runs(db.conn)) is False


# --------------------------------------------------------------------------- #
# Recipe-state derivation (ok / drift / healed / flagged)
# --------------------------------------------------------------------------- #


def test_recipe_state_healed_when_adopted_this_run():
    s = ms.derive_recipe_state(
        crawl_health="ok", audit={"x": 1}, healed_this_run=True, llm_active=True
    )
    assert s == "healed"


def test_recipe_state_drift_keyless_but_flagged_with_key():
    drift = ms.derive_recipe_state(
        crawl_health="broken", audit=None, healed_this_run=False, llm_active=False
    )
    flagged = ms.derive_recipe_state(
        crawl_health="broken", audit=None, healed_this_run=False, llm_active=True
    )
    assert drift == "drift"
    assert flagged == "flagged"


def test_recipe_state_ok_and_unknown():
    assert (
        ms.derive_recipe_state(
            crawl_health="ok", audit=None, healed_this_run=False, llm_active=False
        )
        == "ok"
    )
    assert (
        ms.derive_recipe_state(
            crawl_health=None, audit=None, healed_this_run=False, llm_active=False
        )
        == "unknown"
    )
    # No live probe but a past golden snapshot exists -> degrade to ok, not unknown.
    assert (
        ms.derive_recipe_state(
            crawl_health=None,
            audit=None,
            healed_this_run=False,
            llm_active=False,
            has_golden=True,
        )
        == "ok"
    )


def test_recipe_states_marks_healed_from_db_audit(db):
    from bandiradar.sources.toscana import TOSCANA_RECIPE

    rs = RecipeStore(db)
    rs.set_golden("toscana", [("1", "https://x/1", "Bando 1")])
    rs.adopt(
        "toscana", TOSCANA_RECIPE, reason="drift-heal", validated_by="golden-exact"
    )

    states = ms.recipe_states(
        audits=ms.recipe_audits(db.conn),
        goldens=ms.golden_sources(db.conn),
        crawl_health={"toscana": "ok"},
        run_started=PAST,  # adopted_at (now) >= PAST -> healed this run
        llm_active=True,
    )
    by = {s.source: s for s in states}
    assert by["toscana"].state == "healed"
    assert "drift-heal" in by["toscana"].detail
    assert "golden-exact" in by["toscana"].detail


# --------------------------------------------------------------------------- #
# Match counting from the feed JSON
# --------------------------------------------------------------------------- #


def test_count_matches_array_empty_and_missing(tmp_path):
    two = tmp_path / "mayai.json"
    two.write_text(json.dumps([{"opportunity_id": "a"}, {"opportunity_id": "b"}]))
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    assert ms.count_matches(two) == 2
    assert ms.count_matches(empty) == 0
    assert ms.count_matches(tmp_path / "missing.json") is None


def test_crawl_health_from_doctor(tmp_path):
    doc = tmp_path / "doctor.json"
    doc.write_text(
        json.dumps(
            {
                "sources": [
                    {"source": "toscana", "crawl_health": "broken"},
                    {"source": "incentivi", "crawl_health": None},
                ]
            }
        )
    )
    health = ms.crawl_health_from_doctor(doc)
    assert health == {"toscana": "broken"}
    assert ms.crawl_health_from_doctor(tmp_path / "none.json") == {}


# --------------------------------------------------------------------------- #
# End-to-end: build_status renders a page and decides the verdict
# --------------------------------------------------------------------------- #


def test_build_status_end_to_end(db, tmp_path):
    _finished_run(db, "incentivi", fetched=20, new=4, amended=2, status="ok")
    _finished_run(
        db, "lazio", status="partial", error="rate cap", error_kind="rate_limited"
    )
    _finished_run(
        db, "toscana", status="failed", error="listing 500", error_kind="unavailable"
    )

    rs = RecipeStore(db)
    rs.set_golden("toscana", [("1", "https://x/1", "B1")])

    feeds = tmp_path / "feeds"
    feeds.mkdir()
    (feeds / "mayai.json").write_text(json.dumps([{"opportunity_id": "incentivi:1"}]))
    (feeds / "manifattura.json").write_text("[]")
    # Both profiles COMPLETED (stats sidecar present) -> real counts, no truncation.
    (feeds / "mayai.stats.json").write_text(json.dumps({"scored": 3, "deferred": 0}))
    (feeds / "manifattura.stats.json").write_text(
        json.dumps({"scored": 1, "deferred": 0})
    )

    doctor = tmp_path / "doctor.json"
    doctor.write_text(
        json.dumps({"sources": [{"source": "toscana", "crawl_health": "broken"}]})
    )

    md, failed = ms.build_status(
        db_path=db.db_path,
        feeds_dir=feeds,
        profiles=["mayai", "manifattura"],
        doctor_json=doctor,
        run_date="2026-06-11 06:00 UTC",
        run_started=datetime.now(UTC),
        llm_active=False,
    )

    assert failed is False  # not ALL sources failed
    assert "live monitor status" in md
    assert "`incentivi`" in md and "`toscana`" in md
    assert "keyless" in md  # llm_active=False -> keyless/recall banner
    assert "partial" in md  # the lazio warning surfaces
    assert "truncated" not in md.lower()  # both profiles completed
    # mayai had 1 new match, manifattura 0
    assert "| `mayai` | 1 |" in md
    assert "| `manifattura` | 0 |" in md
    # toscana crawl drifted, no key -> "drift" (not flagged)
    assert "drift" in md


def test_build_status_all_failed_verdict(db, tmp_path):
    _finished_run(db, "incentivi", status="failed", error="x", error_kind="unavailable")
    _finished_run(db, "toscana", status="failed", error="y", error_kind="unavailable")
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    (feeds / "mayai.stats.json").write_text(json.dumps({"scored": 0, "deferred": 0}))
    md, failed = ms.build_status(
        db_path=db.db_path,
        feeds_dir=feeds,
        profiles=["mayai"],
        doctor_json=None,
        run_date="2026-06-11",
        run_started=datetime.now(UTC),
        llm_active=False,
    )
    assert failed is True
    assert "ALL sources failed" in md
    assert "| `mayai` | n/a |" in md  # completed but no feed written -> n/a


def test_build_status_flags_truncated_run(db, tmp_path):
    # 3 expected profiles, only 1 completed (a stats sidecar) -> truncated banner +
    # the missing ones flagged "incomplete" (NOT shown with a stale figure).
    _finished_run(db, "incentivi", fetched=5, new=2, status="ok")
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    (feeds / "mayai.json").write_text(json.dumps([{"opportunity_id": "a"}]))
    (feeds / "mayai.stats.json").write_text(json.dumps({"scored": 7, "deferred": 4}))
    # manifattura was reached but killed mid-scoring: feed exists, NO sidecar.
    (feeds / "manifattura.json").write_text("[]")
    # consulenza never ran: nothing.

    md, failed = ms.build_status(
        db_path=db.db_path,
        feeds_dir=feeds,
        profiles=["mayai", "manifattura", "consulenza"],
        doctor_json=None,
        run_date="2026-06-11",
        run_started=datetime.now(UTC),
        llm_active=True,
    )
    assert "Run truncated" in md
    assert "1/3 profiles completed" in md
    assert "7 scored, 4 deferred" in md  # only the completed profile's stats counted
    assert "| `mayai` | 1 |" in md
    assert "| `manifattura` | ⚠️ incomplete |" in md
    assert "| `consulenza` | ⚠️ incomplete |" in md


def test_scoring_stats_reads_sidecars(tmp_path):
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    (feeds / "a.stats.json").write_text(json.dumps({"scored": 10, "deferred": 2}))
    (feeds / "b.stats.json").write_text("not json")  # corrupt -> None
    stats = ms.scoring_stats(feeds, ["a", "b", "c"])
    assert stats["a"] == {"scored": 10, "deferred": 2}
    assert stats["b"] is None  # corrupt
    assert stats["c"] is None  # missing -> not completed


def test_render_is_pure(db):
    _finished_run(db, "incentivi", fetched=1, new=1, status="ok")
    runs = ms.latest_runs(db.conn)
    kw = dict(
        run_date="2026-06-11",
        runs=runs,
        states=[],
        match_counts={"p": 1},
        stats={"p": {"scored": 0, "deferred": 0}},
        llm_active=True,
    )
    assert ms.render_status(**kw) == ms.render_status(**kw)
    assert "LLM scoring + healer ON" in ms.render_status(**kw)


# --------------------------------------------------------------------------- #
# Trust spine: per-source verdict counts + the STATUS section
# --------------------------------------------------------------------------- #


def _trust_opp(i: str, source: str, verdict: str):
    from bandiradar.models import Opportunity

    return Opportunity(
        id=f"{source}:{i}",
        source=source,
        source_url=f"https://x/{source}/{i}",
        kind="incentive",
        title=f"Bando {i}",
        geo_scope="regional",
        status="open",
        raw_ref=f"{source}:{i}",
        provenance="llm",
        trust_verdict=verdict,
    )


def test_trust_counts_reader_groups_by_source_and_verdict(db):
    db.upsert_opportunity(_trust_opp("1", "toscana", "ok"))
    db.upsert_opportunity(_trust_opp("2", "toscana", "quarantine"))
    db.upsert_opportunity(_trust_opp("3", "veneto", "suspect"))
    counts = ms.trust_counts(db.conn)
    assert counts == {
        "toscana": {"ok": 1, "quarantine": 1},
        "veneto": {"suspect": 1},
    }


def test_status_renders_extraction_trust_section(db, tmp_path):
    _finished_run(db, "toscana", fetched=2, new=2, status="ok")
    db.upsert_opportunity(_trust_opp("1", "toscana", "ok"))
    db.upsert_opportunity(_trust_opp("2", "toscana", "quarantine"))
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    md, _ = ms.build_status(
        db_path=db.db_path,
        feeds_dir=feeds,
        profiles=[],
        doctor_json=None,
        run_date="2026-06-12 06:00 UTC",
        run_started=datetime.now(UTC),
        llm_active=True,
    )
    assert "## Extraction trust (LLM sources)" in md
    assert "| `toscana` | 1 | 0 | 🚧 1 |" in md
    assert "bandiradar trust list" in md


def test_status_omits_trust_section_without_assessed_extractions(db, tmp_path):
    _finished_run(db, "ted", fetched=5, new=5, status="ok")
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    md, _ = ms.build_status(
        db_path=db.db_path,
        feeds_dir=feeds,
        profiles=[],
        doctor_json=None,
        run_date="2026-06-12 06:00 UTC",
        run_started=datetime.now(UTC),
        llm_active=False,
    )
    assert "Extraction trust" not in md
