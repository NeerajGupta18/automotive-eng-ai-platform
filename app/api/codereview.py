from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.deps import get_actor, get_container, get_project
from app.container import Container
from app.modules.codereview.service import CodeReviewService

router = APIRouter(prefix="/projects/{project}/codereview", tags=["Code review"])
public = APIRouter(prefix="/codereview", tags=["Code review"])
Ctx, Prj, Usr = Depends(get_container), Depends(get_project), Depends(get_actor)


class AnalyzeIn(BaseModel):
    sources: list[str] | None = None
    llm: bool = False
    allow_external: bool = False


class TriageIn(BaseModel):
    disposition: str
    comment: str = ""


class DecisionIn(BaseModel):
    decision: str
    comment: str = ""


class CommentIn(BaseModel):
    comment: str = ""


class LinkIn(BaseModel):
    requirement: str


class ContextIn(BaseModel):
    hld_doc_id: str | None = None


@public.get("/rules")
def rules():
    return CodeReviewService.catalog()


@router.post("/sources", status_code=201)
async def add_source(file: UploadFile = File(...), version: str = Form("1.0"), project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.codereview.add_source(project, file.filename or "source.c", await file.read(), version, user)


@router.get("/sources")
def sources(project: str = Prj, c: Container = Ctx):
    return c.codereview.sources(project)


@router.get("/sources/{ref}")
def source(ref: str, project: str = Prj, c: Container = Ctx):
    return c.codereview.source(project, ref)


@router.post("/logs", status_code=201)
async def add_log(file: UploadFile = File(...), project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.codereview.add_log(project, file.filename or "log", await file.read(), user)


@router.get("/logs")
def logs(project: str = Prj, c: Container = Ctx):
    return c.codereview.logs(project)


@router.put("/context")
def context(b: ContextIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.codereview.set_context(project, b.hld_doc_id, user)


@router.post("/analyze")
def analyze(b: AnalyzeIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.codereview.analyze(project, b.sources, b.llm, b.allow_external, user)


@router.get("/findings")
def findings(source: str | None = None, severity: str | None = None, status: str | None = None, rule: str | None = None,
             origin: str | None = None, project: str = Prj, c: Container = Ctx):
    return c.codereview.findings(project, source, severity, status, rule, origin)


@router.post("/findings/{ref}/triage")
def triage(ref: str, b: TriageIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.codereview.triage(project, ref, b.disposition, b.comment, user)


@router.post("/findings/{ref}/decision")
def second(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.codereview.approve_pending(project, ref, b.decision, b.comment, user)


@router.post("/findings/{ref}/reopen")
def reopen(ref: str, b: CommentIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.codereview.reopen(project, ref, b.comment, user)


@router.get("/findings/{ref}/fix")
def fix(ref: str, project: str = Prj, c: Container = Ctx):
    return c.codereview.fix(project, ref)


@router.post("/findings/{ref}/link")
def link(ref: str, b: LinkIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.codereview.link_requirement(project, ref, b.requirement, user)


@router.get("/metrics")
def metrics(project: str = Prj, c: Container = Ctx):
    return c.codereview.metrics(project)


@router.get("/gate")
def gate(fail_on: str = "high", project: str = Prj, c: Container = Ctx):
    return c.codereview.gate(project, fail_on)


@router.get("/report")
def report(format: str = Query("md", pattern="^(md|json|csv|sarif)$"), project: str = Prj, c: Container = Ctx, user: str = Usr):
    body, mime = c.codereview.report(project, format, user)
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="code_review.{"sarif" if format == "sarif" else format}"'})


@router.get("/records/{ref}/history")
def history(ref: str, project: str = Prj, c: Container = Ctx):
    return c.codereview.history(project, ref)
