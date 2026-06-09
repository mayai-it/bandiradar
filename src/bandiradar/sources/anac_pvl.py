"""ANAC PVL — live feed of OPEN public tenders (Pubblicità a Valore Legale).

Since 2024 the legal publicity of tender calls is on
``pubblicitalegale.anticorruzione.it`` — a public JSON API, **no credentials, no
WAF**, with notices online at least until their deadline. This is the *open-tenders*
feed our other sources lack: ``anac`` (OCDS) is a retrospective monthly dataset for
benchmarks, and ``ted`` is EU-above-threshold only. NEW source id ``anac_pvl`` — the
existing ``anac`` source is untouched.

API (Spring ``Page``):
  GET /api/v0/avvisi?page=&size=&sortField=dataPubblicazione&sortDirection=DESC
      &dataPubblicazioneStart=GG/MM/AAAA&dataPubblicazioneEnd=GG/MM/AAAA
  -> {content:[...], totalPages, totalElements, size, number}

The list payload already embeds the detail (committente + SEZ. C lotti), so there is
NO per-item detail call. We also do NOT call ``/avvisi/{id}/cronologia`` per item
(that is an N+1): amendments are caught by storage change-detection (``content_hash``)
on re-fetch, and ``tipo=="rettifica"`` notices are simply not ingested as open gare.

CAVEATS (honest, see README): ~4k avvisi/day of EVERY type are published; only the
open gare are kept (see ``_is_open_tender``). ``cpv`` from PVL is the Italian LABEL —
we resolve it to the official 8-digit code via ``bandiradar.cpv`` (often a coarse
DIVISION code; unresolved labels stay as keyword text in eligibility_text).
``valore_complessivo_stimato`` (importo) is sparse. Region is resolved structured-first:
``luogo_nuts`` (province) -> ``luogo_istat`` (comune, ISTAT table) -> a conservative
"Comune di X" buyer parse -> national.

Attribution: ANAC — Pubblicità a Valore Legale (public, no license restriction).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from bandiradar import cpv, http, resources
from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.sources.base import ProgressFn, register

SOURCE_ID = "anac_pvl"
SOURCE_KIND: Kind = "tender"

API_BASE = "https://pubblicitalegale.anticorruzione.it/api/v0"
AVVISI_URL = f"{API_BASE}/avvisi"
WEB_BASE = "https://pubblicitalegale.anticorruzione.it/bandi"

_PAGE_SIZE = 100
_MAX_RECORDS = 5000  # safety ceiling when no explicit limit is given
# Interactive defaults — bounded so a plain `fetch` doesn't scan the whole window.
# ~4k avvisi/day of every type are published and only ~3% are open gare, so the
# kept-cap (_MAX_RECORDS) is never reached; a PAGE cap is the real bound. Results
# are dataPubblicazione DESC, so capping pages = "the freshest avvisi" — the right
# default for fetch/monitor. Deep backfill is an explicit --max-pages/--limit/--since.
_DEFAULT_MAX_PAGES = 30
# Recent open gare only; the monitor's continuity comes from incremental `since`.
_DEFAULT_WINDOW_DAYS = 7
_TITLE_MAX = 140

# Observed "bando di indizione" scheda codes (open calls/indagini that carry a
# future deadline). RECORDED for reference only — the filter does NOT depend on
# them (codiceScheda taxonomy is unstable); ``dataScadenza``-in-the-future is the
# robust signal. A later slice can tighten to these once they're confirmed stable.
INDIZIONE_SCHEDE_OBSERVED = ("P2", "P3", "P7", "A1", "A2")

FIXTURE_PATH = resources.fixture("anac_pvl.json")


# --------------------------------------------------------------------------- #
# Static ISTAT province -> region table (the mapper stays pure; data, not I/O)
# --------------------------------------------------------------------------- #

_REGION_PROVINCES: dict[str, list[str]] = {
    "Abruzzo": ["L'Aquila", "Chieti", "Pescara", "Teramo"],
    "Basilicata": ["Matera", "Potenza"],
    "Calabria": ["Catanzaro", "Cosenza", "Crotone", "Reggio Calabria", "Vibo Valentia"],
    "Campania": ["Avellino", "Benevento", "Caserta", "Napoli", "Salerno"],
    "Emilia-Romagna": [
        "Bologna",
        "Ferrara",
        "Forlì-Cesena",
        "Modena",
        "Parma",
        "Piacenza",
        "Ravenna",
        "Reggio Emilia",
        "Rimini",
    ],
    "Friuli-Venezia Giulia": ["Gorizia", "Pordenone", "Trieste", "Udine"],
    "Lazio": ["Frosinone", "Latina", "Rieti", "Roma", "Viterbo"],
    "Liguria": ["Genova", "Imperia", "La Spezia", "Savona"],
    "Lombardia": [
        "Bergamo",
        "Brescia",
        "Como",
        "Cremona",
        "Lecco",
        "Lodi",
        "Mantova",
        "Milano",
        "Monza e della Brianza",
        "Pavia",
        "Sondrio",
        "Varese",
    ],
    "Marche": ["Ancona", "Ascoli Piceno", "Fermo", "Macerata", "Pesaro e Urbino"],
    "Molise": ["Campobasso", "Isernia"],
    "Piemonte": [
        "Alessandria",
        "Asti",
        "Biella",
        "Cuneo",
        "Novara",
        "Torino",
        "Verbano-Cusio-Ossola",
        "Vercelli",
    ],
    "Puglia": [
        "Bari",
        "Barletta-Andria-Trani",
        "Brindisi",
        "Foggia",
        "Lecce",
        "Taranto",
    ],
    "Sardegna": [
        "Cagliari",
        "Nuoro",
        "Oristano",
        "Sassari",
        "Sud Sardegna",
        # historic provinces still seen in older data
        "Carbonia-Iglesias",
        "Medio Campidano",
        "Ogliastra",
        "Olbia-Tempio",
    ],
    "Sicilia": [
        "Agrigento",
        "Caltanissetta",
        "Catania",
        "Enna",
        "Messina",
        "Palermo",
        "Ragusa",
        "Siracusa",
        "Trapani",
    ],
    "Toscana": [
        "Arezzo",
        "Firenze",
        "Grosseto",
        "Livorno",
        "Lucca",
        "Massa-Carrara",
        "Pisa",
        "Pistoia",
        "Prato",
        "Siena",
    ],
    "Trentino-Alto Adige": ["Bolzano", "Trento"],
    "Umbria": ["Perugia", "Terni"],
    "Valle d'Aosta": ["Aosta"],
    "Veneto": [
        "Belluno",
        "Padova",
        "Rovigo",
        "Treviso",
        "Venezia",
        "Verona",
        "Vicenza",
    ],
}

# Spelling variants ANAC uses for ``luogo_nuts`` that differ from the ISTAT name.
_PROVINCE_ALIASES: dict[str, str] = {
    "reggio di calabria": "Calabria",
    "reggio nell'emilia": "Emilia-Romagna",
    "bolzano/bozen": "Trentino-Alto Adige",
    "bozen": "Trentino-Alto Adige",
    "monza e brianza": "Lombardia",
    "forli-cesena": "Emilia-Romagna",
    "massa carrara": "Toscana",
    "valle d'aosta/vallée d'aoste": "Valle d'Aosta",
    "vallée d'aoste": "Valle d'Aosta",
    "verbania": "Piemonte",
}


def _norm(s: str) -> str:
    return s.strip().casefold()


_PROVINCE_TO_REGION: dict[str, str] = {
    _norm(prov): region for region, provs in _REGION_PROVINCES.items() for prov in provs
}
_PROVINCE_TO_REGION.update(_PROVINCE_ALIASES)


def region_for_nuts(luogo_nuts: str | None) -> str | None:
    """Resolve a PVL ``luogo_nuts`` (province name) to one of the 20 regions, or
    ``None`` for ITALIA / a country / an unmapped value (-> national geo)."""
    if not luogo_nuts:
        return None
    return _PROVINCE_TO_REGION.get(_norm(luogo_nuts))


@lru_cache(maxsize=1)
def _comune_to_region() -> dict[str, str]:
    """Packaged ISTAT comune (casefolded) -> region map (loaded once)."""
    return json.loads(resources.comuni_map().read_text(encoding="utf-8"))


def region_for_comune(luogo_istat: str | None) -> str | None:
    """Resolve a PVL ``luogo_istat`` (comune) to its region via the ISTAT table."""
    if not luogo_istat:
        return None
    return _comune_to_region().get(luogo_istat.strip().casefold())


_BUYER_COMUNE_RE = re.compile(r"\bcomun[ei] di\s+([a-zà-ù'’\- ]+)", re.IGNORECASE)


def region_from_buyer(buyer: str | None) -> str | None:
    """Conservative last resort: only a "Comune di <X>" buyer name -> X's region.
    Anything else returns None (we do not guess from arbitrary authority names)."""
    if not buyer:
        return None
    m = _BUYER_COMUNE_RE.search(buyer)
    if not m:
        return None
    # take a growing prefix of the captured words (handles multi-word comuni)
    words = m.group(1).replace("’", "'").split()
    region_map = _comune_to_region()
    for n in range(len(words), 0, -1):
        region = region_map.get(" ".join(words[:n]).casefold())
        if region is not None:
            return region
    return None


def resolve_region(
    luogo_nuts: str | None, luogo_istat: str | None, buyer: str | None
) -> str | None:
    """Structured-first region resolution: province (luogo_nuts) -> comune
    (luogo_istat) -> a conservative "Comune di X" buyer parse -> None (national)."""
    return (
        region_for_nuts(luogo_nuts)
        or region_for_comune(luogo_istat)
        or region_from_buyer(buyer)
    )


# --------------------------------------------------------------------------- #
# Pure payload helpers
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    """Wall-clock UTC — the live-fetch reference. Isolated so the offline mapper
    never depends on it (and so it is trivially patchable in tests)."""
    return datetime.now(tz=UTC)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _template(payload: dict[str, Any]) -> dict[str, Any]:
    tpl = payload.get("template") or []
    if tpl and isinstance(tpl[0], dict):
        return tpl[0].get("template") or {}
    return {}


def _lotti(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The SEZ. C - Oggetto items (lotti) across the template."""
    out: list[dict[str, Any]] = []
    for section in _template(payload).get("sections") or []:
        if str(section.get("name", "")).startswith("SEZ. C"):
            out += section.get("items") or []
    return out


