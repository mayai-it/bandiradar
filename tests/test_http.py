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
