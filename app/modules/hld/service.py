"""HLD module service: orchestrates extraction, analysis, Q&A, review queueing, audit."""
from __future__ import annotations

from app.core.parsing import split_sections
from app.modules.hld import analysis
from app.modules.hld.extractor import extract_entities, llm_fallback


class HLDService:
    MODULE = "hld"

    def __init__(self, container):
        self.c = container

    # ---------------------------------------------------------------- helpers
    def _doc(self, doc_id: str) -> dict:
        d = self.c.db.document(doc_id)
        if not d:
            raise KeyError(doc_id)
        return d

    # ---------------------------------------------------------------- Q&A
    def ask(self, project: str, question: str, k: int = 5, include_draft: bool = False,
            doc_ids: list[str] | None = None, actor: str = "anonymous") -> dict:
        docs = self.c.db.documents(project) if include_draft else self.c.db.documents(project, "approved")
        allowed = [d["id"] for d in docs if doc_ids is None or d["id"] in doc_ids]
        ans = self.c.rag(project).ask(question, k=k, doc_ids=allowed)
        self.c.db.audit(actor, "hld.ask", project, None, {
            "question": question, "docs": allowed, "refused": ans.refused,
            "confidence": ans.confidence, "cited": [c.label for c in ans.citations]})
        return ans.to_dict()

    # ---------------------------------------------------------------- extraction
    def extract(self, doc_id: str, refresh: bool = False, use_llm: bool = False, actor: str = "system") -> dict:
        doc = self._doc(doc_id)
        if not refresh:
            cached = self.c.db.get_extraction(doc_id)
            if cached:
                return cached
        pages = self.c.documents.load_pages(doc_id)
        ex = extract_entities(pages)
        if use_llm and not ex["components"]:
            ex = llm_fallback(self.c.llm, split_sections(pages))
        ex["issues"] = analysis.check_consistency(ex)
        ex["dependencies"] = analysis.dependencies(ex)
        ex["document"] = {"id": doc_id, "name": doc["name"], "version": doc["version"], "status": doc["status"]}
        ex["summary"] = {"components": len(ex["components"]), "interfaces": len(ex["interfaces"]),
                         "ports": sum(len(c["ports"]) for c in ex["components"]),
                         "signals": len(ex["signals"]), "dependencies": len(ex["dependencies"]),
                         "issues": len(ex["issues"])}
        self.c.db.save_extraction(doc_id, ex)
        queued = 0
        for i in ex["issues"]:
            if i["severity"] in ("error", "warning"):   # info items are reported, not queued
                queued += self.c.db.add_review(doc["project"], self.MODULE, "inconsistency",
                                               f"{doc_id}:{i['id']}", {**i, "doc_id": doc_id, "doc_name": doc["name"]})
        ex["review_items_queued"] = queued
        self.c.db.audit(actor, "hld.extract", doc["project"], doc_id, {**ex["summary"], "llm": use_llm})
        return ex

    def compare(self, doc_a: str, doc_b: str, actor: str = "system") -> dict:
        a, b = self.extract(doc_a), self.extract(doc_b)
        out = analysis.compare(a, b)
        out["from"], out["to"] = a["document"], b["document"]
        self.c.db.audit(actor, "hld.compare", self._doc(doc_a)["project"], None, {"a": doc_a, "b": doc_b})
        return out

    def impact(self, doc_id: str, component: str) -> dict:
        return analysis.impact(self.extract(doc_id), component)

    def dot(self, doc_id: str) -> str:
        return analysis.to_dot(self.extract(doc_id))

    def export(self, doc_id: str, fmt: str, actor: str = "system") -> tuple[str, str]:
        ex = self.extract(doc_id)
        self.c.db.audit(actor, "hld.export", self._doc(doc_id)["project"], doc_id, {"format": fmt})
        if fmt == "csv":
            return analysis.ports_csv(ex), "text/csv"
        import json
        return json.dumps(ex, indent=2), "application/json"
