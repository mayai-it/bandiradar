"""Shared HTTP resilience for live fetches: retry + backoff + 429/Retry-After.

Live source fetches are flaky (rate limits, 5xx, dropped connections). This module
centralizes the retry policy so every adapter behaves the same and no single 429
kills a run:

- a bounded number of retries on TRANSIENT failures — HTTP 429, any 5xx, request
  timeouts, and connection/transport errors;
- exponential backoff between attempts, but honoring a ``Retry-After`` header on a
  429 when present;
- a CLEAR :class:`FetchError` when retries are exhausted (subclasses
  ``RuntimeError`` so existing ``except RuntimeError`` paths still catch it).

Non-retryable responses (2xx, and 4xx other than 429) are returned as-is — the
caller decides what to do (e.g. the WordPress base treats a 400 as "past the last
page"). Per-request timeouts come from the caller's ``httpx.Client(timeout=...)``;
:data:`DEFAULT_TIMEOUT` is the shared default.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx

from bandiradar import __version__, config
from bandiradar.models import FetchErrorKind

DEFAULT_TIMEOUT = 60.0
# A short connect timeout so an unreachable host fails fast (was the dominant cost:
# a 60s connect timeout x 5 attempts ≈ 5 min). Read stays at DEFAULT_TIMEOUT.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 4
# Hard ceiling on cumulative wall-clock per logical request (across all retries),
# so a host that keeps timing out can't burn minutes. The per-attempt timeout still
# applies; this just bounds the *sum* of attempts + backoff.
DEFAULT_MAX_ELAPSED = 120.0
_BACKOFF_BASE = 0.5  # seconds; attempt n waits base * 2**n (capped)
_BACKOFF_CAP = 30.0

# An honest, identifying User-Agent on every live request (some endpoints — e.g.
# TED — 403 the default ``python-httpx/x`` UA). Sent on ALL sources via `client()`.
USER_AGENT = f"bandiradar/{__version__} (+https://github.com/mayai-it/bandiradar)"
DEFAULT_HEADERS: dict[str, str] = {"User-Agent": USER_AGENT}

# 4xx that mean "the server is refusing us" (UA/IP/geo block, auth) — distinct from
# a transient outage. Surfaced as the structured kind "blocked", not "unknown".
_BLOCKED_STATUSES = frozenset({401, 403, 451})

# Transient httpx exceptions worth retrying (timeouts + connection/transport).
_TRANSIENT_EXC = (httpx.TimeoutException, httpx.TransportError)

# Inject-able sleep + clock so tests run with zero real delay (monkeypatch these).
_sleep = time.sleep
_monotonic = time.monotonic


def default_timeout() -> httpx.Timeout:
    """The shared timeout: a short connect bound + the longer read budget."""
    return httpx.Timeout(DEFAULT_TIMEOUT, connect=DEFAULT_CONNECT_TIMEOUT)


# Header carrying the relay auth token (value from env/secrets, never the repo).
RELAY_TOKEN_HEADER = "X-Relay-Token"  # nosec B105


class RelayTransport(httpx.BaseTransport):
    """Transport wrapper that reroutes allowlisted hosts through an HTTP relay.

    Generic, not per-source: working at the TRANSPORT layer means the FINAL request
    URL (with httpx's ``params`` already merged and encoded) is captured, so the
    rewrite is fully transparent to adapters. A request whose host is in the
    allowlist becomes ``GET <relay>?u=<urlencoded original URL>`` with the
    ``X-Relay-Token`` header; every other request passes through untouched.
    The relay itself (e.g. a Cloudflare Worker with its own host allowlist) is the
    OPERATOR'S infrastructure — this repo only knows how to address one.
    """

    def __init__(
        self,
        relay_url: str,
        token: str,
        hosts: frozenset[str],
        inner: httpx.BaseTransport | None = None,
    ) -> None:
        self._relay = httpx.URL(relay_url)
        self._token = token
        self._hosts = hosts
        self._inner = inner if inner is not None else httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (request.url.host or "").lower() in self._hosts:
            # copy_set_param percent-encodes the full original URL (query included).
            rerouted = self._relay.copy_set_param("u", str(request.url))
            request.url = rerouted
            request.headers["Host"] = rerouted.netloc.decode("ascii")
            request.headers[RELAY_TOKEN_HEADER] = self._token
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def _relay_transport(
    inner: httpx.BaseTransport | None = None,
) -> httpx.BaseTransport | None:
    """A RelayTransport when the relay env is fully configured, else None."""
    cfg = config.relay()
    if cfg is None:
        return None
    relay_url, token, hosts = cfg
    return RelayTransport(relay_url, token, hosts, inner=inner)


def client(
    *,
    timeout: float | httpx.Timeout | None = None,
    headers: dict[str, str] | None = None,
    **kwargs: object,
) -> httpx.Client:
    """Build an ``httpx.Client`` that ALWAYS sends the identifying User-Agent and a
    fail-fast connect timeout. Use this instead of ``httpx.Client(...)`` directly so
    every source behaves the same. Caller headers override the defaults.

    When the optional relay is configured (see :func:`config.relay`), the client's
    transport reroutes allowlisted hosts through it — wrapping any caller-supplied
    ``transport`` as the inner one. With no relay env set, behaviour is unchanged."""
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    inner = kwargs.pop("transport", None)
    relayed = _relay_transport(inner=inner)  # type: ignore[arg-type]
    if relayed is not None:
        kwargs["transport"] = relayed
    elif inner is not None:
        kwargs["transport"] = inner  # no relay: caller's transport passes through
    return httpx.Client(
        timeout=timeout if timeout is not None else default_timeout(),
        headers=merged,
        **kwargs,  # type: ignore[arg-type]
    )


def status_kind(status: int) -> FetchErrorKind:
    """Map a non-2xx status to a structured kind (no string-matching downstream)."""
    if status == 429:
        return "rate_limited"
    if status in _BLOCKED_STATUSES:
        return "blocked"
    if status >= 500:
        return "unavailable"
    return "invalid"  # other 4xx (bad request / not found) = bad call, not an outage


def raise_for_status(response: httpx.Response, *, what: str) -> httpx.Response:
    """Like ``response.raise_for_status()`` but raises a CLASSIFIED ``FetchError`` so
    a 403 block reads as ``blocked`` (not ``unknown``). Returns the response on 2xx.

    Delegates to the response's own ``raise_for_status`` and only re-wraps the raised
    ``HTTPStatusError`` — so any HTTP client (incl. test doubles) works unchanged."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise FetchError(
            f"{what} failed: HTTP {status}", kind=status_kind(status)
        ) from exc
    return response


