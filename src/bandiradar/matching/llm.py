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

        text = "".join(
            block.text
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


def get_client() -> LLMClient | None:
    """Build the configured client, or ``None`` to signal the offline fallback."""
    provider = config.llm_provider()
    if provider in ("", "none"):
        return None

    model = config.llm_model(provider)
    if model is None:
        return None  # unknown provider

    if provider == "anthropic":
        if not config.api_key("anthropic"):
            return None
        if importlib.util.find_spec("anthropic") is None:
            return None
        _announce(provider, model)
        return AnthropicClient(model)

    if provider == "openai":
        if not config.api_key("openai"):
            return None
        if importlib.util.find_spec("openai") is None:
            return None
        _announce(provider, model)
        return OpenAIClient(model)

    return None
