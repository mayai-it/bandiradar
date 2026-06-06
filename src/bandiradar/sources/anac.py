"""ANAC / PNCP (OCDS) source — historical / awarded contracts (ARCHITECTURE.md §5).

**Honest scope:** the ANAC OCDS feed on the Open Contracting mirror is
**retrospective** — awarded public contracts (> €40k, refreshed monthly), with no
future application deadlines. So this source surfaces **mostly-CLOSED**
opportunities; the matcher correctly drops them. Its real value is
historical / market analysis (see the intelligence benchmark track). **Open**
tenders come from TED and the regional sources.

``to_opportunities`` is a PURE map of one OCDS compiled release to an
``Opportunity``. ``fetch`` streams the live mirror via the shared memory-safe
reader (:mod:`bandiradar.ocp`) with a HARD CAP so we never ingest the whole
retrospective dataset. ``load_fixture`` replays recorded real releases offline.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandiradar import resources
from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.ocp import stream_releases
from bandiradar.sources.base import ProgressFn, register

SOURCE_ID = "anac"
SOURCE_KIND: Kind = "tender"

# Hard cap: the per-year OCDS file holds thousands of releases; never ingest the
# whole retrospective dataset into the opportunity store.
MAX_ITEMS = 500

# Bundled offline fixture: real OCDS releases recorded from the OCP mirror.
FIXTURE_PATH = resources.fixture("anac_sample.json")

ReleaseStreamer = Callable[[int], Iterable[dict[str, Any]]]

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _parse_dt(value: Any) -> datetime | None:
    """Parse an OCDS timestamp, tolerant of the mirror's malformed ``date`` field.

    Clean ISO values (e.g. the tenderPeriod ``endDate`` ``2022-02-18T12:00:00Z``)
    parse directly; the compiled-release ``date`` arrives malformed (e.g.
    ``2025-01-08 17:31:38.793T12:00:00Z``), so we fall back to its leading
    ``YYYY-MM-DD`` at UTC midnight. The model coerces any naive result to UTC.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        match = _DATE_RE.match(value.strip())
        if not match:
            return None
        parsed = datetime(int(match[1]), int(match[2]), int(match[3]))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _cpv_from_items(items: Any) -> list[str]:
    """CPV ids from an OCDS items list (classification scheme == CPV)."""
    codes: list[str] = []
    for item in items or []:
        classification = item.get("classification") or {}
        scheme = str(classification.get("scheme", "")).upper()
        if scheme == "CPV" and classification.get("id"):
            codes.append(str(classification["id"]))
    return codes


def _cpv_codes(release: dict[str, Any]) -> list[str]:
    """CPV from the tender items, falling back to the award items."""
    codes = _cpv_from_items((release.get("tender") or {}).get("items"))
    if codes:
        return codes
    for award in release.get("awards") or []:
        codes = _cpv_from_items(award.get("items"))
        if codes:
            return codes
    return []


def _value(release: dict[str, Any]) -> dict[str, Any]:
    """Value preferring the award amount, falling back to the tender value."""
    for award in release.get("awards") or []:
        value = award.get("value") or {}
        if value.get("amount") is not None:
            return value
    return (release.get("tender") or {}).get("value") or {}


def to_opportunities(raw: RawDoc, now: datetime | None = None) -> list[Opportunity]:
    """PURE mapping from one OCDS compiled release (``raw.payload``) to ``Opportunity``.

    No I/O. The OCP/ANAC address carries no region/NUTS field, so ``region`` is
    ``None`` and ``geo_scope`` is ``"national"``. ``now`` is forwarded to
    :func:`default_status` so tests can pin the derived status.
    """
    release: dict[str, Any] = raw.payload
    ocid = release["ocid"]
    tender: dict[str, Any] = release.get("tender") or {}
    value = _value(release)

    deadline = _parse_dt((tender.get("tenderPeriod") or {}).get("endDate"))
    description = tender.get("description")

    opportunity = Opportunity(
        id=f"{SOURCE_ID}:{ocid}",
        source=SOURCE_ID,
        source_url=release.get("url") or raw.url or "",
        kind=SOURCE_KIND,
        title=tender.get("title") or description or ocid,
        summary=description,
        issuer_name=(release.get("buyer") or {}).get("name"),
        issuer_region=None,  # absent in OCP/ANAC data
        cpv=_cpv_codes(release),
        value_amount=value.get("amount"),
        value_currency=value.get("currency") or "EUR",
        geo_scope="national",  # no region in the data
        region=None,
        published_at=_parse_dt(release.get("date")),
        deadline=deadline,
        status=default_status(deadline, now),
        eligibility_text=tender.get("eligibilityCriteria") or description,
        raw_ref=raw.id,
        # content_hash auto-fills from the canonical fields.
    )
    return [opportunity]


