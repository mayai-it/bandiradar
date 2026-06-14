"""Phase-1 generalization of the self-healing crawl to JSON-listing scrapers.

`calabria` and `basilicata` expose a WP-REST JSON listing with the same item shape
as `toscana`, so they opt into the recipe-based crawl (``default_recipe``) and get
the SAME gated self-heal. HTML-listing scrapers (e.g. `veneto`) stay detect-only.

Offline, no network, no LLM: the recipe is applied to the recorded cassette, and the
healer is a FAKE returning dotted paths (mirrors test_heal.py)."""

import json
from pathlib import Path

import pytest

from bandiradar.recipe_store import RecipeStore
from bandiradar.sources import basilicata, calabria, veneto
from bandiradar.sources.llm_scraper import apply_recipe, validate_refs
from bandiradar.storage import Store

CASS = Path(__file__).parent / "cassettes"


def _cassette(name: str) -> list:
    return json.loads((CASS / name).read_text(encoding="utf-8"))


class _FakeHealer:
    """A stand-in LLM healer that returns fixed dotted paths (no network/LLM)."""

    def __init__(self, paths: dict):
        self.paths = paths

    def score(self, system: str, user: str) -> dict:
        return self.paths


# --------------------------------------------------------------------------- #
# The baked recipe reproduces the cassette (same refs the hand parser yields)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "mod, recipe, cassette",
    [
        (calabria, calabria.CALABRIA_RECIPE, "calabria_listing.json"),
        (basilicata, basilicata.BASILICATA_RECIPE, "basilicata_listing.json"),
    ],
)
def test_recipe_reproduces_cassette(mod, recipe, cassette):
    items = _cassette(cassette)
    refs = apply_recipe(recipe, items)
    assert validate_refs(refs) == "ok"
    # The recipe must read the SAME id+url as the hand-written parser (titles may
    # differ only by HTML-entity cleanup, which apply_recipe leaves raw).
    parsed = mod.parse_listing(items)
    assert [(r[0], r[1]) for r in refs] == [(p[0], p[1]) for p in parsed]


# --------------------------------------------------------------------------- #
# Opt-in: JSON sources heal, HTML sources stay detect-only
# --------------------------------------------------------------------------- #


def test_json_sources_opt_into_healing_html_does_not():
    assert calabria.SOURCE.default_recipe is not None
    assert basilicata.SOURCE.default_recipe is not None
    # An HTML-listing scraper keeps the bespoke-code parse -> no auto-heal.
    assert veneto.SOURCE.default_recipe is None


# --------------------------------------------------------------------------- #
# End-to-end: the base recipe path auto-heals a drifted JSON listing
# --------------------------------------------------------------------------- #


def _drift(items: list) -> list:
    """Rename the WP-REST keys so the baked recipe yields broken refs."""
    return [
        {
            "postId": it["id"],
            "permalink": it["link"],
            "title": {"text": (it.get("title") or {}).get("rendered", "")},
        }
        for it in items
    ]


def test_base_recipe_path_auto_heals_on_drift(monkeypatch):
    src = calabria.CalabriaSource()
    store = Store(":memory:")
    rs = RecipeStore(store)
    try:
        healthy = _cassette("calabria_listing.json")

        # 1) Healthy crawl: records the golden, health ok.
        monkeypatch.setattr(src, "_listing_json", lambda recipe: healthy)
        src._list_details(rs, None)
        assert src.last_crawl_health == "ok"
        assert rs.get_golden("calabria")

        # 2) Drift (renamed keys) with NO client -> detect-only, stays broken.
        monkeypatch.setattr(src, "_listing_json", lambda recipe: _drift(healthy))
        src._list_details(rs, None)
        assert src.last_crawl_health == "broken"
        assert rs.get_recipe("calabria") is None  # nothing adopted without a healer

        # 3) Same drift WITH a healer returning the right paths -> gated auto-adopt.
        healer = _FakeHealer(
            {
                "post_id_path": "postId",
                "detail_url_path": "permalink",
                "title_path": "title.text",
            }
        )
        src._list_details(rs, healer)
        assert src.last_crawl_health == "ok"  # crawl recovered
        assert rs.get_recipe("calabria") is not None  # candidate adopted (golden-exact)
    finally:
        store.close()
