"""Build the labelled ANAC-PVL slice of the eval corpus (0.4.0). Regenerable.

Reads the recorded RAW PVL payloads (``data/eval/anac_pvl_raw.jsonl``) and:
  1) maps each through the CURRENT ``anac_pvl`` mapper at ``EVAL_NOW`` and rewrites
     the ``anac_pvl:`` lines in ``data/eval/opportunities.jsonl`` (idempotent);
  2) AUTO-PROPOSES deterministic gold labels for the ``costruzioni`` profile and
     writes them into ``gold.yaml`` (marked pending human review), printing each
     label + a one-line reason.

Keeping the RAW payloads (not just the mapped records) is what makes the
before/after measurable: re-run this after the mapper changes (CPV resolver +
region fallback) to regenerate the same items with richer fields, same gold.

    uv run python scripts/build_pvl_eval.py            # write
    uv run python scripts/build_pvl_eval.py --check     # preview, write nothing
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from bandiradar.evaluation import EVAL_NOW  # noqa: E402
from bandiradar.models import RawDoc  # noqa: E402
from bandiradar.sources import anac_pvl as pvl  # noqa: E402

RAW_PATH = REPO / "src" / "bandiradar" / "data" / "eval" / "anac_pvl_raw.jsonl"
CORPUS_PATH = REPO / "src" / "bandiradar" / "data" / "eval" / "opportunities.jsonl"
GOLD_PATH = REPO / "src" / "bandiradar" / "data" / "eval" / "gold.yaml"

PROFILE = "costruzioni"
_LOMBARDIA = "Lombardia"
_NON_EDILE_NOT = (
    "illuminazione votiva",
    "concorso di progettazione",
    "progettazione",
    "verifica impianti elettrici",
    "verifiche impianti elettrici",
    "manutenzione di impianti elettrici",
)
_ENERGETICO = (
    "efficientamento energetic",
    "riqualificazione energetic",
    "fotovoltaic",
    "cappotto termic",
)


def _load_raws() -> list[RawDoc]:
    raws = []
    for line in RAW_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            raws.append(
                RawDoc(
                    id=f"anac_pvl:{rec['idAvviso']}",
                    source="anac_pvl",
                    fetched_at=EVAL_NOW,
                    payload=rec,
                )
            )
    return raws


def _is_works(raw: RawDoc) -> bool:
    lotti = pvl._lotti(raw.payload)
    return any(lotto.get("natura_principale") == "Lavori" for lotto in lotti)


def _region(raw: RawDoc) -> str | None:
    """Region for the gold rule. Computed ONCE here (the resolver code is frozen), so
    the gold is fixed for the before/after CPV A/B. Uses the full structured
    resolution (province -> comune -> buyer) so an ITALIA/None-nuts Lombardia comune
    (e.g. BRESCIA) is still labelled in-region."""
    lotti = pvl._lotti(raw.payload)
    nuts = next((lt.get("luogo_nuts") for lt in lotti if lt.get("luogo_nuts")), None)
    istat = next((lt.get("luogo_istat") for lt in lotti if lt.get("luogo_istat")), None)
    return pvl.resolve_region(nuts, istat, pvl._buyer(raw.payload))


def propose_label(raw: RawDoc) -> tuple[str, str]:
    """Deterministic costruzioni gold (auto-proposed, pending human review)."""
    title = (
        (pvl._template(raw.payload).get("metadata") or {}).get("descrizione") or ""
    ).lower()
    works = _is_works(raw)
    region = _region(raw)
    if any(k in title for k in _NON_EDILE_NOT):
        return "not", "non-edile (servizi/progettazione)"
    if works and region == _LOMBARDIA:
        return "relevant", "lavori edili in-regione (Lombardia)"
    if works:
        return "borderline", f"lavori edili fuori-regione ({region or 'n/d'})"
    if any(k in title for k in _ENERGETICO):
        return "borderline", "adiacente: efficientamento energetico"
    return "not", "servizi/forniture non-edili"


def main() -> int:
    check = "--check" in sys.argv
    raws = _load_raws()
    pairs: list[tuple[RawDoc, object]] = []  # (raw, mapped Opportunity)
    for raw in raws:
        mapped = pvl.to_opportunities(raw, now=EVAL_NOW)
        if mapped:
            pairs.append((raw, mapped[0]))
        else:
            print(f"  skip (not open at EVAL_NOW): {raw.id.split(':')[1][:8]}")
    opps = [o for _, o in pairs]
    print(f"mapped {len(opps)}/{len(raws)} PVL raws -> Opportunity @ {EVAL_NOW.date()}")

    # 1) rewrite the anac_pvl lines in the corpus (keep the rest, append ours)
    kept = [
        line
        for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["source"] != "anac_pvl"
    ]
    new_lines = [o.model_dump_json() for o in opps]
    corpus_out = "\n".join(kept + new_lines) + "\n"

    # 2) auto-propose costruzioni gold
    gold = yaml.safe_load(GOLD_PATH.read_text(encoding="utf-8"))
    profile_gold = {
        oid: lab
        for oid, lab in gold["profiles"][PROFILE].items()
        if not oid.startswith("anac_pvl:")
    }
    print(
        f"\n=== auto-proposed {PROFILE} gold, {len(opps)} PVL gare (PENDING REVIEW) ==="
    )
    from collections import Counter

    tally: Counter = Counter()
    for raw, opp in pairs:
        label, reason = propose_label(raw)
        profile_gold[opp.id] = label
        tally[label] += 1
        print(
            f"  [{label:10}] {opp.id.split(':')[1][:8]}  {reason:42} | {opp.title[:40]}"
        )
    print(f"  tally: {dict(tally)}")
    gold["profiles"][PROFILE] = profile_gold
    meta = gold.setdefault("_meta", {})
    meta["anac_pvl_note"] = (
        "anac_pvl costruzioni labels are AUTO-PROPOSED (rules in "
        "scripts/build_pvl_eval.py: relevant=Lombardia works, borderline=out-of-region "
        "works or energetico, not=non-edile) — PENDING HUMAN REVIEW."
    )
    gold_out = yaml.safe_dump(gold, sort_keys=False, allow_unicode=True)

    if check:
        print("\n(--check: nothing written)")
        return 0
    CORPUS_PATH.write_text(corpus_out, encoding="utf-8")
    GOLD_PATH.write_text(gold_out, encoding="utf-8")
    print(f"\nwrote {len(kept) + len(new_lines)} corpus rows + {PROFILE} gold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
