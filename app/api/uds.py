from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from app.api.deps import get_actor, get_container, get_project
from app.container import Container
from app.modules.uds.simulator import MUTATIONS
from app.modules.uds.spec import NRC

router = APIRouter(prefix="/projects/{project}/uds", tags=["UDS"])
public = APIRouter(prefix="/uds", tags=["UDS"])
Ctx, Prj, Usr = Depends(get_container), Depends(get_project), Depends(get_actor)


class SpecIn(BaseModel):
    name: str = ""
    spec: dict


class DocIn(BaseModel):
    doc_id: str
    name: str = ""


class ValidateIn(BaseModel):
    requests: list[str]
    session: str | int | None = None
    unlock: int | None = None


class DecisionIn(BaseModel):
    decision: str
    comment: str = ""


class RunIn(BaseModel):
    mutation: str = "none"
    only_approved: bool = False


class LinkIn(BaseModel):
    requirement: str


class AskIn(BaseModel):
    question: str


@public.get("/reference")
def reference():
    return {"nrc": {f"0x{k:02X}": v for k, v in NRC.items()}, "mutations": MUTATIONS}


@router.post("/specs", status_code=201)
def create_spec(b: SpecIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.uds.create_spec(project, b.name, b.spec, user)


@router.post("/specs/from-document", status_code=201)
def spec_from_doc(b: DocIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.uds.spec_from_document(project, b.doc_id, b.name, user)


@router.get("/specs")
def specs(project: str = Prj, c: Container = Ctx):
    return c.uds.specs(project)


@router.put("/specs/{ref}")
def update_spec(ref: str, b: SpecIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.uds.update_spec(project, ref, b.spec, user)


@router.post("/specs/{ref}/decision")
def spec_decision(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.uds.decide_spec(project, ref, b.decision, b.comment, user)


@router.post("/specs/{ref}/validate")
def validate(ref: str, b: ValidateIn, project: str = Prj, c: Container = Ctx):
    return c.uds.validate(project, ref, b.requests, b.session, b.unlock)


@router.post("/specs/{ref}/generate")
def generate(ref: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.uds.generate(project, ref, user)


@router.get("/tests")
def tests(spec: str | None = None, category: str | None = None, status: str | None = None,
          project: str = Prj, c: Container = Ctx):
    return c.uds.tests(project, spec, category, status)


@router.post("/tests/{ref}/decision")
def test_decision(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.uds.decide_test(project, ref, b.decision, b.comment, user)


@router.post("/tests/{ref}/link")
def link(ref: str, b: LinkIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.uds.link_requirement(project, ref, b.requirement, user)


@router.get("/specs/{ref}/coverage")
def coverage(ref: str, project: str = Prj, c: Container = Ctx):
    return c.uds.coverage(project, ref)


@router.get("/specs/{ref}/completeness")
def completeness(ref: str, project: str = Prj, c: Container = Ctx):
    return c.uds.completeness(project, ref)


@router.get("/specs/{ref}/metrics")
def metrics(ref: str, project: str = Prj, c: Container = Ctx):
    return c.uds.metrics(project, ref)


@router.post("/specs/{ref}/run")
def run(ref: str, b: RunIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.uds.run(project, ref, b.mutation, b.only_approved, user)


@router.post("/specs/{ref}/detection-matrix")
def detection(ref: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.uds.detection_matrix(project, ref, user)


@router.get("/specs/{ref}/export")
def export(ref: str, format: str = Query("python"), include_unapproved: bool = False, project: str = Prj,
           c: Container = Ctx, user: str = Usr):
    body, mime, name = c.uds.export(project, ref, format, include_unapproved, user)
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/specs/{ref}/report", response_class=PlainTextResponse)
def report(ref: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.uds.report(project, ref, user)


@router.post("/specs/{ref}/ask")
def ask(ref: str, b: AskIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.uds.ask(project, ref, b.question, user)


@router.get("/records/{ref}/history")
def history(ref: str, project: str = Prj, c: Container = Ctx):
    return c.uds.history(project, ref)
