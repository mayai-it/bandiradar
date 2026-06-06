"""Shared memory-safe streaming over the Open Contracting (OCP) ANAC mirror.

ANAC's OCDS data is published on the Open Contracting mirror as **gzipped JSONL**
— one compiled release per line, one file per year. Two consumers stream the
SAME files: the opportunity source (:mod:`bandiradar.sources.anac`) and the
historical-benchmark track (:mod:`bandiradar.intelligence.anac_history`). This
module is the single reader they share — it gunzips and yields line by line and
**never buffers the whole file in memory**.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Iterable, Iterator
from typing import Any

from bandiradar import http

# Open Contracting mirror of ANAC OCDS (CC BY 4.0, no auth). One compiled release
# per line, gzipped JSONL, one file per year.
OCP_ANAC_URL_TEMPLATE = (
    "https://data.open-contracting.org/en/publication/117/download?name={year}.jsonl.gz"
)


def iter_gz_lines(byte_chunks: Iterable[bytes]) -> Iterator[bytes]:
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


def stream_releases(year: int, *, timeout: float = 120.0) -> Iterator[dict[str, Any]]:
    """Stream the OCP ANAC dataset for ``year``, yielding one OCDS release dict
    per line. Streams + gunzips incrementally — never holds the file in RAM.

    The connection + initial status are retried with backoff (429/5xx/timeouts);
    a clear error is raised if it still fails."""
    url = OCP_ANAC_URL_TEMPLATE.format(year=year)
    import httpx

    try:
        with http.stream_with_retry(
            "GET",
            url,
            what=f"ANAC OCDS download ({year})",
            timeout=timeout,
            follow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            for line in iter_gz_lines(resp.iter_bytes()):
                if line and line.strip():
                    yield json.loads(line)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"ANAC OCDS download failed ({year}): {exc}") from exc
