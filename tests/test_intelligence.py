"""Intelligence track tests (Prompt 12). Offline, tmp db, against the real fixture."""

import statistics

import pytest

from bandiradar.intelligence import anac_history as ah
from bandiradar.intelligence.benchmarks import (
    Benchmark,
    compute_benchmarks,
    lookup,
)
from bandiradar.intelligence.enrichment import enrich
from bandiradar.intelligence.store import BenchmarkStore
from bandiradar.models import Opportunity


def rec(div, region, value, year, supplier):
    return ah.HistoryRecord(
        cpv_division=div, region=region, value=value, year=year, supplier_id=supplier
    )


def bench72(region=None):
    return Benchmark(
        cpv_division="72",
        region=region,
        count=8,
        value_median=104326.0,
        value_p25=71619.0,
        value_p75=183410.0,
        value_min=39532.0,
        value_max=283142.0,
        by_year={2025: 8},
        distinct_suppliers=8,
    )


def opp(cpv, value=None, region=None):
    return Opportunity(
        id="x:1",
        source="x",
        source_url="https://example.invalid/x",
        kind="tender",
        title="t",
        geo_scope="eu",
        region=region,
        status="open",
        raw_ref="x:1",
        cpv=cpv,
        value_amount=value,
    )


@pytest.fixture
def store(tmp_path):
    s = BenchmarkStore(str(tmp_path / "bench.db"))
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# parse_record against the real fixture
# --------------------------------------------------------------------------- #


def test_parse_records_from_fixture():
    records = ah.load_fixture()
    assert records  # non-empty
    for r in records:
        assert len(r.cpv_division) == 2 and r.cpv_division.isdigit()
        assert isinstance(r.value, float) and r.value > 0
        assert isinstance(r.year, int) and 2000 <= r.year <= 2100
        assert r.region is None  # this source has no region/NUTS field
    # division 45 (construction) is the most common awarded division here.
    assert sum(1 for r in records if r.cpv_division == "45") == 22


def test_parse_record_skips_releases_without_cpv_or_value():
    assert ah.parse_record({"tender": {}, "awards": []}) == []
    # CPV present but award has no value -> skipped.
    rel = {
        "tender": {"items": [{"classification": {"id": "72000000-1"}}]},
        "awards": [{"date": "2024-01-01T00:00:00Z"}],
    }
    assert ah.parse_record(rel) == []


# --------------------------------------------------------------------------- #
# compute_benchmarks
# --------------------------------------------------------------------------- #


def test_compute_benchmarks_matches_fixture_stats():
    records = ah.load_fixture()
    benchmarks = compute_benchmarks(records)
    by_key = {(b.cpv_division, b.region): b for b in benchmarks}

    div45 = [r.value for r in records if r.cpv_division == "45"]
    b = by_key[("45", None)]  # national aggregate
    assert b.count == len(div45)
    assert b.value_median == statistics.median(div45)
    q = statistics.quantiles(sorted(div45), n=4, method="inclusive")
    assert b.value_p25 == q[0] and b.value_p75 == q[2]
    assert b.value_min == min(div45) and b.value_max == max(div45)
    assert b.distinct_suppliers == len(
        {r.supplier_id for r in records if r.cpv_division == "45" and r.supplier_id}
    )
    assert sum(b.by_year.values()) == b.count


def test_compute_benchmarks_emits_national_and_regional():
    # Synthetic records WITH regions exercise the regional + national split.
    recs = [
        rec("72", "Lazio", 100.0, 2024, "a"),
        rec("72", "Lazio", 300.0, 2024, "b"),
        rec("72", "Puglia", 200.0, 2023, "a"),
    ]
    out = compute_benchmarks(recs)
    by_key = {(b.cpv_division, b.region): b for b in out}
    assert ("72", None) in by_key  # national aggregate
    assert ("72", "Lazio") in by_key and ("72", "Puglia") in by_key
    nat = by_key[("72", None)]
    assert nat.count == 3
    assert nat.value_median == 200.0
    assert nat.distinct_suppliers == 2  # {a, b}
    assert nat.by_year == {2023: 1, 2024: 2}
    lazio = by_key[("72", "Lazio")]
    assert lazio.count == 2 and lazio.value_median == 200.0


