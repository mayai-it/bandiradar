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


def test_watch_is_stub():
    result = runner.invoke(app, ["watch"])
    assert result.exit_code == 0
    assert "Phase-1" in result.stdout or "not wired" in result.stdout
