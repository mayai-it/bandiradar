"""Compute compact benchmarks per (CPV-division x region) from HistoryRecords.

PURE aggregation (``compute_benchmarks``) + a ``lookup`` with national fallback.
What we derive (and, honestly, what we don't): value distribution, volume,
seasonality (by year), and distinct-supplier counts — NOT a bidder count (the
award data has no tenderers list).
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Protocol

from pydantic import BaseModel

from bandiradar.intelligence.anac_history import HistoryRecord


class Benchmark(BaseModel):
    """Compact stats for one (cpv_division, region) bucket.

    ``region is None`` denotes the NATIONAL aggregate for that CPV division.
    """

    cpv_division: str
    region: str | None
    count: int
    value_median: float
    value_p25: float
    value_p75: float
    value_min: float
    value_max: float
    by_year: dict[int, int]  # year -> number of awards
    distinct_suppliers: int


class _BenchmarkSource(Protocol):
    def get_benchmark(
        self, cpv_division: str, region: str | None
    ) -> Benchmark | None: ...


def _aggregate(
    cpv_division: str, region: str | None, records: list[HistoryRecord]
) -> Benchmark:
    values = sorted(r.value for r in records)
    median = statistics.median(values)
    if len(values) >= 2:
        quartiles = statistics.quantiles(values, n=4, method="inclusive")
        p25, p75 = quartiles[0], quartiles[2]
    else:
        p25 = p75 = median
    by_year = dict(sorted(Counter(r.year for r in records).items()))
    suppliers = {r.supplier_id for r in records if r.supplier_id}
    return Benchmark(
        cpv_division=cpv_division,
        region=region,
        count=len(values),
        value_median=median,
        value_p25=p25,
        value_p75=p75,
        value_min=values[0],
        value_max=values[-1],
        by_year=by_year,
        distinct_suppliers=len(suppliers),
    )


def compute_benchmarks(records: list[HistoryRecord]) -> list[Benchmark]:
    """Group by (cpv_division, region); also emit the (cpv_division, None) national
    aggregate over all records of that division. Deterministic (sorted)."""
    national: dict[str, list[HistoryRecord]] = defaultdict(list)
    regional: dict[tuple[str, str], list[HistoryRecord]] = defaultdict(list)
    for r in records:
        national[r.cpv_division].append(r)
        if r.region is not None:
            regional[(r.cpv_division, r.region)].append(r)

    out: list[Benchmark] = [
        _aggregate(div, None, national[div]) for div in sorted(national)
    ]
    out.extend(
        _aggregate(div, region, regional[(div, region)])
        for div, region in sorted(regional)
    )
    return out


def lookup(
    source: _BenchmarkSource | list[Benchmark],
    cpv_division: str,
    region: str | None,
) -> Benchmark | None:
    """Return the benchmark for (cpv_division, region), falling back to the
    national aggregate (region=None) when the regional bucket is absent."""
    if isinstance(source, list):
        index = {(b.cpv_division, b.region): b for b in source}
        return index.get((cpv_division, region)) or index.get((cpv_division, None))

    found = source.get_benchmark(cpv_division, region)
    if found is None and region is not None:
        found = source.get_benchmark(cpv_division, None)
    return found
