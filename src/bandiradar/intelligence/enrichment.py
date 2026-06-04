"""Optional matcher enrichment from ANAC historical benchmarks (Prompt 13).

PURE (given the benchmark lookup): turns a benchmark for an opportunity's CPV
division into extra ``reasons`` (the historical context) and ``risk_notes``
(value-sanity vs the historical distribution). The matcher works fine with no
benchmarks — this is additive.
"""

from __future__ import annotations

from bandiradar.intelligence.benchmarks import Benchmark, lookup
from bandiradar.models import Opportunity


def opportunity_division(opportunity: Opportunity) -> str | None:
    """The CPV division (first 2 digits) of the opportunity's main CPV, or None."""
    if not opportunity.cpv:
        return None
    digits = "".join(ch for ch in opportunity.cpv[0] if ch.isdigit())
    return digits[:2] if len(digits) >= 2 else None


def benchmark_for(opportunity: Opportunity, benchmarks) -> Benchmark | None:
    """Look up the benchmark for the opportunity (CPV division + region/national)."""
    division = opportunity_division(opportunity)
    if division is None:
        return None
    return lookup(benchmarks, division, opportunity.region)


def benchmark_summary(benchmark: Benchmark) -> str:
    """Compact one-line summary for the LLM prompt / reasons."""
    scope = benchmark.region or "national"
    return (
        f"ANAC history (CPV {benchmark.cpv_division}, {scope}): "
        f"{benchmark.count} awards, median EUR {benchmark.value_median:,.0f}, "
        f"p25-p75 EUR {benchmark.value_p25:,.0f}-{benchmark.value_p75:,.0f}"
    )


def enrich(opportunity: Opportunity, benchmarks) -> tuple[list[str], list[str]]:
    """Return (reasons, risk_notes) from the historical benchmark, or ([], [])."""
    benchmark = benchmark_for(opportunity, benchmarks)
    if benchmark is None:
        return [], []

    reasons = [benchmark_summary(benchmark)]

    risk_notes: list[str] = []
    value = opportunity.value_amount
    if value is not None:
        if value > benchmark.value_max:
            risk_notes.append(
                f"estimated value EUR {value:,.0f} exceeds the historical max "
                f"EUR {benchmark.value_max:,.0f} for this category (outlier)"
            )
        elif value > benchmark.value_p75:
            risk_notes.append(
                f"estimated value EUR {value:,.0f} is above the historical p75 "
                f"EUR {benchmark.value_p75:,.0f} for this category"
            )
        elif value < benchmark.value_p25:
            risk_notes.append(
                f"estimated value EUR {value:,.0f} is below the historical p25 "
                f"EUR {benchmark.value_p25:,.0f} for this category"
            )
        else:
            risk_notes.append(
                f"estimated value EUR {value:,.0f} is typical (within p25-p75) "
                f"for this category"
            )
    return reasons, risk_notes
