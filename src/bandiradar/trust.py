"""Trust spine — deterministic validation of LLM extractions (PURE module).

Ten LLM scrapers feed the corpus; this module is their deterministic gate, the
same philosophy as the self-healing crawl: the LLM PROPOSES an extraction, a
deterministic check DISPOSES. No I/O, no LLM, no network — :func:`assess` is a
pure function over ``(extraction dict, page text)`` so it is fully unit-testable
offline and can never be gamed by the model it judges.

Checks (each ``True`` / ``False`` / ``None`` = not applicable, field absent):

- ``deadline_in_text`` — the extracted deadline must be reconcilable with the
  page text, across the Italian date formats ("30/06/2026", "30 giugno 2026",
  "30.06.2026", ISO, abbreviated months, "1°" for the 1st, two-digit years), the
  English Java/CMS-template form ("Thu Dec 31 23:59:00 CET 2026" — how FVG
  renders Scadenza), and a year-less full-month form ("entro il 31 gennaio di
  ciascun anno" — a recurring/implicit-year deadline is read, not invented).
- ``amount_in_text`` — every extracted amount (value_amount/min/max) must appear
  in the text, normalizing separators ("1.000.000,00", "1 000 000"), the € sign,
  and verbal multipliers ("1,5 milioni", "500 mila", "1 mln").
- ``sane_dates`` — ``published_at`` (when present) must not postdate the
  deadline; a deadline NOT found in the text must also sit inside a plausibility
  window (not in the remote past, not more than ~5 years out). A date the page
  itself states is exempt from the window: an old grounded deadline is an
  archived bando honestly extracted, not a hallucination (calibrated on the prod
  DB — see the bound constants below).
- ``title_grounding`` — the extracted title significantly overlaps the page text
  (an ungrounded title means the model described a different page).

Verdicts: ``quarantine`` ONLY on hard failures (a deadline that is extracted
but NOT in the text, or insane dates — the strongest hallucination signals);
``suspect`` = low confidence but plausible (soft failures: amounts or title);
``ok`` otherwise.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel

__all__ = [
    "TrustReport",
    "Verdict",
    "assess",
    "SUSPECT_CONFIDENCE",
]

Verdict = Literal["ok", "suspect", "quarantine"]

# Below this confidence (and absent a hard failure) the verdict is "suspect".
SUSPECT_CONFIDENCE = 0.85

# Per-check confidence weights, renormalized over the APPLICABLE checks. The
# deadline carries the most weight: it drives matching (Stage-1 open gate,
# closing-soon alerts), so a wrong one is the most damaging hallucination.
_WEIGHTS: dict[str, float] = {
    "deadline_in_text": 0.40,
    "amount_in_text": 0.25,
    "sane_dates": 0.15,
    "title_grounding": 0.20,
}

# Hard failures: the extraction asserts something the page contradicts.
_HARD_CHECKS = ("deadline_in_text", "sane_dates")

# Sanity window for a deadline (sane_dates): older than ~2 years smells like a
# parse error, further out than ~5 years like a typo. The window applies ONLY to
# deadlines NOT found in the page text — a date the page itself states is an
# honest extraction however old (the corpus legitimately holds old bandi;
# lifecycle status already closes them). Calibrated on the prod DB: campania's
# curated set lists real 2023 bandi that the window alone would have quarantined.
_PAST_BOUND_DAYS = 2 * 365
_FUTURE_BOUND_DAYS = 5 * 365

_MONTHS_IT = {
    1: ("gennaio", "gen"),
    2: ("febbraio", "feb"),
    3: ("marzo", "mar"),
    4: ("aprile", "apr"),
    5: ("maggio", "mag"),
    6: ("giugno", "giu"),
    7: ("luglio", "lug"),
    8: ("agosto", "ago"),
    9: ("settembre", "set"),
    10: ("ottobre", "ott"),
    11: ("novembre", "nov"),
    12: ("dicembre", "dic"),
}
_MONTH_BY_NAME = {name: num for num, names in _MONTHS_IT.items() for name in names}

_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b")
_TEXTUAL_DATE_RE = re.compile(
    r"\b(\d{1,2})°?\s+([a-z]{3,9})\s+(\d{4})\b", re.IGNORECASE
)

# English-locale dates as Java/CMS templates render them ("Scadenza: Thu Dec 31
# 23:59:00 CET 2026" — FVG does exactly this; calibrated on the prod DB, where
# missing this format quarantined 11 of fvg's 12 honest extractions).
_EN_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_EN_DATE_RE = re.compile(
    r"\b(" + "|".join(_EN_MONTHS) + r")[a-z]*\s+(\d{1,2})"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"  # optional time
    r"(?:\s+[a-z]{1,5})?"  # optional timezone token (CET, CEST, …)
    r"\s+(\d{4})\b"
)

# Number tokens: Italian thousands groups (dots/spaces) with optional comma
# decimals, or a plain integer/decimal. NBSP counts as a space separator.
_NUM_RE = re.compile(r"\d{1,3}(?:[.\s ]\d{3})+(?:,\d+)?|\d+(?:,\d+)?")

# Verbal multipliers immediately following a number ("1,5 milioni", "500mila").
_MULTIPLIERS: dict[str, float] = {
    "milione": 1e6,
    "milioni": 1e6,
    "mln": 1e6,
    "miliardo": 1e9,
    "miliardi": 1e9,
    "mld": 1e9,
    "mila": 1e3,
}
_MULT_RE = re.compile(
    r"^\s*(" + "|".join(sorted(_MULTIPLIERS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_WORD_RE = re.compile(r"[^\W\d_]{4,}", re.UNICODE)

# Title tokens found in the page / title tokens — below this it is ungrounded.
_TITLE_OVERLAP = 0.5


class TrustReport(BaseModel):
    """The deterministic verdict over one LLM extraction."""

    checks: dict[str, bool | None]
    confidence: float
    verdict: Verdict


# --------------------------------------------------------------------------- #
# normalization helpers
# --------------------------------------------------------------------------- #


def _norm_text(text: str) -> str:
    """Lowercase + collapse all whitespace, the haystack every check searches."""
    return re.sub(r"\s+", " ", (text or "").lower())


def _fold(text: str) -> str:
    """Accent-fold for robust token comparison ("attività" == "attivita")."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _tokens(text: str | None) -> set[str]:
    return {_fold(m.group(0).lower()) for m in _WORD_RE.finditer(text or "")}


