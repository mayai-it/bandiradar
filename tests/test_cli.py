"""Thin-CLI tests (ARCHITECTURE.md §9 / Prompt 6). Offline, tmp db."""

import json

from typer.testing import CliRunner

from bandiradar.cli import app

runner = CliRunner()
# A bundled example profile NAME (resolved from the package) — proves the
# installed-wheel path, not a checkout-relative file.
MAYAI = "mayai"

JSON_KEYS = {
    "opportunity_id",
    "score",
    "status",
    "title",
    "deadline",
    "reasons",
    "matched_capabilities",
    "source_url",
}


def test_sources_list():
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0
    assert "anac" in result.stdout


def test_profile_validate_ok():
    result = runner.invoke(app, ["profile", "validate", MAYAI])
    assert result.exit_code == 0
    assert "valid" in result.stdout.lower()


def test_profile_validate_bad(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: X\nbogus_field: 1\n", encoding="utf-8")
    result = runner.invoke(app, ["profile", "validate", str(bad)])
    assert result.exit_code == 1


def test_fetch_sample(tmp_path):
    db = str(tmp_path / "f.db")
    result = runner.invoke(
        app, ["fetch", "--source", "synthetic", "--sample", "--db", db]
    )
    assert result.exit_code == 0
    # Per-source summary table: source + ok status.
    assert "synthetic" in result.stdout
    assert "ok" in result.stdout


def test_fetch_json_emits_source_results(tmp_path):
    db = str(tmp_path / "fj.db")
    result = runner.invoke(
        app, ["fetch", "--source", "synthetic", "--sample", "--json", "--db", db]
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["source"] == "synthetic"
    assert data[0]["status"] == "ok"
    assert data[0]["new"] == 6


def test_match_human(tmp_path):
    db = str(tmp_path / "m.db")
    result = runner.invoke(app, ["match", "--profile", MAYAI, "--sample", "--db", db])
    assert result.exit_code == 0
    assert "MayAI" in result.stdout
    assert "score" in result.stdout
    assert "#1" in result.stdout


def test_match_json_shape(tmp_path):
    db = str(tmp_path / "j.db")
    result = runner.invoke(
        app, ["match", "--profile", MAYAI, "--sample", "--json", "--db", db]
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list) and data
    assert set(data[0].keys()) == JSON_KEYS
    assert any(d["opportunity_id"] == "synthetic:ocds-bandi-0001" for d in data)


def test_match_limit(tmp_path):
    db = str(tmp_path / "l.db")
    result = runner.invoke(
        app,
        ["match", "--profile", MAYAI, "--sample", "--json", "--limit", "1", "--db", db],
    )
    assert result.exit_code == 0
    assert len(json.loads(result.stdout)) == 1


def test_match_modes_run_and_default_is_balanced(tmp_path):
    # All three operating points run offline; default (no --mode) == balanced.
    for mode in ("precision", "balanced", "recall"):
        db = str(tmp_path / f"{mode}.db")
        r = runner.invoke(
            app, ["match", "--profile", MAYAI, "--sample", "--mode", mode, "--db", db]
        )
        assert r.exit_code == 0

    default = runner.invoke(
        app,
        [
            "match",
            "--profile",
            MAYAI,
            "--sample",
            "--json",
            "--db",
            str(tmp_path / "d.db"),
        ],
    )
    balanced = runner.invoke(
        app,
        [
            "match",
            "--profile",
            MAYAI,
            "--sample",
            "--json",
            "--mode",
            "balanced",
            "--db",
            str(tmp_path / "b.db"),
        ],
    )
    assert default.exit_code == 0 and balanced.exit_code == 0
    assert json.loads(default.stdout) == json.loads(balanced.stdout)


def test_match_invalid_mode_errors(tmp_path):
    db = str(tmp_path / "x.db")
    r = runner.invoke(
        app, ["match", "--profile", MAYAI, "--sample", "--mode", "nope", "--db", db]
    )
    assert r.exit_code == 1
    assert "unknown mode" in (r.stdout + str(r.stderr)).lower()


def test_benchmarks_build_and_show(tmp_path):
    db = str(tmp_path / "b.db")
    built = runner.invoke(app, ["benchmarks", "build", "--sample", "--db", db])
    assert built.exit_code == 0
    assert "records=48" in built.stdout
    assert "benchmarks=" in built.stdout

    shown = runner.invoke(app, ["benchmarks", "show", "--cpv", "45", "--db", db])
    assert shown.exit_code == 0
    assert "CPV division 45" in shown.stdout
    assert "national" in shown.stdout


def test_benchmarks_show_json(tmp_path):
    db = str(tmp_path / "bj.db")
    runner.invoke(app, ["benchmarks", "build", "--sample", "--db", db])
    res = runner.invoke(
        app, ["benchmarks", "show", "--cpv", "45", "--json", "--db", db]
    )
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["cpv_division"] == "45"
    assert data["count"] == 22


def test_benchmarks_show_missing_exits_nonzero(tmp_path):
    db = str(tmp_path / "bm.db")
    runner.invoke(app, ["benchmarks", "build", "--sample", "--db", db])
    res = runner.invoke(app, ["benchmarks", "show", "--cpv", "99", "--db", db])
    assert res.exit_code == 1


def test_watch_first_then_none(tmp_path):
    db = str(tmp_path / "w.db")
    args = [
        "watch",
        "--profile",
        MAYAI,
        "--source",
        "incentivi",
        "--sample",
        "--db",
        db,
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 0
    assert "new/amended" in first.stdout
    assert "#1" in first.stdout

    second = runner.invoke(app, args)
    assert second.exit_code == 0
    assert "No new or amended matches" in second.stdout


def test_watch_rss_writes_file(tmp_path):
    db = str(tmp_path / "wr.db")
    feed = str(tmp_path / "feed.xml")
    res = runner.invoke(
        app,
        [
            "watch",
            "--profile",
            MAYAI,
            "--source",
            "incentivi",
            "--sample",
            "--rss",
            feed,
            "--db",
            db,
        ],
    )
    assert res.exit_code == 0
    assert "wrote RSS feed" in res.stdout
    import xml.etree.ElementTree as ET

    root = ET.parse(feed).getroot()
    assert root.tag == "rss"
    assert root.find("./channel/item") is not None


def test_export_requires_output(tmp_path):
    db = str(tmp_path / "e.db")
    res = runner.invoke(app, ["export", "--profile", MAYAI, "--sample", "--db", db])
    assert res.exit_code == 1


def test_export_json(tmp_path):
    db = str(tmp_path / "ej.db")
    res = runner.invoke(
        app,
        [
            "export",
            "--profile",
            MAYAI,
            "--source",
            "incentivi",
            "--sample",
            "--json",
            "--db",
            db,
        ],
    )
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert isinstance(data, list)
    assert all(set(d.keys()) == JSON_KEYS for d in data)


# --------------------------------------------------------------------------- #
# trust (trust spine over LLM extractions)
# --------------------------------------------------------------------------- #


def _seed_trust_db(db: str) -> None:
    from datetime import UTC, datetime

    from bandiradar import core
    from bandiradar.models import Opportunity

    now = datetime(2026, 6, 1, tzinfo=UTC)
    store = core.Store(db)
    try:
        for i, verdict in (("q1", "quarantine"), ("ok1", "ok")):
            store.upsert_opportunity(
                Opportunity(
                    id=f"toscana:{i}",
                    source="toscana",
                    source_url=f"https://x/{i}",
                    kind="incentive",
                    title=f"Bando {i}",
                    geo_scope="regional",
                    status="open",
                    raw_ref=f"toscana:{i}",
                    provenance="llm",
                    confidence=0.1 if verdict == "quarantine" else 1.0,
                    trust_verdict=verdict,
                ),
                now=now,
            )
    finally:
        store.close()


def test_trust_list_defaults_to_quarantine(tmp_path):
    db = str(tmp_path / "trust.db")
    _seed_trust_db(db)
    res = runner.invoke(app, ["trust", "list", "--db", db])
    assert res.exit_code == 0
    assert "toscana:q1" in res.stdout
    assert "toscana:ok1" not in res.stdout


def test_trust_list_json_and_verdict_filter(tmp_path):
    db = str(tmp_path / "trust.db")
    _seed_trust_db(db)
    res = runner.invoke(app, ["trust", "list", "--verdict", "ok", "--json", "--db", db])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert [d["id"] for d in data] == ["toscana:ok1"]
    assert data[0]["provenance"] == "llm"
    assert data[0]["trust_verdict"] == "ok"


def test_trust_list_rejects_unknown_verdict(tmp_path):
    db = str(tmp_path / "trust.db")
    res = runner.invoke(app, ["trust", "list", "--verdict", "banned", "--db", db])
    assert res.exit_code != 0


def test_trust_list_empty_db(tmp_path):
    db = str(tmp_path / "empty.db")
    res = runner.invoke(app, ["trust", "list", "--db", db])
    assert res.exit_code == 0
    assert "No opportunities" in res.stdout


def test_trust_backfill_copies_cached_reports(tmp_path):
    from datetime import UTC, datetime

    from bandiradar import core
    from bandiradar.models import Opportunity
    from bandiradar.storage import SqliteExtractionCache

    db = str(tmp_path / "backfill.db")
    url = "https://x/bando/legacy"
    store = core.Store(db)
    try:
        store.upsert_opportunity(
            Opportunity(
                id="toscana:legacy",
                source="toscana",
                source_url=url,
                kind="incentive",
                title="Bando legacy",
                geo_scope="regional",
                status="open",
                raw_ref="toscana:legacy",
            ),
            now=datetime(2026, 6, 1, tzinfo=UTC),
        )
        cache = SqliteExtractionCache(store)
        cache.set(url, {"title": "Bando legacy"})
        cache.set_trust(
            url,
            {"checks": {}, "confidence": 0.2, "verdict": "quarantine"},
        )
    finally:
        store.close()

    res = runner.invoke(app, ["trust", "backfill", "--db", db])
    assert res.exit_code == 0
    assert "backfilled trust onto 1" in res.stdout

    # Idempotent + JSON shape.
    res = runner.invoke(app, ["trust", "backfill", "--json", "--db", db])
    assert res.exit_code == 0
    assert json.loads(res.stdout) == {"backfilled": 0}

    # The backfilled row now surfaces in the quarantined list.
    res = runner.invoke(app, ["trust", "list", "--db", db])
    assert "toscana:legacy" in res.stdout
