"""Provincia Autonoma di Trento — FEASR bandi, from a dati.trentino.it CKAN CSV.

The province publishes a *calendar* of rural-development (FEASR) avvisi/bandi as an
open CKAN dataset ("Calendario degli avvisi e degli inviti a partecipare - Bandi
FEASR") whose resource is a structured CSV: ``STATO``, ``INTERVENTO``, ``DATA
APERTURA``/``DATA CHIUSURA``, ``IMPORTO``, ``BENEFICIARI``, an ``INFORMAZIONI`` HTML
link, and ``OBIETTIVO SPECIFICO`` codes. It is refreshed and carries CURRENTLY-OPEN
bandi (``STATO = aperto``), not only historical ones — so it is worth ingesting.

Keyless open-data CSV adapter (the Lombardia-style pattern). ``fetch`` streams the
CSV rows as ``RawDoc``s; ``to_opportunities`` is a PURE row→Opportunity map.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from bandiradar import http, resources
from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.sources.base import ProgressFn, register

SOURCE_ID = "trentino"
REGION = "Trentino-Alto Adige"
ISSUER = "Provincia Autonoma di Trento"
# The CKAN resource is a published Google-Sheets CSV export (the official dataset
# resource). Stable per the dati.trentino.it FEASR-calendar dataset.
TRENTINO_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTOHrmvCu7YH-UBG0snJO_"
    "KsxvdvP4fCpgek-Bb9SBrxx-pM4hKRfNJtlHvWR0RBN_u-NQf0ggeu5pE/pub?output=csv"
)
FIXTURE_PATH = resources.fixture("trentino.json")

_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
# A bando is identified by its intervention + opening date (the SAME intervento can
# recur across years as distinct bandi); STATO/CHIUSURA/IMPORTO are mutable details.
_KEY_FIELDS = ("INTERVENTO", "DATA APERTURA")


def _row_key(row: dict[str, Any]) -> str:
    raw = "|".join((row.get(f) or "").strip().lower() for f in _KEY_FIELDS)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _parse_date(value: Any) -> datetime | None:
    """Parse a messy ``d/m/Y`` cell (1- or 2-digit day/month) to tz-aware UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split("/")
    if len(parts) != 3:
        return None
    try:
        day, month, year = (int(p) for p in parts)
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _parse_amount(value: Any) -> float | None:
    """Parse an Italian-formatted amount ("3.200.000,00") to a float."""
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip().replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _link(info: str) -> str:
    m = _HREF_RE.search(info or "")
    return m.group(1) if m else ""


def _text(info: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", info or "")).strip()


def to_opportunities(raw: RawDoc, now: datetime | None = None) -> list[Opportunity]:
    """PURE map of one FEASR-calendar CSV row (``raw.payload``) to an Opportunity."""
    row: dict[str, Any] = raw.payload
    title = (row.get("INTERVENTO") or "").strip()
    deadline = _parse_date(row.get("DATA CHIUSURA"))
    stato = (row.get("STATO") or "").strip()
    beneficiari = (row.get("BENEFICIARI") or "").strip()
    obiettivi = [
        o.strip()
        for o in (row.get("OBIETTIVO SPECIFICO") or "").split(",")
        if o.strip()
    ]
    link_text = _text(row.get("INFORMAZIONI", ""))
    eligibility = " — ".join(
        part
        for part in (link_text, f"Beneficiari: {beneficiari}" if beneficiari else "")
        if part
    )
    return [
        Opportunity(
            id=f"{SOURCE_ID}:{_row_key(row)}",
            source=SOURCE_ID,
            source_url=_link(row.get("INFORMAZIONI", "")),
            kind="incentive",
            title=title or _row_key(row),
            summary=f"FEASR — {stato}" if stato else "FEASR",
            issuer_name=ISSUER,
            issuer_region=REGION,
            cpv=[],
            keywords=["FEASR", *obiettivi],
            value_amount=_parse_amount(row.get("IMPORTO (Euro)")),
            geo_scope="regional",
            region=REGION,
            deadline=deadline,
            status=default_status(deadline, now),
            eligibility_text=eligibility or None,
            raw_ref=raw.id,
        )
    ]


def _rows_to_rawdocs(rows: list[dict[str, Any]], fetched_at: datetime) -> list[RawDoc]:
    return [
        RawDoc(
            id=f"{SOURCE_ID}:{_row_key(row)}",
            source=SOURCE_ID,
            fetched_at=fetched_at,
            payload=row,
        )
        for row in rows
    ]


def load_fixture(path=None) -> list[RawDoc]:
    import json

    package = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    captured = package.get("_captured")
    fetched_at = datetime.fromisoformat(
        f"{captured}T00:00:00+00:00" if captured else "1970-01-01T00:00:00+00:00"
    )
    return _rows_to_rawdocs(package.get("rows", []), fetched_at)


class TrentinoSource:
    """FEASR bandi calendar for the Provincia Autonoma di Trento (CKAN CSV)."""

    id = SOURCE_ID
    kind: Kind = "incentive"

    def fetch(
        self,
        since: datetime | None = None,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
        progress: ProgressFn | None = None,
    ) -> Iterable[RawDoc]:
        return self._fetch(limit, progress)

    def _fetch(
        self, limit: int | None, progress: ProgressFn | None
    ) -> Iterator[RawDoc]:
        with http.client(follow_redirects=True) as client:
            response = http.with_retry(
                lambda: client.get(TRENTINO_CSV_URL),
                what="Trentino FEASR CSV",
            )
            http.raise_for_status(response, what="Trentino FEASR CSV")
        reader = csv.DictReader(io.StringIO(response.text))
        rows = [
            {(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader
        ]
        cap = limit if limit is not None else len(rows)
        yield from _rows_to_rawdocs(rows[:cap], datetime.now(tz=UTC))
        if progress is not None:
            progress(f"{SOURCE_ID}: {min(cap, len(rows))} rows")

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)

    def load_fixture(self) -> list[RawDoc]:
        return load_fixture()


register(TrentinoSource())
