"""Runtime configuration — loads ``.env`` once and exposes LLM settings.

Importing this module calls ``load_dotenv()`` so ``BANDIRADAR_LLM_PROVIDER``,
``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``, and ``BANDIRADAR_LLM_MODEL`` are read
from ``.env`` with no manual ``export``. Loading is a NO-OP when ``.env`` is
absent, so the engine stays zero-secret offline. ``override=False`` means a real
shell/CI environment variable always wins over the file (and lets tests force the
offline path deterministically).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# Load .env once, at import. Missing file -> no-op; real env vars take precedence.
load_dotenv(override=False)

# Default Stage-2 scorer: a cheap, fast Haiku-class model — the right tier for
# high-volume relevance scoring. Override per provider via BANDIRADAR_LLM_MODEL.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
}

_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

# Optional embeddings backend (the ``embeddings`` extra). A small MULTILINGUAL
# model so Italian opportunity/profile text embeds well. Override via
# BANDIRADAR_EMBEDDINGS_MODEL; disable entirely with BANDIRADAR_EMBEDDINGS=none.
DEFAULT_EMBEDDINGS_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def embeddings_enabled() -> bool:
    """False only when explicitly disabled (``BANDIRADAR_EMBEDDINGS=none``).

    Default-on so that, once the extra is installed, ``eval --embeddings`` / an
    opt-in match can use it; ``get_embedder`` still returns None when fastembed is
    absent. The test suite forces ``none`` so it never loads a model.
    """
    return os.environ.get("BANDIRADAR_EMBEDDINGS", "").strip().lower() != "none"


def embeddings_model() -> str:
    """The embeddings model id: BANDIRADAR_EMBEDDINGS_MODEL override, else default."""
    return (
        os.environ.get("BANDIRADAR_EMBEDDINGS_MODEL", "").strip()
        or DEFAULT_EMBEDDINGS_MODEL
    )


def llm_provider() -> str:
    """The configured provider, lowercased; ``"none"`` (offline) by default."""
    return os.environ.get("BANDIRADAR_LLM_PROVIDER", "none").strip().lower()


def llm_model(provider: str) -> str | None:
    """The model for a provider: BANDIRADAR_LLM_MODEL override, else the default."""
    override = os.environ.get("BANDIRADAR_LLM_MODEL")
    return override.strip() if override else DEFAULT_MODELS.get(provider)


def api_key(provider: str) -> str | None:
    """The API key for a provider from the environment, or None."""
    env_name = _API_KEY_ENV.get(provider)
    if env_name is None:
        return None
    key = os.environ.get(env_name)
    return key or None


def relay() -> tuple[str, str, frozenset[str]] | None:
    """Optional HTTP relay for CI-blocked hosts: ``(url, token, hosts)`` or None.

    Some open endpoints (e.g. incentivi.gov.it) drop datacenter-IP connections, so
    the CI monitor can't reach them directly. When ALL THREE env vars are set —
    ``BANDIRADAR_RELAY_URL`` (the operator's relay/worker endpoint),
    ``BANDIRADAR_RELAY_TOKEN`` (sent as ``X-Relay-Token``; comes from env/secrets,
    NEVER the repo), and ``BANDIRADAR_RELAY_HOSTS`` (comma-separated allowlist of
    hosts to reroute, e.g. ``www.incentivi.gov.it``) — requests to those hosts are
    rewritten to ``<url>?u=<original-url>``. Any var missing/blank => ``None`` and
    behaviour is unchanged (the repo stays keyless and fully functional without)."""
    url = os.environ.get("BANDIRADAR_RELAY_URL", "").strip()
    token = os.environ.get("BANDIRADAR_RELAY_TOKEN", "").strip()
    hosts = frozenset(
        h.strip().lower()
        for h in os.environ.get("BANDIRADAR_RELAY_HOSTS", "").split(",")
        if h.strip()
    )
    if url and token and hosts:
        return _normalize_relay_url(url), token, hosts
    return None


def _normalize_relay_url(url: str) -> str:
    """Normalize ``BANDIRADAR_RELAY_URL``: default the scheme, fail fast if broken.

    A missing scheme gets ``https://`` prepended (workers are https). An URL that is
    plainly malformed (no host, whitespace, non-http scheme) raises a clear
    ``ValueError`` IMMEDIATELY — a config typo must surface as one obvious error,
    not as 5 connect retries against garbage."""
    from urllib.parse import urlsplit

    candidate = url if "://" in url else f"https://{url}"
    parts = urlsplit(candidate)
    host = (parts.netloc or "").strip()
    if (
        parts.scheme not in ("http", "https")
        or not host
        or any(c.isspace() for c in host)
    ):
        raise ValueError(
            f"BANDIRADAR_RELAY_URL is malformed: {url!r} — expected "
            "http(s)://host[/path] (scheme optional, https assumed)"
        )
    return candidate


def llm_budget() -> int | None:
    """Max NEW LLM scorings (cache misses) per run, from ``BANDIRADAR_LLM_BUDGET``.

    ``None`` (unset / blank / non-positive / unparseable) = UNLIMITED — the default,
    so behaviour is unchanged unless explicitly capped. A positive int bounds how
    many cache-miss opportunities are scored by the LLM in one run (a spike guard);
    the rest are deferred to later runs (the score cache amortizes them). Heuristic
    scoring is unaffected (it has no per-call cost)."""
    raw = os.environ.get("BANDIRADAR_LLM_BUDGET", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None
