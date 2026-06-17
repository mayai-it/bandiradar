"""Matching-quality evaluation against a labelled corpus (0.3.0).

Runs the two-stage matcher over a shipped, reproducible corpus of REAL
opportunities (``data/eval/opportunities.jsonl``) for a set of gold profiles with
human-reviewable labels (``data/eval/gold.yaml``), and reports precision@5,
precision@10, recall and false-positive rate — per profile and aggregate.

Label convention (documented + tested):
- ``relevant``   — counts as relevant for BOTH precision and recall.
- ``borderline`` — counts as relevant for RECALL, but NON-relevant for PRECISION
  (a borderline item surfaced near the top still costs precision).
- ``not``        — clearly irrelevant; the negatives used for the false-positive
  rate (FPR = clearly-irrelevant items that leaked into the ranked results).

Labelling is POOLED (heuristic top-k + a cross-source sample) to stay tractable,
so recall is "recall within the labelled pool" — read it as relative, for
comparing matchers on the same gold set, not as absolute corpus recall.

Always reports the HEURISTIC matcher; if an LLM provider+key is configured it also
reports the LLM matcher on the SAME gold set. ``--embeddings`` adds the offline
hybrid-prefilter measurement (semantic signal WITH vs WITHOUT, swept).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast

import yaml
from pydantic import BaseModel

from bandiradar import core, resources
from bandiradar.matching.embeddings import EMBEDDING_SIM_THRESHOLD, get_embedder
from bandiradar.matching.llm import get_client
from bandiradar.matching.prefilter import prefilter_explain
from bandiradar.matching.relevance import HEURISTIC
from bandiradar.models import Opportunity
from bandiradar.storage import Store

logger = logging.getLogger(__name__)

# Fixed reference time so deadline -> status (and thus prefiltering) is stable and
# the metrics are reproducible regardless of when `eval` runs. Matches the corpus
# capture date in gold.yaml's _meta.
EVAL_NOW = datetime(2026, 6, 8, tzinfo=UTC)

Label = str  # "relevant" | "borderline" | "not"


# --------------------------------------------------------------------------- #
# Pure metrics (over a ranked id list + a {id: label} gold pool)
# --------------------------------------------------------------------------- #


def precision_at_k(ranked_ids: list[str], labels: dict[str, Label], k: int) -> float:
    """Fraction of the top-k returned that are ``relevant`` (borderline does NOT
    count). Denominator is ``min(k, len(returned))``; 0.0 when nothing returned."""
    top = ranked_ids[:k]
    if not top:
        return 0.0
    hits = sum(1 for i in top if labels.get(i) == "relevant")
    return hits / len(top)


def useful_at_k(ranked_ids: list[str], labels: dict[str, Label], k: int) -> float:
    """Fraction of the top-k that are USEFUL to a user — ``relevant`` OR
    ``borderline`` (adjacent but worth a look). The lived-experience companion to the
    strict :func:`precision_at_k`: an adjacent grant is not a precision hit but is
    still a useful result, so useful@k reads higher than P@k where borderline items
    rank well. 0.0 when nothing returned."""
    top = ranked_ids[:k]
    if not top:
        return 0.0
    hits = sum(1 for i in top if labels.get(i) in ("relevant", "borderline"))
    return hits / len(top)


def recall(ranked_ids: list[str], labels: dict[str, Label]) -> float:
    """Relevant-for-recall (relevant + borderline) found anywhere in the ranking,
    over all such labelled in the pool. 1.0 when there are none to find."""
    wanted = {i for i, lab in labels.items() if lab in ("relevant", "borderline")}
    if not wanted:
        return 1.0
    returned = set(ranked_ids)
    return len(wanted & returned) / len(wanted)


def false_positive_rate(ranked_ids: list[str], labels: dict[str, Label]) -> float:
    """Clearly-irrelevant (``not``) items that leaked into the ranking, over all
    ``not`` labelled in the pool. 0.0 when there are no negatives."""
    negatives = {i for i, lab in labels.items() if lab == "not"}
    if not negatives:
        return 0.0
    returned = set(ranked_ids)
    return len(negatives & returned) / len(negatives)


def attribute_recall(
    ranked_ids: list[str], labels: dict[str, Label], k: int
) -> tuple[int, int, int, int]:
    """Split the relevant-for-recall items (relevant + borderline) by FATE, to say
    WHERE recall is lost — Stage 1 vs Stage 2.

    Returns ``(wanted, prefilter_drop, below_k, in_top_k)`` where:
    - ``prefilter_drop`` — wanted but NOT in the returned set: Stage-1 prefilter
      never surfaced it (→ a recall/candidate-generation problem; embeddings).
    - ``below_k`` — wanted and returned, but ranked at position ``>= k``: Stage-2
      ranked it out of the visible window (→ a ranking problem; reranking).
    - ``in_top_k`` — wanted and surfaced within the top ``k``.
    The three sum to ``wanted`` (gold ⊆ corpus, so "not returned" == prefiltered out).
    """
    wanted = {i for i, lab in labels.items() if lab in ("relevant", "borderline")}
    pos = {i: idx for idx, i in enumerate(ranked_ids)}
    prefilter_drop = sum(1 for w in wanted if w not in pos)
    in_top_k = sum(1 for w in wanted if w in pos and pos[w] < k)
    below_k = sum(1 for w in wanted if w in pos and pos[w] >= k)
    return len(wanted), prefilter_drop, below_k, in_top_k


# --------------------------------------------------------------------------- #
# Report models
# --------------------------------------------------------------------------- #


class EvalMetrics(BaseModel):
    precision_at_5: float
    precision_at_10: float
    useful_at_5: float  # top-5 that are relevant OR borderline (user-useful)
    useful_at_10: float
    recall: float
    false_positive_rate: float
    returned: int  # ranked results for this profile
    pool: int  # labelled items for this profile
    relevant: int  # 'relevant' labels in the pool


class RecallAttribution(BaseModel):
    """Why relevant-for-recall items were missed (Stage 1 vs Stage 2). Counts."""

    k: int
    wanted: int
    prefilter_drop: int  # dropped by Stage-1 prefilter (never returned)
    below_k: int  # returned by Stage 1 but ranked below k by Stage 2
    in_top_k: int  # surfaced within the top-k


class ThresholdPoint(BaseModel):
    """Aggregate metrics when only score >= threshold is kept (min_score sweep)."""

    threshold: int
    aggregate: EvalMetrics


class EvalProfile(BaseModel):
    profile: str
    metrics: EvalMetrics
    attribution: RecallAttribution | None = None


class EvalMethodReport(BaseModel):
    method: str  # "heuristic" or "<provider>:<model>"
    profiles: list[EvalProfile]
    aggregate: EvalMetrics
    attribution: RecallAttribution | None = None  # summed across profiles
    sweep: list[ThresholdPoint] = []


class FullTextDelta(BaseModel):
    """Full-text experiment: aggregate metrics with the capped brief vs the full
    requirements text, per method (the heuristic already reads full text)."""

    method: str
    brief: EvalMetrics
    full: EvalMetrics


class EmbeddingsRun(BaseModel):
    """One configuration of the semantic-prefilter experiment (heuristic scorer)."""

    label: str
    threshold: float | None  # None = baseline (no semantic signal)
    aggregate: EvalMetrics
    attribution: RecallAttribution | None


class EmbeddingsReport(BaseModel):
    """WITH/WITHOUT semantic-prefilter measurement (offline, heuristic scorer)."""

    available: bool  # False when the embeddings extra/model isn't installed
    model_id: str | None
    runs: list[EmbeddingsRun]  # [baseline, semantic@0.3, @0.4, @0.5]


class GateDrop(BaseModel):
    """A relevant-for-recall gold item that Stage 1 dropped, and WHICH gate did it —
    so an over-strict gate (tunable recall) is told apart from a real ceiling."""

    profile: str
    opportunity_id: str
    label: str
    gate: str  # deadline | seeks | region | value | exclusions | relevance-signal
    reason: str


class EvalReport(BaseModel):
    corpus_size: int
    gold_profiles: list[str]
    eval_now: str
    note: str
    methods: list[EvalMethodReport]
    attribution_k: int | None = None  # set when recall attribution was computed
    thresholds: list[int] = []  # set when the min_score sweep was computed
    gate_drops: list[GateDrop] = []  # set with diagnostics: per-gate prefilter loss
    full_text: list[FullTextDelta] = []  # set when the full-text experiment ran
    embeddings: EmbeddingsReport | None = None  # set when --embeddings ran


# --------------------------------------------------------------------------- #
# Loading + running
# --------------------------------------------------------------------------- #


def load_corpus() -> list[Opportunity]:
    """Read the shipped evaluation corpus (offline; no live fetch)."""
    text = resources.eval_corpus().read_text(encoding="utf-8")
    return [
        Opportunity.model_validate_json(line)
        for line in text.splitlines()
        if line.strip()
    ]


def load_gold() -> dict:
    """Read the gold labels: ``{_meta, profiles: {name: {id: label}}}``."""
    return yaml.safe_load(resources.eval_gold().read_text(encoding="utf-8"))


def _metrics(ranked_ids: list[str], labels: dict[str, Label]) -> EvalMetrics:
    return EvalMetrics(
        precision_at_5=precision_at_k(ranked_ids, labels, 5),
        precision_at_10=precision_at_k(ranked_ids, labels, 10),
        useful_at_5=useful_at_k(ranked_ids, labels, 5),
        useful_at_10=useful_at_k(ranked_ids, labels, 10),
        recall=recall(ranked_ids, labels),
        false_positive_rate=false_positive_rate(ranked_ids, labels),
        returned=len(ranked_ids),
        pool=len(labels),
        relevant=sum(1 for lab in labels.values() if lab == "relevant"),
    )


def _aggregate(metrics: list[EvalMetrics]) -> EvalMetrics:
    """Macro-average per-profile metrics (each profile weighted equally)."""
    n = len(metrics) or 1
    return EvalMetrics(
        precision_at_5=sum(x.precision_at_5 for x in metrics) / n,
        precision_at_10=sum(x.precision_at_10 for x in metrics) / n,
        useful_at_5=sum(x.useful_at_5 for x in metrics) / n,
        useful_at_10=sum(x.useful_at_10 for x in metrics) / n,
        recall=sum(x.recall for x in metrics) / n,
        false_positive_rate=sum(x.false_positive_rate for x in metrics) / n,
        returned=sum(x.returned for x in metrics),
        pool=sum(x.pool for x in metrics),
        relevant=sum(x.relevant for x in metrics),
    )


def _aggregate_attribution(
    attrs: list[RecallAttribution | None],
) -> RecallAttribution | None:
    """Corpus-wide totals of the per-profile recall attribution (summed counts)."""
    present = [a for a in attrs if a is not None]
    if not present:
        return None
    return RecallAttribution(
        k=present[0].k,
        wanted=sum(a.wanted for a in present),
        prefilter_drop=sum(a.prefilter_drop for a in present),
        below_k=sum(a.below_k for a in present),
        in_top_k=sum(a.in_top_k for a in present),
    )


# Diagnostic defaults.
SWEEP_THRESHOLDS = (0, 20, 40, 60)  # min_score cutoffs for the precision/recall curve
ATTRIBUTION_K = 10  # cutoff for the Stage-1-vs-Stage-2 recall split (matches P@10)

# (name, gold labels, ranked [(id, score), ...]) for one profile under one method.
_Scored = tuple[str, dict[str, Label], list[tuple[str, int]]]


def _score_profiles(
    store: Store,
    client_obj: object,
    gold_profiles: dict[str, dict[str, Label]],
    *,
    with_benchmarks: bool,
    with_documents: bool,
    full_text: bool,
    embedder: object | None = None,
    sim_threshold: float = EMBEDDING_SIM_THRESHOLD,
) -> list[_Scored]:
    """Run the matcher (min_score=0 -> the full prefiltered set) for every gold
    profile and capture the ranked ``(id, score)`` list — the raw material the
    metrics, recall attribution and threshold sweep are all derived from.

    ``embedder`` (opt-in) turns on the hybrid semantic prefilter at ``sim_threshold``.
    """
    scored: list[_Scored] = []
    for name in sorted(gold_profiles):
        profile = core.load_profile(name)
        # client_obj/embedder are `object` to carry the HEURISTIC sentinel + a real
        # client interchangeably; run_match special-cases them at runtime.
        ranked = core.run_match(
            profile,
            store,
            client=cast(Any, client_obj),
            now=EVAL_NOW,
            with_benchmarks=with_benchmarks,
            with_documents=with_documents,
            full_text=full_text,
            embedder=cast(Any, embedder),
            sim_threshold=sim_threshold,
        )
        scored.append(
            (name, gold_profiles[name], [(opp.id, m.score) for opp, m in ranked])
        )
    return scored


def _rerank_profiles(
    store: Store,
    client: object,
    gold_profiles: dict[str, dict[str, Label]],
) -> list[_Scored]:
    """Like :func:`_score_profiles` but Stage 2 is LISTWISE rerank (1 call/profile)."""
    scored: list[_Scored] = []
    for name in sorted(gold_profiles):
        profile = core.load_profile(name)
        ranked = core.run_rerank(profile, store, cast(Any, client), now=EVAL_NOW)
        scored.append(
            (name, gold_profiles[name], [(opp.id, m.score) for opp, m in ranked])
        )
    return scored


def _build_method_report(
    method: str,
    scored: list[_Scored],
    *,
    attribution_k: int | None,
    thresholds: list[int],
) -> EvalMethodReport:
    """Assemble a method's report: base metrics + optional recall attribution and
    min_score sweep (both pure, derived from the captured rankings — no rescoring)."""
    per_profile: list[EvalProfile] = []
    for name, labels, ranked_scored in scored:
        ranked_ids = [i for i, _ in ranked_scored]
        attribution = None
        if attribution_k is not None:
            wanted, drop, below, top = attribute_recall(
                ranked_ids, labels, attribution_k
            )
            attribution = RecallAttribution(
                k=attribution_k,
                wanted=wanted,
                prefilter_drop=drop,
                below_k=below,
                in_top_k=top,
            )
        per_profile.append(
            EvalProfile(
                profile=name,
                metrics=_metrics(ranked_ids, labels),
                attribution=attribution,
            )
        )

    sweep: list[ThresholdPoint] = []
    for t in thresholds:
        per_t = [
            _metrics([i for i, s in ranked_scored if s >= t], labels)
            for _, labels, ranked_scored in scored
        ]
        sweep.append(ThresholdPoint(threshold=t, aggregate=_aggregate(per_t)))

    return EvalMethodReport(
        method=method,
        profiles=per_profile,
        aggregate=_aggregate([p.metrics for p in per_profile]),
        attribution=_aggregate_attribution([p.attribution for p in per_profile]),
        sweep=sweep,
    )


def _gate_of(reason: str) -> str:
    """Map a prefilter drop reason to the gate that produced it."""
    if reason.startswith("closed"):
        return "deadline"
    if "does not seek" in reason:
        return "seeks"
    if "region" in reason:
        return "region"
    if "value range" in reason:
        return "value"
    if "excluded term" in reason:
        return "exclusions"
    if "no CPV" in reason:
        return "relevance-signal"
    return "other"


def _gate_drops(
    corpus: list[Opportunity], gold_profiles: dict[str, dict[str, Label]]
) -> list[GateDrop]:
    """For every relevant-for-recall gold item the prefilter dropped, record WHICH
    gate killed it (method-independent — Stage 1 is shared). Lets an over-strict
    hard gate (recoverable recall) be told apart from a genuine ceiling."""
    drops: list[GateDrop] = []
    for name in sorted(gold_profiles):
        profile = core.load_profile(name)
        explained = {
            opp.id: (kept, reason)
            for opp, kept, reason in prefilter_explain(corpus, profile, now=EVAL_NOW)
        }
        for oid, label in gold_profiles[name].items():
            if label not in ("relevant", "borderline"):
                continue
            kept, reason = explained.get(oid, (False, "not in corpus"))
            if not kept:
                drops.append(
                    GateDrop(
                        profile=name,
                        opportunity_id=oid,
                        label=label,
                        gate=_gate_of(reason),
                        reason=reason,
                    )
                )
    return drops


# Cosine cutoffs swept by the embeddings experiment (recall-vs-FPR curve).
EMBEDDING_SWEEP = (0.3, 0.4, 0.5)


def _run_embeddings_experiment(
    store: Store, gold_profiles: dict[str, dict[str, Label]]
) -> EmbeddingsReport:
    """Measure the hybrid semantic prefilter WITH vs WITHOUT, on the HEURISTIC
    matcher (offline, no LLM). Vectors cache in ``store`` so they're embedded once
    and reused across the threshold sweep."""

    def _run(label: str, threshold: float | None, embedder: object | None):
        sim = threshold if threshold is not None else EMBEDDING_SIM_THRESHOLD
        scored = _score_profiles(
            store,
            HEURISTIC,
            gold_profiles,
            with_benchmarks=False,
            with_documents=False,
            full_text=False,
            embedder=embedder,
            sim_threshold=sim,
        )
        rep = _build_method_report(
            label, scored, attribution_k=ATTRIBUTION_K, thresholds=[]
        )
        return EmbeddingsRun(
            label=label,
            threshold=threshold,
            aggregate=rep.aggregate,
            attribution=rep.attribution,
        )

    runs = [_run("baseline (CPV/keyword)", None, None)]
    embedder = get_embedder()
    if embedder is not None:
        for t in EMBEDDING_SWEEP:
            runs.append(_run(f"semantic >= {t}", t, embedder))
            agg = runs[-1].aggregate
            logger.info(
                "eval embeddings thr=%.2f recall=%.2f fpr=%.2f drop=%s",
                t,
                agg.recall,
                agg.false_positive_rate,
                runs[-1].attribution.prefilter_drop if runs[-1].attribution else "-",
            )
    return EmbeddingsReport(
        available=embedder is not None,
        model_id=getattr(embedder, "model_id", None),
        runs=runs,
    )


def run_eval(
    db: str | None = None,
    with_benchmarks: bool = False,
    with_documents: bool = False,
    diagnostics: bool = False,
    full_text: bool = False,
    embeddings: bool = False,
    rerank: bool = False,
) -> EvalReport:
    """Evaluate the matcher(s) over the shipped corpus for the gold profiles.

    Loads the corpus into a throwaway in-memory store (or ``db``) — NO live fetch.
    Always evaluates the heuristic; if an LLM is configured, also the LLM matcher
    on the same gold set.

    ``diagnostics`` adds (free, no extra scoring) the recall attribution
    (Stage-1-prefilter vs Stage-2-ranking loss) and the min_score threshold sweep.
    ``full_text`` runs the controlled experiment: re-score each method feeding the
    UNCAPPED requirements text and report the aggregate delta vs the capped brief.
    ``embeddings`` runs the OFFLINE (heuristic-only) hybrid-prefilter measurement:
    recall/FPR/attribution WITH vs WITHOUT the semantic signal, swept over cutoffs.
    ``rerank`` adds a LISTWISE-LLM method (one comparative call per profile) when an
    LLM is configured, to compare against the pointwise LLM.
    """
    corpus = load_corpus()
    gold = load_gold()
    gold_profiles: dict[str, dict[str, Label]] = gold.get("profiles", {})
    note = (gold.get("_meta") or {}).get("note", "")

    attribution_k = ATTRIBUTION_K if diagnostics else None
    thresholds = list(SWEEP_THRESHOLDS) if diagnostics else []

    store = Store(db if db is not None else ":memory:")
    try:
        for opp in corpus:
            store.upsert_opportunity(opp, now=EVAL_NOW)

        # Pin the heuristic with the HEURISTIC sentinel — NOT client=None, which
        # would silently fall back to the configured LLM when a key is present and
        # make the "heuristic" baseline secretly the LLM (and then the LLM run a
        # cache hit). With the sentinel the two methods are genuinely distinct.
        methods: list[tuple[str, object]] = [("heuristic", HEURISTIC)]
        client = get_client()
        if client is not None:
            methods.append((getattr(client, "cache_id", "llm"), client))

        method_reports: list[EvalMethodReport] = []
        for method, client_obj in methods:
            scored = _score_profiles(
                store,
                client_obj,
                gold_profiles,
                with_benchmarks=with_benchmarks,
                with_documents=with_documents,
                full_text=False,
            )
            report = _build_method_report(
                method, scored, attribution_k=attribution_k, thresholds=thresholds
            )
            method_reports.append(report)
            agg = report.aggregate
            logger.info(
                "eval method=%s P@5=%.2f P@10=%.2f recall=%.2f fpr=%.2f",
                method,
                agg.precision_at_5,
                agg.precision_at_10,
                agg.recall,
                agg.false_positive_rate,
            )

        # Listwise LLM rerank — one comparative call per profile, same prefiltered
        # set as the pointwise LLM (so recall/FPR match; only the ranking differs).
        if rerank and client is not None:
            scored = _rerank_profiles(store, client, gold_profiles)
            label = f"{getattr(client, 'cache_id', 'llm')} (listwise)"
            report = _build_method_report(
                label, scored, attribution_k=attribution_k, thresholds=thresholds
            )
            method_reports.append(report)
            agg = report.aggregate
            logger.info(
                "eval method=%s P@5=%.2f P@10=%.2f recall=%.2f fpr=%.2f",
                label,
                agg.precision_at_5,
                agg.precision_at_10,
                agg.recall,
                agg.false_positive_rate,
            )

        full_text_deltas: list[FullTextDelta] = []
        if full_text:
            brief = {r.method: r.aggregate for r in method_reports}
            for method, client_obj in methods:
                scored_full = _score_profiles(
                    store,
                    client_obj,
                    gold_profiles,
                    with_benchmarks=with_benchmarks,
                    with_documents=with_documents,
                    full_text=True,
                )
                full_report = _build_method_report(
                    method, scored_full, attribution_k=None, thresholds=[]
                )
                full_text_deltas.append(
                    FullTextDelta(
                        method=method,
                        brief=brief[method],
                        full=full_report.aggregate,
                    )
                )
                logger.info(
                    "eval full-text method=%s P@5 %.2f->%.2f P@10 %.2f->%.2f",
                    method,
                    brief[method].precision_at_5,
                    full_report.aggregate.precision_at_5,
                    brief[method].precision_at_10,
                    full_report.aggregate.precision_at_10,
                )
        embeddings_report = (
            _run_embeddings_experiment(store, gold_profiles) if embeddings else None
        )
    finally:
        store.close()

    gate_drops = _gate_drops(corpus, gold_profiles) if diagnostics else []

    return EvalReport(
        corpus_size=len(corpus),
        gold_profiles=sorted(gold_profiles),
        eval_now=EVAL_NOW.date().isoformat(),
        note=note,
        methods=method_reports,
        attribution_k=attribution_k,
        thresholds=thresholds,
        gate_drops=gate_drops,
        full_text=full_text_deltas,
        embeddings=embeddings_report,
    )
