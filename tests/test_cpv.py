"""CPV label->code resolver tests (offline; uses the packaged cpv_it.json)."""

from bandiradar import cpv


def test_normalize_label():
    assert (
        cpv.normalize_label("  Città's àccénti, e-puntì!  ")
        == "citta s accenti e punti"
    )
    assert cpv.normalize_label("SERVIZI  SOCIALI") == "servizi sociali"


def test_resolve_exact_and_coarse():
    # an exact leaf label and a coarse DIVISION label both resolve (coarse is fine)
    assert cpv.resolve("Detersivi per lavastoviglie") == "39831210"
    assert cpv.resolve("Lavori di costruzione") == "45000000"  # division-level code
    # robust to case/whitespace/punctuation
    assert cpv.resolve("  servizi  sociali ") == cpv.resolve("Servizi sociali")


def test_resolve_unmatched_returns_none():
    assert cpv.resolve("etichetta del tutto inventata zzz") is None
    assert cpv.resolve("") is None


def test_resolve_codes_are_eight_digit_no_check_digit():
    code = cpv.resolve("Lavori di costruzione")
    assert code is not None and code.isdigit() and len(code) == 8


def test_resolve_labels_dedups_and_drops_unmatched():
    out = cpv.resolve_labels(
        [
            "Lavori di costruzione",
            "xyz nope",
            "Lavori di costruzione",
            "Servizi sociali",
        ]
    )
    assert out == ["45000000", "85320000"]  # order-preserving, de-duplicated
