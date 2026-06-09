"""Per-source CrawlRecipe overrides + the drift-heal golden — CONFIG, never code.

A scraper loads its crawl recipe from here (override if present, else the baked
default). When the LLM healer re-derives a recipe and the deterministic guard passes,
it is ADOPTED here — auditable ({recipe, adopted_at, reason, validated_by}), never a
code change. Every healthy crawl snapshots its refs as ``last_good`` — the golden the
guard validates the next candidate against.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

from bandiradar.crawl import CrawlRecipe, DetailRef
from bandiradar.storage import Store


def recipe_to_json(recipe: CrawlRecipe) -> str:
    return json.dumps(dataclasses.asdict(recipe), ensure_ascii=False)


def recipe_from_json(blob: str) -> CrawlRecipe:
    return CrawlRecipe(**json.loads(blob))


class RecipeStore:
    """SQLite-backed crawl-recipe overrides + last-good-refs golden."""

    def __init__(self, store: Store) -> None:
        self.store = store

    # -- recipe override ---------------------------------------------------- #

    def get_recipe(self, source_id: str) -> CrawlRecipe | None:
        row = self.store.conn.execute(
            "SELECT recipe FROM crawl_recipes WHERE source_id=?", (source_id,)
        ).fetchone()
        return recipe_from_json(row["recipe"]) if row else None

    def adopt(
        self,
        source_id: str,
        recipe: CrawlRecipe,
        *,
        reason: str,
        validated_by: str,
    ) -> None:
        """Adopt a (guard-validated) recipe override. Auditable; overwrites prior."""
        self.store.conn.execute(
            "INSERT INTO crawl_recipes (source_id, recipe, adopted_at, reason, "
            "validated_by) VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_id) DO UPDATE "
            "SET recipe=excluded.recipe, adopted_at=excluded.adopted_at, "
            "reason=excluded.reason, validated_by=excluded.validated_by",
            (
                source_id,
                recipe_to_json(recipe),
                datetime.now(tz=UTC).isoformat(),
                reason,
                validated_by,
            ),
        )
        self.store.conn.commit()

    def audit(self, source_id: str) -> dict | None:
        """The adoption record for a source, or None if it uses the baked default."""
        row = self.store.conn.execute(
            "SELECT recipe, adopted_at, reason, validated_by FROM crawl_recipes "
            "WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "recipe": recipe_from_json(row["recipe"]),
            "adopted_at": row["adopted_at"],
            "reason": row["reason"],
            "validated_by": row["validated_by"],
        }

    # -- last-good-refs golden --------------------------------------------- #

    def set_golden(self, source_id: str, refs: list[DetailRef]) -> None:
        self.store.conn.execute(
            "INSERT INTO crawl_golden (source_id, refs, saved_at) VALUES (?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET refs=excluded.refs, "
            "saved_at=excluded.saved_at",
            (
                source_id,
                json.dumps(refs, ensure_ascii=False),
                datetime.now(tz=UTC).isoformat(),
            ),
        )
        self.store.conn.commit()

    def get_golden(self, source_id: str) -> list[DetailRef] | None:
        row = self.store.conn.execute(
            "SELECT refs FROM crawl_golden WHERE source_id=?", (source_id,)
        ).fetchone()
        if not row:
            return None
        return [tuple(r) for r in json.loads(row["refs"])]