def load_fixture(path: Path | None = None) -> list[RawDoc]:
    """Read recorded real OCDS releases into ``RawDoc``s (offline, no network)."""
    package = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    fetched_at = _parse_dt(package.get("_captured")) or datetime.fromisoformat(
        "1970-01-01T00:00:00+00:00"
    )
    raws: list[RawDoc] = []
    for release in package.get("releases", []):
        ocid = release["ocid"]
        raws.append(
            RawDoc(
                id=f"{SOURCE_ID}:{ocid}",
                source=SOURCE_ID,
                fetched_at=fetched_at,
                payload=release,
                url=release.get("url"),
            )
        )
    return raws


def _resolve_stream(streamer: ReleaseStreamer, year: int) -> Iterator[dict[str, Any]]:
    """Yield releases for the first reachable year, trying ``year`` then ``year-1``.

    The current year's file may not be published yet (404). We force the HTTP
    connect by pulling the first release, falling back to the previous year on a
    download error.
    """
    last_error: RuntimeError | None = None
    for candidate in (year, year - 1):
        try:
            iterator = iter(streamer(candidate))
            first = next(iterator)
        except StopIteration:
            return iter(())  # reachable but empty
        except RuntimeError as exc:  # download failed -> try the previous year
            last_error = exc
            continue
        return itertools.chain([first], iterator)
    raise last_error if last_error else RuntimeError("ANAC OCDS: no reachable year")


class AnacSource:
    """ANAC/PNCP OCDS source — historical/awarded contracts (mostly closed)."""

    id = SOURCE_ID
    kind: Kind = SOURCE_KIND

    def fetch(
        self,
        since: datetime | None = None,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
        progress: ProgressFn | None = None,
        max_items: int = MAX_ITEMS,
        year: int | None = None,
        streamer: ReleaseStreamer | None = None,
    ) -> Iterable[RawDoc]:
        """Stream live ANAC OCDS releases (capped). Historical data — mostly closed.

        Caps at ``limit`` (else ``max_items``) so we never ingest the whole
        retrospective dataset. With ``since`` set, releases published before it are
        skipped. ``streamer`` is injectable for tests; live it is the shared
        memory-safe OCP reader (which retries transient HTTP failures).
        """
        streamer = streamer if streamer is not None else stream_releases
        target_year = year if year is not None else datetime.now(tz=UTC).year
        cap = limit if limit is not None else max_items
        releases = _resolve_stream(streamer, target_year)
        return self._scrape(releases, since, cap, progress)

    def _scrape(
        self,
        releases: Iterable[dict[str, Any]],
        since: datetime | None,
        max_items: int,
        progress: ProgressFn | None = None,
    ) -> Iterator[RawDoc]:
        count = 0
        for release in releases:
            ocid = release.get("ocid")
            if not ocid:
                continue
            published = _parse_dt(release.get("date"))
            if since is not None and published is not None and published < since:
                continue
            yield RawDoc(
                id=f"{SOURCE_ID}:{ocid}",
                source=SOURCE_ID,
                fetched_at=datetime.now(tz=UTC),
                payload=release,
                url=release.get("url"),
            )
            count += 1
            if progress is not None and count % 100 == 0:
                progress(f"anac: {count} fetched")
            if count >= max_items:
                break

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)

    def load_fixture(self) -> list[RawDoc]:
        """Offline RawDocs from the bundled real-release fixture (used by --sample)."""
        return load_fixture()


register(AnacSource())