class FetchError(RuntimeError):
    """Raised when a live request still fails after exhausting all retries.

    Carries a STRUCTURED :data:`~bandiradar.models.FetchErrorKind` so callers
    classify failures without string-matching the message.
    """

    def __init__(self, message: str, *, kind: FetchErrorKind = "unknown") -> None:
        super().__init__(message)
        self.kind: FetchErrorKind = kind


def _is_retryable_status(status: int) -> bool:
    return status == 429 or status >= 500


def _status_kind(status: int) -> FetchErrorKind:
    """Map a retryable HTTP status to a structured error kind."""
    return "rate_limited" if status == 429 else "unavailable"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header expressed as an integer number of seconds."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form: fall back to exponential backoff


def _backoff_seconds(attempt: int) -> float:
    return min(_BACKOFF_CAP, _BACKOFF_BASE * (2**attempt))


def with_retry(
    send: Callable[[], httpx.Response],
    *,
    what: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_elapsed: float = DEFAULT_MAX_ELAPSED,
) -> httpx.Response:
    """Call ``send`` (one HTTP request), retrying transient failures with backoff.

    ``send`` is a zero-arg callable that performs the request and returns an
    ``httpx.Response`` (e.g. ``lambda: client.get(url, params=params)``). On 429 a
    ``Retry-After`` header is honored; otherwise exponential backoff is used.
    Returns the first non-retryable response; raises :class:`FetchError` once
    ``max_retries`` retries are used up OR the cumulative ``max_elapsed`` budget is
    spent (so a persistently-timing-out host can't burn minutes).
    """
    last_error = "unknown error"
    last_kind: FetchErrorKind = "unknown"
    start = _monotonic()
    for attempt in range(max_retries + 1):
        try:
            response = send()
        except _TRANSIENT_EXC as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            last_kind = "unavailable"  # timeout / connection error
            delay = _backoff_seconds(attempt)
        else:
            if not _is_retryable_status(response.status_code):
                return response
            last_error = f"HTTP {response.status_code}"
            last_kind = _status_kind(response.status_code)
            delay = None
            if response.status_code == 429:
                delay = _retry_after_seconds(response)
            if delay is None:
                delay = _backoff_seconds(attempt)
        if attempt >= max_retries or _monotonic() - start >= max_elapsed:
            break
        _sleep(delay)
    raise FetchError(
        f"{what} failed after {attempt + 1} attempts ({last_error})",
        kind=last_kind,
    )


