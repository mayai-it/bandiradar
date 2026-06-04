"""Prompt templates for the Stage-2 relevance scorer (ARCHITECTURE.md §6).

Privacy guardrail (#2): the LLM only ever sees a COMPACT profile summary and a
MINIMAL opportunity brief — never the raw source payload or full personal data.
"""

from __future__ import annotations

from bandiradar.models import Opportunity, Profile

SCORING_SYSTEM = (
    "You are a matching analyst for Italian public funding opportunities "
    "(tenders, grants, incentives). Given a company profile and one opportunity, "
    "judge how relevant the opportunity is to the company.\n\n"
    "Respond with STRICT JSON and nothing else, exactly this shape:\n"
    "{\n"
    '  "score": <integer 0-100, higher = more relevant>,\n'
    '  "reasons": [<short strings explaining the score>],\n'
    '  "matched_capabilities": [<company capabilities/interests that fit>],\n'
    '  "eligibility_flags": [<eligibility concerns or requirements to check>],\n'
    '  "risk_notes": [<risks, e.g. tight deadline, value mismatch>]\n'
    "}\n"
    "Base the score only on the provided fields. Do not invent facts."
)


def _join(items: list[str]) -> str:
    return ", ".join(items) if items else "—"


def profile_summary(profile: Profile) -> str:
    """Compact, human-readable profile summary (no raw dump)."""
    vr = profile.value_range
    lo = vr.min if vr.min is not None else "—"
    hi = vr.max if vr.max is not None else "—"
    return "\n".join(
        [
            f"Company: {profile.name}",
            f"Language: {profile.language}",
            f"ATECO: {_join(profile.ateco)}",
            f"CPV interests: {_join(profile.cpv_interests)}",
            f"Keywords: {_join(profile.keywords)}",
            f"Regions: {_join(profile.regions)}",
            f"Value range (EUR): {lo}–{hi}",
            f"Capabilities: {profile.capabilities.strip() or '—'}",
            f"Exclusions: {_join(profile.exclusions)}",
        ]
    )


def opportunity_brief(opportunity: Opportunity) -> str:
    """Minimal opportunity brief — only the fields the matcher needs."""
    o = opportunity
    value = "—" if o.value_amount is None else f"{o.value_amount} {o.value_currency}"
    deadline = o.deadline.isoformat() if o.deadline else "—"
    return "\n".join(
        [
            f"Title: {o.title}",
            f"Summary: {o.summary or '—'}",
            f"Issuer: {o.issuer_name or '—'} ({o.issuer_region or '—'})",
            f"Geo scope: {o.geo_scope}; Region: {o.region or '—'}",
            f"CPV: {_join(o.cpv)}",
            f"Value: {value}",
            f"Deadline: {deadline}",
            f"Eligibility: {o.eligibility_text or '—'}",
        ]
    )


def build_user_prompt(
    opportunity: Opportunity, profile: Profile, benchmark=None
) -> str:
    """Assemble the user message: compact profile + minimal opportunity brief.

    When a historical ``benchmark`` (intelligence track) is supplied, a compact
    one-line summary is included so the model can reason about it.
    """
    parts = [
        "COMPANY PROFILE:\n" + profile_summary(profile),
        "OPPORTUNITY:\n" + opportunity_brief(opportunity),
    ]
    if benchmark is not None:
        from bandiradar.intelligence.enrichment import benchmark_summary

        parts.append("HISTORICAL BENCHMARK:\n" + benchmark_summary(benchmark))
    parts.append("Return ONLY the JSON object.")
    return "\n\n".join(parts)
