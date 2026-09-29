"""Dependency container: builds the shared services once and hands them to the API.
Modules (HLD now; HARA/TARA/UDS/Code-review later) plug in here."""
from __future__ import annotations

from app.config import Settings, get_settings
from app.core.db import Database
from app.core.documents import DocumentService
from app.core.embeddings import build_embedder
from app.core.llm import build_llm
from app.core.rag import RAGPipeline
from app.core.vectorstore import build_store


class Container:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.data_dir / "platform.db")
        self.embedder = build_embedder(self.settings)
        self.llm = build_llm(self.settings)
        self._stores: dict[str, object] = {}
        self.documents = DocumentService(self.settings, self.db, self.embedder, self.store)
        # imported here to avoid circular imports as modules grow
        from app.modules.hld.service import HLDService
        self.hld = HLDService(self)

    def store(self, project: str):
        """One isolated vector collection per project (prevents cross-program leakage)."""
        if project not in self._stores:
            self._stores[project] = build_store(self.settings, project)
        return self._stores[project]

    def rag(self, project: str) -> RAGPipeline:
        return RAGPipeline(self.store(project), self.embedder, self.llm, self.settings.min_score)
