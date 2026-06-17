"""BandiRadar — open-source engine for monitoring Italian public funding.

Monitors public tenders, grants, and incentives; normalizes them into one
canonical :class:`Opportunity` model; and ranks them against a company
``Profile`` with a two-stage matcher (deterministic prefilter + LLM relevance).

See ``ARCHITECTURE.md`` for the design and ``CLAUDE.md`` for the operational
contract. This package is the open (MIT) core; managed/delivery features live
in the private ``bandiradar-pro``.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version declared in pyproject (installed metadata),
    # so __version__ never drifts from the package version again.
    __version__ = version("bandiradar")
except PackageNotFoundError:  # not installed (e.g. running from a raw checkout)
    __version__ = "0.0.0+unknown"
