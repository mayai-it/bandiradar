"""Stage 1 — deterministic prefilter (ARCHITECTURE.md §6).

A PURE, deterministic function that cuts thousands of rows to dozens, cheaply
and explainably. No LLM, no I/O. It is intentionally *conservative*: Stage 1
drops only the CLEARLY irrelevant; fine-grained relevance is Stage 2's job. An
opportunity is KEPT unless one of the ordered gates drops it.

Gates, evaluated in order (the first failing gate's reason is reported):

1. Open       — drop if a deadline exists and is at/after... (<= now). Missing
                deadline passes.
2. Geography  — national/eu bypass; regional/local must match a profile region
                (when the profile lists any). Empty profile.regions = no limit.
3. Value      — drop only when BOTH sides carry value info and the ranges do not
                overlap. Missing data never drops.
4. Exclusions — drop if any exclusion term appears in title + summary.
5. Relevance  — when the profile has cpv_interests or keywords, require a CPV
                match or a keyword hit; otherwise this gate is skipped.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from bandiradar.models import Opportunity, Profile

_BYPASS_GEO = {"national", "eu"}


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _haystack(opp: Opportunity) -> str:
    """Lowercased title + summary, the text gates 4 and 5 search."""
    return f"{opp.title} {opp.summary or ''}".lower()


def cpv_key(code: str) -> str:
    """Normalize a CPV code for prefix matching: strip whitespace + trailing zeros.

    "72000000" -> "72", "72212000" -> "72212", "44500000" -> "445".
    """
    return code.strip().rstrip("0")


def cpv_match_depth(opp_cpv: list[str], interests: list[str]) -> int:
    """Depth of the best CPV prefix match (0 = none).

    Two codes match when one normalized key is a prefix of the other; the depth
    is the length of the shorter key, so a more specific shared prefix scores
    higher. This is the single source of truth reused by Stage 2's heuristic.
    """
    interest_keys = [k for k in (cpv_key(i) for i in interests) if k]
    opp_keys = [k for k in (cpv_key(c) for c in opp_cpv) if k]
    best = 0
    for ik in interest_keys:
        for ok in opp_keys:
            if ik.startswith(ok) or ok.startswith(ik):
                best = max(best, min(len(ik), len(ok)))
    return best


def cpv_match(opp_cpv: list[str], interests: list[str]) -> bool:
    """True when any opportunity CPV prefix-matches any profile interest."""
    return cpv_match_depth(opp_cpv, interests) > 0


def _has_value_info(opp: Opportunity) -> bool:
    return (
        opp.value_amount is not None
        or opp.value_min is not None
        or opp.value_max is not None
    )


def _opp_interval(opp: Opportunity) -> tuple[float, float]:
    """Opportunity value interval. A range wins; else a bare amount is a point."""
    if opp.value_min is not None or opp.value_max is not None:
        lo = opp.value_min if opp.value_min is not None else -math.inf
        hi = opp.value_max if opp.value_max is not None else math.inf
        return lo, hi
    # Only value_amount present (gate 3 only runs when some value info exists).
    return opp.value_amount, opp.value_amount  # type: ignore[return-value]


def _evaluate(
    opp: Opportunity, profile: Profile, now: datetime
) -> tuple[bool, str]:
    """Return (kept, reason). ``reason`` is the first failing gate, else ""."""

    # Gate 1 — open.
    if opp.deadline is not None and opp.deadline <= now:
        return False, "closed: deadline at or before now"

    # Gate 2 — geography.
    if opp.geo_scope not in _BYPASS_GEO and profile.regions:
        wanted = {_norm(r) for r in profile.regions}
        if _norm(opp.region) not in wanted:
            return False, "region not among profile regions"

    # Gate 3 — value overlap (only when both sides carry value info).
    range_has_bound = (
        profile.value_range.min is not None or profile.value_range.max is not None
    )
    if _has_value_info(opp) and range_has_bound:
        o_lo, o_hi = _opp_interval(opp)
        vr = profile.value_range
        p_lo = vr.min if vr.min is not None else -math.inf
        p_hi = vr.max if vr.max is not None else math.inf
        if not (o_lo <= p_hi and p_lo <= o_hi):
            return False, "value range does not overlap profile range"

    # Gate 4 — exclusions.
    haystack = _haystack(opp)
    for term in profile.exclusions:
        norm_term = _norm(term)
        if norm_term and norm_term in haystack:
            return False, f"excluded term: {term}"

    # Gate 5 — relevance signal (skipped if the profile gives no signal sources).
    if profile.cpv_interests or profile.keywords:
        cpv_ok = cpv_match(opp.cpv, profile.cpv_interests)
        # Keyword scan also covers eligibility_text: incentives have no CPV, so
        # without this their relevance signal lives only in the requirements text
        # and they would be dropped before scoring.
        keyword_text = f"{haystack} {_norm(opp.eligibility_text)}"
        keyword_ok = any(
            _norm(k) and _norm(k) in keyword_text for k in profile.keywords
        )
        if not (cpv_ok or keyword_ok):
            return False, "no CPV match or keyword hit"

    return True, ""


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def prefilter_explain(
    opportunities: list[Opportunity],
    profile: Profile,
    now: datetime | None = None,
) -> list[tuple[Opportunity, bool, str]]:
    """Like :func:`prefilter`, but report the keep flag + drop reason per item.

    The reason is the first failing gate's message, or ``""`` when kept. Pure
    and order-preserving.
    """
    resolved = _resolve_now(now)
    results: list[tuple[Opportunity, bool, str]] = []
    for opp in opportunities:
        kept, reason = _evaluate(opp, profile, resolved)
        results.append((opp, kept, reason))
    return results


def prefilter(
    opportunities: list[Opportunity],
    profile: Profile,
    now: datetime | None = None,
) -> list[Opportunity]:
    """Return the opportunities that survive every Stage-1 gate (order-preserving)."""
    return [
        opp
        for opp, kept, _ in prefilter_explain(opportunities, profile, now)
        if kept
    ]
