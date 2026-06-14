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

import html as _html
import re
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


# --------------------------------------------------------------------------- #
# HTML-listing recipe (Phase 2): the parse as a DATA regex-template, not code.
# Same propose/dispose as the JSON recipe — the LLM re-derives ``item_regex`` and a
# candidate is adopted ONLY if it reproduces the golden exactly. The regex is read
# by the fixed, audited ``re`` engine (no code execution); the one extra risk is
# ReDoS during evaluation, bounded by :func:`is_safe_regex`.
# --------------------------------------------------------------------------- #

_HTML_TAG_RE = re.compile(r"<[^>]+>")
MAX_ITEM_REGEX_LEN = 2000  # bound an LLM-proposed pattern (ReDoS guard)


def _clean_title(raw: str) -> str:
    """Strip tags, unescape entities, collapse whitespace — the title cleanup every
    HTML listing parser applied, now shared so a recipe reproduces the same refs."""
    return _html.unescape(re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", raw or ""))).strip()


@dataclass(frozen=True)
class HtmlCrawlRecipe:
    """How to parse an HTML listing — DATA (a regex-template), so it is validatable
    and re-derivable by the healer without touching adapter code.

    ``item_regex`` is a regex with NAMED groups: ``post_id`` and ``title`` are read
    directly (title is tag-stripped); the detail URL is built from
    ``url_template`` (``{base}`` + any captured group, e.g. ``{post_id}`` or a
    captured ``{path}``). The listing FETCH (which page(s), params, POST/auth) stays
    in the source's code — only the PARSE is data."""

    listing_url: str
    base_url: str = ""
    item_regex: str = ""
    url_template: str = "{base}{url}"
    params: dict[str, Any] = field(default_factory=dict)


def is_safe_regex(pattern: str) -> bool:
    """Heuristic ReDoS guard for an LLM-proposed ``item_regex``. The golden gate is
    the real correctness check; this only refuses patterns that are over-long, fail to
    compile, or carry a nested quantifier (``(…+)+`` etc.) prone to catastrophic
    backtracking. Conservative: a refused candidate just isn't auto-adopted."""
    if not pattern or len(pattern) > MAX_ITEM_REGEX_LEN:
        return False
    # Nested quantifier on a group: (…+)+ / (…*)* / (…+)* / (…*)+ — the classic ReDoS.
    if re.search(r"\([^)]*[+*][^)]*\)\s*[*+]", pattern):
        return False
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def apply_html_recipe(recipe: HtmlCrawlRecipe, page_html: str) -> list[DetailRef]:
    """PURE: HTML listing -> DetailRefs per the recipe (regex + url-template).

    Deduplicates by ``post_id`` (first occurrence wins; a later match may fill an
    empty title). Tolerant: a non-compiling regex or a bad template yields no/empty
    refs so drift surfaces via :func:`validate_refs` rather than a crash."""
    if not recipe.item_regex or not is_safe_regex(recipe.item_regex):
        return []
    rx = re.compile(recipe.item_regex, re.S | re.I)
    title_by_id: dict[str, str] = {}
    order: list[tuple[str, str]] = []  # (post_id, url) first-seen
    for m in rx.finditer(page_html or ""):
        groups = {k: (v or "") for k, v in m.groupdict().items()}
        post_id = groups.get("post_id", "")
        title = _clean_title(groups.get("title", ""))
        try:
            url = recipe.url_template.format(base=recipe.base_url, **groups)
        except (KeyError, IndexError):
            url = ""
        if post_id not in title_by_id:
            title_by_id[post_id] = title
            order.append((post_id, url))
        elif title and not title_by_id[post_id]:
            title_by_id[post_id] = title
    return [(pid, url, title_by_id[pid]) for pid, url in order]


def html_recipe_reproduces_golden(
    recipe: HtmlCrawlRecipe,
    golden_html: str,
    expected_refs: list[DetailRef],
) -> bool:
    """PURE golden validator for an HTML recipe (the HTML twin of
    :func:`recipe_reproduces_golden`)."""
    return apply_html_recipe(recipe, golden_html) == [tuple(r) for r in expected_refs]
