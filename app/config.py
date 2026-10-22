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
    # --- retrieval quality (Phase 1)
    retrieval_mode: str = "hybrid"         # vector | hybrid (BM25 keyword + vector, fused with RRF)
    min_coverage: float = 0.5              # hybrid: refuse when less of the question than this appears in the documents
    rescue_min_score: float = 0.6          # hybrid + semantic embeddings: let a low-coverage (paraphrased) question through
    rescue_min_coverage: float = 0.2       #   when the best chunk is at least this similar and this much of the question is covered
    rerank_backend: str = "none"           # none | cross-encoder
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    llm_timeout_s: int = 120
    llm_fallback: bool = True              # on LLM errors answer extractively (flagged 'degraded') instead of failing
    # --- agents and enterprise integration (Phase 5)
    integrations_live: bool = False        # False: webhooks and Jira tickets are prepared and signed but not sent (dry run)
    webhook_allow_hosts: tuple[str, ...] = ()   # if set, webhooks may only target these hosts (and their subdomains)
    webhook_allow_private: bool = False    # allow private/loopback addresses (on-premise receivers); blocked by default (SSRF)
    integration_dispatch_s: int = 0        # background delivery interval in seconds; 0 = only on request
    jira_base_url: str = ""
    jira_project_key: str = ""
    jira_user: str = ""                    # Jira Cloud: account e-mail (basic auth with an API token); empty: bearer token (Data Center PAT)
    jira_api_token: str = ""
    jira_issue_type: str = "Task"
    agent_max_steps: int = 25
    field_auto_investigate: bool = True    # Phase 6: a new field issue starts the agent's investigation automatically
    field_triage_llm: bool = False         # Phase 6: let a configured LLM read complaint texts (rules otherwise)


def load_dotenv(path: Path | None = None) -> list[str]:
    """Minimal .env reader (no extra dependency). Real environment variables always win.
    Set AEAI_DOTENV=0 to disable (the test-suite does, so a developer's .env cannot change test results)."""
    if os.getenv("AEAI_DOTENV", "1") == "0":
        return []
    path = path or Path(os.getenv("ENV_FILE", ".env"))
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _is_local(url: str) -> bool:
    return any(h in url for h in ("localhost", "127.0.0.1", "[::1]", "host.docker.internal"))


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
    if s.llm_backend == "openai_compat" and not _is_local(s.openai_base_url):
        warnings.append(f"LLM backend {s.openai_base_url} is not local: prompts (document and source-code excerpts) leave this machine.")
        if s.openai_api_key in ("", "not-needed") or not s.openai_model or s.openai_model == "local-model":
            warnings.append("OPENAI_API_KEY / OPENAI_MODEL not set for a hosted LLM: using the offline extractive answerer (mock) instead.")
    if s.retrieval_mode not in ("vector", "hybrid"):
        errors.append(f"RETRIEVAL_MODE must be 'vector' or 'hybrid', got '{s.retrieval_mode}'")
    for name in ("min_coverage", "rescue_min_score", "rescue_min_coverage"):
        if not 0.0 <= getattr(s, name) <= 1.0:
            errors.append(f"{name.upper()} must be between 0 and 1")
    if s.max_request_mb < s.max_upload_mb:
        warnings.append("MAX_REQUEST_MB is smaller than MAX_UPLOAD_MB: large uploads will be rejected before validation.")
    if s.integration_dispatch_s < 0 or not 1 <= s.agent_max_steps <= 100:
        errors.append("INTEGRATION_DISPATCH_S must be >= 0 and AGENT_MAX_STEPS between 1 and 100")
    if s.jira_base_url and not (s.jira_base_url.startswith("https://") or _is_local(s.jira_base_url)):
        errors.append("JIRA_BASE_URL must use https")
    if s.integrations_live and s.jira_base_url and not (s.jira_project_key and s.jira_api_token):
        warnings.append("JIRA_BASE_URL is set but JIRA_PROJECT_KEY / JIRA_API_TOKEN are missing: tickets stay in dry-run.")
    if s.integrations_live and s.webhook_allow_private:
        warnings.append("WEBHOOK_ALLOW_PRIVATE=1: webhooks may reach internal addresses. Use only on a trusted network.")
    return errors, warnings


def get_settings() -> Settings:
    load_dotenv()
    embed_backend = os.getenv("EMBED_BACKEND", "hash")
    default_min = "0.12" if embed_backend == "hash" else "0.30"
    return Settings(
        data_dir=Path(os.getenv("DATA_DIR", "./storage")).resolve(),
        llm_backend=os.getenv("LLM_BACKEND", "mock"),
        ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
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
        retrieval_mode=os.getenv("RETRIEVAL_MODE", "hybrid").strip().lower(),
        min_coverage=float(os.getenv("MIN_COVERAGE", "0.5")),
        rescue_min_score=float(os.getenv("RESCUE_MIN_SCORE", "0.6")),
        rescue_min_coverage=float(os.getenv("RESCUE_MIN_COVERAGE", "0.2")),
        rerank_backend=os.getenv("RERANK_BACKEND", "none").strip().lower(),
        rerank_model=os.getenv("RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
        llm_timeout_s=int(os.getenv("LLM_TIMEOUT_S", "120")),
        llm_fallback=os.getenv("LLM_FALLBACK", "1").strip().lower() not in ("0", "false", "no"),
        integrations_live=os.getenv("INTEGRATIONS_LIVE", "0").strip().lower() in ("1", "true", "yes"),
        webhook_allow_hosts=tuple(h.strip().lower() for h in os.getenv("WEBHOOK_ALLOW_HOSTS", "").split(",") if h.strip()),
        webhook_allow_private=os.getenv("WEBHOOK_ALLOW_PRIVATE", "0").strip().lower() in ("1", "true", "yes"),
        integration_dispatch_s=int(os.getenv("INTEGRATION_DISPATCH_S", "0")),
        jira_base_url=os.getenv("JIRA_BASE_URL", "").strip().rstrip("/"),
        jira_project_key=os.getenv("JIRA_PROJECT_KEY", "").strip(),
        jira_user=os.getenv("JIRA_USER", "").strip(),
        jira_api_token=os.getenv("JIRA_API_TOKEN", "").strip(),
        jira_issue_type=os.getenv("JIRA_ISSUE_TYPE", "Task").strip() or "Task",
        agent_max_steps=int(os.getenv("AGENT_MAX_STEPS", "25")),
        field_auto_investigate=os.getenv("FIELD_AUTO_INVESTIGATE", "1").strip().lower() in ("1", "true", "yes"),
        field_triage_llm=os.getenv("FIELD_TRIAGE_LLM", "0").strip().lower() in ("1", "true", "yes"),
    )
