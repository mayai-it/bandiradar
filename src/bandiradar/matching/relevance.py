"""Stage 2 — LLM relevance scorer (ARCHITECTURE.md §6).

Scores a single opportunity against a profile::

    score(opportunity, profile) -> Match
    # score / reasons / matched_capabilities / eligibility_flags / risk_notes

Caches by ``hash(profile.version + opportunity.content_hash)`` so re-runs cost
nothing. When no provider/API key is configured, falls back to a DETERMINISTIC
heuristic so the engine runs with zero secrets.

TODO(Prompt 4): implement score() with caching + offline heuristic fallback.
"""
