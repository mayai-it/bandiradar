"""Per-source isolation + structured results + persistence + logging (0.2.0).

The point of this slice is the FAILURE paths: one source failing must never abort
the others, every fetch yields a structured SourceResult that is returned +
persisted, exit codes reflect the worst outcome, and failures are logged while the
user sees a clean message (no traceback). All offline, no real network/sleeps.
"""

import json
import logging
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from bandiradar import core
from bandiradar.cli import app
from bandiradar.http import FetchError
from bandiradar.models import Opportunity, RawDoc, SourceResult
from bandiradar.storage import Store

NOW = datetime(2026, 6, 6, 0, 0, tzinfo=UTC)
runner = CliRunner()


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "iso.db"))
    yield s
    s.close()


def _raw(sid: str, n: int) -> RawDoc:
    return RawDoc(id=f"{sid}:{n}", source=sid, fetched_at=NOW, payload={"n": n})


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


class _OkSource:
    kind = "tender"

    def __init__(self, sid: str, count: int = 2):
        self.id = sid
        self._count = count

    def fetch(self, since=None, *, limit=None, max_pages=None, progress=None):
        for n in range(1, self._count + 1):
            yield _raw(self.id, n)

    def to_opportunities(self, raw, now=None):
        return [_opp(raw)]

    def load_fixture(self):
        return []


class _FailSource:
    kind = "tender"

    def __init__(self, sid: str, msg: str, kind: str = "rate_limited"):
        self.id = sid
        self._msg = msg
        self._kind = kind

    def fetch(self, since=None, *, limit=None, max_pages=None, progress=None):
        raise FetchError(self._msg, kind=self._kind)
        yield  # pragma: no cover (makes fetch a generator)

    def to_opportunities(self, raw, now=None):
        return [_opp(raw)]

    def load_fixture(self):
        return []


# --------------------------------------------------------------------------- #
# isolation: one source failing never aborts the others
# --------------------------------------------------------------------------- #


def test_one_source_failure_does_not_abort_others(store, monkeypatch):
    sources = {
        "ted": _FailSource("ted", "TED search failed after 5 attempts (HTTP 429)"),
        "lazio": _OkSource("lazio", 3),
    }
    monkeypatch.setattr(core, "get", lambda sid: sources[sid])

    results = core.run_fetch_many(["ted", "lazio"], store, sample=False, now=NOW)
    by = {r.source: r for r in results}

    assert set(by) == {"ted", "lazio"}  # all sources reported, none aborted the run
    assert by["ted"].status == "failed" and "429" in by["ted"].error
    assert by["lazio"].status == "ok" and by["lazio"].new == 3
    # The sibling actually ran and saved despite TED blowing up first.
    assert len(store.list_opportunities(source="lazio")) == 3
    assert core.fetch_exit_code(results) == 3  # worst = rate-limited


# --------------------------------------------------------------------------- #
# exit codes by failure kind
# --------------------------------------------------------------------------- #


def _r(status, kind=None):
    return SourceResult(
        source="x", status=status, error=("e" if kind else None), error_kind=kind
    )


def test_exit_codes_by_failure_kind():
    # Driven by the STRUCTURED error_kind, not string-matching the message.
    assert core.fetch_exit_code([_r("ok"), _r("empty")]) == 0
    assert core.fetch_exit_code([_r("failed", "rate_limited")]) == 3
    assert core.fetch_exit_code([_r("failed", "unavailable")]) == 4
    assert core.fetch_exit_code([_r("failed", "invalid")]) == 2
    assert core.fetch_exit_code([_r("failed", "unknown")]) == 1  # generic fallback
    assert core.fetch_exit_code([_r("partial", "rate_limited")]) == 3
    # worst-first: a failed source dominates an ok one.
    assert core.fetch_exit_code([_r("ok"), _r("failed", "unknown")]) == 1


# --------------------------------------------------------------------------- #
# persistence: one runs row per source, with counts + duration
# --------------------------------------------------------------------------- #


def test_per_source_rows_persisted_with_counts(store, monkeypatch):
    sources = {"a": _OkSource("a", 2), "b": _FailSource("b", "boom")}
    monkeypatch.setattr(core, "get", lambda sid: sources[sid])

    core.run_fetch_many(["a", "b"], store, sample=False, now=NOW)
    rows = store.conn.execute(
        "SELECT source, status, fetched, mapped, new, skipped_invalid, "
        "duration_s, error FROM runs ORDER BY source"
    ).fetchall()
    assert [r["source"] for r in rows] == ["a", "b"]  # one row per source
    a, b = rows
    assert (a["status"], a["fetched"], a["mapped"], a["new"]) == ("ok", 2, 2, 2)
    assert a["duration_s"] is not None
    assert (b["status"], b["fetched"], b["new"]) == ("failed", 0, 0)
    assert b["error"] == "boom"


# --------------------------------------------------------------------------- #
# logging: failures are logged; the user sees a clean message (no traceback)
# --------------------------------------------------------------------------- #


def test_failing_source_logs_error_record(store, monkeypatch, caplog):
    monkeypatch.setattr(
        core, "get", lambda sid: _FailSource("ted", "TED search failed (HTTP 429)")
    )
    with caplog.at_level(logging.ERROR, logger="bandiradar.core"):
        result = core.run_fetch("ted", store, sample=False, now=NOW)

    assert result.status == "failed"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("ted" in m and "429" in m for m in errors)


def test_cli_fetch_failure_is_clean_and_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(
        core,
        "get",
        lambda sid: _FailSource("ted", "TED search failed after 5 attempts (HTTP 429)"),
    )
    res = runner.invoke(
        app, ["fetch", "--source", "ted", "--db", str(tmp_path / "c.db")]
    )

    assert res.exit_code == 3  # rate-limited exit code
    # Clean operational output — a SystemExit (typer.Exit), never a raw traceback.
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "Traceback" not in res.stdout
    assert "ted" in res.stdout and "failed" in res.stdout


def test_cli_fetch_multi_source_one_fails_others_ok(tmp_path, monkeypatch):
    sources = {
        "ted": _FailSource("ted", "TED search failed after 5 attempts (HTTP 429)"),
        "lazio": _OkSource("lazio", 2),
    }
    monkeypatch.setattr(core, "get", lambda sid: sources[sid])
    res = runner.invoke(
        app,
        ["fetch", "--source", "ted,lazio", "--json", "--db", str(tmp_path / "m.db")],
    )
    assert res.exit_code == 3  # worst status (TED rate-limited)
    data = {d["source"]: d for d in json.loads(res.stdout)}
    assert data["ted"]["status"] == "failed"
    assert data["lazio"]["status"] == "ok" and data["lazio"]["new"] == 2
