"""Regione Siciliana — EuroInfoSicilia (FESR/FSC) bandi, as a config over
``WordPressBandiSource``.

EuroInfoSicilia (the regional cohesion-policy portal) publishes its bandi/avvisi as
STANDARD WordPress posts under the category "Bandi e Avvisi" (id 321) — not a custom
post type. So this is the same WP-REST base as Lazio, just pointed at ``/posts`` with
a ``categories`` filter (``extra_params``); all fetch/paging/mapping logic stays in
``sources/wordpress.py``. Mostly FESR/FSC grants → ``kind="incentive"``; the scadenza
is parsed from the bando text (``_parse_scadenza``) as usual.
"""

from __future__ import annotations

from bandiradar.sources.base import register

# Re-exported so tests/callers can import the parser from here too.
from bandiradar.sources.wordpress import (  # noqa: F401
    WordPressBandiSource,
    _parse_scadenza,
)

SOURCE_ID = "sicilia"
# Standard WP posts endpoint, filtered to the "Bandi e Avvisi" category (id 321).
SICILIA_DATA_URL = "https://www.euroinfosicilia.it/wp-json/wp/v2/posts"
SICILIA_BANDI_CATEGORY = 321

SOURCE = WordPressBandiSource(
    id=SOURCE_ID,
    region="Sicilia",
    data_url=SICILIA_DATA_URL,
    issuer_name="Regione Siciliana — EuroInfoSicilia",
    kind="incentive",
    # Richer-than-default keyword taxonomies present on these posts (the generic
    # ``category-`` slugs like "bandi"/"decreti" are deliberately excluded).
    keyword_taxonomies=("tag-", "programmi-", "destinatari-", "argomenti-"),
    extra_params={
        "categories": SICILIA_BANDI_CATEGORY,
        "orderby": "date",
        "order": "desc",
    },
)

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
