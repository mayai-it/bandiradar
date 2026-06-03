"""Provider-agnostic LLM client (ARCHITECTURE.md §6, Stage 2).

The provider is chosen from the environment so swapping to an EU/GDPR-friendly or
local model is a config change, not a refactor:

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
import os
import re
from typing import Protocol, runtime_checkable

# Reasonable defaults; overridable via BANDIRADAR_LLM_MODEL. Not exercised in CI.
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o-mini",
}


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
    """Thin Anthropic wrapper. The SDK is imported lazily inside :meth:`score`."""

    def __init__(self, model: str) -> None:
        self.model = model

    def score(self, system: str, user: str) -> dict:
        import anthropic  # lazy: only needed when a key/provider is configured

        client = anthropic.Anthropic()
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system + "\n\nRespond with a single JSON object and nothing else.",
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(
            getattr(block, "text", "") for block in getattr(response, "content", [])
        )
        return _parse_json(text)


class OpenAIClient:
    """Thin OpenAI wrapper. The SDK is imported lazily inside :meth:`score`."""

    def __init__(self, model: str) -> None:
        self.model = model

    def score(self, system: str, user: str) -> dict:
        import openai  # lazy: only needed when a key/provider is configured

        client = openai.OpenAI()
        response = client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return _parse_json(response.choices[0].message.content or "")


def get_client() -> LLMClient | None:
    """Build the configured client, or ``None`` to signal the offline fallback."""
    provider = os.environ.get("BANDIRADAR_LLM_PROVIDER", "none").strip().lower()
    if provider in ("", "none"):
        return None

    model = os.environ.get("BANDIRADAR_LLM_MODEL") or DEFAULT_MODELS.get(provider)
    if model is None:
        return None  # unknown provider

    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        if importlib.util.find_spec("anthropic") is None:
            return None
        return AnthropicClient(model)

    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            return None
        if importlib.util.find_spec("openai") is None:
            return None
        return OpenAIClient(model)

    return None