@contextmanager
def stream_with_retry(
    method: str,
    url: str,
    *,
    what: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_elapsed: float = DEFAULT_MAX_ELAPSED,
    timeout: float | httpx.Timeout | None = None,
    **kwargs: object,
) -> Iterator[httpx.Response]:
    """Open a streaming request, retrying transient failures on ESTABLISHMENT.

    Yields an open ``httpx.Response`` for byte streaming. Only the connection +
    initial status are retried (a failure mid-stream cannot be resumed); that
    partial case is handled at the run level by progressive save. Sends the shared
    identifying User-Agent (caller ``headers`` override). Honours the optional
    relay for allowlisted hosts (the top-level ``httpx.stream`` takes no transport,
    so here the URL/headers are rewritten up front — any ``params`` kwarg is folded
    into the URL first so the relayed ``u`` carries the full final query). Raises
    :class:`FetchError` when retries OR the cumulative ``max_elapsed`` budget are
    exhausted.
    """
    headers = {**DEFAULT_HEADERS, **(kwargs.pop("headers", None) or {})}  # type: ignore[arg-type]
    cfg = config.relay()
    if cfg is not None:
        relay_url, token, hosts = cfg
        # httpx TRAP (bit us twice): ``httpx.URL(url, params=None)`` — like an
        # explicit ``params={}`` on a client call — REPLACES/wipes a query string
        # already embedded in the URL (e.g. anac's ``?name=<year>.jsonl.gz``).
        # Fold ONLY when params is actually populated.
        params = kwargs.pop("params", None)
        full = httpx.URL(url, params=params) if params else httpx.URL(url)  # type: ignore[arg-type]
        if (full.host or "").lower() in hosts:
            url = str(httpx.URL(relay_url).copy_set_param("u", str(full)))
            headers[RELAY_TOKEN_HEADER] = token
        else:
            url = str(full)
    kwargs["headers"] = headers
    effective_timeout = timeout if timeout is not None else default_timeout()
    last_error = "unknown error"
    last_kind: FetchErrorKind = "unknown"
    start = _monotonic()
    for attempt in range(max_retries + 1):
        cm = httpx.stream(method, url, timeout=effective_timeout, **kwargs)
        try:
            response = cm.__enter__()
        except _TRANSIENT_EXC as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            last_kind = "unavailable"
        else:
            if not _is_retryable_status(response.status_code):
                try:
                    yield response
                finally:
                    cm.__exit__(None, None, None)
                return
            last_error = f"HTTP {response.status_code}"
            last_kind = _status_kind(response.status_code)
            delay = (
                _retry_after_seconds(response) if response.status_code == 429 else None
            )
            cm.__exit__(None, None, None)
            if attempt >= max_retries or _monotonic() - start >= max_elapsed:
                break
            _sleep(delay if delay is not None else _backoff_seconds(attempt))
            continue
        if attempt >= max_retries or _monotonic() - start >= max_elapsed:
            break
        _sleep(_backoff_seconds(attempt))
    raise FetchError(
        f"{what} failed after {attempt + 1} attempts ({last_error})",
        kind=last_kind,
    )
