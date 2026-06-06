"""Regione Lazio — LazioInnova bandi, as a config over WordPressBandiSource.

The Lazio CKAN portal (dati.regione.lazio.it) is unreachable and Lazio's CKAN
catalogue carries no open calls; the *live* open business incentives live on
LazioInnova (the regional development agency), a WordPress site whose bandi are
read via its WP REST API. All mapping/fetch logic lives in
``sources/wordpress.py``; this module is just the Lazio config.
"""

from __future__ import annotations

from bandiradar.sources.base import register

# Re-exported so existing tests/callers keep importing the parser from here.
from bandiradar.sources.wordpress import (  # noqa: F401
    WordPressBandiSource,
    _parse_scadenza,
)

SOURCE_ID = "lazio"
LAZIO_DATA_URL = "https://www.lazioinnova.it/wp-json/wp/v2/bandi"

SOURCE = WordPressBandiSource(
    id=SOURCE_ID,
    region="Lazio",
    data_url=LAZIO_DATA_URL,
    issuer_name="LazioInnova",
    kind="incentive",
)

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
