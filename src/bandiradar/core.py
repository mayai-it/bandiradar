"""Service layer — orchestrates the pipeline (ARCHITECTURE.md §3).

The single place that wires fetch -> normalize -> store -> match. Interfaces
(``cli``, ``mcp_server``) are THIN shells over this module and contain NO
business logic. This module contains NO presentation/printing.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from bandiradar import resources
from bandiradar.http import FetchError
from bandiradar.matching.llm import LLMClient
from bandiradar.matching.prefilter import prefilter
from bandiradar.matching.relevance import score_all
from bandiradar.models import Match, Opportunity, Profile, SourceResult
from bandiradar.sources.base import ProgressFn, Source, get, list_sources
from bandiradar.storage import SqliteDocumentCache, SqliteScoreCache, Store

logger = logging.getLogger(__name__)


def load_profile(path_or_name: str | Path) -> Profile:
    """Load a company Profile from a filesystem path OR a bundled example name.

    Resolution order: an existing filesystem path wins; otherwise the string is
    treated as a bundled example profile name (``mayai``, ``medtech_lombardia``,
    ``pmi_toscana``, …, with or without ``.yaml``) so demos run from a
    pip-installed wheel too. An unresolvable name raises a clear error listing the
    available examples.
    """
    fs_path = Path(path_or_name)
    if fs_path.exists():
        text = fs_path.read_text(encoding="utf-8")
    else:
        bundled = resources.resolve_profile(str(path_or_name))
        if bundled is None:
            available = ", ".join(resources.profile_names())
            raise FileNotFoundError(
                f"Profile not found: {path_or_name!r}. Pass a YAML path or a "
                f"bundled example name (one of: {available})."
            )
        text = bundled.read_text(encoding="utf-8")
    return Profile(**yaml.safe_load(text))


# Safety cap so a live fetch never runs unbounded when no explicit --limit is set.
DEFAULT_FETCH_LIMIT = 2000


def _raw_stream(
    source: Source,
    sample: bool,
    since: datetime | None,
    limit: int | None,
    max_pages: int | None,
    progress: ProgressFn | None,
):
    """Yield RawDocs LAZILY: the bundled fixture in sample mode, else live fetch().

    A generator, so ``source.fetch()`` is invoked at first iteration — letting the
    caller save progressively and catch a mid-stream failure without losing what
    already arrived.
    """
    if sample:
        yield from source.load_fixture()  # type: ignore[attr-defined]
        return
    yield from source.fetch(since, limit=limit, max_pages=max_pages, progress=progress)


def _clean_error(exc: BaseException) -> str:
    """A short, secret-free error string (message + type, no traceback)."""
    return str(exc) or type(exc).__name__


def _error_kind(exc: BaseException) -> str:
    """Structured cause: a FetchError's own kind, else "unknown"."""
    return exc.kind if isinstance(exc, FetchError) else "unknown"


def _classify(error: str | None, fetched: int) -> str:
    """Derive the FetchStatus from whether the fetch errored and how much arrived."""
    if error is not None:
        return "partial" if fetched > 0 else "failed"
    return "empty" if fetched == 0 else "ok"