def test_compute_benchmarks_is_deterministic():
    recs = ah.load_fixture()
    a = [b.model_dump() for b in compute_benchmarks(recs)]
    b = [b.model_dump() for b in compute_benchmarks(recs)]
    assert a == b


# --------------------------------------------------------------------------- #
# lookup national fallback
# --------------------------------------------------------------------------- #


def test_lookup_national_fallback_on_list():
    out = compute_benchmarks([rec("72", "Lazio", 100.0, 2024, "a")])
    # Lazio bucket exists.
    assert lookup(out, "72", "Lazio") is not None
    # Unknown region -> falls back to national aggregate.
    fallback = lookup(out, "72", "Campania")
    assert fallback is not None and fallback.region is None
    # Unknown division -> nothing.
    assert lookup(out, "99", "Lazio") is None


# --------------------------------------------------------------------------- #
# BenchmarkStore round-trip
# --------------------------------------------------------------------------- #


def test_store_roundtrip(store):
    recs = ah.load_fixture()
    benchmarks = compute_benchmarks(recs)
    store.save_benchmarks(benchmarks)

    got = store.get_benchmark("45", None)
    assert got is not None
    original = next(
        b for b in benchmarks if b.cpv_division == "45" and b.region is None
    )
    assert got == original
    assert len(store.list_benchmarks()) == len(benchmarks)

    # Store-level national fallback via lookup.
    assert lookup(store, "45", "Lazio").region is None


def test_store_national_and_regional_keys(store):
    store.save_benchmarks(
        [
            Benchmark(
                cpv_division="72",
                region=None,
                count=1,
                value_median=1.0,
                value_p25=1.0,
                value_p75=1.0,
                value_min=1.0,
                value_max=1.0,
                by_year={2024: 1},
                distinct_suppliers=1,
            ),
            Benchmark(
                cpv_division="72",
                region="Lazio",
                count=1,
                value_median=2.0,
                value_p25=2.0,
                value_p75=2.0,
                value_min=2.0,
                value_max=2.0,
                by_year={2024: 1},
                distinct_suppliers=1,
            ),
        ]
    )
    assert store.get_benchmark("72", None).value_median == 1.0
    assert store.get_benchmark("72", "Lazio").value_median == 2.0


# --------------------------------------------------------------------------- #
# enrich() — matcher enrichment (Prompt 13)
# --------------------------------------------------------------------------- #


def test_enrich_no_cpv_returns_empty():
    assert enrich(opp(cpv=[], value=50000.0), [bench72()]) == ([], [])


def test_enrich_adds_history_reason():
    reasons, risk = enrich(opp(cpv=["72322000"]), [bench72()])
    assert len(reasons) == 1
    assert "ANAC history (CPV 72, national)" in reasons[0]
    assert "8 awards" in reasons[0]
    assert risk == []  # no value -> no value-sanity note


def test_enrich_value_sanity_buckets():
    bench = [bench72()]  # p25 71619, p75 183410, max 283142
    _, typical = enrich(opp(cpv=["72322000"], value=120000.0), bench)
    assert "typical" in typical[0]
    _, low = enrich(opp(cpv=["72322000"], value=10000.0), bench)
    assert "below the historical p25" in low[0]
    _, high = enrich(opp(cpv=["72322000"], value=200000.0), bench)
    assert "above the historical p75" in high[0]
    _, outlier = enrich(opp(cpv=["72322000"], value=500000.0), bench)
    assert "exceeds the historical max" in outlier[0] and "outlier" in outlier[0]


def test_enrich_unknown_division_returns_empty():
    assert enrich(opp(cpv=["99999999"]), [bench72()]) == ([], [])


def test_enrich_national_fallback_when_region_set():
    # Only a national (region=None) benchmark exists; a regional opp still matches.
    reasons, _ = enrich(opp(cpv=["72322000"], region="Lazio"), [bench72()])
    assert reasons and "national" in reasons[0]
