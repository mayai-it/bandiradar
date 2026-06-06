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

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 4
_BACKOFF_BASE = 0.5  # seconds; attempt n waits base * 2**n (capped)
_BACKOFF_CAP = 30.0

# Transient httpx exceptions worth retrying (timeouts + connection/transport).
_TRANSIENT_EXC = (httpx.TimeoutException, httpx.TransportError)

# Inject-able sleep so tests can run with zero real delay (monkeypatch this).
_sleep = time.sleep


class FetchError(RuntimeError):
    """Raised when a live request still fails after exhausting all retries."""


def _is_retryable_status(status: int) -> bool:
    return status == 429 or status >= 500


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
) -> httpx.Response:
    """Call ``send`` (one HTTP request), retrying transient failures with backoff.

    ``send`` is a zero-arg callable that performs the request and returns an
    ``httpx.Response`` (e.g. ``lambda: client.get(url, params=params)``). On 429 a
    ``Retry-After`` header is honored; otherwise exponential backoff is used.
    Returns the first non-retryable response; raises :class:`FetchError` once
    ``max_retries`` retries are used up.
    """
    last_error = "unknown error"
    for attempt in range(max_retries + 1):
        try:
            response = send()
        except _TRANSIENT_EXC as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            delay = _backoff_seconds(attempt)
        else:
            if not _is_retryable_status(response.status_code):
                return response
            last_error = f"HTTP {response.status_code}"
            delay = None
            if response.status_code == 429:
                delay = _retry_after_seconds(response)
            if delay is None:
                delay = _backoff_seconds(attempt)
        if attempt >= max_retries:
            break
        _sleep(delay)
    raise FetchError(f"{what} failed after {max_retries + 1} attempts ({last_error})")


@contextmanager
def stream_with_retry(
    method: str,
    url: str,
    *,
    what: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
    **kwargs: object,
) -> Iterator[httpx.Response]:
    """Open a streaming request, retrying transient failures on ESTABLISHMENT.

    Yields an open ``httpx.Response`` for byte streaming. Only the connection +
    initial status are retried (a failure mid-stream cannot be resumed); that
    partial case is handled at the run level by progressive save. Raises
    :class:`FetchError` when retries are exhausted.
    """
    last_error = "unknown error"
    for attempt in range(max_retries + 1):
        cm = httpx.stream(method, url, timeout=timeout, **kwargs)
        try:
            response = cm.__enter__()
        except _TRANSIENT_EXC as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if not _is_retryable_status(response.status_code):
                try:
                    yield response
                finally:
                    cm.__exit__(None, None, None)
                return
            last_error = f"HTTP {response.status_code}"
            delay = (
                _retry_after_seconds(response) if response.status_code == 429 else None
            )
            cm.__exit__(None, None, None)
            if attempt >= max_retries:
                break
            _sleep(delay if delay is not None else _backoff_seconds(attempt))
            continue
        if attempt >= max_retries:
            break
        _sleep(_backoff_seconds(attempt))
    raise FetchError(f"{what} failed after {max_retries + 1} attempts ({last_error})")
