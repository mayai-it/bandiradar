"""Source protocol + registry — the extension point (ARCHITECTURE.md §5).

A ``Source`` is the minimal contract every adapter implements: an ``id``, a
``kind``, a ``fetch`` that yields raw payloads, and a PURE ``to_opportunities``
that maps a raw payload into canonical ``Opportunity`` objects.

Adapters self-register via :func:`register`, so pro and community sources plug
in without touching core. Importing an adapter module (or the ``sources``
package, which imports the bundled ones) populates the registry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Protocol, runtime_checkable

from bandiradar.models import Kind, Opportunity, RawDoc

# A progress sink: live fetches call it per page (e.g. "ted: page 3, 280 fetched").
ProgressFn = Callable[[str], None]


@runtime_checkable
class Source(Protocol):
    """The contract every funding source implements (ARCHITECTURE.md §5)."""

    id: str
    kind: Kind

    def fetch(
        self,
        since: datetime | None = None,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
        progress: ProgressFn | None = None,
    ) -> Iterable[RawDoc]:
        """Yield raw source payloads LAZILY (paginating as it goes).

        ``limit`` caps the number of records, ``max_pages`` the number of pages
        (both safety bounds); ``progress`` receives a short status line per page.
        Yielding lazily lets the caller save progressively and stop early.
        """
        ...

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        """PURE mapping from one raw payload to canonical opportunities.

        ``now`` is the reference time for status derivation (defaults to current
        UTC inside the adapter).
        """
        ...


# Module-level registry. Keyed by source id; values are Source instances.
_REGISTRY: dict[str, Source] = {}


def register(source: Source) -> Source:
    """Register a source instance by its ``id``. Returns it for convenience.

    Raises ``ValueError`` on a duplicate id so a clashing adapter fails loudly
    rather than silently shadowing another.
    """
    if source.id in _REGISTRY:
        raise ValueError(f"source already registered: {source.id!r}")
    _REGISTRY[source.id] = source
    return source


def get(source_id: str) -> Source:
    """Return the registered source with this id, or raise ``KeyError``."""
    try:
        return _REGISTRY[source_id]
    except KeyError:
        raise KeyError(f"no source registered with id {source_id!r}") from None


def list_sources() -> list[Source]:
    """Return all registered sources, ordered by id."""
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]
