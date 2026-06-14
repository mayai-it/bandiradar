"""Add the regional-source slice to the eval corpus (0.13.0). Regenerable.

The eval corpus (``data/eval/opportunities.jsonl``) was built before the regional
coverage waves (v0.6.0+), so it only spanned 7 sources. This script folds in the
12 newer regional adapters by mapping their RECORDED fixtures
(``data/fixtures/<source>.json`` — the same offline payloads the source tests use)
through the CURRENT adapter at ``EVAL_NOW`` and rewriting the ``<source>:`` lines in
the corpus. Only items OPEN at ``EVAL_NOW`` are kept (a closed item is dropped by
the Stage-1 deadline gate, so it can never be returned — inert in the metrics).

Deterministic + offline: no network, no LLM. ``EVAL_NOW`` is fixed, the fixtures
are committed, so "open at EVAL_NOW" is stable forever and the script is idempotent.

    uv run python scripts/build_regional_eval.py            # write
    uv run python scripts/build_regional_eval.py --check     # preview, write nothing
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from bandiradar.evaluation import EVAL_NOW  # noqa: E402
from bandiradar.sources import base  # noqa: E402

CORPUS_PATH = REPO / "src" / "bandiradar" / "data" / "eval" / "opportunities.jsonl"

# The regional adapters added after the eval corpus was first built (v0.6.0+).
REGIONAL_SOURCES = [
    "sicilia",
    "emilia_romagna",
    "trentino",
    "veneto",
    "piemonte",
    "puglia",
    "sardegna",
    "fvg",
    "campania",
    "calabria",
    "basilicata",
    "liguria",
]


def _map_open(source_id: str) -> list:
    """Map a source's fixture through its adapter; keep items open at EVAL_NOW."""
    src = base.get(source_id)
    opps = []
    for raw in src.load_fixture():
        for opp in src.to_opportunities(raw, now=EVAL_NOW):
            if opp.status != "closed":
                opps.append(opp)
    return opps


def main() -> int:
    check = "--check" in sys.argv

    # Map every regional source (sorted ids inside each source for stable output).
    new_opps: list = []
    tally: Counter = Counter()
    for sid in REGIONAL_SOURCES:
        opps = sorted(_map_open(sid), key=lambda o: o.id)
        new_opps.extend(opps)
        tally[sid] = len(opps)
        print(f"  {sid:16} open@{EVAL_NOW.date()}: {len(opps)}")
    print(
        f"\nmapped {len(new_opps)} open regional opportunities ({len(tally)} sources)"
    )

    # Rewrite: keep all lines NOT from a regional source, append our fresh slice.
    regional = set(REGIONAL_SOURCES)
    kept = [
        line
        for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["source"] not in regional
    ]
    new_lines = [o.model_dump_json() for o in new_opps]
    corpus_out = "\n".join(kept + new_lines) + "\n"
    total = len(kept) + len(new_lines)

    if check:
        print(f"\n(--check: nothing written; corpus would be {total} rows)")
        return 0
    CORPUS_PATH.write_text(corpus_out, encoding="utf-8")
    print(f"\nwrote {total} rows ({len(kept)} existing + {len(new_lines)} regional)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
