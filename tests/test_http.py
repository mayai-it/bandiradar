"""Retry/backoff/429 + timeout tests for the shared HTTP helper.

Fully offline: a fake ``send`` callable returns scripted responses/exceptions and
``http._sleep`` is patched so backoff incurs ZERO real delay (the autouse
conftest already no-ops it; here we record the durations to assert backoff).
"""

import httpx
import pytest

from bandiradar import http


class _Resp:
    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}


@pytest.fixture
def sleeps(monkeypatch):
    """Record (don't perform) backoff delays."""
    recorded: list[float] = []
    monkeypatch.setattr(http, "_sleep", lambda s: recorded.append(s))
    return recorded


def _scripted(*outcomes):
    """A send() that returns/raises each outcome in turn."""
    seq = iter(outcomes)

    def send():
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    return send


# --------------------------------------------------------------------------- #
# with_retry
# --------------------------------------------------------------------------- #


def test_429_then_200_succeeds_after_backoff(sleeps):
    send = _scripted(_Resp(429), _Resp(200))
    resp = http.with_retry(send, what="test")
    assert resp.status_code == 200
    assert len(sleeps) == 1  # one backoff between the two attempts


def test_429_honors_retry_after_header(sleeps):
    send = _scripted(_Resp(429, {"retry-after": "7"}), _Resp(200))
    resp = http.with_retry(send, what="test")
    assert resp.status_code == 200
    assert sleeps == [7.0]  # Retry-After respected over exponential backoff


def test_5xx_then_200_succeeds(sleeps):
    send = _scripted(_Resp(503), _Resp(500), _Resp(200))
    resp = http.with_retry(send, what="test")
    assert resp.status_code == 200
    assert len(sleeps) == 2


def test_timeout_then_200_succeeds(sleeps):
    send = _scripted(httpx.ConnectTimeout("slow"), _Resp(200))
    resp = http.with_retry(send, what="test")
    assert resp.status_code == 200
    assert len(sleeps) == 1


def test_exhausted_retries_raise_clean_error(sleeps):
    send = _scripted(*[_Resp(429) for _ in range(10)])
    with pytest.raises(http.FetchError, match="failed after 3 attempts"):
        http.with_retry(send, what="TED search", max_retries=2)
    assert len(sleeps) == 2  # retried twice, then gave up


def test_connection_error_exhausted_raises(sleeps):
    send = _scripted(*[httpx.ConnectError("nope") for _ in range(10)])
    with pytest.raises(http.FetchError, match="ConnectError"):
        http.with_retry(send, what="x", max_retries=1)


def test_non_retryable_4xx_returned_not_retried(sleeps):
    # A 400/404 is NOT retried — returned for the caller to handle (e.g. WP's
    # "past the last page" 400).
    send = _scripted(_Resp(404))
    resp = http.with_retry(send, what="x")
    assert resp.status_code == 404
    assert sleeps == []


def test_backoff_is_exponential(sleeps):
    send = _scripted(*[_Resp(500) for _ in range(10)])
    with pytest.raises(http.FetchError):
        http.with_retry(send, what="x", max_retries=3)
    # 0.5, 1, 2 (base * 2**attempt)
    assert sleeps == [0.5, 1.0, 2.0]


# --------------------------------------------------------------------------- #
# structured error kinds (no string-matching downstream)
# --------------------------------------------------------------------------- #


def test_fetch_error_kind_rate_limited_on_429(sleeps):
    send = _scripted(*[_Resp(429) for _ in range(5)])
    with pytest.raises(http.FetchError) as ei:
        http.with_retry(send, what="x", max_retries=2)
    assert ei.value.kind == "rate_limited"


def test_fetch_error_kind_unavailable_on_5xx(sleeps):
    send = _scripted(*[_Resp(503) for _ in range(5)])
    with pytest.raises(http.FetchError) as ei:
        http.with_retry(send, what="x", max_retries=1)
    assert ei.value.kind == "unavailable"


def test_fetch_error_kind_unavailable_on_connection_error(sleeps):
    send = _scripted(*[httpx.ConnectError("down") for _ in range(5)])
    with pytest.raises(http.FetchError) as ei:
        http.with_retry(send, what="x", max_retries=1)
    assert ei.value.kind == "unavailable"


