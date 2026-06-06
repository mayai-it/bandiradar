"""Shared test fixtures.

SAFETY: the whole suite stays OFFLINE and zero-secret, even though config.py
auto-loads a real ``.env`` (which may contain a live ANTHROPIC_API_KEY). This
autouse fixture forces the offline path for every test, so no test can reach a
provider API. Tests that exercise the LLM client do so with an injected fake SDK,
never the network.
"""

import pytest

# Register the test-only synthetic OCDS source (id "synthetic") for the whole
# suite — the matcher/storage/CLI/MCP tests use it as their region-aware corpus
# now that `anac` is wired to real (regionless, historical) live data.
import synthetic_source  # noqa: F401,E402  (registration side effect)


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    monkeypatch.setenv("BANDIRADAR_LLM_PROVIDER", "none")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BANDIRADAR_LLM_MODEL", raising=False)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Never sleep for real in the suite — retry backoff is patched to a no-op.

    Tests that assert backoff timing override this with their own recorder.
    """
    import bandiradar.http as _http

    monkeypatch.setattr(_http, "_sleep", lambda *_a, **_k: None)
