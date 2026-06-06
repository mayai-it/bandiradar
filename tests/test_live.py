"""Opt-in LIVE drift check — NOT run in default CI (needs the network).

Deselected by default via ``addopts = -m "not live"``. Run on demand with:

    uv run pytest -m live

Each test hits a KEY-LESS source's real endpoint with a minimal query (limit=1)
and asserts the response still has the top-level shape/fields we parse — so real
API drift is caught on demand. A failure here means re-record the matching
cassette in ``tests/cassettes/`` and update the parser (see CONTRIBUTING).
"""

import pytest

from bandiradar.sources.base import get

pytestmark = pytest.mark.live

# Key-less sources only — no provider/key needed. (Toscana needs an LLM key.)
KEYLESS_SOURCES = ["ted", "incentivi", "lombardia", "lazio", "anac"]


@pytest.mark.parametrize("source_id", KEYLESS_SOURCES)
def test_source_live_shape_unchanged(source_id):
    src = get(source_id)
    raws = list(src.fetch(limit=1))
    assert raws, (
        f"{source_id}: no records for limit=1 — the top-level response shape "
        f"may have drifted (re-record tests/cassettes/{source_id}.*)."
    )
    opportunities = [opp for raw in raws for opp in src.to_opportunities(raw)]
    assert opportunities, (
        f"{source_id}: records did not map to opportunities — the field shape "
        f"may have drifted."
    )
    opp = opportunities[0]
    assert opp.source == source_id
    assert opp.id.startswith(f"{source_id}:")
