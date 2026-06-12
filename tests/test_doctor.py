"""`bandiradar doctor` — source + environment diagnostics (0.2.0 slice 3).

Offline: probes are MOCKED sources (no network, no LLM). Covers all-healthy,
one-source-unreachable (isolated, others still checked), a key-dependent source
with no key ("needs key", not a failure), and environment reporting.
"""

import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from bandiradar import core
from bandiradar.cli import app
from bandiradar.http import FetchError
from bandiradar.models import Opportunity, RawDoc

NOW = datetime(2026, 6, 6, 0, 0, tzinfo=UTC)
runner = CliRunner()


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
    requires_llm = False

    def __init__(self, sid: str):
        self.id = sid
        self.kind = "tender"

    def fetch(self, since=None, *, limit=None, max_pages=None, progress=None):
        n = 0
        while limit is None or n < limit:
            n += 1
            yield _raw(self.id, n)

    def to_opportunities(self, raw, now=None):
        return [_opp(raw)]

    def load_fixture(self):
        return []


class _FailSource:
    requires_llm = False

    def __init__(self, sid: str, kind: str = "unavailable"):
        self.id = sid
        self.kind = "tender"
        self._kind = kind

    def fetch(self, since=None, *, limit=None, max_pages=None, progress=None):
        raise FetchError("source is down", kind=self._kind)
        yield  # pragma: no cover

    def to_opportunities(self, raw, now=None):
        return [_opp(raw)]

    def load_fixture(self):
        return []


class _KeySource:
    """LLM-dependent: doctor must NOT probe it when no key is configured."""

    requires_llm = True

    def __init__(self, sid: str):
        self.id = sid
        self.kind = "incentive"

    def fetch(self, since=None, *, limit=None, max_pages=None, progress=None):
        raise AssertionError("must not be probed without a key")
        yield  # pragma: no cover

    def to_opportunities(self, raw, now=None):
        return [_opp(raw)]

    def load_fixture(self):
        return []


def _wire(monkeypatch, sources: dict):
    monkeypatch.setattr(core, "list_sources", lambda: list(sources.values()))
    monkeypatch.setattr(core, "get", lambda sid: sources[sid])


# --------------------------------------------------------------------------- #
# core.run_doctor
# --------------------------------------------------------------------------- #


def test_doctor_all_healthy(monkeypatch, tmp_path):
    _wire(monkeypatch, {"ted": _OkSource("ted"), "lazio": _OkSource("lazio")})
    report = core.run_doctor(db=str(tmp_path / "d.db"))

    assert report.healthy is True
    assert report.exit_code == 0
    by = {s.source: s for s in report.sources}
    assert by["ted"].reachable is True and by["ted"].status == "ok"
    assert by["ted"].parsed is True and by["ted"].needs_key is False
    assert report.env.db_ok is True


def test_doctor_one_unreachable_isolated(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        {"ted": _FailSource("ted", "unavailable"), "lazio": _OkSource("lazio")},
    )
    report = core.run_doctor(db=str(tmp_path / "d.db"))

    by = {s.source: s for s in report.sources}
    assert by["ted"].reachable is False
    assert by["ted"].status == "failed"
    assert by["ted"].error_kind == "unavailable"
    # The other source was still checked (isolation).
    assert by["lazio"].reachable is True and by["lazio"].status == "ok"
    assert report.healthy is False
    assert report.exit_code == 4  # unavailable -> exit 4


def test_doctor_rate_limited_exit_code(monkeypatch, tmp_path):
    _wire(monkeypatch, {"ted": _FailSource("ted", "rate_limited")})
    report = core.run_doctor(db=str(tmp_path / "d.db"))
    assert report.exit_code == 3  # rate_limited


