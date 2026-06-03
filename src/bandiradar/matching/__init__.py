"""Two-stage matching engine (ARCHITECTURE.md §6).

Stage 1 (``prefilter``): pure deterministic filter, no LLM, no I/O.
Stage 2 (``relevance``): LLM relevance scorer with a deterministic offline
fallback and a cache, backed by a provider-agnostic client (``llm``) and prompt
templates (``prompts``).
"""
