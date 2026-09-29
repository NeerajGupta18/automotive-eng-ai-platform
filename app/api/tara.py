from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from app.api.deps import get_actor, get_container, get_project
from app.container import Container
from app.core.errors import Invalid
from app.modules.tara import rules

router = APIRouter(prefix="/projects/{project}/tara", tags=["TARA"])
public = APIRouter(prefix="/tara", tags=["TARA"])
MAX_CSV = 2 * 1024 * 1024

Ctx = Depends(get_container)
Prj = Depends(get_project)
Usr = Depends(get_actor)


class LibraryIn(BaseModel):
    library: dict | None = None


class SystemIn(BaseModel):
    name: str
    description: str = ""
    architecture: dict | None = None


class ArchIn(BaseModel):
    architecture: dict


class HldIn(BaseModel):
    doc_id: str


class ThreatIn(BaseModel):
    name: str
    stride: str
    property: str
    interface: str
    description: str
    damage: str


class AssessIn(BaseModel):
    impact: dict
    ap: dict
    rationale: str = ""
    safety_link: str | None = None


class TreatIn(BaseModel):
    decision: str
    justification: str = ""


class DecisionIn(BaseModel):
    decision: str
    comment: str = ""


class CommentIn(BaseModel):
    comment: str = ""


class ReqIn(BaseModel):
    text: str | None = None
    allocated_to: str | None = None
    verification: list[str] | None = None


class NAIn(BaseModel):
    component: str
    interface: str
    stride: str
    justification: str


@public.get("/rules")
def rules_info():
    return {"ruleset": rules.RULESET_VERSION, "impact_levels": rules.IMPACT_LEVELS, "impact_categories": rules.IMPACT_CATEGORIES,
            "attack_potential": rules.ATTACK_POTENTIAL, "risk_matrix": rules.RISK_MATRIX,
            "mitigation_threshold": rules.MITIGATION_THRESHOLD, "stride": rules.STRIDE, "treatments": rules.TREATMENTS}


@router.post("/library/load", status_code=201)
def load_library(b: LibraryIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.load_library(project, user, b.library)


@router.get("/library")
def library(project: str = Prj, c: Container = Ctx):
    return c.tara.library_status(project)


@router.post("/systems", status_code=201)
def create_system(b: SystemIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.create_system(project, b.name, b.description, b.architecture, user)


@router.get("/systems")
def systems(project: str = Prj, c: Container = Ctx):
    return c.tara.systems(project)


@router.put("/systems/{system}/architecture")
def set_arch(system: str, b: ArchIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.set_architecture(project, system, b.architecture, user)


@router.post("/systems/{system}/import/comm-matrix")
async def import_matrix(system: str, file: UploadFile = File(...), project: str = Prj, c: Container = Ctx, user: str = Usr):
    raw = await file.read()
    if len(raw) > MAX_CSV:
        raise Invalid("Communication matrix larger than 2 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Invalid("Communication matrix must be UTF-8 text")
    return c.tara.import_comm_matrix(project, system, text, user)


@router.post("/systems/{system}/import/hld")
def import_hld(system: str, b: HldIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.import_hld(project, system, b.doc_id, user)


@router.get("/systems/{system}/attack-surface")
def surface(system: str, project: str = Prj, c: Container = Ctx):
    return c.tara.attack_surface(project, system)


@router.post("/systems/{system}/analyze")
def analyze(system: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.analyze(project, system, user)


@router.get("/assets")
def assets(system: str | None = None, project: str = Prj, c: Container = Ctx):
    return c.tara.assets(project, system)


@router.post("/assets/{ref}/threats", status_code=201)
def add_threat(ref: str, b: ThreatIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.add_threat(project, ref, b.name, b.stride, b.property, b.interface, b.description, b.damage, user)


@router.get("/threats")
def threats(system: str | None = None, status: str | None = None, project: str = Prj, c: Container = Ctx):
    return c.tara.threats(project, system, status)


@router.put("/threats/{ref}/assessment")
def assess(ref: str, b: AssessIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.assess_threat(project, ref, b.impact, b.ap, b.rationale, b.safety_link, user)


@router.post("/threats/{ref}/decision")
def threat_decision(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.decide_threat(project, ref, b.decision, b.comment, user)


@router.put("/threats/{ref}/treatment")
def treat(ref: str, b: TreatIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.treat_threat(project, ref, b.decision, b.justification, user)


@router.post("/threats/{ref}/accept-risk")
def accept_risk(ref: str, b: CommentIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.accept_risk(project, ref, b.comment, user)


@router.post("/systems/{system}/derive-goals")
def derive_goals(system: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.derive_goals(project, system, user)


@router.get("/goals")
def goals(system: str | None = None, project: str = Prj, c: Container = Ctx):
    return c.tara.goals(project, system)


@router.post("/goals/{ref}/decision")
def goal_decision(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.decide_goal(project, ref, b.decision, b.comment, user)


@router.post("/goals/{ref}/recommend-controls")
def recommend(ref: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.recommend_controls(project, ref, user)


@router.get("/requirements")
def requirements(system: str | None = None, project: str = Prj, c: Container = Ctx):
    return c.tara.csrs(project, system)


@router.put("/requirements/{ref}")
def update_req(ref: str, b: ReqIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.update_csr(project, ref, user, b.text, b.allocated_to, b.verification)


@router.post("/requirements/{ref}/decision")
def req_decision(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.decide_csr(project, ref, b.decision, b.comment, user)


@router.get("/systems/{system}/coverage")
def coverage(system: str, project: str = Prj, c: Container = Ctx):
    return c.tara.coverage(project, system)


@router.post("/systems/{system}/coverage/na", status_code=201)
def add_na(system: str, b: NAIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.add_na(project, system, b.component, b.interface, b.stride, b.justification, user)


@router.post("/na/{ref}/decision")
def na_decision(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.tara.decide_na(project, ref, b.decision, b.comment, user)


@router.get("/systems/{system}/completeness")
def completeness(system: str, project: str = Prj, c: Container = Ctx):
    return c.tara.completeness(project, system)


@router.get("/systems/{system}/trace")
def trace(system: str, project: str = Prj, c: Container = Ctx):
    return c.tara.trace_matrix(project, system)


@router.get("/systems/{system}/metrics")
def metrics(system: str, project: str = Prj, c: Container = Ctx):
    return c.tara.metrics(project, system)


@router.get("/systems/{system}/report")
def report(system: str, format: str = Query("md", pattern="^(md|json|csv)$"), project: str = Prj,
           c: Container = Ctx, user: str = Usr):
    body, mime = c.tara.report(project, system, format, user)
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{system}_tara.{format}"'})


@router.get("/records/{ref}/history")
def history(ref: str, project: str = Prj, c: Container = Ctx):
    return c.tara.history(project, ref)


@router.get("/graph", response_class=PlainTextResponse)
def graph(project: str = Prj, c: Container = Ctx):
    return c.tara.trace.dot(project)
