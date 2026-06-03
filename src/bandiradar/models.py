"""Canonical data model — THE contract (see ARCHITECTURE.md §4).

Defines the pydantic v2 models shared across the whole engine:

- ``Opportunity`` — the superset model covering tenders AND grants/incentives.
- ``RawDoc`` — the untouched payload from a source (audit + re-mapping).
- ``Profile`` — the company we match opportunities against (ARCHITECTURE.md §7).
- ``Match`` — the Stage-2 relevance result for an (opportunity, profile) pair.

Do NOT break field names/shape without updating ARCHITECTURE.md and every
adapter + test in the same change.

TODO(Prompt 1): implement Opportunity, RawDoc, Profile, Match as pydantic v2
models with full type hints and validators (status derived from deadline;
content_hash helper).
"""
