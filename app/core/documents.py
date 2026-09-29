"""Controlled document ingestion: validate -> parse -> chunk -> embed -> store -> register."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from app.core.chunking import chunk_sections
from app.core.db import Database, now
from app.core.embeddings import Embedder
from app.core.parsing import Page, parse_document, split_sections
from app.config import Settings


class IngestError(ValueError):
    pass


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", Path(name).name)[:120] or "document"


class DocumentService:
    def __init__(self, settings: Settings, db: Database, embedder: Embedder, store_factory):
        self.s, self.db, self.embedder, self.store_for = settings, db, embedder, store_factory
        (settings.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "parsed").mkdir(parents=True, exist_ok=True)

    def ingest(self, project: str, filename: str, content: bytes, version: str = "1.0",
               status: str = "approved", actor: str = "anonymous") -> dict:
        if not self.db.project_exists(project):
            raise IngestError(f"Unknown project '{project}'")
        fname = safe_filename(filename)
        if Path(fname).suffix.lower() not in self.s.allowed_extensions:
            raise IngestError(f"File type not allowed. Allowed: {', '.join(self.s.allowed_extensions)}")
        if len(content) > self.s.max_upload_mb * 1024 * 1024:
            raise IngestError(f"File exceeds {self.s.max_upload_mb} MB limit")
        if not content:
            raise IngestError("Empty file")
        if status not in ("approved", "draft"):
            raise IngestError("status must be 'approved' or 'draft'")
        sha = hashlib.sha256(content).hexdigest()
        dup = self.db.document_by_hash(project, sha)
        if dup:
            raise IngestError(f"Identical document already ingested as {dup['id']} ({dup['name']} v{dup['version']})")

        doc_id = uuid.uuid4().hex[:12]
        path = self.s.data_dir / "uploads" / f"{doc_id}_{fname}"
        path.write_bytes(content)
        try:
            pages = parse_document(path)
        except Exception as e:  # corrupt/encrypted PDFs etc.
            path.unlink(missing_ok=True)
            raise IngestError(f"Could not parse document: {e}") from e
        if not any(p.text.strip() for p in pages):
            path.unlink(missing_ok=True)
            raise IngestError("No extractable text found (scanned PDF? install Tesseract for OCR)")

        (self.s.data_dir / "parsed" / f"{doc_id}.json").write_text(
            json.dumps([{"number": p.number, "text": p.text} for p in pages]))
        meta = {"doc_id": doc_id, "doc_name": fname, "version": version, "project": project}
        chunks = chunk_sections(split_sections(pages), meta, self.s.chunk_chars, self.s.chunk_overlap_chars)
        if chunks:
            self.store_for(project).add(chunks, self.embedder.embed([c.text for c in chunks]))
        rec = {"id": doc_id, "project": project, "name": fname, "version": version, "status": status,
               "sha256": sha, "pages": len(pages), "chunks": len(chunks), "uploaded_by": actor,
               "uploaded_at": now()}
        self.db.add_document(rec)
        self.db.audit(actor, "document.ingest", project, doc_id,
                      {"name": fname, "version": version, "status": status, "chunks": len(chunks)})
        return rec

    def load_pages(self, doc_id: str) -> list[Page]:
        p = self.s.data_dir / "parsed" / f"{doc_id}.json"
        if not p.exists():
            raise IngestError(f"Parsed content for {doc_id} not found")
        return [Page(x["number"], x["text"]) for x in json.loads(p.read_text())]

    def delete(self, project: str, doc_id: str, actor: str) -> None:
        self.store_for(project).delete({"doc_id": doc_id})
        self.db._q("DELETE FROM documents WHERE id=?", (doc_id,))
        self.db._q("DELETE FROM extractions WHERE doc_id=?", (doc_id,))
        self.db.audit(actor, "document.delete", project, doc_id)