def _buyer(payload: dict[str, Any]) -> str | None:
    for section in _template(payload).get("sections") or []:
        sa = (section.get("fields") or {}).get("soggetti_sa")
        if sa:
            return sa[0].get("denominazione_amministrazione")
    return None


def _deadline(payload: dict[str, Any]) -> datetime | None:
    """Effective deadline: ``dataScadenza``, else a lotto ``termine_ricezione`` /
    ``termine_invito``. The single source of truth for both filter and status."""
    dl = _parse_dt(payload.get("dataScadenza"))
    if dl is not None:
        return dl
    for lotto in _lotti(payload):
        dl = _parse_dt(lotto.get("termine_ricezione") or lotto.get("termine_invito"))
        if dl is not None:
            return dl
    return None


def _is_open_tender(payload: dict[str, Any], now: datetime | None = None) -> bool:
    """Keep an avviso IFF it is a still-open call: an original notice (not a
    rettifica/esito), active, not obscured, and with a deadline in the FUTURE.

    ``dataScadenza``-in-the-future is the robust signal; we deliberately do NOT
    gate on ``codiceScheda`` (see ``INDIZIONE_SCHEDE_OBSERVED``)."""
    if payload.get("tipo") != "avviso":
        return False
    if not payload.get("attivo") or payload.get("oscurato"):
        return False
    deadline = _deadline(payload)
    if deadline is None:
        return False
    reference = now if now is not None else _now()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return deadline > reference


