"""Source framework and reference adapters (ARCHITECTURE.md §5).

Every source is ``fetch`` + ``to_opportunities`` and ships a recorded fixture so
it is testable offline. Sources self-register so community/pro adapters plug in
without touching core. See ``base`` for the protocol/registry and ``anac`` for
the reference adapter.

Importing this package imports the bundled adapters for their self-registration
side effect, so ``base.list_sources()`` is populated after ``import
bandiradar.sources``.
"""

from bandiradar.sources import anac as anac  # noqa: F401  (registration side effect)
from bandiradar.sources import ted as ted  # noqa: F401  (registration side effect)
