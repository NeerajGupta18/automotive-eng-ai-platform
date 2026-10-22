"""Vector store abstraction with two interchangeable backends.

numpy  : zero-dependency, persisted to disk, exact cosine search. Great for tests
         and small corpora (a few 10k chunks).
chroma : ChromaDB persistent client (the pilot store recommended by the case study).

`where` filter: {"doc_id": ["a","b"]} means doc_id IN [...]; scalar means equality."""
from __future__ import annotations

import json
import re
import threading
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from app.config import Settings
from app.core.models import Chunk, Hit


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


class VectorStore(ABC):
    # bumped on every add/delete so derived indexes (BM25) know when to rebuild
    generation: int = 0

    @abstractmethod
    def add(self, chunks: list[Chunk], embeddings: np.ndarray) -> None: ...
    @abstractmethod
    def query(self, embedding: np.ndarray, k: int, where: dict | None = None) -> list[Hit]: ...
    @abstractmethod
    def delete(self, where: dict) -> None: ...
    @abstractmethod
    def count(self) -> int: ...
    @abstractmethod
    def records(self) -> list[dict]:
        """All stored chunks as {"id", "text", "meta"} (used to build the keyword index)."""


def _match(meta: dict, where: dict | None) -> bool:
    if not where:
        return True
    for key, want in where.items():
        have = meta.get(key)
        if isinstance(want, (list, tuple, set)):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


class NumpyStore(VectorStore):
    def __init__(self, root: Path, collection: str):
        root.mkdir(parents=True, exist_ok=True)
        self._meta_path = root / f"{_safe(collection)}.json"
        self._vec_path = root / f"{_safe(collection)}.npy"
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._vecs = np.zeros((0, 0), dtype=np.float32)
        if self._meta_path.exists() and self._vec_path.exists():
            self._records = json.loads(self._meta_path.read_text())
            self._vecs = np.load(self._vec_path)

    def _save(self) -> None:
        self._meta_path.write_text(json.dumps(self._records))
        np.save(self._vec_path, self._vecs)

    def add(self, chunks, embeddings):
        with self._lock:
            new = np.asarray(embeddings, dtype=np.float32)
            self._vecs = new if self._vecs.size == 0 else np.vstack([self._vecs, new])
            self._records += [{"id": c.id, "text": c.text, "meta": c.meta} for c in chunks]
            self._save()
            self.generation += 1

    def query(self, embedding, k, where=None):
        with self._lock:
            if not self._records:
                return []
            idx = [i for i, r in enumerate(self._records) if _match(r["meta"], where)]
            if not idx:
                return []
            sims = self._vecs[idx] @ np.asarray(embedding, dtype=np.float32)
            order = np.argsort(-sims)[:k]
            return [
                Hit(self._records[idx[j]]["id"], self._records[idx[j]]["text"],
                    self._records[idx[j]]["meta"], float(sims[j]))
                for j in order
            ]

    def delete(self, where):
        with self._lock:
            keep = [i for i, r in enumerate(self._records) if not _match(r["meta"], where)]
            self._records = [self._records[i] for i in keep]
            self._vecs = self._vecs[keep] if keep else np.zeros((0, 0), dtype=np.float32)
            self._save()
            self.generation += 1

    def count(self):
        return len(self._records)

    def records(self):
        with self._lock:
            return list(self._records)


class ChromaStore(VectorStore):
    def __init__(self, root: Path, collection: str):
        import chromadb

        root.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(root))
        self._col = client.get_or_create_collection("proj_" + _safe(collection), metadata={"hnsw:space": "cosine"})

    @staticmethod
    def _where(where: dict | None):
        if not where:
            return None
        clauses = [
            {k: {"$in": list(v)}} if isinstance(v, (list, tuple, set)) else {k: v}
            for k, v in where.items()
        ]
        return clauses[0] if len(clauses) == 1 else {"$and": clauses}

    def add(self, chunks, embeddings):
        for i in range(0, len(chunks), 256):  # Chroma batch limits
            part = chunks[i:i + 256]
            self._col.add(
                ids=[c.id for c in part],
                documents=[c.text for c in part],
                embeddings=np.asarray(embeddings[i:i + 256], dtype=np.float32).tolist(),
                metadatas=[c.meta for c in part],
            )
        self.generation += 1

    def query(self, embedding, k, where=None):
        if self._col.count() == 0:
            return []
        res = self._col.query(
            query_embeddings=[np.asarray(embedding, dtype=np.float32).tolist()],
            n_results=min(k, self._col.count()),
            where=self._where(where),
        )
        return [
            Hit(i, d, m, 1.0 - float(dist))  # cosine distance -> similarity
            for i, d, m, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])
        ]

    def delete(self, where):
        self._col.delete(where=self._where(where))
        self.generation += 1

    def count(self):
        return self._col.count()

    def records(self):
        if self._col.count() == 0:
            return []
        res = self._col.get(include=["documents", "metadatas"])
        return [{"id": i, "text": d, "meta": m} for i, d, m in zip(res["ids"], res["documents"], res["metadatas"])]


def build_store(s: Settings, project: str) -> VectorStore:
    root = s.data_dir / "vectors" / s.vector_backend
    if s.vector_backend == "numpy":
        return NumpyStore(root, project)
    if s.vector_backend == "chroma":
        return ChromaStore(root, project)
    raise ValueError(f"Unknown VECTOR_BACKEND '{s.vector_backend}'")
