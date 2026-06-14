"""Regional eval-corpus refresh (0.13.0) — offline guards on the outcome.

The 12 regional adapters (v0.6.0+) are folded into the eval corpus by
``scripts/build_regional_eval.py`` and 3 regional example profiles + their gold
labels were added. These tests pin the invariants without any network/LLM:
the corpus covers the regional sources, every regional item is OPEN at EVAL_NOW,
the 3 new profiles are bundled + loadable, and their gold pools are non-degenerate.
"""

from __future__ import annotations

from bandiradar import core, resources
from bandiradar import evaluation as ev

REGIONAL_SOURCES = {
    "sicilia",
    "emilia_romagna",
    "trentino",
    "veneto",
    "piemonte",
    "puglia",
    "sardegna",
    "fvg",
    "campania",
    "calabria",
    "basilicata",
    "liguria",
}
NEW_PROFILES = {"piemonte_industria", "sardegna_impresa", "sicilia_pmi"}


def test_corpus_covers_regional_sources():
    corpus = ev.load_corpus()
    present = {o.source for o in corpus}
    # The refresh added the regional adapters; most carry at least one open item.
    covered = REGIONAL_SOURCES & present
    assert len(covered) >= 8, f"too few regional sources in corpus: {sorted(covered)}"
    # The corpus grew well past the pre-refresh size.
    assert len(corpus) >= 400


def test_regional_corpus_items_are_open_at_eval_now():
    # build_regional_eval keeps only items OPEN at EVAL_NOW, so none are closed.
    for opp in ev.load_corpus():
        if opp.source in REGIONAL_SOURCES:
            assert opp.status != "closed", f"{opp.id} should be open at EVAL_NOW"


def test_regional_profiles_bundled_and_loadable():
    bundled = set(resources.profile_names())
    assert NEW_PROFILES <= bundled
    for name in NEW_PROFILES:
        p = core.load_profile(name)
        assert p.regions and p.seeks  # region-scoped, instrument-scoped


def test_regional_gold_profiles_are_non_degenerate():
    gold = ev.load_gold()["profiles"]
    corpus_ids = {o.id for o in ev.load_corpus()}
    assert NEW_PROFILES <= set(gold)
    for name in NEW_PROFILES:
        labels = gold[name]
        vals = set(labels.values())
        # A usable pool has BOTH positives (for precision/recall) and negatives
        # (for FPR) — otherwise the metrics are vacuous.
        assert {"relevant", "borderline"} & vals, f"{name}: no positive labels"
        assert "not" in vals, f"{name}: no negative labels"
        # Every labelled id must exist in the corpus (gold ⊆ corpus).
        assert set(labels) <= corpus_ids, f"{name}: gold ids missing from corpus"


def test_eval_runs_over_refreshed_corpus_offline():
    # conftest forces provider=none -> heuristic only, deterministic, no network.
    r = ev.run_eval()
    assert r.corpus_size >= 400
    assert NEW_PROFILES <= set(r.gold_profiles)
