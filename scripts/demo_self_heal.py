"""Demo: self-healing crawl on a drifted listing (offline, fake LLM — for the writeup).

Shows the loop end to end without network:
  1. a healthy crawl snapshots the last-good refs (the golden);
  2. the listing's item shape DRIFTS (fields renamed) -> the default recipe breaks;
  3. an LLM re-derives the recipe; the deterministic guard validates it against the
     golden and ADOPTS it (config, not code);
  4. the crawl is recovered — and the adoption is auditable.

Run:  uv run python scripts/demo_self_heal.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from bandiradar.recipe_store import RecipeStore  # noqa: E402
from bandiradar.sources.heal import heal_crawl  # noqa: E402
from bandiradar.sources.llm_scraper import apply_recipe, validate_refs  # noqa: E402
from bandiradar.sources.toscana import TOSCANA_RECIPE  # noqa: E402
from bandiradar.storage import Store  # noqa: E402

CASS = REPO / "tests" / "cassettes"
SOURCE = "toscana"


class _FakeHealer:
    """Stands in for the LLM (offline). Returns the corrected dotted paths."""

    def score(self, system: str, user: str) -> dict:
        return {
            "post_id_path": "postId",
            "detail_url_path": "permalink",
            "title_path": "title.text",
        }


def main() -> int:
    golden = json.loads((CASS / "toscana_listing.json").read_text())
    drifted = json.loads((CASS / "toscana_listing_drifted.json").read_text())

    store = Store(":memory:")
    rs = RecipeStore(store)
    try:
        good = apply_recipe(TOSCANA_RECIPE, golden)
        rs.set_golden(SOURCE, good)
        print(
            f"1. healthy crawl:        {validate_refs(good)}  "
            f"({len(good)} refs) — golden snapshotted"
        )

        broken = apply_recipe(TOSCANA_RECIPE, drifted)
        print(
            f"2. listing DRIFTS:       {validate_refs(broken)}  "
            "(default recipe can't read the renamed fields)"
        )

        result = heal_crawl(
            SOURCE, drifted, rs.get_golden(SOURCE), TOSCANA_RECIPE, _FakeHealer(), rs
        )
        print(
            f"3. LLM heal + guard:     status={result.status}  "
            f"adopted={result.adopted}  — {result.reason}"
        )

        healed = rs.get_recipe(SOURCE)
        recovered = apply_recipe(healed, drifted)
        print(
            f"4. recovered crawl:      {validate_refs(recovered)}  "
            f"(matches golden: {recovered == good})"
        )
        audit = rs.audit(SOURCE)
        print(
            f"   adopted recipe paths: post_id={healed.post_id_path} "
            f"detail_url={healed.detail_url_path} title={healed.title_path}"
        )
        print(
            f"   audit: reason={audit['reason']} "
            f"validated_by={audit['validated_by']} at={audit['adopted_at'][:19]}"
        )
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
