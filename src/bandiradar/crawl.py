"""Self-healing spine — crawl recipes + drift detection + golden validation.

Generic, dependency-free framework (no I/O, no adapters). Lives at the top level so
``recipe_store`` / ``heal`` can use it without importing the ``sources`` package
(which would create an import cycle). Re-exported by ``sources.llm_scraper`` for
back-compat.

The FRAGILE part of an LLM scraper is the CRAWL (the listing it depends on), not the
extraction (the LLM adapts to HTML changes already). So the crawl is made a DATA
recipe — validatable and replaceable — with a drift detector and a golden validator.
A future agent can re-derive a broken recipe; it must produce one that passes
:func:`recipe_reproduces_golden` — the deterministic socket it cannot bypass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# One listing item -> (post_id, detail_url, listing_title).
DetailRef = tuple[Any, str, str]

# Crawl health: ok (all refs usable) / degraded (some empty) / broken (none usable).
Health = Literal["ok", "degraded", "broken"]


@dataclass(frozen=True)
class CrawlRecipe:
    """How to crawl a scraper's listing — DATA, not code, so it can be validated and
    swapped (e.g. re-derived by an agent) without touching the adapter.

    ``listing_url`` + ``params`` describe the request; the ``*_path`` fields are
    DOTTED paths into each listing item (e.g. ``"title.rendered"``) for the three
    fields the crawl needs. :func:`apply_recipe` reads them — no hardcoded parse.
    """

    listing_url: str
    params: dict[str, Any] = field(default_factory=dict)
    post_id_path: str = "id"
    detail_url_path: str = "link"
    title_path: str = "title.rendered"


def _dig(item: Any, path: str) -> Any:
    """Follow a dotted path into nested dicts; ``None`` if any step is missing."""
    cur = item
    for key in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def apply_recipe(recipe: CrawlRecipe, listing_json: Any) -> list[DetailRef]:
    """PURE: turn a listing JSON (a list of items) into DetailRefs per the recipe.

    Replaces the hardcoded listing parse. Tolerant: a missing field becomes ``""``
    (string) or ``None`` (post_id) so drift surfaces via :func:`validate_refs`
    rather than a crash."""
    if not isinstance(listing_json, list):
        return []
    refs: list[DetailRef] = []
    for item in listing_json:
        post_id = _dig(item, recipe.post_id_path)
        url = _dig(item, recipe.detail_url_path)
        title = _dig(item, recipe.title_path)
        refs.append(
            (
                post_id,
                str(url) if url is not None else "",
                str(title) if title is not None else "",
            )
        )
    return refs


def validate_refs(refs: list[DetailRef]) -> Health:
    """PURE drift detector. A ref is USABLE iff it has a non-empty url AND title.

    broken = no refs, or none usable (the listing shape/fields drifted);
    degraded = some usable, some empty; ok = all usable."""
    if not refs:
        return "broken"
    usable = sum(1 for (_pid, url, title) in refs if url.strip() and title.strip())
    if usable == 0:
        return "broken"
    if usable < len(refs):
        return "degraded"
    return "ok"


def recipe_reproduces_golden(
    recipe: CrawlRecipe,
    golden_listing: Any,
    expected_refs: list[DetailRef],
) -> bool:
    """PURE golden-sample validator: a candidate recipe is valid IFF it reproduces
    the known-good refs from a recorded golden listing. The gate an agent-derived
    recipe must pass before it can replace the current one."""
    return apply_recipe(recipe, golden_listing) == [tuple(r) for r in expected_refs]
