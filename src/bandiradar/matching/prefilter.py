"""Stage 1 — deterministic prefilter (ARCHITECTURE.md §6).

A PURE, deterministic function that cuts thousands of rows to dozens, cheaply
and explainably. No LLM, no I/O. It is intentionally *conservative*: Stage 1
drops only the CLEARLY irrelevant; fine-grained relevance is Stage 2's job. An
opportunity is KEPT unless one of the ordered gates drops it.

Gates, evaluated in order (the first failing gate's reason is reported):

1. Open       — drop if a deadline exists and is at/after... (<= now). Missing
                deadline passes.
2. Instrument — drop if the opportunity's seek class (tender vs grant/incentive)
                is not in profile.seeks. Default seeks = both, so unset profiles
                are unaffected.
3. Geography  — national/eu bypass; regional/local must match a profile region
                (when the profile lists any). Empty profile.regions = no limit.
4. Value      — drop only when BOTH sides carry value info and the ranges do not
                overlap. Missing data never drops.
5. Exclusions — drop if any exclusion term appears in title + summary.
6. Relevance  — when the profile has cpv_interests or keywords, require a CPV
                match or a keyword hit; OR (hybrid, opt-in) a semantic-embedding
                similarity >= threshold. Skipped if the profile gives no signal.

The semantic signal is OPT-IN: pass an ``embedder`` (else ``None`` -> the gate is
exactly the deterministic CPV/keyword test it has always been). This is the only
place Stage 1 may do local model inference + a cache lookup; the default path stays
pure (no model, no network, no I/O).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import UTC, datetime

from bandiradar.matching.embeddings import (
    EMBEDDING_SIM_THRESHOLD,
    Embedder,
    EmbeddingCache,
    cosine,
    embed_opportunity,
    profile_text,
)
from bandiradar.models import Kind, Opportunity, Profile, Seek

_BYPASS_GEO = {"national", "eu"}


def seek_class(kind: Kind) -> Seek:
    """Map an opportunity ``kind`` to the company-intent class it satisfies.

    Tenders are bid on ("tender"); grants and incentives are applied for ("grant").
    Single source of truth for the Stage-1 instrument gate and the gold corrections.
    """
    return "tender" if kind == "tender" else "grant"


# Tokenizer for keyword/capability overlap: runs of >=4 ASCII letters, lowercased.
_TOKEN_RE = re.compile(r"[a-zA-Z]{4,}")

# Generic Italian/English procurement & business filler. These words appear in
# almost any tender/grant regardless of sector, so they must NOT create
# keyword/capability overlap across unrelated sectors (e.g. a construction
# profile matching IT tenders on "lavori"/"manutenzione"). They are removed from
# BOTH the profile terms and the opportunity tokens before intersecting. Kept
# deliberately narrow: only sector-AGNOSTIC words — domain terms like
# "lavorazioni", "macchine", "software", "dispositivi" are intentionally absent.
STOPWORDS = frozenset(
    {
        # Italian — procurement/process filler
        "lavori",
        "lavoro",
        "servizio",
        "servizi",
        "fornitura",
        "forniture",
        "manutenzione",
        "gestione",
        "appalto",
        "appalti",
        "acquisto",
        "acquisti",
        "contratto",
        "contratti",
        "progetto",
        "progetti",
        "sistema",
        "sistemi",
        "realizzazione",
        "affidamento",
        "affidamenti",
        "procedura",
        "procedure",
        "intervento",
        "interventi",
        "attivita",
        "attivit",
        "bando",
        "bandi",
        "offerta",
        "offerte",
        "importo",
        "importi",
        "oggetto",
        "annuale",
        "triennale",
        "mediante",
        "ulteriori",
        "presentazione",
        "domande",
        # generic "process(es)" — the sector signal is the term beside it
        "processo",
        "processi",
        # Italian — generic entities/geo (carry no sector signal)
        "pubblico",
        "pubblica",
        "pubblici",
        "pubbliche",
        "comune",
        "comunale",
        "comuni",
        "regione",
        "regionale",
        "azienda",
        "aziende",
        "ente",
        "enti",
        # English — generic
        "works",
        "service",
        "services",
        "supply",
        "supplies",
        "maintenance",
        "management",
        "contract",
        "contracts",
        "procurement",
        "system",
        "systems",
        "project",
        "projects",
        "public",
        "tender",
        "tenders",
        "activity",
        "provision",
        "award",
        "awards",
        "company",
        "companies",
    }
)


def meaningful_tokens(text: str | None) -> set[str]:
    """Sector-bearing tokens of ``text``: >=4-letter words minus :data:`STOPWORDS`.

    The single tokenizer shared by the prefilter keyword gate and the Stage-2
    heuristic, so both judge keyword overlap the same way.
    """
    return {
        token
        for token in (m.group(0).lower() for m in _TOKEN_RE.finditer(text or ""))
        if token not in STOPWORDS
    }


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
    opp: Opportunity,
    profile: Profile,
    now: datetime,
    semantic: Callable[[Opportunity], float] | None = None,
    sim_threshold: float = EMBEDDING_SIM_THRESHOLD,
) -> tuple[bool, str]:
    """Return (kept, reason). ``reason`` is the first failing gate, else ""."""

    # Gate 1 — open.
    if opp.deadline is not None and opp.deadline <= now:
        return False, "closed: deadline at or before now"

    # Gate 2 — instrument type (grant vs tender). A profile only matches the
    # instrument classes it pursues; default seeks = both, so unset profiles pass.
    klass = seek_class(opp.kind)
    if klass not in profile.seeks:
        return False, f"profile does not seek {klass}s"

    # Gate 3 — geography.
    if opp.geo_scope not in _BYPASS_GEO and profile.regions:
        wanted = {_norm(r) for r in profile.regions}
        if _norm(opp.region) not in wanted:
            return False, "region not among profile regions"

    # Gate 4 — value overlap (only when both sides carry value info).
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

    # Gate 5 — exclusions.
    haystack = _haystack(opp)
    for term in profile.exclusions:
        norm_term = _norm(term)
        if norm_term and norm_term in haystack:
            return False, f"excluded term: {term}"

    # Gate 6 — relevance signal (skipped if the profile gives no signal sources).
    if profile.cpv_interests or profile.keywords or semantic is not None:
        cpv_ok = cpv_match(opp.cpv, profile.cpv_interests)
        # Keyword overlap on MEANINGFUL tokens only (stopwords stripped from both
        # sides), so generic procurement words don't create cross-sector hits.
        # Covers eligibility_text AND extracted document_text (PDF enrichment), so
        # requirements that live only in the attachments still drive a match.
        profile_tokens = meaningful_tokens(" ".join(profile.keywords))
        elig = f"{_norm(opp.eligibility_text)} {_norm(opp.document_text)}"
        opp_tokens = meaningful_tokens(f"{haystack} {elig}")
        keyword_ok = bool(profile_tokens & opp_tokens)
        # Semantic rescue (opt-in): only computed when the cheap signals miss, so
        # an embedder is invoked just for the items it might actually save.
        if not (cpv_ok or keyword_ok):
            if semantic is None:
                return False, "no CPV match or keyword hit"
            if semantic(opp) < sim_threshold:
                return False, "no CPV match, keyword hit, or semantic similarity"

    return True, ""


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def _semantic_scorer(
    profile: Profile,
    embedder: Embedder | None,
    cache: EmbeddingCache | None,
) -> Callable[[Opportunity], float] | None:
    """Build the ``opp -> cosine`` scorer, embedding the profile ONCE. ``None`` when
    no embedder is injected (the prefilter then behaves exactly as before)."""
    if embedder is None:
        return None
    profile_vec = embedder.embed([profile_text(profile)])[0]

    def score(opp: Opportunity) -> float:
        return cosine(profile_vec, embed_opportunity(opp, embedder, cache))

    return score


def prefilter_explain(
    opportunities: list[Opportunity],
    profile: Profile,
    now: datetime | None = None,
    *,
    embedder: Embedder | None = None,
    embedding_cache: EmbeddingCache | None = None,
    sim_threshold: float = EMBEDDING_SIM_THRESHOLD,
) -> list[tuple[Opportunity, bool, str]]:
    """Like :func:`prefilter`, but report the keep flag + drop reason per item.

    The reason is the first failing gate's message, or ``""`` when kept.
    Order-preserving. With no ``embedder`` it is pure (no I/O); an injected embedder
    adds the opt-in semantic relevance signal (Gate 6).
    """
    resolved = _resolve_now(now)
    semantic = _semantic_scorer(profile, embedder, embedding_cache)
    results: list[tuple[Opportunity, bool, str]] = []
    for opp in opportunities:
        kept, reason = _evaluate(opp, profile, resolved, semantic, sim_threshold)
        results.append((opp, kept, reason))
    return results


def prefilter(
    opportunities: list[Opportunity],
    profile: Profile,
    now: datetime | None = None,
    *,
    embedder: Embedder | None = None,
    embedding_cache: EmbeddingCache | None = None,
    sim_threshold: float = EMBEDDING_SIM_THRESHOLD,
) -> list[Opportunity]:
    """Return the opportunities that survive every Stage-1 gate (order-preserving)."""
    return [
        opp
        for opp, kept, _ in prefilter_explain(
            opportunities,
            profile,
            now,
            embedder=embedder,
            embedding_cache=embedding_cache,
            sim_threshold=sim_threshold,
        )
        if kept
    ]
