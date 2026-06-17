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


def test_useful_at_k_counts_borderline():
    ranked = ["a", "b", "c", "d", "e", "f"]
    labels = {
        "a": "relevant",
        "b": "not",
        "c": "relevant",
        "d": "borderline",
        "e": "relevant",
        "f": "not",
    }
    # top-5 = a,b,c,d,e -> useful (relevant+borderline) = a,c,d,e = 4/5
    assert ev.useful_at_k(ranked, labels, 5) == 0.8
    # borderline DOES count as useful (vs precision, where it doesn't)
    assert ev.useful_at_k(["d"], {"d": "borderline"}, 5) == 1.0
    # useful >= precision always (same hits + borderline)
    assert ev.useful_at_k(ranked, labels, 5) >= ev.precision_at_k(ranked, labels, 5)
    assert ev.useful_at_k([], labels, 5) == 0.0


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


def test_attribute_recall_splits_stage1_vs_stage2():
    ranked = ["a", "b", "c"]  # returned set; positions a=0,b=1,c=2
    labels = {
        "a": "relevant",  # returned, top
        "b": "not",  # not wanted-for-recall
        "c": "borderline",  # returned, position 2
        "d": "relevant",  # NOT returned -> prefilter drop
        "e": "borderline",  # NOT returned -> prefilter drop
    }
    # wanted-for-recall = {a, c, d, e} = 4
    wanted, drop, below, top = ev.attribute_recall(ranked, labels, k=2)
    assert (wanted, drop, below, top) == (4, 2, 1, 1)  # a=top, c=below(k=2), d+e=drop
    assert drop + below + top == wanted
    # widen k: c now lands in the top -> no Stage-2 loss
    assert ev.attribute_recall(ranked, labels, k=10) == (4, 2, 0, 2)
    # nothing wanted -> all zero
    assert ev.attribute_recall(ranked, {"a": "not"}, k=5) == (0, 0, 0, 0)


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

    # Diagnostics are OFF by default — base report only.
    assert first.attribution_k is None
    assert first.thresholds == []
    assert first.full_text == []

    for method in first.methods:
        assert len(method.profiles) == len(first.gold_profiles)
        assert method.attribution is None  # no diagnostics by default
        assert method.sweep == []
        for p in method.profiles:
            m = p.metrics
            for value in (
                m.precision_at_5,
                m.precision_at_10,
                m.recall,
                m.false_positive_rate,
            ):
                assert 0.0 <= value <= 1.0


def test_run_eval_diagnostics_offline():
    r = ev.run_eval(diagnostics=True)
    assert r.attribution_k == ev.ATTRIBUTION_K
    assert r.thresholds == list(ev.SWEEP_THRESHOLDS)

    for method in r.methods:
        # Recall attribution present + the three fates sum to wanted (aggregate
        # and per profile), since gold ⊆ corpus.
        agg = method.attribution
        assert agg is not None
        assert agg.prefilter_drop + agg.below_k + agg.in_top_k == agg.wanted
        for p in method.profiles:
            pa = p.attribution
            assert pa is not None
            assert pa.prefilter_drop + pa.below_k + pa.in_top_k == pa.wanted

        # Sweep covers each threshold; returned is non-increasing as the cutoff
        # rises; threshold 0 reproduces the base aggregate.
        assert [pt.threshold for pt in method.sweep] == list(ev.SWEEP_THRESHOLDS)
        rets = [pt.aggregate.returned for pt in method.sweep]
        assert rets == sorted(rets, reverse=True)
        assert method.sweep[0].aggregate.returned == method.aggregate.returned


def test_full_text_experiment_offline():
    r = ev.run_eval(full_text=True)
    assert r.full_text  # the experiment ran
    heuristic = next(d for d in r.full_text if d.method == "heuristic")
    # The heuristic already reads the full requirements text, so feeding "full
    # text" changes nothing — brief == full by design (the delta isolates the LLM).
    assert heuristic.brief.model_dump() == heuristic.full.model_dump()


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


