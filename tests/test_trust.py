"""trust.assess — deterministic validation of LLM extractions (PURE, offline).

The LLM proposes an extraction; these checks dispose. Every test is a pure
function call over (extraction dict, page text) — no I/O, no LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bandiradar.trust import TrustReport, assess

NOW = datetime(2026, 6, 12, tzinfo=UTC)


def _ex(**overrides) -> dict:
    """A complete extraction record (the extract_bando_fields shape)."""
    base = {
        "title": None,
        "summary": None,
        "eligibility_text": None,
        "value_amount": None,
        "value_min": None,
        "value_max": None,
        "deadline": None,
        "keywords": [],
        "kind": "incentive",
    }
    base.update(overrides)
    return base


def _checks(extraction: dict, page_text: str) -> dict:
    return assess(extraction, page_text, now=NOW).checks


# --------------------------------------------------------------------------- #
# (a) deadline-in-text — Italian date formats
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "Scadenza per la presentazione delle domande: 30/06/2026 ore 13:00",
        "entro e non oltre il 30 giugno 2026",
        "Entro il 30 GIUGNO 2026",  # case-insensitive
        "data di chiusura 2026-06-30",  # ISO in the page
        "termine: 30.06.2026",
        "termine: 30-06-2026",
        "scade il 30/6/2026",  # no leading zero on the month
        "chiusura sportello 30/06/26",  # two-digit year
        "scadenza 30 giu 2026",  # abbreviated month
        "scadenza il giorno  30  giugno   2026",  # ragged whitespace
    ],
)
def test_deadline_found_in_italian_formats(text):
    checks = _checks(_ex(deadline="2026-06-30"), text)
    assert checks["deadline_in_text"] is True


def test_deadline_primo_del_mese():
    # "1° luglio" (and plain "1 luglio") are how Italian pages write the 1st.
    checks = _checks(_ex(deadline="2026-07-01"), "entro il 1° luglio 2026")
    assert checks["deadline_in_text"] is True
    checks = _checks(_ex(deadline="2026-07-01"), "entro il 1 luglio 2026")
    assert checks["deadline_in_text"] is True


def test_deadline_with_time_component_matches_date():
    checks = _checks(
        _ex(deadline="2026-06-30T12:00:00"), "domande entro il 30 giugno 2026"
    )
    assert checks["deadline_in_text"] is True


def test_deadline_not_in_text_is_a_hard_failure():
    report = assess(
        _ex(deadline="2026-06-30"),
        "Scadenza per la presentazione: 15/05/2026.",
        now=NOW,
    )
    assert report.checks["deadline_in_text"] is False
    assert report.verdict == "quarantine"


def test_deadline_absent_is_not_applicable():
    checks = _checks(_ex(title="Bando X"), "Bando X a sportello, senza scadenza")
    assert checks["deadline_in_text"] is None


def test_deadline_non_iso_but_parseable_is_tolerated():
    # The LLM was told ISO but sometimes echoes the Italian form; if it parses
    # and reconciles with the page, it is NOT a hard failure.
    checks = _checks(_ex(deadline="30/06/2026"), "scadenza 30/06/2026")
    assert checks["deadline_in_text"] is True


def test_deadline_in_english_java_template_format():
    # FVG renders Scadenza via a Java template in the ENGLISH locale; missing
    # this format quarantined 11/12 honest fvg extractions in calibration.
    checks = _checks(
        _ex(deadline="2026-12-31"),
        "Scadenza: Thu Dec 31 23:59:00 CET 2026 — Avviso contributi",
    )
    assert checks["deadline_in_text"] is True


def test_deadline_yearless_full_month_grounds():
    # A recurring/implicit-year deadline ("entro il 31 gennaio di ciascun
    # anno"): the model READ a real date, it did not invent one.
    checks = _checks(
        _ex(deadline="2026-01-31"),
        "la domanda è da presentare entro il 31 gennaio di ciascun anno",
    )
    assert checks["deadline_in_text"] is True


def test_deadline_yearless_abbreviated_month_does_not_ground():
    # The year-less form is restricted to the FULL month name: "31 gen" (or a
    # bare numeric "31/01") would substring-match far too loosely.
    report = assess(
        _ex(deadline="2026-01-31"), "una pagina con 31 gen. citato", now=NOW
    )
    assert report.checks["deadline_in_text"] is False


def test_deadline_garbage_string_quarantines():
    report = assess(_ex(deadline="fine giugno"), "scadenza 30/06/2026", now=NOW)
    assert report.checks["deadline_in_text"] is False
    assert report.checks["sane_dates"] is False
    assert report.verdict == "quarantine"


# --------------------------------------------------------------------------- #
# (b) amount-in-text — separators, €, "milioni"/"mila"
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "dotazione finanziaria di € 1.000.000",
        "dotazione di 1.000.000,00 euro",
        "un budget complessivo di 1 milione di euro",
        "stanziati 1.000.000 €",
        "risorse pari a 1000000 euro",
        "risorse pari a 1 000 000 euro",  # space-separated thousands
        "1 mln di euro",
    ],
)
def test_amount_found_in_italian_formats(text):
    checks = _checks(_ex(value_amount=1_000_000.0), text)
    assert checks["amount_in_text"] is True


def test_amount_fractional_millions():
    checks = _checks(_ex(value_amount=1_500_000.0), "dotazione di 1,5 milioni di euro")
    assert checks["amount_in_text"] is True


def test_amount_mila():
    checks = _checks(_ex(value_amount=500_000.0), "contributo fino a 500 mila euro")
    assert checks["amount_in_text"] is True
    checks = _checks(_ex(value_amount=500_000.0), "contributo fino a 500mila euro")
    assert checks["amount_in_text"] is True


def test_amount_with_decimals():
    checks = _checks(
        _ex(value_amount=1_234_567.89), "importo pari a € 1.234.567,89 totali"
    )
    assert checks["amount_in_text"] is True


def test_amount_not_in_text_fails_check():
    checks = _checks(_ex(value_amount=750_000.0), "dotazione di € 2.000.000")
    assert checks["amount_in_text"] is False


def test_all_amounts_must_be_found():
    text = "dotazione di € 1.000.000, contributo massimo 50.000 euro"
    ok = _checks(_ex(value_amount=1_000_000.0, value_max=50_000.0), text)
    assert ok["amount_in_text"] is True
    # value_min=10000 never appears -> the check fails as a whole
    partial = _checks(
        _ex(value_amount=1_000_000.0, value_min=10_000.0),
        "dotazione di € 1.000.000",
    )
    assert partial["amount_in_text"] is False


def test_no_amounts_extracted_is_not_applicable():
    checks = _checks(_ex(title="Bando"), "un bando senza importi")
    assert checks["amount_in_text"] is None


# --------------------------------------------------------------------------- #
# (c) sane-dates
# --------------------------------------------------------------------------- #


def test_sane_dates_ok_for_near_future():
    checks = _checks(_ex(deadline="2026-06-30"), "scadenza 30/06/2026")
    assert checks["sane_dates"] is True


def test_sane_dates_recently_closed_is_fine():
    # The corpus legitimately holds recently-closed bandi.
    checks = _checks(_ex(deadline="2026-05-30"), "scadenza 30/05/2026")
    assert checks["sane_dates"] is True


def test_sane_dates_ungrounded_remote_past_quarantines():
    # The page does NOT state the date AND it's in the remote past: a parse
    # error / hallucination — both deadline checks fail hard.
    report = assess(_ex(deadline="2019-01-31"), "nessuna data in pagina", now=NOW)
    assert report.checks["sane_dates"] is False
    assert report.verdict == "quarantine"


def test_sane_dates_ungrounded_far_future_quarantines():
    report = assess(_ex(deadline="2033-12-31"), "nessuna data in pagina", now=NOW)
    assert report.checks["sane_dates"] is False
    assert report.verdict == "quarantine"


def test_sane_dates_grounded_old_deadline_is_honest():
    # The page itself states the old date (an archived bando, e.g. campania's
    # curated 2023 set in the prod DB): honest extraction, NOT a hallucination —
    # lifecycle status closes it, the trust spine must not quarantine it.
    report = assess(_ex(deadline="2024-01-10"), "scadenza 10/01/2024", now=NOW)
    assert report.checks["deadline_in_text"] is True
    assert report.checks["sane_dates"] is True
    assert report.verdict == "ok"


def test_sane_dates_published_after_deadline_fails_even_grounded():
    report = assess(
        _ex(deadline="2024-01-10", published_at="2024-03-01"),
        "pubblicato 01/03/2024, scadenza 10/01/2024",
        now=NOW,
    )
    assert report.checks["sane_dates"] is False


def test_sane_dates_published_after_deadline_fails():
    report = assess(
        _ex(deadline="2026-06-30", published_at="2026-08-01"),
        "pubblicato il 01/08/2026, scadenza 30/06/2026",
        now=NOW,
    )
    assert report.checks["sane_dates"] is False


def test_sane_dates_published_before_deadline_ok():
    checks = _checks(
        _ex(deadline="2026-06-30", published_at="2026-01-01"),
        "scadenza 30/06/2026",
    )
    assert checks["sane_dates"] is True


def test_sane_dates_not_applicable_without_deadline():
    checks = _checks(_ex(title="Bando"), "testo qualunque")
    assert checks["sane_dates"] is None


# --------------------------------------------------------------------------- #
# (d) title-grounding
# --------------------------------------------------------------------------- #


def test_title_verbatim_in_page_grounds():
    checks = _checks(
        _ex(title="Bando per la transizione digitale delle PMI"),
        "Regione X — Bando per la transizione digitale delle PMI. Domande dal...",
    )
    assert checks["title_grounding"] is True


def test_title_tokens_scattered_still_ground():
    checks = _checks(
        _ex(title="Contributi per la digitalizzazione delle imprese artigiane"),
        "La Regione sostiene la digitalizzazione: contributi alle imprese "
        "artigiane del territorio.",
    )
    assert checks["title_grounding"] is True


def test_title_unrelated_to_page_fails():
    checks = _checks(
        _ex(title="Acquacoltura sostenibile in zone montane"),
        "Bando per servizi informatici comunali, scadenza e modalità di domanda.",
    )
    assert checks["title_grounding"] is False


def test_title_grounding_handles_accents():
    checks = _checks(
        _ex(title="Sostegno alle attività produttive"),
        "SOSTEGNO ALLE ATTIVITÀ PRODUTTIVE — presentazione delle domande",
    )
    assert checks["title_grounding"] is True


def test_title_absent_is_not_applicable():
    checks = _checks(_ex(deadline="2026-06-30"), "scadenza 30/06/2026")
    assert checks["title_grounding"] is None


# --------------------------------------------------------------------------- #
# verdict + confidence
# --------------------------------------------------------------------------- #

_GOOD_TEXT = (
    "Bando per la transizione digitale delle PMI. Dotazione € 1.000.000. "
    "Contributo massimo 50.000 euro. Scadenza 30/06/2026."
)
_GOOD_EXTRACTION = _ex(
    title="Bando per la transizione digitale delle PMI",
    value_amount=1_000_000.0,
    value_max=50_000.0,
    deadline="2026-06-30",
)


def test_all_checks_pass_is_ok_with_full_confidence():
    report = assess(_GOOD_EXTRACTION, _GOOD_TEXT, now=NOW)
    assert all(v is True for v in report.checks.values())
    assert report.confidence == 1.0
    assert report.verdict == "ok"


def test_soft_failure_amount_is_suspect_not_quarantine():
    extraction = dict(_GOOD_EXTRACTION, value_amount=999_999.0)
    report = assess(extraction, _GOOD_TEXT, now=NOW)
    assert report.checks["amount_in_text"] is False
    assert report.verdict == "suspect"
    assert 0.0 < report.confidence < 1.0


def test_soft_failure_title_is_suspect_not_quarantine():
    extraction = dict(_GOOD_EXTRACTION, title="Acquacoltura in zone montane")
    report = assess(extraction, _GOOD_TEXT, now=NOW)
    assert report.checks["title_grounding"] is False
    assert report.verdict == "suspect"


def test_empty_extraction_is_suspect_with_neutral_confidence():
    report = assess(_ex(), "qualunque testo di pagina", now=NOW)
    assert all(v is None for v in report.checks.values())
    assert report.confidence == 0.5
    assert report.verdict == "suspect"


def test_missing_keys_tolerated():
    # assess never raises on a malformed/partial extraction dict.
    report = assess({}, "", now=NOW)
    assert report.verdict == "suspect"


def test_no_deadline_but_grounded_title_is_ok():
    # "A sportello" bandi without a deadline are normal — not suspicious per se.
    report = assess(
        _ex(title="Voucher internazionalizzazione"),
        "Voucher per l'internazionalizzazione delle imprese",
        now=NOW,
    )
    assert report.verdict == "ok"


def test_report_round_trips_as_dict():
    report = assess(_GOOD_EXTRACTION, _GOOD_TEXT, now=NOW)
    data = report.model_dump()
    assert TrustReport.model_validate(data) == report
