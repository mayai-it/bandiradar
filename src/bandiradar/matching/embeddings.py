"""Optional semantic embeddings for the hybrid Stage-1 prefilter (0.3.0 slice 2).

Open-core, OPTIONAL, injected like the score/document caches. The default install
has NO embedding backend: :func:`get_embedder` returns ``None`` and the prefilter
falls back to its deterministic CPV/keyword signal — no model, no network, no
behaviour change. Install the backend with the ``embeddings`` extra
(``uv sync --extra embeddings``); the model (a small multilingual ONNX model, good
for Italian) downloads ONCE on first real use and is then cached by fastembed.

The recall lever: a genuine Stage-1 prefilter drop is what no downstream ranker
can recover. A semantic signal can rescue an opportunity that is clearly relevant
but shares no exact CPV/keyword with the profile. Whether it nets positive
(recall up without FPR exploding) is MEASURED by ``bandiradar eval --embeddings``.
"""

from __future__ import annotations

import importlib.util
import math
from typing import Protocol, runtime_checkable

from bandiradar import config
from bandiradar.models import Opportunity, Profile

# Cosine cutoff above which the semantic signal counts as a relevance hit. The
# right value is model-dependent; ``eval --embeddings`` sweeps it.
EMBEDDING_SIM_THRESHOLD = 0.4


@runtime_checkable
class Embedder(Protocol):
    """Turns texts into dense vectors. ``model_id`` namespaces the vector cache."""

    @property
    def model_id(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class EmbeddingCache(Protocol):
    """Opportunity-vector cache keyed by (content_hash, model_id)."""

    def get(self, content_hash: str, model_id: str) -> list[float] | None: ...

    def set(self, content_hash: str, model_id: str, vector: list[float]) -> None: ...


class InMemoryEmbeddingCache:
    """Default process-local vector cache (the SQLite one lives in storage.py)."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], list[float]] = {}

    def get(self, content_hash: str, model_id: str) -> list[float] | None:
        return self._store.get((content_hash, model_id))

    def set(self, content_hash: str, model_id: str, vector: list[float]) -> None:
        self._store[(content_hash, model_id)] = vector


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 for an empty/zero vector or length mismatch."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def profile_text(profile: Profile) -> str:
    """The profile's semantic surface: capabilities + keywords (no raw dump)."""
    return " ".join(
        part for part in (profile.capabilities, " ".join(profile.keywords)) if part
    ).strip()


def opportunity_text(opportunity: Opportunity) -> str:
    """The opportunity's semantic surface: title + summary + requirements text."""
    o = opportunity
    return " ".join(
        part
        for part in (o.title, o.summary, o.eligibility_text, o.document_text)
        if part
    ).strip()


def embed_opportunity(
    opportunity: Opportunity,
    embedder: Embedder,
    cache: EmbeddingCache | None = None,
) -> list[float]:
    """Vector for an opportunity, cache-first by ``(content_hash, model_id)``."""
    if cache is not None:
        cached = cache.get(opportunity.content_hash, embedder.model_id)
        if cached is not None:
            return cached
    vector = embedder.embed([opportunity_text(opportunity)])[0]
    if cache is not None:
        cache.set(opportunity.content_hash, embedder.model_id, vector)
    return vector


class FastEmbedEmbedder:
    """fastembed backend (ONNX, no torch). The model is loaded LAZILY on first
    :meth:`embed`, so constructing one is cheap and triggers no download."""

    def __init__(self, model: str) -> None:
        self._model_name = model
        self._model = None  # lazy

    @property
    def model_id(self) -> str:
        return f"fastembed:{self._model_name}"

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding  # lazy: only when actually embedding

            self._model = TextEmbedding(model_name=self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure()
        return [[float(x) for x in vec] for vec in model.embed(texts)]


def get_embedder() -> Embedder | None:
    """The configured embedder, or ``None`` to signal the offline fallback.

    Returns ``None`` when embeddings are disabled (``BANDIRADAR_EMBEDDINGS=none``)
    or the ``embeddings`` extra (fastembed) is not installed — so the default,
    zero-dependency install and the test suite never touch a model or the network.
    """
    if not config.embeddings_enabled():
        return None
    if importlib.util.find_spec("fastembed") is None:
        return None
    return FastEmbedEmbedder(config.embeddings_model())
