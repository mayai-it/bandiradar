"""Build the packaged CPV resolver map (Italian label -> 8-digit code). One-time.

Source: the official CPV 2008 vocabulary (Commission Regulation (EC) No 213/2008),
multilingual workbook. SIMAP's historical ``cpv_2008_xml.zip`` now 404s, so we read
the maintained multilingual mirror (publictendering.com, 26 languages incl. IT) —
the CODES are the official vocabulary regardless of mirror. Run once:

    uv run --with "xlrd==1.2.0" python scripts/build_cpv_map.py [path-to.xls]

Writes ``src/bandiradar/data/cpv_it.json`` = {normalized-it-label: 8-digit-code}.
Normalization is shared with the resolver (``bandiradar.cpv.normalize_label``) so the
map keys and lookups always agree. On a normalized-label collision the FIRST (most
general, lowest code) wins — coarse division matches are intentionally acceptable.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from bandiradar.cpv import normalize_label  # noqa: E402

SOURCE_URL = "https://www.publictendering.com/xls/CPV-CODES-2008.xls"
OUT_PATH = REPO / "src" / "bandiradar" / "data" / "cpv_it.json"
_SHEET = "CPV codes (Multi)"
_HEADER_ROW = 3  # CODE | EN | FR | NL | ES | DE | IT | ...
_CODE_COL = 0
_IT_COL = 6


def main() -> int:
    import xlrd  # lazy: only needed to (re)build, not at runtime

    src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if src is None:
        src = Path("/tmp/CPV-CODES-2008.xls")
        if not src.exists():
            print(f"downloading {SOURCE_URL} ...")
            urllib.request.urlretrieve(SOURCE_URL, src)  # noqa: S310 (documented source)

    sheet = xlrd.open_workbook(str(src)).sheet_by_name(_SHEET)
    assert sheet.cell_value(_HEADER_ROW, _IT_COL) == "IT", "IT column moved"

    mapping: dict[str, str] = {}
    collisions = 0
    for r in range(_HEADER_ROW + 1, sheet.nrows):
        raw_code = str(sheet.cell_value(r, _CODE_COL)).strip()
        label = str(sheet.cell_value(r, _IT_COL)).strip()
        if not raw_code or not label:
            continue
        code = raw_code.split("-")[0]  # drop the "-N" check digit -> 8-digit code
        key = normalize_label(label)
        if key in mapping:
            collisions += 1
            continue  # keep first (lowest code = most general)
        mapping[key] = code

    OUT_PATH.write_text(
        json.dumps(dict(sorted(mapping.items())), ensure_ascii=False, indent=0),
        encoding="utf-8",
    )
    print(
        f"wrote {len(mapping)} labels -> {OUT_PATH.relative_to(REPO)} "
        f"({collisions} normalized-label collisions kept-first)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
