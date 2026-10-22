"""Dependency container: builds the shared services once and hands them to the API.
Modules (HLD now; HARA/TARA/UDS/Code-review later) plug in here."""
from __future__ import annotations

from app.config import ConfigError, Settings, get_settings, validate
from app.core.auth import AuthService
from app.core.db import Database
from app.core.documents import DocumentService
from app.core.embeddings import build_embedder
from app.core.llm import build_llm
from app.core.observability import InstrumentedLLM, configure_logging, new_metrics
from app.core.lexical import BM25Index
from app.core.llm import MockLLM
from app.core.rag import RAGPipeline
from app.core.rerank import build_reranker
from app.core.vectorstore import build_store


class Container:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        errors, self.config_warnings = validate(self.settings)
        if errors:
            raise ConfigError("; ".join(errors))
        self.log = configure_logging(self.settings.log_level, self.settings.log_format)
        for w in self.config_warnings:
            self.log.warning(w)
        self.metrics = new_metrics()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.data_dir / "platform.db")
        self.embedder = build_embedder(self.settings)
        self.llm = InstrumentedLLM(build_llm(self.settings), self.metrics)
        self.auth = AuthService(self.db, self.settings)
        self.auth.bootstrap()
        self._stores: dict[str, object] = {}
        self._lexical: dict[str, tuple[int, int, BM25Index]] = {}
        self.reranker = build_reranker(self.settings)
        self.documents = DocumentService(self.settings, self.db, self.embedder, self.store)
        # imported here to avoid circular imports as modules grow
        from app.modules.hld.service import HLDService
        self.hld = HLDService(self)
        from app.modules.hara.service import HARAService
        self.hara = HARAService(self)
        from app.modules.tara.service import TARAService
        self.tara = TARAService(self)
        from app.modules.uds.service import UDSService
        self.uds = UDSService(self)
        from app.modules.codereview.service import CodeReviewService
        self.codereview = CodeReviewService(self)
        from app.modules.vuln.service import VulnService
        self.vuln = VulnService(self)
        from app.modules.ota.service import OTAService
        self.ota = OTAService(self)
        from app.modules.compliance.service import ComplianceService
        self.compliance = ComplianceService(self)
        from app.modules.adas.service import AdasService
        self.adas = AdasService(self)
        from app.modules.readiness.service import ReadinessService
        self.readiness = ReadinessService(self)
        from app.modules.integration.service import IntegrationService
        self.integration = IntegrationService(self)
        from app.modules.agent.service import AgentService
        self.agent = AgentService(self)
        from app.modules.field.service import FieldService
        self.field = FieldService(self)

    def store(self, project: str):
        """One isolated vector collection per project (prevents cross-program leakage)."""
        if project not in self._stores:
            self._stores[project] = build_store(self.settings, project)
        return self._stores[project]

    def lexical(self, project: str) -> BM25Index:
        """Keyword index over the project's chunks; rebuilt only when its documents change."""
        store = self.store(project)
        key = (id(store), store.generation)
        cached = self._lexical.get(project)
        if cached is None or cached[:2] != key:
            cached = (*key, BM25Index(store.records()))
            self._lexical[project] = cached
        return cached[2]

    def rag(self, project: str) -> RAGPipeline:
        s = self.settings
        return RAGPipeline(
            self.store(project), self.embedder, self.llm, s.min_score,
            mode=s.retrieval_mode, min_coverage=s.min_coverage,
            rescue_min_score=s.rescue_min_score, rescue_min_coverage=s.rescue_min_coverage,
            lexical=self.lexical(project) if s.retrieval_mode == "hybrid" else None,
            reranker=self.reranker,
            fallback_llm=MockLLM() if (s.llm_fallback and self.llm.name != "mock") else None,
        )
