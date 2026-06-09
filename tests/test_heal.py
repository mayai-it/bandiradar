"""Self-healing crawl tests — recipe store + LLM healer + gated adoption.

Offline, no LLM, no network: the healer is a FAKE returning dotted paths. The
"drifted" listing is the golden cassette with its fields renamed
(id->postId, link->permalink, title.rendered->title.text)."""

import json
from pathlib import Path

from bandiradar.recipe_store import RecipeStore
from bandiradar.sources.heal import heal_crawl, propose_recipe
from bandiradar.sources.llm_scraper import (
    CrawlRecipe,
    apply_recipe,
    validate_refs,
)
from bandiradar.sources.toscana import TOSCANA_RECIPE
from bandiradar.storage import Store

CASS = Path(__file__).parent / "cassettes"
SOURCE = "toscana"
RIGHT = {
    "post_id_path": "postId",
    "detail_url_path": "permalink",
    "title_path": "title.text",
}
WRONG = {"post_id_path": "x", "detail_url_path": "y", "title_path": "z"}


def golden() -> list:
    return json.loads((CASS / "toscana_listing.json").read_text(encoding="utf-8"))


def drifted() -> list:
    return json.loads(
        (CASS / "toscana_listing_drifted.json").read_text(encoding="utf-8")
    )


class _FakeHealer:
    def __init__(self, paths: dict):
        self.paths = paths

    def score(self, system: str, user: str) -> dict:
        return self.paths


def _store() -> Store:
    return Store(":memory:")


# --------------------------------------------------------------------------- #
# recipe store
# --------------------------------------------------------------------------- #


def test_recipe_store_roundtrip_and_audit():
    s = _store()
    try:
        rs = RecipeStore(s)
        assert rs.get_recipe(SOURCE) is None  # default until adopted
        assert rs.audit(SOURCE) is None
        rec = CrawlRecipe(listing_url="u", post_id_path="postId")
        rs.adopt(SOURCE, rec, reason="drift-heal", validated_by="golden-exact")
        assert rs.get_recipe(SOURCE) == rec
        audit = rs.audit(SOURCE)
        assert audit["recipe"] == rec
        assert (
            audit["reason"] == "drift-heal" and audit["validated_by"] == "golden-exact"
        )
        # golden snapshot roundtrip (tuples preserved)
        refs = apply_recipe(TOSCANA_RECIPE, golden())
        rs.set_golden(SOURCE, refs)
        assert rs.get_golden(SOURCE) == refs
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# drift + healer + gated adoption
# --------------------------------------------------------------------------- #


def test_default_recipe_is_broken_on_drift():
    assert validate_refs(apply_recipe(TOSCANA_RECIPE, drifted())) == "broken"


def test_propose_recipe_strict_parse():
    item = drifted()[0]
    rec = propose_recipe(item, TOSCANA_RECIPE, _FakeHealer(RIGHT))
    assert rec.post_id_path == "postId" and rec.detail_url_path == "permalink"
    assert rec.listing_url == TOSCANA_RECIPE.listing_url  # carried over
    # missing a required path -> None (strict)
    assert (
        propose_recipe(item, TOSCANA_RECIPE, _FakeHealer({"post_id_path": "postId"}))
        is None
    )


def test_heal_adopts_when_guard_passes():
    s = _store()
    try:
        rs = RecipeStore(s)
        expected = apply_recipe(TOSCANA_RECIPE, golden())  # last-good refs
        rs.set_golden(SOURCE, expected)
        result = heal_crawl(
            SOURCE, drifted(), expected, TOSCANA_RECIPE, _FakeHealer(RIGHT), rs
        )
        assert result.status == "healed" and result.adopted is True
        # adopted into the store (config), and the crawl is recovered exactly
        healed = rs.get_recipe(SOURCE)
        assert healed is not None
        assert apply_recipe(healed, drifted()) == expected
        assert rs.audit(SOURCE)["validated_by"] == "golden-exact"
    finally:
        s.close()


def test_heal_rejects_wrong_paths_recipe_unchanged():
    s = _store()
    try:
        rs = RecipeStore(s)
        expected = apply_recipe(TOSCANA_RECIPE, golden())
        result = heal_crawl(
            SOURCE, drifted(), expected, TOSCANA_RECIPE, _FakeHealer(WRONG), rs
        )
        assert result.status == "failed" and result.adopted is False
        assert rs.get_recipe(SOURCE) is None  # NOT adopted — recipe unchanged
        # what doctor would surface: still broken -> needs a human
        assert validate_refs(apply_recipe(TOSCANA_RECIPE, drifted())) == "broken"
    finally:
        s.close()


def test_heal_needs_review_when_ok_but_not_exact_match():
    s = _store()
    try:
        rs = RecipeStore(s)
        # golden expects an EXTRA ref that the (correctly parsed) drift won't contain
        expected = apply_recipe(TOSCANA_RECIPE, golden()) + [(999, "u", "t")]
        result = heal_crawl(
            SOURCE, drifted(), expected, TOSCANA_RECIPE, _FakeHealer(RIGHT), rs
        )
        assert result.status == "needs_review" and result.adopted is False
        assert rs.get_recipe(SOURCE) is None  # human confirmation required
    finally:
        s.close()


def test_heal_fails_without_a_golden():
    s = _store()
    try:
        rs = RecipeStore(s)
        result = heal_crawl(
            SOURCE, drifted(), [], TOSCANA_RECIPE, _FakeHealer(RIGHT), rs
        )
        assert result.status == "failed" and result.adopted is False
    finally:
        s.close()
