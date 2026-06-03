"""Service layer — orchestrates the pipeline (ARCHITECTURE.md §3).

The single place that wires fetch -> normalize -> store -> match. Interfaces
(``cli``, ``mcp_server``) are THIN shells over this module and contain NO
business logic. This module contains NO presentation/printing.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from bandiradar.matching.llm import LLMClient
from bandiradar.matching.prefilter import prefilter
from bandiradar.matching.relevance import score_all
from bandiradar.models import Match, Opportunity, Profile
from bandiradar.sources.base import Source, get, list_sources
from bandiradar.storage import SqliteScoreCache, Store


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
) -> list[tuple[Opportunity, Match]]:
    """Prefilter + score stored opportunities, ranked by score descending."""

    def _stored() -> list[Opportunity]:
        return store.list_opportunities(source=source_id)

    opportunities = _stored()
    if sample and not opportunities:
        sources_to_fetch = [source_id] if source_id else [s.id for s in list_sources()]
        for sid in sources_to_fetch:
            run_fetch(sid, store, sample=True, now=now)
        opportunities = _stored()

    kept = prefilter(opportunities, profile, now=now)
    cache = SqliteScoreCache(store)
    matches = score_all(kept, profile, client=client, cache=cache, now=now)

    by_id = {opp.id: opp for opp in kept}
    ranked = [
        (by_id[m.opportunity_id], m) for m in matches if m.score >= min_score
    ]
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
