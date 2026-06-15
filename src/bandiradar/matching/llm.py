"""Provider-agnostic LLM client (ARCHITECTURE.md §6, Stage 2).

The provider is chosen from configuration (``.env`` / environment) so swapping to
an EU/GDPR-friendly or local model is a config change, not a refactor:

    BANDIRADAR_LLM_PROVIDER = anthropic | openai | none   (default: none)
    BANDIRADAR_LLM_MODEL    = <optional model override>
    ANTHROPIC_API_KEY / OPENAI_API_KEY  (read only for the matching provider)

:func:`get_client` returns ``None`` whenever the engine should fall back to the
deterministic offline heuristic — i.e. provider is ``none``, the API key is
missing, or the provider SDK is not importable. Provider SDKs are imported
LAZILY inside the client methods, so the repo builds and tests with ZERO secrets
and WITHOUT those SDKs installed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from typing import Protocol, runtime_checkable

from bandiradar import config

# Back-compat alias; the source of truth lives in config.
DEFAULT_MODELS = config.DEFAULT_MODELS

_REQUEST_TIMEOUT = 60.0  # seconds
_announced = False


def _announce(provider: str, model: str) -> None:
    """Emit a one-time stderr note so it's visible WHICH path scored (LLM)."""
    global _announced
    if not _announced:
        print(f"[bandiradar] scoring via {provider}:{model}", file=sys.stderr)
        _announced = True


@runtime_checkable
class LLMClient(Protocol):
    """A minimal scoring client: prompt in, parsed relevance JSON out."""

    def score(self, system: str, user: str) -> dict:
        """Return the model's response parsed into a relevance-schema dict."""
        ...


def _parse_json(text: str) -> dict:
    """Tolerant JSON extraction: take the outermost ``{...}`` block and parse it."""
    text = (text or "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


class AnthropicClient:
    """Thin Anthropic Messages-API wrapper. SDK imported lazily in :meth:`score`."""

    def __init__(self, model: str) -> None:
        self.model = model

    @property
    def cache_id(self) -> str:
        """Stable identity of this scoring backend (part of the relevance cache key)."""
        return f"anthropic:{self.model}"

    def score(self, system: str, user: str) -> dict:
        import anthropic  # lazy: only needed when a key/provider is configured

        client = anthropic.Anthropic(timeout=_REQUEST_TIMEOUT)
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AnthropicError as exc:
            raise RuntimeError(f"Anthropic scoring failed: {exc}") from exc

        # getattr (not block.text): response.content is a union of block types, only
        # TextBlock has `.text`; the type filter selects them, getattr keeps mypy happy.
        text = "".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        return _parse_json(text)


class OpenAIClient:
    """Thin OpenAI wrapper. The SDK is imported lazily inside :meth:`score`."""

    def __init__(self, model: str) -> None:
        self.model = model

    @property
    def cache_id(self) -> str:
        """Stable identity of this scoring backend (part of the relevance cache key)."""
        return f"openai:{self.model}"

    def score(self, system: str, user: str) -> dict:
        import openai  # lazy: only needed when a key/provider is configured

        client = openai.OpenAI(timeout=_REQUEST_TIMEOUT)
        try:
            response = client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except openai.OpenAIError as exc:
            raise RuntimeError(f"OpenAI scoring failed: {exc}") from exc
        return _parse_json(response.choices[0].message.content or "")


def _resolve() -> tuple[LLMClient | None, str]:
    """Resolve the configured client AND the reason, in one place.

    Returns ``(client, "active")`` when a live client is available, else
    ``(None, <human-readable reason for the offline fallback>)``. Crucially this
    DISTINGUISHES "no provider/key configured" from "provider+key set but the SDK
    is not installed" — so a misconfig (e.g. ``uv sync`` without ``--extra
    anthropic``) is reported honestly instead of silently falling back."""
    provider = config.llm_provider()
    if provider in ("", "none"):
        return None, "no LLM provider configured — offline heuristic"

    model = config.llm_model(provider)
    if model is None:
        return None, f"unknown LLM provider {provider!r}"

    if provider not in ("anthropic", "openai"):
        return None, f"unsupported LLM provider {provider!r}"

    if not config.api_key(provider):
        return None, f"{provider} provider set but its API key is missing"

    if importlib.util.find_spec(provider) is None:
        return None, f"{provider} SDK not installed — run: uv sync --extra {provider}"

    client = AnthropicClient(model) if provider == "anthropic" else OpenAIClient(model)
    return client, "active"


def get_client() -> LLMClient | None:
    """Build the configured client, or ``None`` to signal the offline fallback."""
    client, _reason = _resolve()
    if client is not None:
        _announce(config.llm_provider(), getattr(client, "model", ""))
    return client


def client_status() -> str:
    """Human-readable LLM-client state: ``"active"`` or WHY it falls back offline.

    A pure diagnostic (no SDK import, no announce, no network) for callers that need
    to tell the user what is happening — e.g. the monitor's pre-flight guard and the
    LLM-scraper's error message. Does NOT change the ``get_client() -> None`` fallback
    contract; it just exposes the reason behind it."""
    return _resolve()[1]
