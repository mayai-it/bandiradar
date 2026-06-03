"""Stage 2 — relevance scorer with offline fallback + cache (ARCHITECTURE.md §6).

``score`` is cache-first, then uses the configured LLM client, and finally falls
back to a DETERMINISTIC offline heuristic so the engine runs with ZERO secrets.
The heuristic reuses the prefilter's CPV helper (single source of truth) and
grades continuously from the same signals the prefilter gates on.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from bandiradar.matching.llm import LLMClient, get_client
from bandiradar.matching.prefilter import cpv_match, cpv_match_depth
from bandiradar.matching.prompts import SCORING_SYSTEM, build_user_prompt
from bandiradar.models import Match, Opportunity, Profile

CacheKey = tuple[str, str]  # (profile.version, opportunity.content_hash)

_TOKEN_RE = re.compile(r"[a-zA-Z]{4,}")


class RelevanceResult(BaseModel):
    """The Stage-2 structured output (the five relevance fields)."""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    matched_capabilities: list[str] = Field(default_factory=list)
    eligibility_flags: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


@runtime_checkable
class ScoreCache(Protocol):
    """Relevance cache keyed by (profile.version, opportunity.content_hash)."""

    def get(self, key: CacheKey) -> Match | None: ...

    def set(self, key: CacheKey, match: Match) -> None: ...


class InMemoryScoreCache:
    """Default process-local cache. Prompt 5 adds a SQLite-backed implementation."""

    def __init__(self) -> None:
        self._store: dict[CacheKey, Match] = {}

    def get(self, key: CacheKey) -> Match | None:
        return self._store.get(key)

    def set(self, key: CacheKey, match: Match) -> None:
        self._store[key] = match


# --------------------------------------------------------------------------- #
# Tolerant LLM-JSON coercion
# --------------------------------------------------------------------------- #


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]


def _coerce_result(data: object) -> RelevanceResult:
    """Coerce arbitrary LLM JSON into a valid RelevanceResult (clamp + defaults)."""
    if not isinstance(data, dict):
        data = {}
    try:
        score_int = int(round(float(data.get("score", 0))))
    except (TypeError, ValueError):
        score_int = 0
    score_int = max(0, min(100, score_int))
    return RelevanceResult(
        score=score_int,
        reasons=_as_str_list(data.get("reasons")),
        matched_capabilities=_as_str_list(data.get("matched_capabilities")),
        eligibility_flags=_as_str_list(data.get("eligibility_flags")),
        risk_notes=_as_str_list(data.get("risk_notes")),
    )


# --------------------------------------------------------------------------- #
# Deterministic offline heuristic
# --------------------------------------------------------------------------- #


def _tokens(text: str | None) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _value_fit(opp: Opportunity, profile: Profile) -> float:
    """1.0 overlap, 0.0 no overlap, 0.5 when not comparable (missing data)."""
    vr = profile.value_range
    has_bound = vr.min is not None or vr.max is not None
    has_value = any(
        v is not None for v in (opp.value_amount, opp.value_min, opp.value_max)
    )
    if not (has_bound and has_value):
        return 0.5
    if opp.value_min is not None or opp.value_max is not None:
        o_lo = opp.value_min if opp.value_min is not None else -math.inf
        o_hi = opp.value_max if opp.value_max is not None else math.inf
    else:
        o_lo = o_hi = opp.value_amount
    p_lo = vr.min if vr.min is not None else -math.inf
    p_hi = vr.max if vr.max is not None else math.inf
    return 1.0 if (o_lo <= p_hi and p_lo <= o_hi) else 0.0


def _geo_fit(opp: Opportunity, profile: Profile) -> float:
    if opp.geo_scope in ("national", "eu"):
        return 0.7
    if not profile.regions:
        return 0.7
    wanted = {r.strip().lower() for r in profile.regions}
    return 1.0 if (opp.region or "").strip().lower() in wanted else 0.2


def _geo_reason(opp: Opportunity, profile: Profile, fit: float) -> str:
    if opp.geo_scope in ("national", "eu"):
        return f"{opp.geo_scope} scope"
    if fit >= 1.0:
        return f"region match: {opp.region}"
    if not profile.regions:
        return "no region restriction"
    return f"region mismatch: {opp.region or '—'}"


def heuristic_fallback(opportunity: Opportunity, profile: Profile) -> RelevanceResult:
    """Deterministic, network-free relevance grade in [0, 100]."""
    depth = cpv_match_depth(opportunity.cpv, profile.cpv_interests)
    cpv_component = min(depth / 5.0, 1.0)

    profile_terms = _tokens(profile.capabilities) | {
        token for kw in profile.keywords for token in _tokens(kw)
    }
    opp_text = " ".join(
        part
        for part in (
            opportunity.title,
            opportunity.summary,
            opportunity.eligibility_text,
        )
        if part
    )
    overlap = sorted(profile_terms & _tokens(opp_text))
    overlap_component = min(len(overlap) / 3.0, 1.0)

    value_component = _value_fit(opportunity, profile)
    geo_component = _geo_fit(opportunity, profile)

    raw = 100.0 * (
        0.40 * cpv_component
        + 0.30 * overlap_component
        + 0.15 * value_component
        + 0.15 * geo_component
    )
    score_int = max(0, min(100, int(round(raw))))

    matched_cpv = sorted(
        {i for i in profile.cpv_interests if cpv_match(opportunity.cpv, [i])}
    )
    matched_capabilities = overlap + [f"CPV {code}" for code in matched_cpv]

    reasons: list[str] = []
    if depth > 0:
        reasons.append(f"CPV prefix match (depth {depth})")
    if overlap:
        reasons.append("capability overlap: " + ", ".join(overlap[:5]))
    if value_component >= 1.0:
        reasons.append("within profile value range")
    elif value_component == 0.0:
        reasons.append("outside profile value range")
    reasons.append(_geo_reason(opportunity, profile, geo_component))

    risk_notes: list[str] = []
    if opportunity.status == "closing_soon":
        risk_notes.append("deadline closing soon")
    elif opportunity.status == "closed":
        risk_notes.append("deadline already passed")

    return RelevanceResult(
        score=score_int,
        reasons=reasons,
        matched_capabilities=matched_capabilities,
        eligibility_flags=[],
        risk_notes=risk_notes,
    )


# --------------------------------------------------------------------------- #
# Public scoring API
# --------------------------------------------------------------------------- #


def cache_key(opportunity: Opportunity, profile: Profile) -> CacheKey:
    return (profile.version, opportunity.content_hash)


def score(
    opportunity: Opportunity,
    profile: Profile,
    client: LLMClient | None = None,
    cache: ScoreCache | None = None,
    now: datetime | None = None,
) -> Match:
    """Score one opportunity: cache-first, then LLM, then offline heuristic."""
    key = cache_key(opportunity, profile)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached

    active = client if client is not None else get_client()
    if active is None:
        result = heuristic_fallback(opportunity, profile)
    else:
        raw = active.score(SCORING_SYSTEM, build_user_prompt(opportunity, profile))
        result = _coerce_result(raw)

    match = Match(
        opportunity_id=opportunity.id,
        opportunity_hash=opportunity.content_hash,
        profile_version=profile.version,
        score=result.score,
        reasons=result.reasons,
        matched_capabilities=result.matched_capabilities,
        eligibility_flags=result.eligibility_flags,
        risk_notes=result.risk_notes,
    )
    if cache is not None:
        cache.set(key, match)
    return match


def score_all(
    opportunities: list[Opportunity],
    profile: Profile,
    client: LLMClient | None = None,
    cache: ScoreCache | None = None,
    now: datetime | None = None,
) -> list[Match]:
    """Score many opportunities, sorted by score descending."""
    matches = [
        score(opp, profile, client=client, cache=cache, now=now)
        for opp in opportunities
    ]
    return sorted(matches, key=lambda m: m.score, reverse=True)
