"""LLM-propose gold labels for the regional eval slice (0.13.0). Regenerable.

After ``build_regional_eval.py`` folds the 12 regional sources into the corpus, the
gold set still lacks labels for (a) the 3 new regional profiles and (b) any new
regional item that now survives an existing profile's Stage-1 prefilter. This
script fills exactly those gaps and NOTHING ELSE — existing, human-reviewed labels
are never touched.

Method (mirrors the corpus's established "pooled, LLM-proposed, then deterministically
corrected" provenance — see gold.yaml _meta):
  - For every gold profile, heuristic-rank the Stage-1 survivors over the full corpus.
  - Pool = the survivors. For a NEW profile, every survivor is a candidate (its label
    set is empty). For an EXISTING profile, only NEW-source survivors not already
    labelled are candidates (keep the reviewed labels intact).
  - An LLM classifies each (profile, opportunity) into relevant / borderline / not,
    using the eval label convention. Output is marked PENDING HUMAN REVIEW.
Then run ``scripts/correct_gold.py`` to apply the deterministic GEO/SEEKS/INSTRUMENT
corrections on top.

Needs a configured LLM (BANDIRADAR_LLM_PROVIDER + key in env). Offline-safe: with no
client it prints what it WOULD label and writes nothing.

    uv run python scripts/propose_regional_gold.py            # write
    uv run python scripts/propose_regional_gold.py --check     # preview only
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from bandiradar import core  # noqa: E402
from bandiradar import evaluation as ev  # noqa: E402
from bandiradar.matching.llm import get_client  # noqa: E402
from bandiradar.matching.relevance import HEURISTIC  # noqa: E402
from bandiradar.storage import Store  # noqa: E402

GOLD_PATH = REPO / "src" / "bandiradar" / "data" / "eval" / "gold.yaml"

NEW_PROFILES = ["piemonte_industria", "sardegna_impresa", "sicilia_pmi"]
REGIONAL_SOURCES = {
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
}
VALID = {"relevant", "borderline", "not"}
POOL_K = 30  # cap per profile (survivor sets are small; this is a safety bound)

_SYSTEM = (
    "Sei un valutatore esperto di bandi e incentivi pubblici italiani.\n"
    "Giudica quanto un'OPPORTUNITÀ di finanziamento è pertinente per un'AZIENDA.\n"
    'Rispondi SOLO con JSON: {"label": "relevant|borderline|not", "reason": "..."}.\n\n'
    "Definizioni (convenzione di valutazione):\n"
    '- "relevant": azienda chiaramente eleggibile, opportunità nel suo settore/scopo.\n'
    '- "borderline": adiacente o parzialmente pertinente (settore vicino, dubbia).\n'
    '- "not": non pertinente (altro settore, beneficiari diversi, misura sociale).\n'
    'Una misura sociale/welfare (disabilità, scuola, sport, famiglie) è "not".'
)


def _profile_blurb(p) -> str:
    return (
        f"AZIENDA: {p.name}\n"
        f"Settore/keywords: {', '.join(p.keywords) or '-'}\n"
        f"ATECO: {', '.join(p.ateco) or '-'}\n"
        f"Capacità: {(p.capabilities or '').strip()[:240]}\n"
        f"Regioni: {', '.join(p.regions) or 'tutta Italia'}\n"
        f"Cerca: {', '.join(p.seeks)}\n"
        f"Taglia progetti (euro): {p.value_range.min}-{p.value_range.max}"
    )


def _opp_blurb(o) -> str:
    val = o.value_amount or (
        f"{o.value_min}-{o.value_max}" if o.value_min or o.value_max else "-"
    )
    return (
        f"OPPORTUNITÀ: {o.title}\n"
        f"Tipo: {o.kind} | Regione: {o.region or 'nazionale'} | Importo/budget: {val}\n"
        f"Sintesi: {(o.summary or '').strip()[:400]}\n"
        f"Eleggibilità: {(o.eligibility_text or '').strip()[:200]}"
    )


def _classify(client, profile, opp) -> tuple[str, str]:
    user = f"{_profile_blurb(profile)}\n\n{_opp_blurb(opp)}\n\nClassifica:"
    out = client.score(_SYSTEM, user)
    label = str(out.get("label", "")).strip().lower()
    reason = str(out.get("reason", "")).strip()[:80]
    if label not in VALID:
        return "not", f"(label non valido: {label!r}) {reason}"
    return label, reason


def main() -> int:
    check = "--check" in sys.argv
    corpus = ev.load_corpus()
    gold = yaml.safe_load(GOLD_PATH.read_text(encoding="utf-8"))
    gold_profiles: dict = gold["profiles"]

    client = get_client()
    if client is None:
        print(
            "NO LLM client (provider=none/missing key). Preview of CANDIDATES only:\n"
        )

    store = Store(":memory:")
    try:
        for opp in corpus:
            store.upsert_opportunity(opp, now=ev.EVAL_NOW)

        all_profiles = sorted(set(gold_profiles) | set(NEW_PROFILES))
        # Gather every (profile, opp) pair to classify, so the LLM calls can run
        # concurrently (sequential would exceed practical time budgets).
        tasks: list[tuple[str, object, object]] = []  # (profile_name, profile, opp)
        for name in all_profiles:
            profile = core.load_profile(name)
            ranked = core.run_match(profile, store, client=HEURISTIC, now=ev.EVAL_NOW)
            existing = gold_profiles.get(name, {})
            is_new = name not in gold_profiles or not existing
            cands = [
                opp
                for opp, _m in ranked[:POOL_K]
                if opp.id not in existing and (is_new or opp.source in REGIONAL_SOURCES)
            ]
            kind = "NEW" if is_new else "existing"
            print(f"== {name} ({kind}): {len(cands)} candidate(s)")
            for opp in cands:
                tasks.append((name, profile, opp))

        if client is None:
            for name, _p, opp in tasks:
                print(f"   [?         ] {name:22} {opp.id:42} {opp.title[:40]}")
            print("\n(no client: nothing written)")
            return 0

        def _do(task):
            name, profile, opp = task
            label, reason = _classify(client, profile, opp)
            return name, opp.id, label, reason, opp.title

        added = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_do, tasks))
        for name, oid, label, reason, title in results:
            gold_profiles.setdefault(name, dict(gold_profiles.get(name, {})))
            gold_profiles[name][oid] = label
            added += 1
            print(f"   [{label:10}] {name:22} {oid:38} {title[:34]} | {reason}")

        if client is None:
            print("\n(no client: nothing written)")
            return 0

        meta = gold.setdefault("_meta", {})
        meta["regional_note"] = (
            "Regional sources (v0.6.0+) folded into the corpus by "
            "scripts/build_regional_eval.py; their labels + the 3 regional profiles "
            "(piemonte_industria, sardegna_impresa, sicilia_pmi) are LLM-PROPOSED by "
            "scripts/propose_regional_gold.py — PENDING HUMAN REVIEW, then "
            "deterministically corrected by scripts/correct_gold.py."
        )
        print(f"\nproposed {added} new label(s) across {len(all_profiles)} profiles")
        if check:
            print("(--check: gold.yaml NOT written)")
            return 0
        GOLD_PATH.write_text(
            yaml.safe_dump(gold, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        print(f"wrote {GOLD_PATH.relative_to(REPO)}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
