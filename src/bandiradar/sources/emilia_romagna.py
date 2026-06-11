"""Regione Emilia-Romagna — Politiche territoriali / fondi europei bandi, as a
config over :class:`PloneBandoSource`.

The regional cohesion-policy portal (``politicheterritoriali.regione.emilia-romagna.it``
— the successor of the old ``fondieuropei.…`` host) is a Plone 6 site exposing its
bandi as the structured AGID ``Bando`` content type over plone.restapi. All
fetch/paging/mapping logic lives in ``sources/plone.py``; this module is just the
Emilia-Romagna config. Mostly grants/incentives (``tipologia_bando`` =
"Agevolazioni, finanziamenti, contributi") → ``kind="incentive"``.
"""

from __future__ import annotations

from bandiradar.sources.base import register
from bandiradar.sources.plone import PloneBandoSource

SOURCE_ID = "emilia_romagna"
EMILIA_ROMAGNA_BASE_URL = "https://politicheterritoriali.regione.emilia-romagna.it"

SOURCE = PloneBandoSource(
    id=SOURCE_ID,
    region="Emilia-Romagna",
    issuer_name="Regione Emilia-Romagna — Politiche territoriali",
    base_url=EMILIA_ROMAGNA_BASE_URL,
    kind="incentive",
)

# Convenience aliases (the registered instance is the source of truth).
to_opportunities = SOURCE.to_opportunities
load_fixture = SOURCE.load_fixture

register(SOURCE)
