"""Matching-evaluation tests (0.3.0). Offline: metrics on synthetic data + a
deterministic run over the shipped corpus (no live fetch, heuristic only)."""

import json

from typer.testing import CliRunner

from bandiradar import evaluation as ev
from bandiradar.cli import app

runner = CliRunner()


# --------------------------------------------------------------------------- #
# pure metrics
# --------------------------------------------------------------------------- #


def test_precision_at_k():
    ranked = ["a", "b", "c", "d", "e", "f"]
    labels = {
        "a": "relevant",
        "b": "not",
        "c": "relevant",
        "d": "borderline",
        "e": "relevant",
        "f": "not",
    }
    # top-5 = a,b,c,d,e -> relevant a,c,e = 3/5
    assert ev.precision_at_k(ranked, labels, 5) == 0.6
    assert ev.precision_at_k(ranked, labels, 3) == 2 / 3
    # borderline does NOT count as relevant for precision
    assert ev.precision_at_k(["d"], {"d": "borderline"}, 5) == 0.0
    # nothing returned
    assert ev.precision_at_k([], labels, 5) == 0.0
    # fewer than k returned -> denominator is what was returned
    assert ev.precision_at_k(["a", "c"], labels, 5) == 1.0


def test_recall_counts_borderline_as_relevant():
    labels = {"a": "relevant", "b": "borderline", "c": "not", "d": "relevant"}
    # relevant-for-recall = {a, b, d}; returned {a, b} -> 2/3
    assert ev.recall(["a", "b"], labels) == 2 / 3
    assert ev.recall(["a", "b", "d"], labels) == 1.0
    # nothing relevant to find -> 1.0 (vacuous)
    assert ev.recall([], {"x": "not"}) == 1.0


def test_false_positive_rate_uses_not_labels_only():
    labels = {"a": "relevant", "b": "not", "c": "not", "d": "borderline"}
    # negatives = {b, c}; returned includes b -> 1/2 (borderline d is not a negative)
    assert ev.false_positive_rate(["a", "b", "d"], labels) == 0.5
    assert ev.false_positive_rate(["a", "d"], labels) == 0.0
    # no negatives -> 0.0
    assert ev.false_positive_rate(["a"], {"a": "relevant"}) == 0.0


def test_borderline_split_precision_recall_fpr():
    labels = {"x": "borderline"}
    assert ev.precision_at_k(["x"], labels, 5) == 0.0  # non-relevant for precision
    assert ev.recall(["x"], labels) == 1.0  # relevant for recall
    assert ev.false_positive_rate(["x"], labels) == 0.0  # not a "not" negative


# --------------------------------------------------------------------------- #
# run_eval over the shipped corpus (offline, deterministic, heuristic only)
# --------------------------------------------------------------------------- #


def test_run_eval_is_offline_and_deterministic():
    # conftest forces provider=none -> only the heuristic method, no network.
    first = ev.run_eval()
    second = ev.run_eval()

    assert first.corpus_size >= 100  # the shipped real corpus
    assert first.gold_profiles  # at least one gold profile
    assert {m.method for m in first.methods} == {"heuristic"}
    assert first.model_dump() == second.model_dump()  # deterministic

    for method in first.methods:
        assert len(method.profiles) == len(first.gold_profiles)
        for p in method.profiles:
            m = p.metrics
            for value in (
                m.precision_at_5,
                m.precision_at_10,
                m.recall,
                m.false_positive_rate,
            ):
                assert 0.0 <= value <= 1.0


class _FakeLLM:
    """A stand-in LLM client: distinct cache_id, constant score so its ranking
    differs from the heuristic's."""

    cache_id = "fake:model"

    def score(self, system: str, user: str) -> dict:
        return {"score": 50, "reasons": ["fake"]}


def test_heuristic_baseline_uncontaminated_when_a_key_is_present(monkeypatch):
    # Offline (conftest forces provider=none): only the heuristic method.
    offline = ev.run_eval()
    assert {m.method for m in offline.methods} == {"heuristic"}
    offline_heuristic = next(m for m in offline.methods if m.method == "heuristic")

    # Now simulate a configured LLM key: get_client() returns a real client.
    monkeypatch.setattr(ev, "get_client", lambda: _FakeLLM())
    with_key = ev.run_eval()

    # Both methods are reported, and they are genuinely DISTINCT (the comparison
    # is real, not the LLM scored twice under two labels).
    assert {m.method for m in with_key.methods} == {"heuristic", "fake:model"}
    with_key_heuristic = next(m for m in with_key.methods if m.method == "heuristic")
    fake = next(m for m in with_key.methods if m.method == "fake:model")
    assert fake.model_dump() != with_key_heuristic.model_dump()

    # The heuristic baseline is IDENTICAL with or without a key present — i.e. it
    # really used the heuristic, never the configured client (the regression).
    assert with_key_heuristic.model_dump() == offline_heuristic.model_dump()


def test_cli_eval_human_and_json():
    human = runner.invoke(app, ["eval"])
    assert human.exit_code == 0
    assert "method: heuristic" in human.stdout
    assert "AGGREGATE" in human.stdout

    js = runner.invoke(app, ["eval", "--json"])
    assert js.exit_code == 0
    report = json.loads(js.stdout)
    assert report["corpus_size"] >= 100
    assert report["methods"][0]["method"] == "heuristic"
    assert report["methods"][0]["aggregate"]["precision_at_5"] >= 0.0
