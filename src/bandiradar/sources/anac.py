"""ANAC / PNCP (OCDS) reference adapter (ARCHITECTURE.md §5).

The backbone source: real, structured, free open-contracting data. Maps an
ANAC/OCDS record to ``Opportunity`` objects with a PURE ``to_opportunities``,
and (in --sample mode) reads the bundled fixture for fully offline runs.

TODO(Prompt 2):
- to_opportunities(raw): pure ANAC/OCDS -> list[Opportunity].
- fetch(since): read data/fixtures/anac_sample.json in --sample mode.
- Leave a clearly-marked TODO + config constant for the live PNCP/ANAC
  open-data endpoint. Do NOT invent a URL — confirm against live docs first.
- Register "anac" in the sources registry.
"""
