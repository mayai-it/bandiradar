"""Thin-CLI tests (ARCHITECTURE.md §9 / Prompt 6). Offline, tmp db."""

import json

from typer.testing import CliRunner

from bandiradar.cli import app

runner = CliRunner()
MAYAI = "data/profiles/mayai.yaml"

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
    result = runner.invoke(app, ["fetch", "--source", "anac", "--sample", "--db", db])
    assert result.exit_code == 0
    assert "fetched=6" in result.stdout


def test_match_human(tmp_path):
    db = str(tmp_path / "m.db")
    result = runner.invoke(
        app, ["match", "--profile", MAYAI, "--sample", "--db", db]
    )
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
    assert any(d["opportunity_id"] == "anac:ocds-bandi-0001" for d in data)


def test_match_limit(tmp_path):
    db = str(tmp_path / "l.db")
    result = runner.invoke(
        app,
        ["match", "--profile", MAYAI, "--sample", "--json", "--limit", "1", "--db", db],
    )
    assert result.exit_code == 0
    assert len(json.loads(result.stdout)) == 1


def test_benchmarks_build_and_show(tmp_path):
    db = str(tmp_path / "b.db")
    built = runner.invoke(app, ["benchmarks", "build", "--sample", "--db", db])
    assert built.exit_code == 0
    assert "records=40" in built.stdout
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


def test_watch_is_stub():
    result = runner.invoke(app, ["watch"])
    assert result.exit_code == 0
    assert "Phase-1" in result.stdout or "not wired" in result.stdout
