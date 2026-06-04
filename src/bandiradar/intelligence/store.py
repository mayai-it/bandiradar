"""SQLite store for benchmarks — separate from the opportunity Store.

Own ``benchmarks`` table; same DB-path convention (env BANDIRADAR_DB or
~/.bandiradar/bandiradar.db). National aggregates (region=None) are stored with
an empty-string region key so they fit a composite primary key.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from bandiradar.intelligence.benchmarks import Benchmark

_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmarks (
    cpv_division TEXT NOT NULL,
    region       TEXT NOT NULL,   -- '' = national aggregate
    count        INTEGER NOT NULL,
    value_median REAL,
    data         TEXT NOT NULL,
    PRIMARY KEY (cpv_division, region)
);
"""


def _default_db_path() -> str:
    return os.environ.get("BANDIRADAR_DB") or str(
        Path.home() / ".bandiradar" / "bandiradar.db"
    )


def _region_key(region: str | None) -> str:
    return region if region is not None else ""


class BenchmarkStore:
    """SQLite-backed persistence for benchmarks (intelligence track)."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path if db_path is not None else _default_db_path()
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def save_benchmarks(self, benchmarks: list[Benchmark]) -> None:
        self.conn.executemany(
            "INSERT INTO benchmarks "
            "(cpv_division, region, count, value_median, data) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(cpv_division, region) DO UPDATE SET "
            "count=excluded.count, value_median=excluded.value_median, "
            "data=excluded.data",
            [
                (
                    b.cpv_division,
                    _region_key(b.region),
                    b.count,
                    b.value_median,
                    b.model_dump_json(),
                )
                for b in benchmarks
            ],
        )
        self.conn.commit()

    def get_benchmark(
        self, cpv_division: str, region: str | None
    ) -> Benchmark | None:
        row = self.conn.execute(
            "SELECT data FROM benchmarks WHERE cpv_division=? AND region=?",
            (cpv_division, _region_key(region)),
        ).fetchone()
        return Benchmark.model_validate_json(row["data"]) if row else None

    def list_benchmarks(self) -> list[Benchmark]:
        rows = self.conn.execute(
            "SELECT data FROM benchmarks ORDER BY cpv_division, region"
        ).fetchall()
        return [Benchmark.model_validate_json(r["data"]) for r in rows]
