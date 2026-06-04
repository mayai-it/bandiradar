"""Shared test fixtures.

SAFETY: the whole suite stays OFFLINE and zero-secret, even though config.py
auto-loads a real ``.env`` (which may contain a live ANTHROPIC_API_KEY). This
autouse fixture forces the offline path for every test, so no test can reach a
provider API. Tests that exercise the LLM client do so with an injected fake SDK,
never the network.
"""

import pytest


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    monkeypatch.setenv("BANDIRADAR_LLM_PROVIDER", "none")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BANDIRADAR_LLM_MODEL", raising=False)
