"""Source framework and reference adapters (ARCHITECTURE.md §5).

Every source is ``fetch`` + ``to_opportunities`` and ships a recorded fixture so
it is testable offline. Sources self-register so community/pro adapters plug in
without touching core. See ``base`` for the protocol/registry and ``anac`` for
the reference adapter.
"""
