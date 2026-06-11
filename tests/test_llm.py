"""LLM client tests — OFFLINE, no key, no network, no real SDK.

The Anthropic SDK is injected as a fake module so we can assert the request
payload and response parsing without installing the SDK or hitting the API.
"""

import sys
import types

import pytest

from bandiradar.matching import llm
from bandiradar.matching.relevance import _coerce_result

_RESPONSE_JSON = (
    '{"score": 73, "reasons": ["good sector fit"], '
    '"matched_capabilities": ["software"], "eligibility_flags": [], '
    '"risk_notes": ["tight deadline"]}'
)


def _install_fake_anthropic(monkeypatch, recorder, *, raise_error=False):
    fake = types.ModuleType("anthropic")

    class AnthropicError(Exception):
        pass

    class _Block:
        def __init__(self, text):
            self.type = "text"
            self.text = text

    class _Resp:
        def __init__(self, text):
            self.content = [_Block(text)]

    class _Messages:
        def create(self, **kwargs):
            recorder.update(kwargs)
            if raise_error:
                raise AnthropicError("boom")
            return _Resp(_RESPONSE_JSON)

    class Anthropic:
        def __init__(self, **kwargs):
            recorder["_client_kwargs"] = kwargs
            self.messages = _Messages()

    fake.AnthropicError = AnthropicError
    fake.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return fake


def test_anthropic_client_builds_request_and_parses(monkeypatch):
    recorder: dict = {}
    _install_fake_anthropic(monkeypatch, recorder)

    raw = llm.AnthropicClient("claude-haiku-4-5-20251001").score("SYS", "USER")

    # Correct Messages API request shape.
    assert recorder["model"] == "claude-haiku-4-5-20251001"
    assert recorder["system"] == "SYS"
    assert recorder["messages"] == [{"role": "user", "content": "USER"}]
    assert isinstance(recorder.get("max_tokens"), int)

    # Parses into a valid RelevanceResult.
    result = _coerce_result(raw)
    assert result.score == 73
    assert result.matched_capabilities == ["software"]
    assert result.risk_notes == ["tight deadline"]


def test_anthropic_client_wraps_sdk_errors(monkeypatch):
    recorder: dict = {}
    _install_fake_anthropic(monkeypatch, recorder, raise_error=True)
    with pytest.raises(RuntimeError, match="Anthropic scoring failed"):
        llm.AnthropicClient("m").score("s", "u")


def test_get_client_builds_anthropic_when_configured(monkeypatch):
    # Override the autouse offline fixture for this test only.
    monkeypatch.setenv("BANDIRADAR_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    # Pretend the SDK is importable regardless of whether the extra is installed.
    monkeypatch.setattr(llm.importlib.util, "find_spec", lambda name: object())

    client = llm.get_client()
    assert isinstance(client, llm.AnthropicClient)
    assert client.model == "claude-haiku-4-5-20251001"  # Haiku-class default


def test_get_client_model_override(monkeypatch):
    monkeypatch.setenv("BANDIRADAR_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("BANDIRADAR_LLM_MODEL", "claude-sonnet-4-6")
    monkeypatch.setattr(llm.importlib.util, "find_spec", lambda name: object())
    assert llm.get_client().model == "claude-sonnet-4-6"


def test_get_client_none_when_key_missing(monkeypatch):
    monkeypatch.setenv("BANDIRADAR_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.get_client() is None


# --------------------------------------------------------------------------- #
# client_status(): the honest reason behind the get_client() fallback.
# The key fix — "key present but SDK not installed" must NOT look like "no key".
# --------------------------------------------------------------------------- #


def test_client_status_no_provider():
    # conftest forces provider=none.
    assert "no LLM provider configured" in llm.client_status()
    assert llm.get_client() is None


def test_client_status_provider_set_but_no_key(monkeypatch):
    monkeypatch.setenv("BANDIRADAR_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "API key is missing" in llm.client_status()
    assert llm.get_client() is None


def test_client_status_key_present_but_sdk_missing(monkeypatch):
    # The production bug: key IS set but `uv sync` omitted the anthropic extra.
    monkeypatch.setenv("BANDIRADAR_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(llm.importlib.util, "find_spec", lambda name: None)
    status = llm.client_status()
    assert "SDK not installed" in status
    assert "uv sync --extra anthropic" in status
    # Contract unchanged: still falls back to the heuristic (None), just honestly.
    assert llm.get_client() is None


def test_client_status_active_when_fully_configured(monkeypatch):
    monkeypatch.setenv("BANDIRADAR_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(llm.importlib.util, "find_spec", lambda name: object())
    assert llm.client_status() == "active"
    assert llm.get_client() is not None
