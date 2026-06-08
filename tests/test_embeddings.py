"""Embeddings tests — OFFLINE, NO model, NO network.

The semantic path is driven by a DETERMINISTIC fake embedder (stable hashed
bag-of-words), so the suite never downloads or loads a real model. conftest forces
``BANDIRADAR_EMBEDDINGS=none`` so ``get_embedder()`` is None for the whole suite.
"""

import hashlib
import re

from bandiradar import config
from bandiradar.matching import embeddings as emb
from bandiradar.matching.embeddings import (
    InMemoryEmbeddingCache,
    cosine,
    get_embedder,
    opportunity_text,
    profile_text,
)
from bandiradar.models import Opportunity, Profile
from bandiradar.storage import SqliteEmbeddingCache, Store

_TOKEN = re.compile(r"[a-zà-ù]{3,}")


class FakeEmbedder:
    """Deterministic hashed bag-of-words embedder — similar texts -> high cosine,
    no model, no randomness (stable hash, unlike Python's salted ``hash``)."""

    model_id = "fake:bow"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in _TOKEN.findall(text.lower()):
            h = int.from_bytes(hashlib.sha1(tok.encode()).digest()[:4], "big")
            v[h % self.dim] += 1.0
        return v


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


def test_cosine_basic():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0  # empty / mismatch -> 0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector -> 0


def test_fake_embedder_similarity_tracks_shared_tokens():
    e = FakeEmbedder()
    near = cosine(
        e.embed(["ricerca quantistica fotonica"])[0],
        e.embed(["progetto di ricerca quantistica"])[0],
    )
    far = cosine(
        e.embed(["ricerca quantistica fotonica"])[0],
        e.embed(["ristorazione turismo commercio"])[0],
    )
    assert near > 0.4 > far


def test_text_surfaces():
    p = Profile(name="p", keywords=["alpha", "beta"], capabilities="gamma delta")
    assert "gamma" in profile_text(p) and "alpha" in profile_text(p)
    opp = _opp(title="T", summary="S", eligibility_text="E")
    text = opportunity_text(opp)
    assert "T" in text and "S" in text and "E" in text


# --------------------------------------------------------------------------- #
# get_embedder gating (no model load)
# --------------------------------------------------------------------------- #


def test_get_embedder_none_when_disabled():
    # conftest sets BANDIRADAR_EMBEDDINGS=none for the whole suite.
    assert get_embedder() is None


def test_get_embedder_none_when_fastembed_absent(monkeypatch):
    monkeypatch.setattr(config, "embeddings_enabled", lambda: True)
    monkeypatch.setattr(emb.importlib.util, "find_spec", lambda name: None)
    assert get_embedder() is None


def test_get_embedder_builds_lazily_when_available(monkeypatch):
    # Pretend the extra is installed; constructing must NOT load/download a model.
    monkeypatch.setattr(config, "embeddings_enabled", lambda: True)
    monkeypatch.setattr(config, "embeddings_model", lambda: "test/model")
    monkeypatch.setattr(emb.importlib.util, "find_spec", lambda name: object())
    embedder = get_embedder()
    assert embedder is not None
    assert embedder.model_id == "fastembed:test/model"  # built, not yet loaded


# --------------------------------------------------------------------------- #
# caches
# --------------------------------------------------------------------------- #


def test_in_memory_cache_round_trip():
    cache = InMemoryEmbeddingCache()
    assert cache.get("h", "m") is None
    cache.set("h", "m", [0.1, 0.2])
    assert cache.get("h", "m") == [0.1, 0.2]
    assert cache.get("h", "other-model") is None  # model_id namespaced


def test_sqlite_embedding_cache_round_trip(tmp_path):
    store = Store(str(tmp_path / "emb.db"))
    try:
        cache = SqliteEmbeddingCache(store)
        assert cache.get("hash1", "fake:bow") is None
        cache.set("hash1", "fake:bow", [0.5, -0.25, 1.0])
        assert cache.get("hash1", "fake:bow") == [0.5, -0.25, 1.0]
        # distinct content_hash and distinct model_id both miss
        assert cache.get("hash2", "fake:bow") is None
        assert cache.get("hash1", "other:model") is None
        # overwrite
        cache.set("hash1", "fake:bow", [1.0])
        assert cache.get("hash1", "fake:bow") == [1.0]
    finally:
        store.close()


def _opp(**overrides) -> Opportunity:
    base = dict(
        id="x:1",
        source="x",
        source_url="https://example.invalid/x/1",
        kind="incentive",
        title="T",
        geo_scope="national",
        status="open",
        raw_ref="x:1",
    )
    base.update(overrides)
    return Opportunity(**base)
