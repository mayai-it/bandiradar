"""Optional LISTWISE LLM reranking (0.3.0 — the precision lever).

Pointwise scoring (``relevance.score``) judges each opportunity in ISOLATION. This
module instead shows the LLM the whole candidate set for a profile in ONE call and
asks it to rank them COMPARATIVELY, returning a 0-100 score per item. That gives
both an order (precision@k) and scores (the min_score sweep) — and costs one call
per profile instead of N. Whether it beats pointwise is MEASURED by
``bandiradar eval --rerank``; nothing here changes default matcher behaviour.
"""

from __future__ import annotations

from bandiradar.matching.llm import LLMClient
from bandiradar.matching.prompts import _join, profile_summary
from bandiradar.matching.relevance import heuristic_fallback
from bandiradar.models import Match, Opportunity, Profile

# Candidates sent in a single listwise call. The eval pools are well under this, so
# the model ranks the entire prefiltered set per profile; if ever exceeded, the
# heuristic picks the head and the tail is appended (kept, ranked last).
RERANK_TOP_N = 25

_MAX_ELIG_CHARS = 200

RERANK_SYSTEM = (
    "You are a matching analyst for Italian public funding opportunities. Given a "
    "company profile and a NUMBERED list of opportunities, RANK them by relevance to "
    "the company, comparing them against each other.\n\n"
    "Respond with STRICT JSON and nothing else:\n"
    '{"ranking": [{"n": <candidate number>, "score": <integer 0-100>}, ...]}\n'
    "Order most-relevant first, include EVERY candidate exactly once, and use the "
    "scores to separate strong from weak matches (comparative, not absolute)."
)


def _compact_brief(opp: Opportunity) -> str:
    """One-line candidate summary — enough to compare, small enough to batch."""
    value = (
        "—" if opp.value_amount is None else f"{opp.value_amount} {opp.value_currency}"
    )
    elig = " ".join(p for p in (opp.eligibility_text, opp.document_text) if p).strip()
    if len(elig) > _MAX_ELIG_CHARS:
        elig = elig[:_MAX_ELIG_CHARS] + "…"
    region = opp.region or opp.issuer_region or opp.geo_scope
    return f"{opp.title} | CPV {_join(opp.cpv)} | {region} | {value} | {elig or '—'}"


def build_rerank_prompt(profile: Profile, candidates: list[Opportunity]) -> str:
    """Compact profile + a numbered candidate list for a single listwise call."""
    lines = [f"[{i}] {_compact_brief(o)}" for i, o in enumerate(candidates, start=1)]
    return "\n\n".join(
        [
            "COMPANY PROFILE:\n" + profile_summary(profile),
            "CANDIDATES (rank ALL of these):\n" + "\n".join(lines),
            'Return ONLY the JSON object {"ranking": [...]}.',
        ]
    )


def _parse_ranking(raw: object, n: int) -> dict[int, int]:
    """Map 0-based candidate index -> score from the model's JSON. Tolerant: skips
    malformed/out-of-range entries; clamps scores to 0-100."""
    out: dict[int, int] = {}
    if not isinstance(raw, dict):
        return out
    for item in raw.get("ranking", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["n"]) - 1
            score = int(round(float(item.get("score", 0))))
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < n and idx not in out:
            out[idx] = max(0, min(100, score))
    return out


def _match(opp: Opportunity, profile: Profile, score: int, reason: str) -> Match:
    return Match(
        opportunity_id=opp.id,
        opportunity_hash=opp.content_hash,
        profile_version=profile.version,
        score=score,
        reasons=[reason],
    )


def rerank(
    profile: Profile,
    candidates: list[Opportunity],
    client: LLMClient,
    now=None,
    top_n: int = RERANK_TOP_N,
) -> list[Match]:
    """Listwise-rerank the prefiltered candidates in ONE LLM call, scored desc.

    The deterministic heuristic provides a stable initial order (so a >top_n pool is
    truncated sensibly and ties are broken reproducibly); the LLM's comparative
    scores then drive the final ranking. Candidates the model omits keep a 0 score
    so they stay in the returned set (recall is unchanged vs pointwise)."""
    if not candidates:
        return []
    ordered = sorted(
        candidates,
        key=lambda o: heuristic_fallback(o, profile, now=now).score,
        reverse=True,
    )
    head, tail = ordered[:top_n], ordered[top_n:]
    scores = _parse_ranking(
        client.score(RERANK_SYSTEM, build_rerank_prompt(profile, head)), len(head)
    )

    matches = [
        _match(opp, profile, scores.get(i, 0), "listwise rerank")
        for i, opp in enumerate(head)
    ]
    matches += [_match(opp, profile, 0, "beyond rerank window") for opp in tail]
    return sorted(matches, key=lambda m: m.score, reverse=True)
