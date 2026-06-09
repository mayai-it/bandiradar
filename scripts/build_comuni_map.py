"""Build the packaged comune -> region map (ISTAT). One-time, for the region fallback.

Source: ISTAT "Elenco dei comuni italiani" (official statistical units list). Run:

    uv run python scripts/build_comuni_map.py

Writes ``src/bandiradar/data/comuni_it.json`` = {casefolded-comune-name: region}, with
region canonicalised to the 20-region naming used elsewhere (bilingual ISTAT names
like "Valle d'Aosta/Vallée d'Aoste" -> "Valle d'Aosta"). Used by ``anac_pvl`` to map
``luogo_istat`` (comune) -> region when ``luogo_nuts`` is missing/national.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_PATH = REPO / "src" / "bandiradar" / "data" / "comuni_it.json"
SOURCE_URL = "https://www.istat.it/storage/codici-unita-amministrative/Elenco-comuni-italiani.csv"
_COMUNE_COL = "Denominazione in italiano"
_REGION_COL = "Denominazione Regione"


def _canonical_region(name: str) -> str:
    # bilingual ISTAT names -> the single canonical form used across the engine
    return name.split("/")[0].strip()


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/istat_comuni.csv")
    if not src.exists():
        print(f"downloading {SOURCE_URL} ...")
        urllib.request.urlretrieve(SOURCE_URL, src)  # noqa: S310 (documented source)

    text = src.read_bytes().decode("latin-1")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    mapping: dict[str, str] = {}
    for row in reader:
        comune = (row.get(_COMUNE_COL) or "").strip()
        region = (row.get(_REGION_COL) or "").strip()
        if comune and region:
            mapping[comune.casefold()] = _canonical_region(region)

    OUT_PATH.write_text(
        json.dumps(dict(sorted(mapping.items())), ensure_ascii=False, indent=0),
        encoding="utf-8",
    )
    print(
        f"wrote {len(mapping)} comuni -> {OUT_PATH.relative_to(REPO)} "
        f"({len(set(mapping.values()))} distinct regions)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