def _value_total(lotti: list[dict[str, Any]]) -> float | None:
    """Sum the per-lotto estimated values; ``None`` when none are present."""
    vals = []
    for lotto in lotti:
        v = lotto.get("valore_complessivo_stimato")
        if isinstance(v, int | float):
            vals.append(float(v))
    return sum(vals) if vals else None


def _matcher_text(oggetto: str, lotti: list[dict[str, Any]]) -> str | None:
    """Free text fed to the matcher: oggetto + per-lotto CPV LABELS and
    descriptions + the CIGs (PVL gives no numeric CPV — labels carry the signal)."""
    parts: list[str] = [oggetto] if oggetto else []
    for lotto in lotti:
        for key in ("cpv", "descrizione"):
            val = lotto.get(key)
            if val:
                parts.append(str(val))
    cigs = [lotto.get("cig") for lotto in lotti if lotto.get("cig")]
    if cigs:
        parts.append("CIG: " + ", ".join(cigs))
    text = " — ".join(dict.fromkeys(parts)).strip()  # dedupe, keep order
    return text or None


def to_opportunities(raw: RawDoc, now: datetime | None = None) -> list[Opportunity]:
    """PURE mapping of one PVL avviso -> Opportunity. Non-open avvisi map to ``[]``
    (the filter lives here too, so it is unit-testable without any network).

    Reference time: an explicit ``now`` wins; otherwise ``raw.fetched_at`` — the
    moment the doc was observed (≈now for a live fetch; the fixture's ``_captured``
    for ``--sample``). This pins the offline demo to the capture snapshot, so it
    always shows the gare that were open *then* — never silently 0 once the fixture
    deadlines pass wall-clock (the "--sample always runs offline" guarantee)."""
    payload: dict[str, Any] = raw.payload
    reference = now if now is not None else raw.fetched_at
    if not _is_open_tender(payload, reference):
        return []

    id_avviso = payload["idAvviso"]
    oggetto = (_template(payload).get("metadata") or {}).get("descrizione") or ""
    oggetto = oggetto.strip()
    lotti = _lotti(payload)
    buyer = _buyer(payload)
    nuts = next(
        (lotto.get("luogo_nuts") for lotto in lotti if lotto.get("luogo_nuts")), None
    )
    istat = next(
        (lotto.get("luogo_istat") for lotto in lotti if lotto.get("luogo_istat")), None
    )
    # Region: province -> comune -> conservative "Comune di X" buyer parse -> national.
    region = resolve_region(nuts, istat, buyer)
    # CPV: resolve the Italian LABEL(s) to official codes (often coarse divisions);
    # unresolved labels stay in eligibility_text (built below) as keyword signal.
    cpv_codes = cpv.resolve_labels(
        [lotto.get("cpv") for lotto in lotti if lotto.get("cpv")]
    )
    deadline = _deadline(payload)

    opportunity = Opportunity(
        id=f"{SOURCE_ID}:{id_avviso}",
        source=SOURCE_ID,
        source_url=f"{WEB_BASE}/{id_avviso}",
        kind=SOURCE_KIND,
        title=(oggetto or id_avviso)[:_TITLE_MAX],
        summary=None,
        issuer_name=buyer,
        issuer_region=nuts or istat if region else None,  # the locality, when resolved
        cpv=cpv_codes,  # resolved 8-digit codes (label kept in eligibility_text)
        value_amount=_value_total(lotti),
        value_currency="EUR",
        geo_scope="regional" if region else "national",  # unresolved -> national
        region=region,
        published_at=_parse_dt(payload.get("dataPubblicazione")),
        deadline=deadline,
        status=default_status(deadline, reference),
        eligibility_text=_matcher_text(oggetto, lotti),
        raw_ref=raw.id,
        # content_hash auto-fills.
    )
    return [opportunity]


