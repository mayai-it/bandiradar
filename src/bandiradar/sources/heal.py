"""Self-healing crawl — LLM re-derives a CrawlRecipe, a DETERMINISTIC guard adopts it.

When a scraper's crawl drifts (``validate_refs != ok``), :func:`heal_crawl` asks an LLM
to re-derive the recipe (DATA only: dotted field paths + url/params), then the spine's
:func:`recipe_reproduces_golden` guard decides:
  - exact match to the last-good refs  -> AUTO-ADOPT into the recipe store;
  - ok refs but not an exact match     -> candidate available, needs human confirm;
  - still broken                       -> auto-heal failed, needs a human.
The LLM proposes only data; it can never bypass the guard. Generic — Toscana first user.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from bandiradar.crawl import (
    CrawlRecipe,
    DetailRef,
    apply_recipe,
    recipe_reproduces_golden,
    validate_refs,
)
from bandiradar.matching.llm import LLMClient
from bandiradar.recipe_store import RecipeStore

# The three fields a crawl needs from each listing item.
TARGET_FIELDS = ("post_id", "detail_url", "title")

HealStatus = Literal["healed", "needs_review", "failed"]

HEAL_SYSTEM = (
    "You repair the CRAWL recipe for a listing API whose item shape changed. You are "
    "given ONE example item (JSON) from the live listing and the three fields a "
    "crawler must read from each item: post_id, detail_url, title. Respond with "
    "STRICT JSON and nothing else:\n"
    "{\n"
    '  "post_id_path": <dotted path to the id in the item>,\n'
    '  "detail_url_path": <dotted path to the detail URL>,\n'
    '  "title_path": <dotted path to the title text>\n'
    "}\n"
    'Paths are dot-separated keys into the item (e.g. "title.rendered"). Use ONLY keys '
    "that exist in the example item. Output data only — no prose."
)


@dataclass(frozen=True)
class HealResult:
    """Outcome of an attempted self-heal (auditable, surfaced by doctor)."""

    status: HealStatus
    adopted: bool
    reason: str
    candidate: CrawlRecipe | None = None


def _as_path(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def propose_recipe(
    live_item: dict[str, Any],
    current_recipe: CrawlRecipe,
    client: LLMClient,
) -> CrawlRecipe | None:
    """Ask the LLM for the new dotted paths for ONE live listing item. STRICT parse
    into a CrawlRecipe (data only); ``None`` if the reply lacks all three paths.

    listing_url/params are carried over from the current recipe (the healer fixes the
    item SHAPE; a moved endpoint is out of scope for the auto path)."""
    user = (
        "Live listing item (JSON):\n"
        + json.dumps(live_item, ensure_ascii=False)
        + "\n\nThe current (now-broken) recipe paths were: "
        + json.dumps(
            {
                "post_id_path": current_recipe.post_id_path,
                "detail_url_path": current_recipe.detail_url_path,
                "title_path": current_recipe.title_path,
            }
        )
        + "\n\nReturn ONLY the JSON object with the corrected paths."
    )
    raw = client.score(HEAL_SYSTEM, user)
    if not isinstance(raw, dict):
        return None
    post_id = _as_path(raw.get("post_id_path"))
    detail_url = _as_path(raw.get("detail_url_path"))
    title = _as_path(raw.get("title_path"))
    if not (post_id and detail_url and title):
        return None  # strict: all three required
    return CrawlRecipe(
        listing_url=current_recipe.listing_url,
        params=dict(current_recipe.params),
        post_id_path=post_id,
        detail_url_path=detail_url,
        title_path=title,
    )


def heal_crawl(
    source_id: str,
    live_listing: list[Any],
    expected_refs: list[DetailRef],
    current_recipe: CrawlRecipe,
    client: LLMClient,
    recipe_store: RecipeStore,
) -> HealResult:
    """Attempt to heal a drifted crawl. Adopts a candidate ONLY if it reproduces the
    known-good refs exactly (the spine guard). Never adopts otherwise."""
    if not live_listing:
        return HealResult("failed", False, "empty live listing — nothing to heal")
    if not expected_refs:
        return HealResult("failed", False, "no last-good golden to validate against")

    candidate = propose_recipe(live_listing[0], current_recipe, client)
    if candidate is None:
        return HealResult("failed", False, "healer returned no usable recipe")

    # STRONG guard (spine slice 1): exact reproduction of the last-good refs.
    if recipe_reproduces_golden(candidate, live_listing, expected_refs):
        recipe_store.adopt(
            source_id, candidate, reason="drift-heal", validated_by="golden-exact"
        )
        return HealResult(
            "healed", True, "candidate reproduces the last-good refs exactly", candidate
        )

    # Candidate parses the listing cleanly but doesn't match the golden — the content
    # likely changed (new/removed bandi). Do NOT auto-adopt; surface for a human.
    if validate_refs(apply_recipe(candidate, live_listing)) == "ok":
        return HealResult(
            "needs_review",
            False,
            "candidate yields valid refs but they differ from last-good "
            "(content may have changed) — human confirmation required",
            candidate,
        )

    return HealResult(
        "failed", False, "candidate still broken — human required", candidate
    )