def run_fetch(
    source_id: str,
    store: Store,
    sample: bool = False,
    now: datetime | None = None,
    *,
    since: datetime | None = None,
    limit: int | None = None,
    max_pages: int | None = None,
    progress: ProgressFn | None = None,
) -> SourceResult:
    """Ingest one source into the store, saving PROGRESSIVELY, and return a
    :class:`SourceResult` describing exactly what happened.

    Resilience (all preserved):
    - **never propagates:** any failure is caught into ``status="failed"`` (nothing
      saved) or ``status="partial"`` (saves kept) with a clean error string — so a
      sibling source is never aborted by this one.
    - **dirty data:** a record that fails to map/validate is QUARANTINED (skipped +
      counted), never fatal.
    - **bounded:** live fetches stop at ``limit`` (default
      :data:`DEFAULT_FETCH_LIMIT`).

    The same result is persisted as one ``runs`` row and logged.
    """
    started = datetime.now(UTC)
    t0 = time.monotonic()
    run_id = store.start_run(source_id, started_at=started)
    if limit is not None:
        effective_limit = limit
    else:
        effective_limit = None if sample else DEFAULT_FETCH_LIMIT

    fetched = mapped = new = amended = skipped_invalid = 0
    error: str | None = None
    error_kind: str | None = None

    try:
        source = get(source_id)
        iterator = iter(
            _raw_stream(source, sample, since, effective_limit, max_pages, progress)
        )
        while True:
            try:
                raw = next(iterator)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001 — fetch raised mid-stream
                error = _clean_error(exc)
                error_kind = _error_kind(exc)
                logger.error(
                    "source=%s fetch stopped after %d records: %s (kind=%s)",
                    source_id,
                    fetched,
                    error,
                    error_kind,
                )
                break
            fetched += 1
            store.save_raw_doc(raw)
            try:
                opportunities = source.to_opportunities(raw, now=now)
            except Exception as exc:  # noqa: BLE001 — quarantine a dirty record
                skipped_invalid += 1
                logger.warning(
                    "source=%s skipped invalid record %s: %s",
                    source_id,
                    getattr(raw, "id", "?"),
                    _clean_error(exc),
                )
                continue
            for opp in opportunities:
                mapped += 1
                result = store.upsert_opportunity(opp, now=now)
                if result == "new":
                    new += 1
                elif result == "amended":
                    amended += 1
    except Exception as exc:  # noqa: BLE001 — setup error (e.g. unknown source)
        error = _clean_error(exc)
        error_kind = _error_kind(exc)
        logger.error("source=%s fetch setup failed: %s", source_id, error)

    finished = datetime.now(UTC)
    duration_s = time.monotonic() - t0
    status = _classify(error, fetched)
    store.finish_run(
        run_id,
        fetched=fetched,
        new=new,
        amended=amended,
        mapped=mapped,
        skipped_invalid=skipped_invalid,
        duration_s=duration_s,
        status=status,
        error=error,
        error_kind=error_kind,
        finished_at=finished,
    )
    log = logger.error if status == "failed" else logger.info
    log(
        "source=%s status=%s fetched=%d mapped=%d new=%d amended=%d "
        "skipped_invalid=%d duration=%.2fs",
        source_id,
        status,
        fetched,
        mapped,
        new,
        amended,
        skipped_invalid,
        duration_s,
    )
    return SourceResult(
        source=source_id,
        status=status,
        fetched=fetched,
        mapped=mapped,
        skipped_invalid=skipped_invalid,
        new=new,
        amended=amended,
        error=error,
        error_kind=error_kind,
        duration_s=duration_s,
        started_at=started,
        finished_at=finished,
    )


def run_fetch_many(
    source_ids: list[str],
    store: Store,
    *,
    sample: bool = False,
    now: datetime | None = None,
    since: datetime | None = None,
    limit: int | None = None,
    max_pages: int | None = None,
    progress: ProgressFn | None = None,
) -> list[SourceResult]:
    """Fetch several sources with PER-SOURCE ISOLATION.

    Each source runs independently — one failing (TED) never aborts the others
    (Lazio still runs). ``run_fetch`` already absorbs its own errors into a
    ``SourceResult``; the extra guard here is defense-in-depth so even an
    unexpected error can't break the loop.
    """
    results: list[SourceResult] = []
    for sid in source_ids:
        try:
            results.append(
                run_fetch(
                    sid,
                    store,
                    sample=sample,
                    now=now,
                    since=since,
                    limit=limit,
                    max_pages=max_pages,
                    progress=progress,
                )
            )
        except Exception as exc:  # noqa: BLE001 — never let one source abort siblings
            error = _clean_error(exc)
            logger.error("source=%s unexpected error, isolated: %s", sid, error)
            results.append(
                SourceResult(
                    source=sid,
                    status="failed",
                    error=error,
                    error_kind=_error_kind(exc),
                )
            )
    return results