def _parse_date(value: Any) -> date | None:
    """Parse an extracted date LENIENTLY: ISO first, then the Italian forms.

    The extractor asks for ISO, but a model sometimes echoes the page's format;
    a parseable date is still an honest extraction, so tolerate it.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    m = _NUMERIC_DATE_RE.search(text)
    if m:
        day, month, year = (int(g) for g in m.groups())
        return _safe_date(year, month, day)
    m = _TEXTUAL_DATE_RE.search(text)
    if m:
        month_num = _MONTH_BY_NAME.get(_fold(m.group(2).lower()))
        if month_num is not None:
            return _safe_date(int(m.group(3)), month_num, int(m.group(1)))
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# check (a) — deadline-in-text
# --------------------------------------------------------------------------- #


def _date_candidates(d: date) -> set[str]:
    """Every textual form the page might use for ``d`` (lowercased)."""
    days = {f"{d.day:02d}", str(d.day)}
    months_num = {f"{d.month:02d}", str(d.month)}
    years = {str(d.year), f"{d.year % 100:02d}"}
    out: set[str] = {d.isoformat()}
    for sep in ("/", ".", "-"):
        for dd in days:
            for mm in months_num:
                for yy in years:
                    out.add(f"{dd}{sep}{mm}{sep}{yy}")
    full, abbr = _MONTHS_IT[d.month]
    for month_name in (full, abbr):
        for dd in days:
            out.add(f"{dd} {month_name} {d.year}")
    # Day + FULL textual month WITHOUT a year ("entro il 31 gennaio di ciascun
    # anno") still grounds the extraction: the model read a real recurring /
    # implicit-year deadline, it did not invent one. Restricted to the full
    # month name — an abbreviated ("31 gen") or numeric ("31/01") year-less form
    # would substring-match far too loosely.
    for dd in days:
        out.add(f"{dd} {full}")
    if d.day == 1:
        out.add(f"1° {full} {d.year}")
        out.add(f"1° {abbr} {d.year}")
        out.add(f"1° {full}")
    return out


def _english_dates_in_text(text: str) -> set[date]:
    """Dates rendered in the English Java/CMS form found in the page text."""
    out: set[date] = set()
    for m in _EN_DATE_RE.finditer(text):
        parsed = _safe_date(int(m.group(3)), _EN_MONTHS[m.group(1)], int(m.group(2)))
        if parsed is not None:
            out.add(parsed)
    return out


def _deadline_in_text(deadline: date, text: str) -> bool:
    if any(candidate in text for candidate in _date_candidates(deadline)):
        return True
    return deadline in _english_dates_in_text(text)


# --------------------------------------------------------------------------- #
# check (b) — amount-in-text
# --------------------------------------------------------------------------- #


def _parse_number(token: str) -> float | None:
    cleaned = re.sub(r"[.\s ]", "", token).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _amounts_in_text(text: str) -> set[float]:
    """All monetary-looking numbers in the text, verbal multipliers applied.

    Both the bare value and the multiplied value are kept ("1,5" AND 1500000 for
    "1,5 milioni"), so an extraction matching either form reconciles.
    """
    found: set[float] = set()
    for m in _NUM_RE.finditer(text):
        value = _parse_number(m.group(0))
        if value is None:
            continue
        found.add(value)
        mult = _MULT_RE.match(text[m.end() :])
        if mult:
            found.add(value * _MULTIPLIERS[mult.group(1).lower()])
    return found


def _amount_found(target: float, candidates: set[float]) -> bool:
    return any(math.isclose(target, c, rel_tol=1e-9, abs_tol=0.005) for c in candidates)


# --------------------------------------------------------------------------- #
# check (c) — sane-dates
# --------------------------------------------------------------------------- #


def _sane_dates(
    deadline: date, published: date | None, now: datetime, grounded: bool
) -> bool:
    if published is not None and published > deadline:
        return False  # internally contradictory whatever the page says
    if grounded:
        # The page itself states this date: however old/far it is honest data
        # (an archived bando), not a parse error — lifecycle status closes it.
        return True
    today = now.date()
    if deadline < today - timedelta(days=_PAST_BOUND_DAYS):
        return False
    if deadline > today + timedelta(days=_FUTURE_BOUND_DAYS):
        return False
    return True


# --------------------------------------------------------------------------- #
# check (d) — title-grounding
# --------------------------------------------------------------------------- #


def _title_grounded(title: str, page_tokens: set[str]) -> bool | None:
    title_tokens = _tokens(title)
    if not title_tokens:
        return None  # nothing measurable (e.g. an all-numeric/short title)
    overlap = len(title_tokens & page_tokens) / len(title_tokens)
    return overlap >= _TITLE_OVERLAP


# --------------------------------------------------------------------------- #
# assess
# --------------------------------------------------------------------------- #


def _confidence(checks: dict[str, bool | None]) -> float:
    applicable = {k: v for k, v in checks.items() if v is not None}
    if not applicable:
        return 0.5  # nothing verifiable either way — neutral, never confident
    total = sum(_WEIGHTS[k] for k in applicable)
    passed = sum(_WEIGHTS[k] for k, v in applicable.items() if v)
    return round(passed / total, 4)


def _verdict(checks: dict[str, bool | None], confidence: float) -> Verdict:
    if any(checks.get(k) is False for k in _HARD_CHECKS):
        return "quarantine"
    if confidence < SUSPECT_CONFIDENCE:
        return "suspect"
    return "ok"


def assess(
    extraction: dict, page_text: str, now: datetime | None = None
) -> TrustReport:
    """Deterministically validate one LLM extraction against its page text.

    PURE: no I/O, no LLM. ``extraction`` is the :func:`~bandiradar.sources.
    llm_scraper.extract_bando_fields` record (missing keys tolerated);
    ``page_text`` is the same stripped text the extractor read. ``now`` anchors
    the sane-dates window (tests pass it for determinism).
    """
    moment = datetime.now(UTC) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    text = _norm_text(page_text)

    checks: dict[str, bool | None] = {
        "deadline_in_text": None,
        "amount_in_text": None,
        "sane_dates": None,
        "title_grounding": None,
    }

    # (a) + (c) — deadline checks. An extracted-but-unparseable deadline is
    # garbage data asserting a deadline exists: both checks fail HARD.
    raw_deadline = extraction.get("deadline")
    if raw_deadline not in (None, ""):
        deadline = _parse_date(raw_deadline)
        if deadline is None:
            checks["deadline_in_text"] = False
            checks["sane_dates"] = False
        else:
            grounded = _deadline_in_text(deadline, text)
            checks["deadline_in_text"] = grounded
            published = _parse_date(
                extraction.get("published_at") or extraction.get("published")
            )
            checks["sane_dates"] = _sane_dates(deadline, published, moment, grounded)

    # (b) — every extracted amount must reconcile with the text.
    amounts = [
        v
        for v in (
            extraction.get("value_amount"),
            extraction.get("value_min"),
            extraction.get("value_max"),
        )
        if isinstance(v, int | float)
    ]
    if amounts:
        in_text = _amounts_in_text(text)
        checks["amount_in_text"] = all(_amount_found(a, in_text) for a in amounts)

    # (d) — the title must overlap the page it claims to describe.
    title = extraction.get("title")
    if isinstance(title, str) and title.strip():
        checks["title_grounding"] = _title_grounded(title, _tokens(text))

    confidence = _confidence(checks)
    return TrustReport(
        checks=checks, confidence=confidence, verdict=_verdict(checks, confidence)
    )