def test_fetch_error_default_kind_is_unknown():
    assert http.FetchError("boom").kind == "unknown"


# --------------------------------------------------------------------------- #
# blocked classification (403 etc. -> "blocked", not "unknown")
# --------------------------------------------------------------------------- #


def test_status_kind_maps_blocked_and_others():
    assert http.status_kind(401) == "blocked"
    assert http.status_kind(403) == "blocked"  # the TED case
    assert http.status_kind(451) == "blocked"
    assert http.status_kind(429) == "rate_limited"
    assert http.status_kind(500) == "unavailable"
    assert http.status_kind(404) == "invalid"


def _resp(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", "https://x"))


def test_raise_for_status_blocks_on_403():
    with pytest.raises(http.FetchError) as ei:
        http.raise_for_status(_resp(403), what="TED search")
    assert ei.value.kind == "blocked"
    assert "403" in str(ei.value)


def test_raise_for_status_passes_2xx_through():
    ok = _resp(200)
    assert http.raise_for_status(ok, what="x") is ok


# --------------------------------------------------------------------------- #
# total-time cap (a persistently-timing-out host can't burn minutes)
# --------------------------------------------------------------------------- #


def test_with_retry_stops_when_elapsed_budget_spent(monkeypatch, sleeps):
    # monotonic jumps past the budget right after the first attempt.
    clock = iter([0.0] + [500.0] * 20)
    monkeypatch.setattr(http, "_monotonic", lambda: next(clock))
    calls = {"n": 0}

    def send():
        calls["n"] += 1
        raise httpx.ConnectError("down")

    with pytest.raises(http.FetchError) as ei:
        http.with_retry(send, what="x", max_retries=5, max_elapsed=100.0)
    assert calls["n"] == 1  # stopped after one attempt, not all 6
    assert ei.value.kind == "unavailable"


# --------------------------------------------------------------------------- #
# client factory: identifying User-Agent + fail-fast connect timeout
# --------------------------------------------------------------------------- #


def test_client_sends_identifying_user_agent():
    with http.client() as c:
        assert c.headers["user-agent"] == http.USER_AGENT
        assert "bandiradar/" in c.headers["user-agent"]


def test_default_timeout_has_short_connect_bound():
    t = http.default_timeout()
    assert t.connect == http.DEFAULT_CONNECT_TIMEOUT
    assert t.read == http.DEFAULT_TIMEOUT


# --------------------------------------------------------------------------- #
# Optional HTTP relay for CI-blocked hosts (BANDIRADAR_RELAY_*)
# Real httpx over a MockTransport: the rewrite happens at the transport layer
# (final URL, params already merged), so encoding semantics are exercised.
# --------------------------------------------------------------------------- #

RELAY = "https://relay.example.workers.dev/"
SOLR = "https://www.incentivi.gov.it/solr/coredrupal/select"
SOLR_PARAMS = {"q": "*:*", "fq": "index_id:incentivi", "rows": 50, "wt": "json"}


def _recording_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def _set_relay_env(monkeypatch, hosts: str = "www.incentivi.gov.it") -> None:
    monkeypatch.setenv("BANDIRADAR_RELAY_URL", RELAY)
    monkeypatch.setenv("BANDIRADAR_RELAY_TOKEN", "test-relay-token")
    monkeypatch.setenv("BANDIRADAR_RELAY_HOSTS", hosts)


def test_relay_rewrites_allowlisted_host_with_encoded_query(monkeypatch):
    _set_relay_env(monkeypatch)
    seen: list[httpx.Request] = []
    with http.client(transport=_recording_transport(seen)) as c:
        c.get(SOLR, params=SOLR_PARAMS)

    assert len(seen) == 1
    sent = seen[0]
    # The wire request targets the relay, not incentivi.
    assert sent.url.host == "relay.example.workers.dev"
    assert sent.headers["X-Relay-Token"] == "test-relay-token"
    assert sent.headers["Host"] == "relay.example.workers.dev"
    # `u` round-trips to the EXACT original URL — Solr query included.
    original = httpx.URL(sent.url.params["u"])
    assert original.host == "www.incentivi.gov.it"
    assert dict(original.params) == {k: str(v) for k, v in SOLR_PARAMS.items()}
    # And on the wire the original URL is percent-encoded inside the query: its
    # own `?`/`&`/`=` must NOT leak into the relay query — `u` stays the ONLY
    # param, with no literal separators from the Solr query surviving unencoded.
    raw_query = sent.url.query.decode()
    assert raw_query.startswith("u=https%3A%2F%2Fwww.incentivi.gov.it")
    assert list(sent.url.params.keys()) == ["u"]
    assert "&fq=" not in raw_query and "?q=" not in raw_query
    assert "%26fq%3D" in raw_query  # the Solr '&fq=' separator, safely encoded


def test_relay_leaves_other_hosts_direct(monkeypatch):
    _set_relay_env(monkeypatch)
    seen: list[httpx.Request] = []
    with http.client(transport=_recording_transport(seen)) as c:
        c.get("https://api.ted.europa.eu/v3/notices/search", params={"page": 1})

    sent = seen[0]
    assert sent.url.host == "api.ted.europa.eu"  # untouched
    assert "x-relay-token" not in sent.headers
    assert dict(sent.url.params) == {"page": "1"}


def test_no_relay_env_means_zero_rewrites(monkeypatch):
    # conftest already clears the env; be explicit that even the listed host
    # goes direct when the relay is not configured.
    monkeypatch.delenv("BANDIRADAR_RELAY_URL", raising=False)
    seen: list[httpx.Request] = []
    with http.client(transport=_recording_transport(seen)) as c:
        c.get(SOLR, params=SOLR_PARAMS)

    sent = seen[0]
    assert sent.url.host == "www.incentivi.gov.it"
    assert "x-relay-token" not in sent.headers


def test_relay_requires_all_three_vars(monkeypatch):
    # URL + hosts but NO token -> not configured -> direct.
    monkeypatch.setenv("BANDIRADAR_RELAY_URL", RELAY)
    monkeypatch.setenv("BANDIRADAR_RELAY_HOSTS", "www.incentivi.gov.it")
    monkeypatch.delenv("BANDIRADAR_RELAY_TOKEN", raising=False)
    seen: list[httpx.Request] = []
    with http.client(transport=_recording_transport(seen)) as c:
        c.get(SOLR)
    assert seen[0].url.host == "www.incentivi.gov.it"


def test_relay_transport_wraps_caller_transport_and_close_propagates(monkeypatch):
    _set_relay_env(monkeypatch)
    closed: list[bool] = []

    class _Inner(httpx.MockTransport):
        def close(self) -> None:
            closed.append(True)

    inner = _Inner(lambda r: httpx.Response(200, json={}))
    c = http.client(transport=inner)
    c.get(SOLR)
    c.close()
    assert closed == [True]


def test_stream_with_retry_relays_allowlisted_host(monkeypatch):
    _set_relay_env(monkeypatch)
    calls: list[tuple[str, dict]] = []

    class _CM:
        def __enter__(self):
            return httpx.Response(200, request=httpx.Request("GET", RELAY))

        def __exit__(self, *exc):
            return False

    def fake_stream(method, url, **kwargs):
        calls.append((url, kwargs))
        return _CM()

    monkeypatch.setattr(http.httpx, "stream", fake_stream)
    with http.stream_with_retry(
        "GET", SOLR, what="x", params={"fq": "index_id:incentivi"}
    ) as resp:
        assert resp.status_code == 200

    url, kwargs = calls[0]
    parsed = httpx.URL(url)
    assert parsed.host == "relay.example.workers.dev"
    # params were folded into the original URL BEFORE rewriting (no params kwarg
    # left to re-merge onto the relay URL), and the token header is present.
    assert "params" not in kwargs
    inner = httpx.URL(parsed.params["u"])
    assert inner.host == "www.incentivi.gov.it"
    assert inner.params["fq"] == "index_id:incentivi"
    assert kwargs["headers"]["X-Relay-Token"] == "test-relay-token"


def test_stream_with_retry_unconfigured_keeps_params_kwarg(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class _CM:
        def __enter__(self):
            return httpx.Response(200, request=httpx.Request("GET", SOLR))

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        http.httpx, "stream", lambda m, u, **kw: calls.append((u, kw)) or _CM()
    )
    with http.stream_with_retry("GET", SOLR, what="x", params={"a": "b"}) as resp:
        assert resp.status_code == 200
    url, kwargs = calls[0]
    assert url == SOLR  # untouched
    assert kwargs["params"] == {"a": "b"}  # passthrough as before
    assert "X-Relay-Token" not in kwargs["headers"]


# --------------------------------------------------------------------------- #
# Regression — the httpx params trap, stream variant (anac 400 in prod).
# httpx.URL(url, params=None) WIPES a query already embedded in the URL; with the
# relay configured, anac's "?name=<year>.jsonl.gz" template (no params kwarg) went
# out queryless -> 400. Fold only when params is populated.
# --------------------------------------------------------------------------- #

OCP_URL = "https://data.open-contracting.example/download?name=2026.jsonl.gz"


class _StreamCM:
    def __init__(self, url: str):
        self._url = url

    def __enter__(self):
        return httpx.Response(200, request=httpx.Request("GET", self._url))

    def __exit__(self, *exc):
        return False


def test_stream_relay_configured_preserves_embedded_query_for_direct_host(
    monkeypatch,
):
    # Relay ON, but the host is NOT allowlisted (the anac case): the embedded
    # query must survive untouched.
    _set_relay_env(monkeypatch)  # allowlist = www.incentivi.gov.it only
    calls: list[str] = []
    monkeypatch.setattr(
        http.httpx, "stream", lambda m, u, **kw: calls.append(u) or _StreamCM(u)
    )
    with http.stream_with_retry("GET", OCP_URL, what="anac") as resp:
        assert resp.status_code == 200
    assert calls[0] == OCP_URL  # "?name=2026.jsonl.gz" NOT wiped


def test_stream_relay_allowlisted_host_carries_embedded_query_in_u(monkeypatch):
    # Same variant but the host IS allowlisted: the relay `u` must carry the
    # FULL original URL, embedded query included.
    blocked = "https://www.incentivi.gov.it/download?name=2026.jsonl.gz"
    _set_relay_env(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        http.httpx, "stream", lambda m, u, **kw: calls.append(u) or _StreamCM(u)
    )
    with http.stream_with_retry("GET", blocked, what="x") as resp:
        assert resp.status_code == 200
    relayed = httpx.URL(calls[0])
    assert relayed.host == "relay.example.workers.dev"
    inner = httpx.URL(relayed.params["u"])
    assert inner.params["name"] == "2026.jsonl.gz"  # query travelled inside u


# --------------------------------------------------------------------------- #
# Relay URL normalization (config) — default scheme, fail fast on garbage.
# --------------------------------------------------------------------------- #


def test_relay_url_without_scheme_gets_https(monkeypatch):
    from bandiradar import config

    monkeypatch.setenv("BANDIRADAR_RELAY_URL", "relay.example.workers.dev/fwd")
    monkeypatch.setenv("BANDIRADAR_RELAY_TOKEN", "t")
    monkeypatch.setenv("BANDIRADAR_RELAY_HOSTS", "www.incentivi.gov.it")
    url, _token, _hosts = config.relay()
    assert url == "https://relay.example.workers.dev/fwd"


def test_relay_url_malformed_fails_fast_with_clear_message(monkeypatch):
    from bandiradar import config

    monkeypatch.setenv("BANDIRADAR_RELAY_TOKEN", "t")
    monkeypatch.setenv("BANDIRADAR_RELAY_HOSTS", "www.incentivi.gov.it")
    for bad in ("https://", "ftp://relay.example", "https://bad host/x"):
        monkeypatch.setenv("BANDIRADAR_RELAY_URL", bad)
        with pytest.raises(ValueError, match="BANDIRADAR_RELAY_URL is malformed"):
            config.relay()
