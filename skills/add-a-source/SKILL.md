---
name: add-a-source
description: Add a new funding-opportunity source adapter to BandiRadar (e.g. a regional bandi portal, a national incentives feed, an OCDS endpoint). Use when the user wants to integrate a new data source, write a Source adapter, map a raw payload into the canonical Opportunity model, or contribute a regional/sector source. Produces an adapter + fixture + offline test that plug in without touching core.
---

# Add a Source to BandiRadar

A source is the project's one extension point (ARCHITECTURE.md §5). Adding one is
a **new file + a fixture + a test** — no changes to core, the matcher, or
storage. The long tail of regional *bandi* is meant to be crowdsourced this way.

## The `Source` contract

```python
class Source(Protocol):
    id: str                         # unique, e.g. "regione_lazio"
    kind: Literal["tender", "grant", "incentive"]

    def fetch(self, since: datetime | None = None) -> Iterable[RawDoc]: ...
    def to_opportunities(self, raw: RawDoc, now=None) -> list[Opportunity]: ...
```

- `fetch()` pulls raw payloads (HTTP/feed/API) and yields `RawDoc`s.
- `to_opportunities()` is a **PURE** mapping: raw → canonical `Opportunity`. No
  network, no I/O — so it is unit-testable offline against a fixture.

## Steps

### 1. Skeleton adapter — `src/bandiradar/sources/<name>.py`

```python
"""<Name> source adapter."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from bandiradar.models import Kind, Opportunity, RawDoc, default_status
from bandiradar.sources.base import register

SOURCE_ID = "<name>"
SOURCE_KIND: Kind = "grant"  # or "tender" / "incentive"

# Do NOT invent a live endpoint. Leave empty + a TODO until confirmed.
SOURCE_URL = ""

FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "fixtures" / "<name>.json"
)


def to_opportunities(raw: RawDoc, now: datetime | None = None) -> list[Opportunity]:
    """PURE: map one raw record (raw.payload) into Opportunity objects."""
    record: dict[str, Any] = raw.payload
    deadline = None  # parse from record, e.g. datetime.fromisoformat(...)
    return [
        Opportunity(
            id=f"{SOURCE_ID}:{record['id']}",
            source=SOURCE_ID,
            source_url=record.get("url") or raw.url or "",
            kind=SOURCE_KIND,
            title=record["title"],
            summary=record.get("description"),
            issuer_name=record.get("issuer"),
            issuer_region=record.get("region"),
            cpv=record.get("cpv", []),
            value_amount=record.get("amount"),
            geo_scope="regional" if record.get("region") else "national",
            region=record.get("region"),
            deadline=deadline,
            status=default_status(deadline, now),
            eligibility_text=record.get("eligibility"),
            raw_ref=raw.id,
            # content_hash auto-fills — do not set it.
        )
    ]


def load_fixture(path: Path | None = None) -> list[RawDoc]:
    """Recorded payloads -> RawDocs, for offline use and tests."""
    data = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    fetched_at = datetime.fromisoformat("1970-01-01T00:00:00+00:00")
    return [
        RawDoc(
            id=f"{SOURCE_ID}:{rec['id']}",
            source=SOURCE_ID,
            fetched_at=fetched_at,
            payload=rec,
            url=rec.get("url"),
        )
        for rec in data["records"]
    ]


class <Name>Source:
    id = SOURCE_ID
    kind: Kind = SOURCE_KIND

    def fetch(self, since: datetime | None = None) -> Iterable[RawDoc]:
        if not SOURCE_URL:
            raise NotImplementedError(
                "Live fetch not wired: confirm the endpoint, then implement. "
                "Use load_fixture() for offline runs."
            )
        raise NotImplementedError("Live fetch not implemented yet.")

    def to_opportunities(
        self, raw: RawDoc, now: datetime | None = None
    ) -> list[Opportunity]:
        return to_opportunities(raw, now=now)

    def load_fixture(self) -> list[RawDoc]:
        return load_fixture()


register(<Name>Source())
```

Then make the package import it for self-registration, in
`src/bandiradar/sources/__init__.py`:

```python
from bandiradar.sources import <name> as <name>  # noqa: F401
```

### 2. Fixture — `data/fixtures/<name>.json`

Record a **real** payload from the source (a small, representative slice). This
is what makes the adapter testable offline with no network and no secrets. If
the source data is private/large, trim it to a handful of records but keep the
real shape. Use a clearly-synthetic URL (`example.invalid`) if you must redact.

### 3. Offline test — `tests/test_<name>.py`

```python
from bandiradar.sources import <name>
from bandiradar.sources.base import get

def test_mapping_against_fixture():
    raws = <name>.load_fixture()
    opps = [o for r in raws for o in <name>.to_opportunities(r)]
    assert opps and all(o.id.startswith("<name>:") for o in opps)
    # assert the specific field mapping you care about, e.g. title/region/cpv.

def test_registered():
    assert get("<name>").id == "<name>"
```

Assert the mapper output against the fixture: ids, titles, region, CPV, value,
deadline → derived status. No network.

### 4. Register + go green

Registration is the `register(<Name>Source())` call at the bottom of the adapter
(step 1). Then:

```bash
uv run pytest        # green before stopping
uv run ruff check .
```

## Guardrails (do not violate)

- **Pure mapper.** `to_opportunities` does no network/I/O — only reads
  `raw.payload`. All side effects live in `fetch()` / `load_fixture()`.
- **Offline test.** Every source ships a fixture + a test that passes with
  **zero secrets** and no network.
- **`extra="forbid"`.** `Opportunity` rejects unknown fields — set only real
  model fields, or construction fails loudly (this catches typos in your mapper).
- **Datetimes normalize to UTC** automatically; pass whatever the source gives
  (naive or offset) and the model coerces it.
- **`content_hash` auto-fills** from the canonical fields — never set it by hand.
- **Don't invent a live endpoint.** Leave it empty with a TODO and raise
  `NotImplementedError` until confirmed against the source's docs.
- **No business logic in the adapter** beyond fetch + map. Matching, storage, and
  orchestration are core's job.
