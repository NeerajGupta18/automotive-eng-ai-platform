"""Optional second-stage reranking of retrieved chunks.

none          : keep the fused (BM25 + vector) order. Default; zero cost.
cross-encoder : score every (question, chunk) pair with a cross-encoder such as
                cross-encoder/ms-marco-MiniLM-L-6-v2 or BAAI/bge-reranker-base
                (needs requirements-ml.txt; the model downloads on first use)."""
from __future__ import annotations

from app.config import Settings
from app.core.models import Hit


class Reranker:
    name = "none"

    def rerank(self, question: str, hits: list[Hit]) -> list[Hit]:
        return hits


class CrossEncoderReranker(Reranker):
    name = "cross-encoder"

    def __init__(self, model_name: str, model=None):
        if model is None:                                   # injectable for tests
            from sentence_transformers import CrossEncoder  # lazy: heavy import
            model = CrossEncoder(model_name)
        self.model, self.model_name = model, model_name

    def rerank(self, question: str, hits: list[Hit]) -> list[Hit]:
        if len(hits) < 2:
            return hits
        scores = self.model.predict([(question, h.text) for h in hits])
        order = sorted(range(len(hits)), key=lambda i: -float(scores[i]))
        return [hits[i] for i in order]


def build_reranker(s: Settings) -> Reranker:
    if s.rerank_backend in ("", "none"):
        return Reranker()
    if s.rerank_backend == "cross-encoder":
        return CrossEncoderReranker(s.rerank_model)
    raise ValueError(f"Unknown RERANK_BACKEND '{s.rerank_backend}'. Options: none, cross-encoder")
