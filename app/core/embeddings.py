"""Embedding backends. 'hash' needs no download (used for tests/CI);
'sentence-transformers' gives production quality (BGE / E5)."""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod

import numpy as np

from app.config import Settings


class Embedder(ABC):
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([text])[0]


class HashingEmbedder(Embedder):
    """Signed feature-hashing bag of words + bigrams, L2-normalised.
    Lexical, deterministic, zero dependencies. Not semantic, but enough to
    exercise the pipeline offline."""

    def __init__(self, dim: int = 512):
        self.dim = dim

    @staticmethod
    def _tokens(text: str) -> list[str]:
        # keep identifiers like SWC_DoorLockCtrl whole AND split on underscores/camelCase
        raw = re.findall(r"[A-Za-z0-9_\-]+", text)
        toks: list[str] = []
        for t in raw:
            toks.append(t.lower())
            parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", t)
            if len(parts) > 1:
                toks.extend(p.lower() for p in parts)
        return toks

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            toks = self._tokens(text)
            feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
            for f in feats:
                h = int(hashlib.md5(f.encode()).hexdigest(), 16)
                out[i, h % self.dim] += 1.0 if (h >> 64) & 1 else -1.0
            out[i] = np.sign(out[i]) * np.log1p(np.abs(out[i]))
            n = np.linalg.norm(out[i])
            if n:
                out[i] /= n
        return out


class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer  # lazy: heavy import

        self.model_name = model_name
        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()
        # E5 models expect prefixes; BGE uses a query instruction
        self._e5 = "e5" in model_name.lower()
        self._bge = "bge" in model_name.lower()

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._e5:
            texts = [f"passage: {t}" for t in texts]
        return np.asarray(self.model.encode(texts, normalize_embeddings=True), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        if self._e5:
            text = f"query: {text}"
        elif self._bge:
            text = f"Represent this sentence for searching relevant passages: {text}"
        return np.asarray(self.model.encode([text], normalize_embeddings=True)[0], dtype=np.float32)


def build_embedder(s: Settings) -> Embedder:
    if s.embed_backend == "hash":
        return HashingEmbedder()
    if s.embed_backend == "sentence-transformers":
        return SentenceTransformerEmbedder(s.embed_model)
    raise ValueError(f"Unknown EMBED_BACKEND '{s.embed_backend}'")
