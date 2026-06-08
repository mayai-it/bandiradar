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
reports the LLM matcher on the SAME gold set. Embeddings join in slice 2.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import yaml
from pydantic import BaseModel

from bandiradar import core, resources
from bandiradar.matching.llm import get_client
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


# --------------------------------------------------------------------------- #
# Report models
# --------------------------------------------------------------------------- #


class EvalMetrics(BaseModel):
    precision_at_5: float
    precision_at_10: float
    recall: float
    false_positive_rate: float
    returned: int  # ranked results for this profile
    pool: int  # labelled items for this profile
    relevant: int  # 'relevant' labels in the pool


class EvalProfile(BaseModel):
    profile: str
    metrics: EvalMetrics


class EvalMethodReport(BaseModel):
    method: str  # "heuristic" or "<provider>:<model>"
    profiles: list[EvalProfile]
    aggregate: EvalMetrics


class EvalReport(BaseModel):
    corpus_size: int
    gold_profiles: list[str]
    eval_now: str
    note: str
    methods: list[EvalMethodReport]


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
        recall=recall(ranked_ids, labels),
        false_positive_rate=false_positive_rate(ranked_ids, labels),
        returned=len(ranked_ids),
        pool=len(labels),
        relevant=sum(1 for lab in labels.values() if lab == "relevant"),
    )


def _aggregate(profiles: list[EvalProfile]) -> EvalMetrics:
    """Macro-average of the per-profile metrics (each profile weighted equally)."""
    n = len(profiles) or 1
    m = [p.metrics for p in profiles]
    return EvalMetrics(
        precision_at_5=sum(x.precision_at_5 for x in m) / n,
        precision_at_10=sum(x.precision_at_10 for x in m) / n,
        recall=sum(x.recall for x in m) / n,
        false_positive_rate=sum(x.false_positive_rate for x in m) / n,
        returned=sum(x.returned for x in m),
        pool=sum(x.pool for x in m),
        relevant=sum(x.relevant for x in m),
    )


def run_eval(
    db: str | None = None,
    with_benchmarks: bool = False,
    with_documents: bool = False,
) -> EvalReport:
    """Evaluate the matcher(s) over the shipped corpus for the gold profiles.

    Loads the corpus into a throwaway in-memory store (or ``db``) — NO live fetch.
    Always evaluates the heuristic; if an LLM is configured, also the LLM matcher
    on the same gold set.
    """
    corpus = load_corpus()
    gold = load_gold()
    gold_profiles: dict[str, dict[str, Label]] = gold.get("profiles", {})
    note = (gold.get("_meta") or {}).get("note", "")

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
            per_profile: list[EvalProfile] = []
            for name in sorted(gold_profiles):
                profile = core.load_profile(name)
                ranked = core.run_match(
                    profile,
                    store,
                    client=client_obj,
                    now=EVAL_NOW,
                    with_benchmarks=with_benchmarks,
                    with_documents=with_documents,
                )
                ranked_ids = [opp.id for opp, _ in ranked]
                per_profile.append(
                    EvalProfile(
                        profile=name,
                        metrics=_metrics(ranked_ids, gold_profiles[name]),
                    )
                )
            report = EvalMethodReport(
                method=method,
                profiles=per_profile,
                aggregate=_aggregate(per_profile),
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
    finally:
        store.close()

    return EvalReport(
        corpus_size=len(corpus),
        gold_profiles=sorted(gold_profiles),
        eval_now=EVAL_NOW.date().isoformat(),
        note=note,
        methods=method_reports,
    )
