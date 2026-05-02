from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from typing import Sequence

import numpy as np

logger = logging.getLogger(__name__)

_MODEL_NAME = "all-MiniLM-L6-v2"
_model_lock = threading.Lock()
_cached_model = None  # module-level singleton so all instances share one load


def _load_model():
    global _cached_model
    if _cached_model is not None:
        return _cached_model
    with _model_lock:
        if _cached_model is not None:
            return _cached_model
        try:
            import os as _os
            _os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
            from sentence_transformers import SentenceTransformer
            logger.info("Loading sentence-transformer model %s …", _MODEL_NAME)
            _cached_model = SentenceTransformer(_MODEL_NAME)
            logger.info("Embedding model ready (dim=384)")
        except Exception as exc:
            logger.warning("sentence-transformers unavailable (%s) – using hash fallback", exc)
            _cached_model = None
    return _cached_model


class EmbeddingService:
    """Semantic embeddings via sentence-transformers (all-MiniLM-L6-v2, 384-dim).

    Falls back to a deterministic hash projection if the library is missing.
    """

    def __init__(self, dim: int = 384):
        self._dim = dim

    @property
    def uses_model(self) -> bool:
        return _load_model() is not None

    def embed_sync(self, text: str) -> list[float]:
        cleaned = text.strip()
        if not cleaned:
            return [0.0] * self._dim
        model = _load_model()
        if model is not None:
            try:
                vec = model.encode(cleaned, convert_to_numpy=True, show_progress_bar=False)
                return vec.tolist()
            except Exception as exc:
                logger.warning("Model encode failed (%s), falling back to hash", exc)
        return self._local_hash_embedding(cleaned)

    async def embed(self, text: str) -> list[float]:
        return self.embed_sync(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = _load_model()
        if model is not None:
            try:
                vecs = model.encode(texts, convert_to_numpy=True, show_progress_bar=False, batch_size=64)
                return vecs.tolist()
            except Exception as exc:
                logger.warning("Batch encode failed (%s), falling back to hash", exc)
        return [self._local_hash_embedding(t) for t in texts]

    def _local_hash_embedding(self, text: str) -> list[float]:
        tokens = re.findall(r"[a-z0-9']+", text.lower())
        vec = np.zeros(self._dim, dtype=np.float32)
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            idx = int(digest[:8], 16) % self._dim
            sign = -1.0 if int(digest[8:9], 16) % 2 else 1.0
            vec[idx] += sign
        norm = math.sqrt(float(np.dot(vec, vec)))
        if norm == 0.0:
            return vec.tolist()
        return (vec / norm).tolist()

    @staticmethod
    def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
        if not a or not b:
            return 0.0
        if len(a) != len(b):
            return 0.0
        num = sum(x * y for x, y in zip(a, b, strict=False))
        den_a = math.sqrt(sum(x * x for x in a))
        den_b = math.sqrt(sum(y * y for y in b))
        if den_a == 0.0 or den_b == 0.0:
            return 0.0
        return num / (den_a * den_b)
