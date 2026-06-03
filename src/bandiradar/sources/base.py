"""Source protocol + registry — the extension point (ARCHITECTURE.md §5).

A ``Source`` is the minimal contract every adapter implements::

    class Source(Protocol):
        id: str
        kind: Literal["tender", "grant", "incentive"]
        def fetch(self, since: datetime | None = None) -> Iterable[RawDoc]: ...
        def to_opportunities(self, raw: RawDoc) -> list[Opportunity]: ...

The registry lets sources self-register (``register`` / ``get`` / ``list``) so
pro and community adapters plug in without touching core.

TODO(Prompt 2): implement the Source Protocol and the register/get/list registry.
"""
