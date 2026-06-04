"""Ingest ANAC historical OCDS award data into compact HistoryRecords.

PURE parsing (``parse_record``) + a memory-safe streaming live fetch
(``fetch_live``) over the Open Contracting mirror, plus an offline fixture
reader. ``build_benchmarks`` is the thin orchestration the CLI calls.

Data shape (verified against a real capture, not guessed): CPV lives in
``tender.items[].classification.id`` (e.g. "44130000-0"); award value in
``award.value.amount``; supplier in ``award.suppliers[].id``; year from
``award.date``. The release address has no region/NUTS field, so region is None.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel

if TYPE_CHECKING:
    from bandiradar.intelligence.store import BenchmarkStore

# Open Contracting mirror of ANAC OCDS (CC BY 4.0, no auth). One compiled release
# per line, gzipped JSONL.
OCP_ANAC_URL_TEMPLATE = (
    "https://data.open-contracting.org/en/publication/117/download?name={year}.jsonl.gz"
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "fixtures" / "anac_history.jsonl"
)


class HistoryRecord(BaseModel):
    """One awarded-contract observation extracted from an OCDS release."""

    cpv_division: str  # first 2 digits of the tender's main CPV
    region: str | None  # buyer region, or None when the source has none
    value: float  # award value (EUR)
    year: int  # award year
    supplier_id: str | None


def _cpv_division(release: dict[str, Any]) -> str | None:
    for item in (release.get("tender") or {}).get("items") or []:
        cid = (item.get("classification") or {}).get("id")
        if cid:
            digits = "".join(ch for ch in str(cid) if ch.isdigit())
            if len(digits) >= 2:
                return digits[:2]
    return None


def _buyer_region(release: dict[str, Any]) -> str | None:
    # The OCP/ANAC OCDS address has locality + postalCode + countryName but no
    # region/NUTS, so we cannot derive a region from this source.
    return None


def _year(*candidates: Any) -> int | None:
    for value in candidates:
        if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
            return int(value[:4])
    return None


def parse_record(release: dict[str, Any]) -> list[HistoryRecord]:
    """PURE: one OCDS compiled release -> a HistoryRecord per usable award.

    Skips releases with no usable CPV, and awards with no value/year.
    """
    division = _cpv_division(release)
    if division is None:
        return []
    region = _buyer_region(release)
    release_date = release.get("date")

    records: list[HistoryRecord] = []
    for award in release.get("awards") or []:
        amount = (award.get("value") or {}).get("amount")
        if amount is None:
            continue
        year = _year(award.get("date"), release_date)
        if year is None:
            continue
        suppliers = award.get("suppliers") or []
        supplier_id = suppliers[0].get("id") if suppliers else None
        records.append(
            HistoryRecord(
                cpv_division=division,
                region=region,
                value=float(amount),
                year=year,
                supplier_id=supplier_id,
            )
        )
    return records


def stream_records(lines: Iterable[Any]) -> Iterator[HistoryRecord]:
    """Parse a JSONL line iterable (str or bytes) into HistoryRecords."""
    for line in lines:
        if not line or not str(line).strip():
            continue
        yield from parse_record(json.loads(line))


def _iter_gz_lines(byte_chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Incrementally gunzip a byte-chunk stream into lines (memory-safe)."""
    decompressor = zlib.decompressobj(31)  # 31 = gzip
    buffer = b""
    for chunk in byte_chunks:
        buffer += decompressor.decompress(chunk)
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line
    buffer += decompressor.flush()
    if buffer.strip():
        yield buffer


def fetch_live(year: int) -> Iterator[HistoryRecord]:
    """Stream + gunzip the OCP ANAC dataset line-by-line (does NOT buffer in RAM)."""
    url = OCP_ANAC_URL_TEMPLATE.format(year=year)
    try:
        with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as resp:
            resp.raise_for_status()
            yield from stream_records(_iter_gz_lines(resp.iter_bytes()))
    except httpx.HTTPError as exc:
        raise RuntimeError(f"ANAC history download failed: {exc}") from exc


def load_fixture(path: Path | None = None) -> list[HistoryRecord]:
    """Read the recorded JSONL fixture into HistoryRecords (offline)."""
    text = (path or FIXTURE_PATH).read_text(encoding="utf-8")
    return list(stream_records(text.splitlines()))


def build_benchmarks(
    sample: bool, year: int, store: BenchmarkStore
) -> dict[str, int]:
    """Orchestrate fetch -> parse -> aggregate -> save. Returns counts."""
    from bandiradar.intelligence.benchmarks import compute_benchmarks

    records = load_fixture() if sample else list(fetch_live(year))
    benchmarks = compute_benchmarks(records)
    store.save_benchmarks(benchmarks)
    return {"records": len(records), "benchmarks": len(benchmarks)}
