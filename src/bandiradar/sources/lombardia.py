"""Regione Lombardia — regional procurement source (Socrata SODA API).

The first REGIONAL / sub-threshold source. Dataset ``k6cb-4hbm`` ("Bandi di gara
- Osservatorio Regionale") on dati.lombardia.it is a Socrata dataset exposed via
the SODA API (JSON, no auth, CC0). Records are public tender calls (appalti) with
CPV, value, province, object, contracting authority, and an offer deadline.

``to_opportunities`` maps the real record shape to an Opportunity
(``kind="tender"``, ``geo_scope="regional"``, ``region="Lombardia"``).

Attribution: Regione Lombardia open data (CC0 1.0).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from bandiradar import resources
from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.sources.base import register

SOURCE_ID = "lombardia"
SOURCE_KIND: Kind = "tender"  # procurement calls (appalti)

# Socrata SODA endpoint (no auth). Dataset k6cb-4hbm = regional tender observatory.
LOMBARDIA_DATA_URL = "https://dati.lombardia.it/resource/k6cb-4hbm.json"
_PAGE_LIMIT = 1000

FIXTURE_PATH = resources.fixture("lombardia.json")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_opportunities(raw: RawDoc, now: datetime | None = None) -> list[Opportunity]:
    """PURE mapping from one Lombardia tender record (``raw.payload``)."""
    rec: dict[str, Any] = raw.payload
    code = rec["codice_bando"]
    cpv = rec.get("codice_cpv")
    deadline = _parse_dt(rec.get("data_presentazioni_offerte"))
    province = rec.get("provincia")

    opportunity = Opportunity(
        id=f"lombardia:{code}",
        source=SOURCE_ID,
        source_url=f"{LOMBARDIA_DATA_URL}?codice_bando={code}",
        kind=SOURCE_KIND,
        title=rec.get("oggetto_dell_appalto") or code,
        summary=rec.get("procedura_gara"),
        issuer_name=rec.get("stazione_appaltante"),
        issuer_region=province,  # the province within Lombardy
        cpv=[str(cpv)] if cpv else [],
        value_amount=_to_float(rec.get("importo_complessivo_base")),
        value_currency="EUR",
        geo_scope="regional",
        region="Lombardia",
        published_at=_parse_dt(rec.get("data_pubblicazione")),
        deadline=deadline,
        status=default_status(deadline, now),
        eligibility_text=None,  # the observatory has no requirements/body text
        raw_ref=raw.id,
        # content_hash auto-fills.
    )
    return [opportunity]


def load_fixture(path: Path | None = None) -> list[RawDoc]:
    """Read the recorded Lombardia capture into RawDocs (offline)."""
    package = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    fetched_at = _parse_dt(package.get("_captured")) or datetime.fromisoformat(
        "1970-01-01T00:00:00+00:00"
    )
    raws: list[RawDoc] = []
    for rec in package.get("records", []):
        raws.append(
            RawDoc(
                id=f"lombardia:{rec['codice_bando']}",
                source=SOURCE_ID,
                fetched_at=fetched_at,
                payload=rec,
            )
        )
    return raws


class LombardiaSource:
    """Regione Lombardia source. Offline via load_fixture(); live SODA fetch."""

    id = SOURCE_ID
    kind: Kind = SOURCE_KIND

    def fetch(self, since: datetime | None = None) -> Iterable[RawDoc]:
        """Paginated SODA GET (no auth). Deduped by codice_bando (skips lotti)."""
        return self._fetch_pages(since)

    def _fetch_pages(self, since: datetime | None) -> Iterator[RawDoc]:
        offset = 0
        seen: set[str] = set()
        with httpx.Client(timeout=60.0) as client:
            while True:
                params = {
                    "$order": "data_pubblicazione DESC",
                    "$limit": _PAGE_LIMIT,
                    "$offset": offset,
                }
                if since is not None:
                    iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
                    params["$where"] = f"data_pubblicazione > '{iso}'"
                try:
                    response = client.get(LOMBARDIA_DATA_URL, params=params)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise RuntimeError(f"Lombardia SODA fetch failed: {exc}") from exc

                rows = response.json()
                if not rows:
                    break
                for rec in rows:
                    code = rec.get("codice_bando")
                    if not code or code in seen:
                        continue  # one Opportunity per bando, not per lotto
                    seen.add(code)
                    yield RawDoc(
                        id=f"lombardia:{code}",
                        source=SOURCE_ID,
                        fetched_at=datetime.now(tz=UTC),
                        payload=rec,
                    )
                if len(rows) < _PAGE_LIMIT:
                    break
                offset += _PAGE_LIMIT

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)

    def load_fixture(self) -> list[RawDoc]:
        return load_fixture()


register(LombardiaSource())
