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
from bandiradar.sources import anac_pvl as anac_pvl  # noqa: F401  (registration)
from bandiradar.sources import campania as campania  # noqa: F401  (registration)
from bandiradar.sources import (  # noqa: F401  (registration)
    emilia_romagna as emilia_romagna,
)
from bandiradar.sources import fvg as fvg  # noqa: F401  (registration)
from bandiradar.sources import incentivi as incentivi  # noqa: F401  (registration)
from bandiradar.sources import lazio as lazio  # noqa: F401  (registration)
from bandiradar.sources import lombardia as lombardia  # noqa: F401  (registration)
from bandiradar.sources import piemonte as piemonte  # noqa: F401  (registration)
from bandiradar.sources import puglia as puglia  # noqa: F401  (registration)
from bandiradar.sources import sardegna as sardegna  # noqa: F401  (registration)
from bandiradar.sources import sicilia as sicilia  # noqa: F401  (registration)
from bandiradar.sources import ted as ted  # noqa: F401  (registration side effect)
from bandiradar.sources import toscana as toscana  # noqa: F401  (registration)
from bandiradar.sources import trentino as trentino  # noqa: F401  (registration)
from bandiradar.sources import veneto as veneto  # noqa: F401  (registration)