def test_doctor_key_dependent_without_key_is_not_a_failure(monkeypatch, tmp_path):
    # conftest forces BANDIRADAR_LLM_PROVIDER=none -> no key -> not probed.
    _wire(monkeypatch, {"toscana": _KeySource("toscana"), "ted": _OkSource("ted")})
    report = core.run_doctor(db=str(tmp_path / "d.db"))

    by = {s.source: s for s in report.sources}
    assert by["toscana"].needs_key is True
    assert by["toscana"].status == "needs_key"
    assert by["toscana"].reachable is None  # never probed
    assert by["toscana"].key_ok is False
    # "needs key" is informational, NOT a failure.
    assert report.healthy is True
    assert report.exit_code == 0


def test_doctor_env_reflects_config_and_extras(monkeypatch, tmp_path):
    _wire(monkeypatch, {"ted": _OkSource("ted")})
    monkeypatch.setenv("BANDIRADAR_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    # Deterministic SDK/extras presence: only "anthropic" importable.
    monkeypatch.setattr(
        core.importlib.util,
        "find_spec",
        lambda name: object() if name == "anthropic" else None,
    )
    report = core.run_doctor(db=str(tmp_path / "d.db"))

    e = report.env
    assert e.llm_provider == "anthropic"
    assert e.llm_key_present is True
    assert e.llm_ready is True  # provider + key + SDK present
    assert e.extras == {"anthropic": True, "openai": False, "ocr": False}
    assert e.db_ok is True


def test_doctor_reports_unwritable_db(monkeypatch, tmp_path):
    _wire(monkeypatch, {"ted": _OkSource("ted")})
    # A db path under a non-existent, un-creatable parent -> open fails cleanly.
    bad = "/proc/nonexistent-dir/cannot.db"
    report = core.run_doctor(db=bad)
    assert report.env.db_ok is False
    assert report.env.db_error
    assert report.healthy is False  # env problem -> non-zero
    assert report.exit_code != 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_doctor_human_ok(monkeypatch, tmp_path):
    _wire(monkeypatch, {"ted": _OkSource("ted")})
    res = runner.invoke(app, ["doctor", "--db", str(tmp_path / "d.db")])
    assert res.exit_code == 0
    assert "ted" in res.stdout
    assert "environment:" in res.stdout
    assert "verdict: healthy" in res.stdout


def test_cli_doctor_json_and_exit_code(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        {"ted": _FailSource("ted", "rate_limited"), "lazio": _OkSource("lazio")},
    )
    res = runner.invoke(app, ["doctor", "--json", "--db", str(tmp_path / "d.db")])
    assert res.exit_code == 3  # worst = rate_limited
    assert "Traceback" not in res.stdout
    report = json.loads(res.stdout)
    assert report["healthy"] is False
    by = {s["source"]: s for s in report["sources"]}
    assert by["ted"]["error_kind"] == "rate_limited"
    assert by["lazio"]["reachable"] is True


def test_doctor_reports_trust_counts_from_the_real_db(monkeypatch, tmp_path):
    _wire(monkeypatch, {"ted": _OkSource("ted")})
    db = str(tmp_path / "d.db")
    store = core.Store(db)
    try:
        store.upsert_opportunity(
            Opportunity(
                id="toscana:q1",
                source="toscana",
                source_url="https://x/q1",
                kind="incentive",
                title="Bando quarantinato",
                geo_scope="regional",
                status="open",
                raw_ref="toscana:q1",
                provenance="llm",
                confidence=0.1,
                trust_verdict="quarantine",
            ),
            now=NOW,
        )
    finally:
        store.close()

    report = core.run_doctor(db=db)
    assert report.trust_counts == {"toscana": {"quarantine": 1}}

    # The human render surfaces it too.
    res = runner.invoke(app, ["doctor", "--db", db])
    assert "trust (LLM extractions): toscana: quarantine=1" in res.stdout


def test_doctor_trust_counts_empty_without_llm_extractions(monkeypatch, tmp_path):
    _wire(monkeypatch, {"ted": _OkSource("ted")})
    report = core.run_doctor(db=str(tmp_path / "d.db"))
    assert report.trust_counts == {}
    res = runner.invoke(app, ["doctor", "--db", str(tmp_path / "d.db")])
    assert "trust (LLM extractions)" not in res.stdout
