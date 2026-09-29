"""FastAPI service layer. Every mutating call records the acting user (X-User header)
in the audit log. Real authentication/RBAC is a Stage-5 item (see README)."""
from __future__ import annotations

import re

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from app.container import Container
from app.core.documents import IngestError
from app.core.errors import Conflict, Invalid, NotFound

PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")


class ProjectIn(BaseModel):
    name: str


class QueryIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    k: int = Field(5, ge=1, le=20)
    include_draft: bool = False
    doc_ids: list[str] | None = None


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    comment: str = ""


def create_app(container: Container | None = None) -> FastAPI:
    app = FastAPI(title="Automotive Engineering AI Platform", version="0.1.0")
    app.state.container = container

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound):
        return JSONResponse({"detail": str(e)}, 404)

    @app.exception_handler(Invalid)
    async def _inv(_: Request, e: Invalid):
        return JSONResponse({"detail": str(e)}, 422)

    @app.exception_handler(Conflict)
    async def _conf(_: Request, e: Conflict):
        return JSONResponse({"detail": str(e)}, 409)

    from app.api import hara as hara_api
    app.include_router(hara_api.router)
    app.include_router(hara_api.public)
    from app.api import tara as tara_api
    app.include_router(tara_api.router)
    app.include_router(tara_api.public)
    from app.api import uds as uds_api
    app.include_router(uds_api.router)
    app.include_router(uds_api.public)
    from app.api import codereview as cr_api
    app.include_router(cr_api.router)
    app.include_router(cr_api.public)

    def c(request: Request) -> Container:
        if request.app.state.container is None:      # lazy: built on first request
            request.app.state.container = Container()
        return request.app.state.container

    def actor(x_user: str = Header(default="anonymous")) -> str:
        return x_user[:64]

    def need_project(cont: Container, name: str) -> None:
        if not cont.db.project_exists(name):
            raise HTTPException(404, f"Project '{name}' not found")

    def need_doc(cont: Container, project: str, doc_id: str) -> dict:
        d = cont.db.document(doc_id)
        if not d or d["project"] != project:          # project isolation
            raise HTTPException(404, "Document not found in this project")
        return d

    # ------------------------------------------------------------ system
    @app.get("/health")
    def health(cont: Container = Depends(c)):
        s = cont.settings
        return {"status": "ok", "llm": cont.llm.name, "embeddings": s.embed_backend, "vector_store": s.vector_backend}

    # ------------------------------------------------------------ projects
    @app.post("/projects", status_code=201)
    def create_project(p: ProjectIn, cont: Container = Depends(c), user: str = Depends(actor)):
        if not PROJECT_RE.match(p.name):
            raise HTTPException(422, "Project name: 2-64 chars, letters/digits/_-. only")
        cont.db.create_project(p.name)
        cont.db.audit(user, "project.create", p.name)
        return {"name": p.name}

    @app.get("/projects")
    def list_projects(cont: Container = Depends(c)):
        return cont.db.projects()

    # ------------------------------------------------------------ documents
    @app.post("/projects/{project}/documents", status_code=201)
    async def upload(project: str, file: UploadFile = File(...), version: str = Form("1.0"),
                     status: str = Form("approved"), cont: Container = Depends(c), user: str = Depends(actor)):
        need_project(cont, project)
        try:
            return cont.documents.ingest(project, file.filename or "document", await file.read(),
                                         version=version[:32], status=status, actor=user)
        except IngestError as e:
            raise HTTPException(400, str(e))

    @app.get("/projects/{project}/documents")
    def documents(project: str, cont: Container = Depends(c)):
        need_project(cont, project)
        return cont.db.documents(project)

    @app.post("/projects/{project}/documents/{doc_id}/status")
    def set_status(project: str, doc_id: str, status: str = Query(pattern="^(approved|draft)$"),
                   cont: Container = Depends(c), user: str = Depends(actor)):
        need_doc(cont, project, doc_id)
        cont.db.set_document_status(doc_id, status)
        cont.db.audit(user, "document.status", project, doc_id, {"status": status})
        return {"id": doc_id, "status": status}

    @app.delete("/projects/{project}/documents/{doc_id}")
    def delete_document(project: str, doc_id: str, cont: Container = Depends(c), user: str = Depends(actor)):
        need_doc(cont, project, doc_id)
        cont.documents.delete(project, doc_id, user)
        return {"deleted": doc_id}

    # ------------------------------------------------------------ HLD module
    @app.post("/projects/{project}/hld/query")
    def hld_query(project: str, q: QueryIn, cont: Container = Depends(c), user: str = Depends(actor)):
        need_project(cont, project)
        return cont.hld.ask(project, q.question, q.k, q.include_draft, q.doc_ids, user)

    @app.get("/projects/{project}/hld/{doc_id}/entities")
    def hld_entities(project: str, doc_id: str, refresh: bool = False, use_llm: bool = False,
                     cont: Container = Depends(c), user: str = Depends(actor)):
        need_doc(cont, project, doc_id)
        return cont.hld.extract(doc_id, refresh=refresh, use_llm=use_llm, actor=user)

    @app.get("/projects/{project}/hld/{doc_id}/issues")
    def hld_issues(project: str, doc_id: str, cont: Container = Depends(c)):
        need_doc(cont, project, doc_id)
        return cont.hld.extract(doc_id)["issues"]

    @app.get("/projects/{project}/hld/{doc_id}/dependencies")
    def hld_deps(project: str, doc_id: str, cont: Container = Depends(c)):
        need_doc(cont, project, doc_id)
        return cont.hld.extract(doc_id)["dependencies"]

    @app.get("/projects/{project}/hld/{doc_id}/impact/{component}")
    def hld_impact(project: str, doc_id: str, component: str, cont: Container = Depends(c)):
        need_doc(cont, project, doc_id)
        try:
            return cont.hld.impact(doc_id, component)
        except KeyError:
            raise HTTPException(404, f"Component '{component}' not found")

    @app.get("/projects/{project}/hld/{doc_id}/graph", response_class=PlainTextResponse)
    def hld_graph(project: str, doc_id: str, cont: Container = Depends(c)):
        need_doc(cont, project, doc_id)
        return cont.hld.dot(doc_id)

    @app.get("/projects/{project}/hld/{doc_id}/export")
    def hld_export(project: str, doc_id: str, format: str = Query("json", pattern="^(json|csv)$"),
                   cont: Container = Depends(c), user: str = Depends(actor)):
        need_doc(cont, project, doc_id)
        body, mime = cont.hld.export(doc_id, format, user)
        return Response(body, media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="{doc_id}.{format}"'})

    @app.get("/projects/{project}/hld/compare")
    def hld_compare(project: str, a: str, b: str, cont: Container = Depends(c), user: str = Depends(actor)):
        need_doc(cont, project, a); need_doc(cont, project, b)
        return cont.hld.compare(a, b, user)

    # ------------------------------------------------------------ review queue & audit
    @app.get("/projects/{project}/reviews")
    def reviews(project: str, status: str | None = Query(None, pattern="^(pending|approved|rejected)$"),
                module: str | None = None, cont: Container = Depends(c)):
        need_project(cont, project)
        return cont.db.reviews(project, status, module)

    @app.post("/projects/{project}/reviews/{review_id}/decision")
    def decide(project: str, review_id: int, d: DecisionIn, cont: Container = Depends(c),
               user: str = Depends(actor)):
        r = cont.db.review(review_id)
        if not r or r["project"] != project:
            raise HTTPException(404, "Review item not found")
        if r["status"] != "pending":
            raise HTTPException(409, f"Already decided: {r['status']}")
        cont.db.decide_review(review_id, d.decision, user, d.comment)
        cont.db.audit(user, "review.decision", project, str(review_id),
                      {"decision": d.decision, "ref": r["ref"], "comment": d.comment})
        return cont.db.review(review_id)

    @app.get("/projects/{project}/audit")
    def audit(project: str, limit: int = Query(200, le=1000), cont: Container = Depends(c)):
        need_project(cont, project)
        return cont.db.audit_log(project, limit)

    return app


app = create_app()
