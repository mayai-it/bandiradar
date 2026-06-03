"""FastMCP server — a THIN shell over ``core`` (ARCHITECTURE.md §9).

Planned tools: ``list_sources``, ``fetch_opportunities``,
``search_opportunities``, ``score_opportunity``, ``get_matches``,
``get_profile`` — each a thin wrapper that calls into ``core``. Lets you drive
BandiRadar from Claude itself (dogfood).

TODO(Prompt 7): instantiate FastMCP and expose the tools.
"""
