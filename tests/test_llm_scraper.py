"""Self-healing spine tests — crawl recipes, drift detection, golden validator.

Offline, no LLM, no network: the golden listing is the recorded cassette
``tests/cassettes/toscana_listing.json``."""

import json
from pathlib import Path

from bandiradar import core
from bandiradar.sources import toscana
from bandiradar.sources.llm_scraper import (
    CrawlRecipe,
    apply_recipe,
    recipe_reproduces_golden,
    validate_refs,
)

GOLDEN = Path(__file__).parent / "cassettes" / "toscana_listing.json"


def golden_listing() -> list:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# apply_recipe — the data recipe reproduces the known-good refs
# --------------------------------------------------------------------------- #


def test_apply_recipe_reproduces_known_refs_from_golden():
    listing = golden_listing()
    refs = apply_recipe(toscana.TOSCANA_RECIPE, listing)
    assert len(refs) == len(listing) and refs  # one ref per listing item
    # each ref matches the item's id / link / title.rendered
    for ref, item in zip(refs, listing, strict=True):
        assert ref == (item["id"], item["link"], item["title"]["rendered"])
    assert validate_refs(refs) == "ok"


def test_apply_recipe_tolerates_a_non_list():
    assert apply_recipe(toscana.TOSCANA_RECIPE, {"not": "a list"}) == []


# --------------------------------------------------------------------------- #
# validate_refs — drift detector
# --------------------------------------------------------------------------- #


def test_validate_refs_health_levels():
    assert validate_refs([]) == "broken"  # zero refs
    assert validate_refs([(1, "", ""), (2, "", "")]) == "broken"  # all empty
    assert validate_refs([(1, "u", "")]) == "broken"  # title empty -> unusable
    assert validate_refs([(1, "u", "t"), (2, "", "")]) == "degraded"  # some empty
    assert validate_refs([(1, "u", "t"), (2, "v", "s")]) == "ok"


def test_wrong_recipe_drifts_to_broken_on_golden():
    # A recipe with wrong field paths yields empty links/titles -> broken.
    wrong = CrawlRecipe(listing_url="x", detail_url_path="nope", title_path="also.nope")
    assert validate_refs(apply_recipe(wrong, golden_listing())) == "broken"


# --------------------------------------------------------------------------- #
# recipe_reproduces_golden — the gate a (future agent-derived) recipe must pass
# --------------------------------------------------------------------------- #


def test_golden_validator_accepts_default_rejects_wrong():
    listing = golden_listing()
    expected = apply_recipe(toscana.TOSCANA_RECIPE, listing)  # known-good refs
    assert recipe_reproduces_golden(toscana.TOSCANA_RECIPE, listing, expected) is True
    # a wrong recipe (detail_url from the wrong field) does NOT reproduce them
    wrong = CrawlRecipe(
        listing_url=toscana.TOSCANA_LIST_URL,
        post_id_path="id",
        detail_url_path="slug",  # WP item has no "slug" in this listing
        title_path="title.rendered",
    )
    assert recipe_reproduces_golden(wrong, listing, expected) is False


# --------------------------------------------------------------------------- #
# doctor surfaces crawl health key-lessly (drift visible without an LLM key)
# --------------------------------------------------------------------------- #


def test_doctor_surfaces_crawl_health_without_key(monkeypatch, tmp_path):
    class _Scraper:
        id = "toscana"
        kind = "incentive"
        requires_llm = True

        def crawl_health(self):
            return "degraded"

        def load_fixture(self):
            return []

    source = _Scraper()
    monkeypatch.setattr(core, "list_sources", lambda: [source])
    monkeypatch.setattr(core, "get", lambda _sid: source)
    # conftest forces BANDIRADAR_LLM_PROVIDER=none -> needs_key branch (not probed)
    report = core.run_doctor(db=str(tmp_path / "d.db"))
    r = report.sources[0]
    assert r.status == "needs_key"
    assert r.crawl_health == "degraded"
    assert "crawl: degraded" in (r.note or "")
