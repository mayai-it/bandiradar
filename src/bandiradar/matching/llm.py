"""Provider-agnostic LLM client (ARCHITECTURE.md §6).

Selects a provider from the environment
(``BANDIRADAR_LLM_PROVIDER=anthropic|openai|none``, default ``none``) so
swapping to an EU/GDPR-friendly or local model is a config change, not a
refactor. Sends only minimal opportunity text + a compact profile summary.

TODO(Prompt 4): implement the provider-agnostic client.
"""
