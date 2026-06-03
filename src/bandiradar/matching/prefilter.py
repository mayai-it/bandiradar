"""Stage 1 — deterministic prefilter (ARCHITECTURE.md §6).

A PURE function that cuts thousands of rows to dozens, cheaply and explainably::

    prefilter(opportunities, profile) -> list[Opportunity]

Filters on region/geo, ``cpv ∩ ateco``, value range, ``deadline > now``, and
keyword overlap. No LLM, no I/O — must stay unit-testable without network.

TODO(Prompt 3): implement the pure prefilter and its edge-case tests.
"""
