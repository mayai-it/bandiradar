"""Service layer — orchestrates the pipeline (ARCHITECTURE.md §3).

The single place that wires fetch -> normalize -> store -> match. Interfaces
(``cli``, ``mcp_server``) are THIN shells over this module and contain NO
business logic. This module contains NO presentation/printing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from bandiradar.matching.llm import LLMClient
from bandiradar.matching.prefilter import prefilter
from bandiradar.matching.relevance import score_all
from bandiradar.models import Match, Opportunity, Profile
from bandiradar.sources.base import Source, get, list_sources
from bandiradar.storage import SqliteDocumentCache, SqliteScoreCache, Store


def load_profile(path: str | Path) -> Profile:
    """Load a company Profile from a YAML file."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Profile(**data)


def _fetch_raw(source: Source, sample: bool):
    """Sample mode reads the bundled fixture; live mode calls fetch()."""
    if sample:
        return source.load_fixture()  # type: ignore[attr-defined]
    return list(source.fetch())


def run_fetch(
    source_id: str,
    store: Store,
    sample: bool = False,
    now: datetime | None = None,
) -> dict[str, int]:
    """Ingest one source into the store; returns {fetched, new, amended}."""
    source = get(source_id)
    run_id = store.start_run(source_id)
    raws = _fetch_raw(source, sample)

    new = 0
    amended = 0
    for raw in raws:
        store.save_raw_doc(raw)
        for opp in source.to_opportunities(raw, now=now):
            result = store.upsert_opportunity(opp, now=now)
            if result == "new":
                new += 1
            elif result == "amended":
                amended += 1

    fetched = len(raws)
    store.finish_run(run_id, fetched=fetched, new=new, amended=amended)
    return {"fetched": fetched, "new": new, "amended": amended}


def run_match(
    profile: Profile,
    store: Store,
    source_id: str | None = None,
    sample: bool = False,
    client: LLMClient | None = None,
    min_score: int = 0,
    limit: int | None = None,
    now: datetime | None = None,
    with_benchmarks: bool = False,
    with_documents: bool = False,
) -> list[tuple[Opportunity, Match]]:
    """Prefilter + score stored opportunities, ranked by score descending.

    ``with_benchmarks`` adds optional ANAC-history enrichment (intelligence
    track), read from a BenchmarkStore on the same DB. ``with_documents`` fetches
    each prefiltered opportunity's attachment PDFs and folds their text into the
    matcher input (cached per URL). Both are graceful no-ops when there's nothing
    to add.
    """

    def _stored() -> list[Opportunity]:
        return store.list_opportunities(source=source_id)

    opportunities = _stored()
    if sample and not opportunities:
        sources_to_fetch = [source_id] if source_id else [s.id for s in list_sources()]
        for sid in sources_to_fetch:
            run_fetch(sid, store, sample=True, now=now)
        opportunities = _stored()

    kept = prefilter(opportunities, profile, now=now)

    if with_documents:
        from bandiradar.documents import enrich as enrich_documents

        doc_cache = SqliteDocumentCache(store)
        kept = [enrich_documents(opp, cache=doc_cache) for opp in kept]

    cache = SqliteScoreCache(store)

    benchmark_store = None
    if with_benchmarks:
        from bandiradar.intelligence.store import BenchmarkStore

        benchmark_store = BenchmarkStore(store.db_path)
    try:
        matches = score_all(
            kept,
            profile,
            client=client,
            cache=cache,
            now=now,
            benchmarks=benchmark_store,
        )
    finally:
        if benchmark_store is not None:
            benchmark_store.close()

    by_id = {opp.id: opp for opp in kept}
    ranked = [(by_id[m.opportunity_id], m) for m in matches if m.score >= min_score]
    if limit is not None:
        ranked = ranked[:limit]
    return ranked


def run_monitor(
    profile: Profile,
    source_id: str,
    store: Store,
    sample: bool = False,
    client: LLMClient | None = None,
    min_score: int = 0,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[tuple[Opportunity, Match]]:
    """Fetch then match — the "what should I look at" view."""
    run_fetch(source_id, store, sample=sample, now=now)
    return run_match(
        profile,
        store,
        source_id=source_id,
        sample=sample,
        client=client,
        min_score=min_score,
        limit=limit,
        now=now,
    )


def run_batch(
    profiles: list[Profile],
    store: Store,
    source_ids: list[str] | None = None,
    sample: bool = False,
    client: LLMClient | None = None,
    min_score: int = 0,
    top: int | None = None,
    with_benchmarks: bool = False,
    with_documents: bool = False,
    now: datetime | None = None,
) -> list[tuple[Profile, list[tuple[Opportunity, Match]]]]:
    """Run every profile against the sources; return (profile, ranked) per profile.

    Pure orchestration (no printing). Fetches each requested source once (sample
    mode) before matching, so the shared opportunity set is built a single time.
    """
    if sample:
        targets = source_ids if source_ids else [s.id for s in list_sources()]
        for sid in targets:
            if not store.list_opportunities(source=sid):
                run_fetch(sid, store, sample=True, now=now)

    results: list[tuple[Profile, list[tuple[Opportunity, Match]]]] = []
    for profile in profiles:
        ranked = run_match(
            profile,
            store,
            source_id=None,
            sample=False,  # sources already ensured above
            client=client,
            min_score=min_score,
            now=now,
            with_benchmarks=with_benchmarks,
            with_documents=with_documents,
        )
        if source_ids:
            wanted = set(source_ids)
            ranked = [(o, m) for o, m in ranked if o.source in wanted]
        if top is not None:
            ranked = ranked[:top]
        results.append((profile, ranked))
    return results


def run_watch(
    profile: Profile,
    store: Store,
    source_ids: list[str] | None = None,
    sample: bool = False,
    since: datetime | None = None,
    client: LLMClient | None = None,
    with_benchmarks: bool = False,
    with_documents: bool = False,
    now: datetime | None = None,
) -> list[tuple[Opportunity, Match]]:
    """Monitor loop: fetch + dedupe/change-detect, then return ONLY matches whose
    opportunity is NEW or AMENDED since the last watch run for this profile.

    Reuses storage change-detection (``upsert_opportunity`` + ``list_new``) and the
    existing matcher. A per-profile watch marker is persisted; ``since`` overrides
    it. Deterministic given a fixed ``now``.
    """
    moment = now if now is not None else datetime.now(UTC)
    marker = since if since is not None else store.get_watch_marker(profile.version)

    # Stamp this run's fetch/upserts AND the marker with the same `moment`, so the
    # next run's `since` (== this marker) excludes exactly what we saw this run.
    targets = source_ids if source_ids else [s.id for s in list_sources()]
    for sid in targets:
        run_fetch(sid, store, sample=sample, now=moment)

    # Opportunities the store saw change (insert/amend) after the marker.
    changed = store.list_new(marker)
    if source_ids:
        wanted = set(source_ids)
        changed = [o for o in changed if o.source in wanted]
    changed_ids = {o.id for o in changed}

    ranked = run_match(
        profile,
        store,
        source_id=None,
        sample=False,  # already fetched above
        client=client,
        now=moment,
        with_benchmarks=with_benchmarks,
        with_documents=with_documents,
    )
    delta = [(opp, match) for opp, match in ranked if opp.id in changed_ids]

    store.set_watch_marker(profile.version, moment)
    return delta
