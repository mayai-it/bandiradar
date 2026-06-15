"""FastMCP server — a THIN shell over ``core``/``storage`` (ARCHITECTURE.md §9).

Each tool is a thin wrapper that calls into core/storage and returns ONLY
canonical fields — never raw source payloads (privacy guardrail #2). Works
offline in sample mode with zero secrets. Lets you drive BandiRadar from Claude
itself (dogfood).
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from bandiradar import core
from bandiradar.matching.relevance import score
from bandiradar.models import Match, Opportunity, Profile
from bandiradar.sources.base import list_sources as _list_sources
from bandiradar.storage import SqliteScoreCache, Store

mcp = FastMCP("bandiradar")


def _resolve_profile(
    profile_path: str | None, profile: dict[str, Any] | None
) -> Profile:
    """Load a Profile from EITHER a path OR an inline dict (exactly one)."""
    if (profile_path is None) == (profile is None):
        raise ValueError("provide exactly one of 'profile_path' or 'profile'")
    if profile_path is not None:
        return core.load_profile(profile_path)
    assert profile is not None  # the XOR check above guarantees it
    return Profile(**profile)


def _opportunity_view(opp: Opportunity, match: Match) -> dict[str, Any]:
    """Canonical, payload-free view of a ranked opportunity."""
    return {
        "opportunity_id": opp.id,
        "score": match.score,
        "status": opp.status,
        "title": opp.title,
        "issuer": opp.issuer_name,
        "region": opp.region or opp.issuer_region,
        "deadline": opp.deadline.isoformat() if opp.deadline else None,
        "reasons": match.reasons,
        "matched_capabilities": match.matched_capabilities,
        "source_url": opp.source_url,
    }


@mcp.tool()
def list_sources() -> list[dict[str, str]]:
    """List the registered funding sources as [{id, kind}]."""
    return [{"id": s.id, "kind": s.kind} for s in _list_sources()]


@mcp.tool()
def fetch_opportunities(
    source: str = "anac", sample: bool = True, db: str | None = None
) -> dict[str, Any]:
    """Ingest a source into the store, saving progressively.

    Returns a structured SourceResult dict: source, status
    (ok/partial/failed/empty), fetched / mapped / new / amended / skipped_invalid,
    error, duration_s, started_at / finished_at. ``sample`` defaults to True so it
    is usable offline with zero secrets.
    """
    store = Store(db)
    try:
        return core.run_fetch(source, store, sample=sample).model_dump(mode="json")
    finally:
        store.close()


@mcp.tool()
def search_opportunities(
    profile_path: str | None = None,
    profile: dict[str, Any] | None = None,
    source: str | None = None,
    sample: bool = True,
    mode: str = core.DEFAULT_MODE,
    min_score: int | None = None,
    limit: int | None = None,
    with_benchmarks: bool = False,
    with_documents: bool = False,
    db: str | None = None,
) -> list[dict[str, Any]]:
    """Rank opportunities for a profile (offline in sample mode).

    Accepts EITHER ``profile_path`` OR an inline ``profile`` dict. Returns ranked
    canonical views — no raw payloads. ``mode`` is the operating point
    (precision|balanced|recall; precision needs an LLM key); an explicit ``min_score``
    overrides it. ``with_benchmarks`` adds ANAC historical benchmark notes;
    ``with_documents`` folds attachment-PDF text into matching.
    """
    company = _resolve_profile(profile_path, profile)
    cutoff = {"min_score": min_score} if min_score is not None else {"mode": mode}
    store = Store(db)
    try:
        ranked = core.run_match(
            company,
            store,
            source_id=source,
            sample=sample,
            limit=limit,
            with_benchmarks=with_benchmarks,
            with_documents=with_documents,
            **cutoff,
        )
        return [_opportunity_view(opp, m) for opp, m in ranked]
    finally:
        store.close()


@mcp.tool()
def score_opportunity(
    opportunity_id: str,
    profile_path: str | None = None,
    profile: dict[str, Any] | None = None,
    db: str | None = None,
) -> dict[str, Any]:
    """Score one stored opportunity for a profile; returns a single Match dict."""
    company = _resolve_profile(profile_path, profile)
    store = Store(db)
    try:
        opp = store.get_opportunity(opportunity_id)
        if opp is None:
            raise ValueError(f"opportunity not found: {opportunity_id!r}")
        match = score(opp, company, cache=SqliteScoreCache(store))
        return match.model_dump(mode="json")
    finally:
        store.close()


@mcp.tool()
def get_matches(
    profile_path: str | None = None,
    profile: dict[str, Any] | None = None,
    min_score: int = 0,
    limit: int | None = None,
    db: str | None = None,
) -> list[dict[str, Any]]:
    """Return PERSISTED matches for this profile_version (no recompute)."""
    company = _resolve_profile(profile_path, profile)
    store = Store(db)
    try:
        matches = store.list_matches(company.version, min_score=min_score, limit=limit)
        return [m.model_dump(mode="json") for m in matches]
    finally:
        store.close()


@mcp.tool()
def get_profile(profile_path: str) -> dict[str, Any]:
    """Return the parsed profile as a dict."""
    return core.load_profile(profile_path).model_dump(mode="json")


def main() -> None:
    """Entry point so the CLI `bandiradar mcp` command launches the server."""
    mcp.run()


# Alias: cli.py looks for either main or run.
run = main


if __name__ == "__main__":
    main()