def test_run_eval_embeddings_unavailable_is_graceful():
    # conftest disables embeddings -> get_embedder() is None.
    r = ev.run_eval(embeddings=True)
    assert r.embeddings is not None
    assert r.embeddings.available is False
    assert len(r.embeddings.runs) == 1  # baseline only, no sweep
    assert r.embeddings.runs[0].threshold is None


def test_run_eval_embeddings_experiment_with_fake_embedder(monkeypatch):
    from test_embeddings import FakeEmbedder

    monkeypatch.setattr(ev, "get_embedder", lambda: FakeEmbedder())
    r = ev.run_eval(embeddings=True)
    assert r.embeddings is not None and r.embeddings.available is True
    runs = r.embeddings.runs
    assert runs[0].label.startswith("baseline")
    assert len(runs) == 1 + len(ev.EMBEDDING_SWEEP)

    base = runs[0]
    for run in runs[1:]:
        # The semantic signal is an OR rescue: it can only KEEP more items, so
        # recall never falls and Stage-1 prefilter drops never rise vs baseline.
        assert run.aggregate.recall >= base.aggregate.recall - 1e-9
        assert run.aggregate.returned >= base.aggregate.returned
        assert run.attribution.prefilter_drop <= base.attribution.prefilter_drop


def test_cli_eval_embeddings_section():
    out = runner.invoke(app, ["eval", "--embeddings"])
    assert out.exit_code == 0
    assert "embeddings semantic prefilter" in out.stdout
    # extra disabled in the suite -> graceful "unavailable" line, never a crash.
    assert "backend unavailable" in out.stdout


def test_diagnostics_include_gate_attribution():
    r = ev.run_eval(diagnostics=True)
    # Every prefilter-dropped relevant/borderline item is attributed to a gate.
    assert r.gate_drops  # the corrected gold still has a few real drops
    gates = {d.gate for d in r.gate_drops}
    assert gates <= {
        "deadline",
        "seeks",
        "region",
        "value",
        "exclusions",
        "relevance-signal",
        "other",
    }
    # The count of gate-drops equals the aggregate prefilter_drop of the heuristic.
    heuristic = next(m for m in r.methods if m.method == "heuristic")
    assert len(r.gate_drops) == heuristic.attribution.prefilter_drop
    # Off by default.
    assert ev.run_eval().gate_drops == []


def test_listwise_rerank_method_with_fake_client(monkeypatch):
    from test_rerank import FakeRankClient

    monkeypatch.setattr(ev, "get_client", lambda: FakeRankClient())
    r = ev.run_eval(diagnostics=True, rerank=True)
    methods = {m.method for m in r.methods}
    assert "fake:rank" in methods  # pointwise
    assert "fake:rank (listwise)" in methods  # listwise
    listwise = next(m for m in r.methods if m.method == "fake:rank (listwise)")
    # same prefiltered SET as pointwise -> identical recall/FPR, only ranking differs
    pointwise = next(m for m in r.methods if m.method == "fake:rank")
    assert listwise.aggregate.returned == pointwise.aggregate.returned
    assert abs(listwise.aggregate.recall - pointwise.aggregate.recall) < 1e-9
    for value in (listwise.aggregate.precision_at_5, listwise.aggregate.recall):
        assert 0.0 <= value <= 1.0


def test_rerank_off_when_no_llm():
    # conftest -> get_client None -> no listwise method even with rerank=True.
    r = ev.run_eval(rerank=True)
    assert all("listwise" not in m.method for m in r.methods)


def test_cli_eval_diagnostics_and_full_text():
    human = runner.invoke(app, ["eval", "--diagnostics", "--full-text"])
    assert human.exit_code == 0
    assert "recall attribution" in human.stdout
    assert "gate attribution" in human.stdout
    assert "min_score sweep" in human.stdout
    assert "full-text experiment" in human.stdout

    js = runner.invoke(app, ["eval", "-d", "--json"])
    assert js.exit_code == 0
    report = json.loads(js.stdout)
    assert report["attribution_k"] == ev.ATTRIBUTION_K
    assert report["thresholds"] == list(ev.SWEEP_THRESHOLDS)
    method = report["methods"][0]
    assert method["attribution"] is not None
    assert len(method["sweep"]) == len(ev.SWEEP_THRESHOLDS)
