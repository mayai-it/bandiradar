"""Deterministic, auditable corrections to the eval gold labels (0.3.0).

Rule-based fixes that make the gold trustworthy WITHOUT human judgment — every
change is traceable to a rule + the opportunity's own fields. Run from the repo
root:  ``uv run python scripts/correct_gold.py``  (use ``--check`` to preview
without writing). It rewrites ``src/bandiradar/data/eval/gold.yaml`` in place and
prints (1) the per-change audit log and (2) the residual JUDGMENT-CALL labels a
rule cannot decide — for a quick human verdict.

Rules (applied to relevant/borderline labels only; never promotes, never touches
existing ``not``):
  GEO        — a regional opportunity whose region ≠ the profile's region(s) and is
               not national/eu → ``not`` (Opportunity.region/geo_scope vs
               Profile.regions). Profiles with no region restriction are exempt.
  INSTRUMENT — for grant-seeking SME profiles (those NOT explicitly seeking
               financing), pure debt/equity/financing instruments and non-funding
               items (by title) → ``not``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from bandiradar import evaluation as ev  # noqa: E402

GOLD_PATH = REPO / "src" / "bandiradar" / "data" / "eval" / "gold.yaml"
PROFILES_DIR = REPO / "src" / "bandiradar" / "data" / "profiles"

# INSTRUMENT rule — title patterns (matched on the lowercased title).
_INSTRUMENT_PATTERNS = [
    (r"\bbond\b", "debt instrument (bond)"),
    (r"piccolo credito", "debt instrument (credito)"),
    (r"garanzi", "guarantee instrument (garanzia)"),
    (r"\bequity\b", "equity instrument"),
    (r"investimenti strategici", "strategic-investment instrument"),
    (r"attrazione investimenti", "investment-attraction instrument"),
    (r"\bhackathon\b", "non-funding (hackathon)"),
    (r"team[\s\-]?building", "non-funding (team building)"),
]

# A profile "explicitly seeks financing" if any of these appear in its keywords /
# capabilities — such a profile KEEPS instrument-type opportunities.
_FINANCING_TERMS = (
    "finanziament",
    "credito",
    "equity",
    "debito",
    "prestito",
    "garanzia",
    "bond",
    "capitale di rischio",
)

RELEVANT_FOR_RECALL = ("relevant", "borderline")


def _load_profiles() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted(PROFILES_DIR.iterdir()):
        if f.name.endswith(".yaml"):
            out[f.name[:-5]] = yaml.safe_load(f.read_text(encoding="utf-8"))
    return out


def _regions(profile: dict) -> set[str]:
    return {r.strip().lower() for r in (profile.get("regions") or [])}


def _seeks_financing(profile: dict) -> bool:
    blob = " ".join(
        [
            *(profile.get("keywords") or []),
            profile.get("capabilities") or "",
        ]
    ).lower()
    return any(term in blob for term in _FINANCING_TERMS)


def _instrument_reason(title: str) -> str | None:
    low = (title or "").lower()
    for pattern, reason in _INSTRUMENT_PATTERNS:
        if re.search(pattern, low):
            return reason
    return None


def main() -> int:
    check_only = "--check" in sys.argv

    corpus = {o.id: o for o in ev.load_corpus()}
    gold = ev.load_gold()
    profiles = _load_profiles()
    gold_profiles: dict[str, dict[str, str]] = gold["profiles"]

    changes: list[tuple[str, str, str, str, str]] = []  # prof, id, old, rule, reason
    for prof in sorted(gold_profiles):
        meta = profiles.get(prof, {})
        regions = _regions(meta)
        seeks_financing = _seeks_financing(meta)
        for oid, label in gold_profiles[prof].items():
            if label not in RELEVANT_FOR_RECALL:
                continue
            opp = corpus.get(oid)
            if opp is None:
                continue

            # GEO rule (skipped when the profile has no region restriction).
            oregion = (opp.region or "").strip().lower()
            if (
                regions
                and opp.geo_scope not in ("national", "eu")
                and oregion
                and oregion not in regions
            ):
                gold_profiles[prof][oid] = "not"
                changes.append(
                    (
                        prof,
                        oid,
                        label,
                        "GEO",
                        f"region {opp.region} ∉ {sorted(regions)}",
                    )
                )
                continue

            # INSTRUMENT rule (only for grant-seekers).
            if not seeks_financing:
                reason = _instrument_reason(opp.title or "")
                if reason is not None:
                    gold_profiles[prof][oid] = "not"
                    changes.append((prof, oid, label, "INSTRUMENT", reason))

    # Provenance in the file itself.
    gold.setdefault("_meta", {})["corrections"] = [
        "GEO: regional opportunity whose region != profile region(s) and not "
        "national/eu -> not (uses Opportunity.region/geo_scope vs Profile.regions).",
        "INSTRUMENT: for grant-seeking SME profiles, debt/equity/financing and "
        "non-funding items (by title) -> not. See scripts/correct_gold.py.",
    ]

    # ---- audit log ---------------------------------------------------------- #
    print(f"=== deterministic corrections: {len(changes)} label(s) -> 'not'\n")
    for rule in ("GEO", "INSTRUMENT"):
        rows = [c for c in changes if c[3] == rule]
        print(f"-- {rule} ({len(rows)})")
        for prof, oid, old, _rule, reason in rows:
            title = (corpus[oid].title or "")[:48]
            print(f"   {prof:22} {oid:26} {old:10}->not  | {reason}  | {title}")
        print()

    # ---- residual judgment calls (rule can't decide) ------------------------ #
    print("=== residual JUDGMENT-CALL labels — procurement tenders kept as")
    print("    relevant/borderline (human verdict needed; rules don't touch these)\n")
    for prof in sorted(gold_profiles):
        residual = [
            (oid, lab)
            for oid, lab in gold_profiles[prof].items()
            if lab in RELEVANT_FOR_RECALL
            and (o := corpus.get(oid)) is not None
            and o.kind == "tender"
        ]
        if not residual:
            continue
        print(f"-- {prof}")
        for oid, lab in residual:
            title = (corpus[oid].title or "")[:56]
            print(f"   [{lab:10}] {oid:26} {title}")
        print()

    if check_only:
        print("(--check: gold.yaml NOT written)")
        return 0

    GOLD_PATH.write_text(
        yaml.safe_dump(
            gold, sort_keys=False, allow_unicode=True, default_flow_style=False
        ),
        encoding="utf-8",
    )
    print(f"wrote {GOLD_PATH.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