_EXIT_OK = 0
_EXIT_FAILED = 1  # generic / unknown
_EXIT_INVALID_DATA = 2
_EXIT_RATE_LIMITED = 3
_EXIT_UNAVAILABLE = 4

# Structured error kind -> exit code. "unknown" (and any unmapped kind) -> generic.
_EXIT_BY_KIND: dict[str, int] = {
    "rate_limited": _EXIT_RATE_LIMITED,
    "unavailable": _EXIT_UNAVAILABLE,
    "invalid": _EXIT_INVALID_DATA,
}


def fetch_exit_code(results: list[SourceResult]) -> int:
    """Worst-status exit code: 0 if all ok/empty, else a code by ``error_kind``.

    Maps the representative failure's STRUCTURED kind (no string-matching):
    rate_limited -> 3, unavailable -> 4, invalid -> 2; unknown/anything else -> 1.
    """
    bad = [r for r in results if r.status in ("failed", "partial")]
    if not bad:
        return _EXIT_OK
    rep = next((r for r in bad if r.status == "failed"), bad[0])
    return _EXIT_BY_KIND.get(rep.error_kind or "unknown", _EXIT_FAILED)


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
        # Pass `now` so each opportunity's lifecycle status is recomputed for the
        # same reference time the matcher uses (never a stale stored status).
        return store.list_opportunities(source=source_id, now=now)

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
    fetch_limit: int | None = None,
    max_pages: int | None = None,
    progress: ProgressFn | None = None,
) -> tuple[list[SourceResult], list[tuple[Profile, list[tuple[Opportunity, Match]]]]]:
    """Run every profile against the sources, with per-source-isolated fetching.

    Returns ``(fetch_results, per_profile_results)``. Fetches each requested source
    once (so the shared opportunity set is built a single time); one failing source
    never aborts the others or the matching.
    """
    fetch_results: list[SourceResult] = []
    if sample:
        targets = source_ids if source_ids else [s.id for s in list_sources()]
        to_fetch = [s for s in targets if not store.list_opportunities(source=s)]
        fetch_results = run_fetch_many(to_fetch, store, sample=True, now=now)
    elif source_ids:  # live batch over explicit sources
        fetch_results = run_fetch_many(
            source_ids,
            store,
            sample=False,
            now=now,
            limit=fetch_limit,
            max_pages=max_pages,
            progress=progress,
        )

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
    return fetch_results, results


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
    fetch_limit: int | None = None,
    max_pages: int | None = None,
    progress: ProgressFn | None = None,
) -> tuple[list[SourceResult], list[tuple[Opportunity, Match]]]:
    """Monitor loop: fetch (per-source isolated) + dedupe/change-detect, then return
    ONLY matches whose opportunity is NEW or AMENDED since the last watch run.

    Returns ``(fetch_results, delta)``. One failing source never aborts the monitor:
    the others still fetch and matching proceeds over whatever was saved. A
    per-profile watch marker is persisted; ``since`` overrides it.
    """
    moment = now if now is not None else datetime.now(UTC)
    marker = since if since is not None else store.get_watch_marker(profile.version)

    # Stamp this run's fetch/upserts AND the marker with the same `moment`, so the
    # next run's `since` (== this marker) excludes exactly what we saw this run.
    targets = source_ids if source_ids else [s.id for s in list_sources()]
    fetch_results = run_fetch_many(
        targets,
        store,
        sample=sample,
        now=moment,
        limit=fetch_limit,
        max_pages=max_pages,
        progress=progress,
    )

    # Opportunities the store saw change (insert/amend) after the marker.
    changed = store.list_new(marker, now=moment)
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
    return fetch_results, delta
