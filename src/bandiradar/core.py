"""Service layer — orchestrates the pipeline (ARCHITECTURE.md §3).

The single place that wires fetch -> normalize -> store -> match. Interfaces
(``cli``, ``mcp_server``) are THIN shells over this module and contain NO
business logic; all logic lives here, in ``sources/``, ``matching/``, and
``storage``.

TODO(Prompt 6): implement the orchestration layer (no presentation logic).
"""