# --------------------------------------------------------------------------- #
# Fixture (offline) + live fetch
# --------------------------------------------------------------------------- #


def load_fixture(path: Path | None = None) -> list[RawDoc]:
    """Read the recorded PVL capture into RawDocs (offline)."""
    package = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    fetched_at = _parse_dt(package.get("_captured")) or datetime(1970, 1, 1, tzinfo=UTC)
    return [
        RawDoc(
            id=f"{SOURCE_ID}:{rec['idAvviso']}",
            source=SOURCE_ID,
            fetched_at=fetched_at,
            payload=rec,
        )
        for rec in package.get("records", [])
    ]


class AnacPvlSource:
    """ANAC PVL source. Offline via load_fixture(); live via the public JSON API."""

    id = SOURCE_ID
    kind: Kind = SOURCE_KIND

    def fetch(
        self,
        since: datetime | None = None,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
        progress: ProgressFn | None = None,
    ) -> Iterable[RawDoc]:
        """Paginated GET /avvisi (no auth), yielded LAZILY. Pre-filters to OPEN gare
        so the ``limit`` budget is spent on real candidates, not the ~70% of
        esiti/rettifiche. Retries transient HTTP failures with backoff."""
        return self._fetch_pages(since, limit, max_pages, progress)

    def _fetch_pages(
        self,
        since: datetime | None,
        limit: int | None,
        max_pages: int | None,
        progress: ProgressFn | None,
    ) -> Iterator[RawDoc]:
        now = _now()
        start = (
            since if since is not None else now - timedelta(days=_DEFAULT_WINDOW_DAYS)
        )
        params_base = {
            "sortField": "dataPubblicazione",
            "sortDirection": "DESC",
            "dataPubblicazioneStart": start.astimezone(UTC).strftime("%d/%m/%Y"),
            "dataPubblicazioneEnd": now.strftime("%d/%m/%Y"),
            "size": _PAGE_SIZE,
        }
        # Bounds: a kept-record cap (limit) AND a page cap (the real bound — the
        # keep-rate is ~3%, so limit is rarely hit). max_pages overrides the default.
        cap = limit if limit is not None else _MAX_RECORDS
        page_cap = max_pages if max_pages is not None else _DEFAULT_MAX_PAGES
        kept = 0
        scanned = 0
        stop = "window-end"
        with httpx.Client(timeout=http.DEFAULT_TIMEOUT) as client:
            while True:
                if kept >= cap:
                    stop = "limit"
                    break
                if scanned >= page_cap:
                    stop = "page-cap"
                    break
                params = {**params_base, "page": scanned}
                response = http.with_retry(
                    lambda params=params: client.get(AVVISI_URL, params=params),
                    what="ANAC PVL fetch",
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise RuntimeError(f"ANAC PVL fetch failed: {exc}") from exc

                body = response.json()
                scanned += 1
                content = body.get("content") or []
                if not content:
                    break
                for rec in content:
                    if kept >= cap:
                        break
                    if not _is_open_tender(rec, now):
                        continue  # skip esiti/rettifiche/closed before spending budget
                    kept += 1
                    yield RawDoc(
                        id=f"{SOURCE_ID}:{rec['idAvviso']}",
                        source=SOURCE_ID,
                        fetched_at=now,
                        payload=rec,
                    )
                if progress is not None:
                    progress(f"anac_pvl: page {scanned}, {kept} open gare kept")
                if scanned >= (body.get("totalPages") or 0):
                    break
        if progress is not None:
            progress(
                f"anac_pvl: stopped [{stop}] — {scanned} pages scanned, "
                f"{kept} open gare kept"
            )

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)

    def load_fixture(self) -> list[RawDoc]:
        return load_fixture()


register(AnacPvlSource())
