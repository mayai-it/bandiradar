"""Phase-2a: self-healing generalized to HTML-listing scrapers (regex-template).

The clean single-regex HTML scrapers (`veneto`, `sardegna`, `piemonte`) model their
listing parse as an `HtmlCrawlRecipe` (DATA), so a drifted markup is auto-healable the
SAME gated way as the JSON scrapers: the LLM re-derives `item_regex` (ReDoS-guarded),
adopted only if it reproduces the golden exactly. Bespoke HTML scrapers stay
detect-only.

Offline, no network, no LLM: recipes are applied to recorded cassettes; the healer is
a FAKE returning a regex."""

import json
from pathlib import Path

import pytest

from bandiradar.crawl import (
    HtmlCrawlRecipe,
    apply_html_recipe,
    is_safe_regex,
)
from bandiradar.recipe_store import RecipeStore, recipe_from_json, recipe_to_json
from bandiradar.sources import liguria, piemonte, sardegna, veneto
from bandiradar.storage import Store

CASS = Path(__file__).parent / "cassettes"


class _FakeHtmlHealer:
    def __init__(self, item_regex: str):
        self.item_regex = item_regex

    def score(self, system: str, user: str) -> dict:
        return {"item_regex": self.item_regex}


# --------------------------------------------------------------------------- #
# The baked HTML recipe reproduces the hand parser's refs exactly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "mod, recipe, cassette",
    [
        (veneto, veneto.VENETO_RECIPE, "veneto_listing.html"),
        (sardegna, sardegna.SARDEGNA_RECIPE, "sardegna_listing.html"),
        (piemonte, piemonte.PIEMONTE_RECIPE, "piemonte_listing.html"),
    ],
)
def test_html_recipe_reproduces_cassette(mod, recipe, cassette):
    page = (CASS / cassette).read_text(encoding="utf-8")
    assert apply_html_recipe(recipe, page) == mod.parse_listing(page)


# --------------------------------------------------------------------------- #
# ReDoS guard
# --------------------------------------------------------------------------- #


def test_is_safe_regex_guard():
    # The real baked recipes are safe.
    for recipe in (
        veneto.VENETO_RECIPE,
        sardegna.SARDEGNA_RECIPE,
        piemonte.PIEMONTE_RECIPE,
    ):
        assert is_safe_regex(recipe.item_regex)
    # Nested quantifiers (classic ReDoS) are refused.
    assert not is_safe_regex(r"(a+)+")
    assert not is_safe_regex(r"(.*)*x")
    # Invalid + over-long are refused.
    assert not is_safe_regex(r"(unbalanced")
    assert not is_safe_regex("a" * 2001)
    assert not is_safe_regex("")
    # apply_html_recipe refuses an unsafe pattern rather than running it.
    bad = HtmlCrawlRecipe(listing_url="x", item_regex=r"(a+)+")
    assert apply_html_recipe(bad, "<a>whatever</a>") == []


# --------------------------------------------------------------------------- #
# Opt-in: clean HTML scrapers heal, bespoke HTML scrapers stay detect-only
# --------------------------------------------------------------------------- #


def test_html_recipe_sources_opt_in_bespoke_does_not():
    for src in (veneto.SOURCE, sardegna.SOURCE, piemonte.SOURCE):
        assert src.html_recipe is not None
        assert src.default_recipe is None  # not a JSON listing
    # A bespoke HTML scraper (POST + CSRF) keeps neither recipe -> detect-only.
    assert liguria.SOURCE.html_recipe is None
    assert liguria.SOURCE.default_recipe is None


# --------------------------------------------------------------------------- #
# recipe_store persists an HtmlCrawlRecipe (polymorphic, round-trips by kind)
# --------------------------------------------------------------------------- #


def test_recipe_store_roundtrips_html_recipe():
    blob = recipe_to_json(veneto.VENETO_RECIPE)
    assert json.loads(blob)["_kind"] == "html"
    back = recipe_from_json(blob)
    assert isinstance(back, HtmlCrawlRecipe)
    assert back == veneto.VENETO_RECIPE


# --------------------------------------------------------------------------- #
# End-to-end: the base HTML-recipe path auto-heals a drifted listing
# --------------------------------------------------------------------------- #


def test_base_html_recipe_path_auto_heals_on_drift():
    page = (CASS / "veneto_listing.html").read_text(encoding="utf-8")
    # Rename the anchor target so the baked item_regex misses; the URL template still
    # builds the same detail URL from post_id, so a corrected regex reproduces golden.
    drifted = page.replace("Dettaglio?idAtto=", "Atto?id=")
    new_regex = (
        r'<a[^>]+href="(?:/Public/)?Atto\?id=(?P<post_id>\d+)"'
        r"[^>]*>(?P<title>.*?)</a>"
    )

    src = veneto.VenetoSource()
    store = Store(":memory:")
    rs = RecipeStore(store)
    try:
        # 1) Healthy crawl records the golden.
        src._listing_html = lambda recipe: page
        src._list_details(rs, None)
        assert src.last_crawl_health == "ok"
        assert rs.get_golden("veneto")

        # 2) Drift with NO client -> detect-only, stays broken, nothing adopted.
        src._listing_html = lambda recipe: drifted
        src._list_details(rs, None)
        assert src.last_crawl_health == "broken"
        assert rs.get_recipe("veneto") is None

        # 3) Same drift WITH a healer returning the right regex -> gated auto-adopt.
        src._list_details(rs, _FakeHtmlHealer(new_regex))
        assert src.last_crawl_health == "ok"
        adopted = rs.get_recipe("veneto")
        assert isinstance(adopted, HtmlCrawlRecipe)
        assert adopted.item_regex == new_regex
    finally:
        store.close()
