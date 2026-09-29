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
    # --- security / operations (Stage 5)
    auth_mode: str = "off"                 # off (development: X-User header is trusted) | enforced (API tokens + RBAC)
    admin_token: str = ""                  # optional bootstrap admin token (>= 32 chars); generated if empty
    token_ttl_days: int = 90
    rate_limit_per_min: int = 0            # per identity; 0 = disabled (defaults to 600 when auth is enforced)
    auth_fail_limit_per_min: int = 10      # failed authentications per client address
    max_request_mb: int = 52
    log_format: str = "text"               # text | json
    log_level: str = "INFO"


class ConfigError(Exception):
    pass


def validate(s: "Settings") -> tuple[list[str], list[str]]:
    """(errors, warnings). Errors stop the service from starting."""
    errors, warnings = [], []
    if s.auth_mode not in ("off", "enforced"):
        errors.append(f"AUTH_MODE must be 'off' or 'enforced', got '{s.auth_mode}'")
    if s.auth_mode == "enforced" and s.admin_token and len(s.admin_token) < 32:
        errors.append("ADMIN_TOKEN must be at least 32 characters (use: python -c \"import secrets; print(secrets.token_urlsafe(32))\")")
    if s.auth_mode == "off":
        warnings.append("AUTH_MODE=off: identities are NOT authenticated and there is no access control. Use only on a trusted workstation.")
    if s.log_format not in ("text", "json"):
        errors.append("LOG_FORMAT must be 'text' or 'json'")
    if min(s.rate_limit_per_min, s.auth_fail_limit_per_min, s.token_ttl_days, s.max_request_mb) < 0:
        errors.append("Rate, TTL and size limits must not be negative")
    if s.llm_backend == "openai_compat" and not any(h in s.openai_base_url for h in ("localhost", "127.0.0.1", "[::1]", "host.docker.internal")):
        warnings.append(f"LLM backend {s.openai_base_url} is not local: prompts (document and source-code excerpts) leave this machine.")
    if s.max_request_mb < s.max_upload_mb:
        warnings.append("MAX_REQUEST_MB is smaller than MAX_UPLOAD_MB: large uploads will be rejected before validation.")
    return errors, warnings


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
        auth_mode=os.getenv("AUTH_MODE", "off").strip().lower(),
        admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
        token_ttl_days=int(os.getenv("TOKEN_TTL_DAYS", "90")),
        rate_limit_per_min=int(os.getenv("RATE_LIMIT_PER_MIN", "600" if os.getenv("AUTH_MODE", "off").strip().lower() == "enforced" else "0")),
        auth_fail_limit_per_min=int(os.getenv("AUTH_FAIL_LIMIT_PER_MIN", "10")),
        max_request_mb=int(os.getenv("MAX_REQUEST_MB", str(int(os.getenv("MAX_UPLOAD_MB", "50")) + 2))),
        log_format=os.getenv("LOG_FORMAT", "text").strip().lower(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )
