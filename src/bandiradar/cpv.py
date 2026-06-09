"""CPV label → code resolver (official EU CPV 2008 vocabulary, Italian labels).

Some sources (notably ``anac_pvl``) expose the CPV as the Italian LABEL, not the
numeric code, so the prefilter's CPV-prefix gate can't fire. This module resolves a
label to its official 8-digit CPV code via an EXACT normalized match against the
packaged vocabulary (``data/cpv_it.json``, built once by ``scripts/build_cpv_map.py``
from the official CPV 2008 list — Commission Reg. (EC) 213/2008). Pure + offline.

Matching is intentionally exact-on-normalized-text: PVL labels are official CPV
phrases, often DIVISION/GROUP names, so a resolved code is frequently coarse (2-3
significant digits) — which is fine, it still feeds the prefix gate. Labels that
don't resolve are left as text (the caller keeps them as keywords).
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache

from bandiradar import resources


def normalize_label(label: str) -> str:
    """Canonical form for matching: lowercase, accents stripped, punctuation removed,
    whitespace collapsed. Used to BUILD the map and to RESOLVE — must stay identical."""
    text = unicodedata.normalize("NFKD", label)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=1)
def _label_to_code() -> dict[str, str]:
    """The packaged {normalized-italian-label: 8-digit-code} map (loaded once)."""
    return json.loads(resources.cpv_map().read_text(encoding="utf-8"))


def resolve(label: str) -> str | None:
    """The official 8-digit CPV code for an Italian label, or ``None`` if unmatched."""
    if not label:
        return None
    return _label_to_code().get(normalize_label(label))


def resolve_labels(labels: list[str]) -> list[str]:
    """Resolve many labels to codes, de-duplicated and order-preserving (drops
    unresolved ones)."""
    out: list[str] = []
    for label in labels:
        code = resolve(label)
        if code is not None and code not in out:
            out.append(code)
    return out
