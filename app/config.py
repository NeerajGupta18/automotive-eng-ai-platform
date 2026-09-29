"""Central configuration. Everything is driven by environment variables so the
same code runs on a laptop (Ollama), in Docker, or against a private LLM server."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    # LLM: mock | ollama | openai_compat (vLLM, LM Studio, llama.cpp server, ...)
    llm_backend: str
    ollama_url: str
    ollama_model: str
    openai_base_url: str
    openai_model: str
    openai_api_key: str
    # Embeddings: hash (offline, zero-download) | sentence-transformers (BGE / E5)
    embed_backend: str
    embed_model: str
    # Vector DB: numpy (zero-dependency) | chroma
    vector_backend: str
    chunk_chars: int
    chunk_overlap_chars: int
    min_score: float          # below this the RAG layer refuses to answer
    max_upload_mb: int
    allowed_extensions: tuple[str, ...] = (".pdf", ".md", ".txt")


def get_settings() -> Settings:
    embed_backend = os.getenv("EMBED_BACKEND", "hash")
    default_min = "0.12" if embed_backend == "hash" else "0.30"
    return Settings(
        data_dir=Path(os.getenv("DATA_DIR", "./storage")).resolve(),
        llm_backend=os.getenv("LLM_BACKEND", "mock"),
        ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:8001/v1"),
        openai_model=os.getenv("OPENAI_MODEL", "local-model"),
        openai_api_key=os.getenv("OPENAI_API_KEY", "not-needed"),
        embed_backend=embed_backend,
        embed_model=os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
        vector_backend=os.getenv("VECTOR_BACKEND", "numpy"),
        chunk_chars=int(os.getenv("CHUNK_CHARS", "1200")),
        chunk_overlap_chars=int(os.getenv("CHUNK_OVERLAP_CHARS", "150")),
        min_score=float(os.getenv("MIN_SCORE", default_min)),
        max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "50")),
    )
