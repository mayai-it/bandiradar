"""Live-fetch resilience: pagination cap + progress, and progressive save.

All offline with mocked sources/clients — no real network, no real sleeps
(conftest no-ops backoff). Covers:
- a source honoring `limit` / `max_pages` and emitting progress;
- core's DEFAULT safety cap bounding an otherwise-unbounded source;
- progressive save: records before a mid-stream failure are kept, the run is
  marked partial with the error; a clean source completes.
"""

from datetime import UTC, datetime

import pytest

from bandiradar import core
from bandiradar.models import Opportunity, RawDoc
from bandiradar.sources import ted
from bandiradar.storage import Store

NOW = datetime(2026, 6, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "resilience.db"))
    yield s
    s.close()


def _raw(n: int, source: str = "inf") -> RawDoc:
    return RawDoc(id=f"{source}:{n}", source=source, fetched_at=NOW, payload={"n": n})


def _opp(raw: RawDoc) -> Opportunity:
    return Opportunity(
        id=raw.id,
        source=raw.source,
        source_url="",
        kind="tender",
        title=f"opp {raw.payload['n']}",
        geo_scope="national",
        status="open",
        raw_ref=raw.id,
    )


# --------------------------------------------------------------------------- #
# Pagination cap + progress (source level: TED with a mocked client)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _PagingClient:
    """Returns a FULL page of distinct notices on every call (never ends)."""

    def __init__(self):
        self._n = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        notices = []
        for _ in range(ted._PAGE_LIMIT):
            self._n += 1
            notices.append({"publication-number": f"P{self._n}"})
        return _FakeResponse({"notices": notices})


def test_limit_stops_at_n(monkeypatch):
    monkeypatch.setattr(ted.httpx, "Client", lambda *a, **k: _PagingClient())
    raws = list(ted.TedSource().fetch(limit=5))
    assert len(raws) == 5  # stops mid-page at the cap


def test_max_pages_bounds_fetch(monkeypatch):
    monkeypatch.setattr(ted.httpx, "Client", lambda *a, **k: _PagingClient())
    progress: list[str] = []
    raws = list(ted.TedSource().fetch(max_pages=2, progress=progress.append))
    assert len(raws) == 2 * ted._PAGE_LIMIT
    assert progress and progress[0].startswith("ted: page 1")
    assert any("page 2" in line for line in progress)


# --------------------------------------------------------------------------- #
# Default cap bounds an otherwise-unbounded source (core level)
# --------------------------------------------------------------------------- #


class _UnboundedSource:
    """Yields forever unless given a `limit` (like the real paginating sources)."""

    id = "inf"
    kind = "tender"

    def fetch(self, since=None, *, limit=None, max_pages=None, progress=None):
        n = 0
        while limit is None or n < limit:
            n += 1
            yield _raw(n)
            if n >= 1_000_000:  # absolute backstop so a bug can't hang the test
                return

    def to_opportunities(self, raw, now=None):
        return [_opp(raw)]

    def load_fixture(self):
        return []


def test_default_cap_bounds_unbounded_source(store, monkeypatch):
    monkeypatch.setattr(core, "get", lambda _sid: _UnboundedSource())
    result = core.run_fetch("inf", store, sample=False, now=NOW)
    assert result.fetched == core.DEFAULT_FETCH_LIMIT
    assert result.status == "ok"


def test_explicit_limit_overrides_default(store, monkeypatch):
    monkeypatch.setattr(core, "get", lambda _sid: _UnboundedSource())
    result = core.run_fetch("inf", store, sample=False, now=NOW, limit=7)
    assert result.fetched == 7


# --------------------------------------------------------------------------- #
# Progressive save: keep what arrived before a mid-stream failure
# --------------------------------------------------------------------------- #


class _CleanSource:
    """Yields 3 records and completes normally."""

    id = "flaky"
    kind = "tender"

    def fetch(self, since=None, *, limit=None, max_pages=None, progress=None):
        for n in range(1, 4):
            yield _raw(n, source="flaky")

    def to_opportunities(self, raw, now=None):
        return [_opp(raw)]

    def load_fixture(self):
        return []


def _last_run(store):
    row = store.conn.execute(
        "SELECT fetched, status, error FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["fetched"], row["status"], row["error"]


def test_partial_run_keeps_saved_records(store, monkeypatch):
    class _Flaky:
        id = "flaky"
        kind = "tender"

        def fetch(self, since=None, *, limit=None, max_pages=None, progress=None):
            for n in range(1, 4):
                yield _raw(n, source="flaky")
            raise RuntimeError("TED search failed after 5 attempts (HTTP 429)")

        def to_opportunities(self, raw, now=None):
            return [_opp(raw)]

        def load_fixture(self):
            return []

    monkeypatch.setattr(core, "get", lambda _sid: _Flaky())
    result = core.run_fetch("flaky", store, sample=False, now=NOW)

    assert result.fetched == 3  # the 3 that arrived before the failure
    assert result.new == 3  # ...were saved
    assert result.status == "partial"
    assert "429" in result.error
    assert len(store.list_opportunities(source="flaky")) == 3

    fetched, status, error = _last_run(store)
    assert (fetched, status) == (3, "partial")
    assert "429" in error


def test_clean_source_completes(store, monkeypatch):
    monkeypatch.setattr(core, "get", lambda _sid: _CleanSource())
    result = core.run_fetch("flaky", store, sample=False, now=NOW)
    assert result.fetched == 3
    assert result.status == "ok"
    assert result.error is None
    assert _last_run(store)[1] == "ok"


def test_fetch_failure_before_any_yield_is_failed(store, monkeypatch):
    class _DeadOnArrival:
        id = "dead"
        kind = "tender"

        def fetch(self, since=None, *, limit=None, max_pages=None, progress=None):
            raise RuntimeError("provider/key required")
            yield  # pragma: no cover (makes this a generator)

        def to_opportunities(self, raw, now=None):
            return []

        def load_fixture(self):
            return []

    monkeypatch.setattr(core, "get", lambda _sid: _DeadOnArrival())
    result = core.run_fetch("dead", store, sample=False, now=NOW)
    assert result.fetched == 0
    assert result.status == "failed"  # nothing saved -> failed (not partial)
    assert "required" in result.error
