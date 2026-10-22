from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import get_actor, get_container, get_project
from app.container import Container
from app.modules.vuln.service import JUSTIFICATIONS, RESPONSES, STATES

router = APIRouter(prefix="/projects/{project}/vuln", tags=["Vulnerability monitoring"])
Ctx, Prj, Usr = Depends(get_container), Depends(get_project), Depends(get_actor)


class TriageIn(BaseModel):
    state: str
    justification: str | None = None
    response: list[str] = Field(default_factory=list)
    detail: str = ""


class DecisionIn(BaseModel):
    decision: str
    comment: str = ""


class CommentIn(BaseModel):
    comment: str = ""


@router.get("/reference")
def reference():
    return {"states": STATES, "justifications": JUSTIFICATIONS, "responses": RESPONSES}


@router.post("/library/load", status_code=201)
def load_bundled_feed(project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Load the bundled demo advisory feed (OSV format) and rescan every ECU."""
    return c.vuln.load_feed(project, user)


@router.post("/library/upload", status_code=201)
async def upload_feed(file: UploadFile = File(...), project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Load an OSV feed: JSON (object, list or {"vulnerabilities": [...]}) or a zip of OSV files."""
    return c.vuln.load_feed(project, user, await file.read(), file.filename or "feed.json")


@router.get("/library")
def feed_status(project: str = Prj, c: Container = Ctx):
    return c.vuln.feed_status(project)


@router.post("/sboms", status_code=201)
async def add_sbom(file: UploadFile = File(...), project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.vuln.add_sbom(project, file.filename or "sbom.json", await file.read(), user)


@router.get("/sboms")
def sboms(project: str = Prj, c: Container = Ctx):
    return c.vuln.sboms(project)


@router.get("/sboms/{ref}")
def sbom(ref: str, project: str = Prj, c: Container = Ctx):
    return c.vuln.sbom(project, ref)


@router.get("/ecus")
def ecus(project: str = Prj, c: Container = Ctx):
    return c.vuln.ecus(project)


@router.post("/scan")
def scan(project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.vuln.scan(project, user)


@router.get("/findings")
def findings(status: str | None = None, ecu: str | None = None, severity: str | None = None,
             project: str = Prj, c: Container = Ctx):
    return c.vuln.findings(project, status, ecu, severity)


@router.get("/findings/{ref}")
def finding(ref: str, project: str = Prj, c: Container = Ctx):
    return c.vuln.get(project, ref, "vfinding")


@router.get("/findings/{ref}/history")
def finding_history(ref: str, project: str = Prj, c: Container = Ctx):
    return c.vuln.history(project, ref)


@router.post("/findings/{ref}/triage")
def triage(ref: str, b: TriageIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.vuln.triage(project, ref, b.state, user, b.justification, b.response, b.detail)


@router.post("/findings/{ref}/decision")
def decision(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Second person confirms or returns a not_affected / false_positive analysis of a high or critical finding."""
    return c.vuln.approve_pending(project, ref, b.decision, b.comment, user)


@router.post("/findings/{ref}/reopen")
def reopen(ref: str, b: CommentIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.vuln.reopen(project, ref, b.comment, user)


@router.get("/vex")
def vex(ecu: str | None = Query(None), project: str = Prj, c: Container = Ctx):
    """CycloneDX 1.5 VEX document with the analysis state of every finding."""
    body = json.dumps(c.vuln.vex(project, ecu), indent=1)
    name = f"vex_{ecu or 'all'}.cdx.json"
    return Response(body, media_type="application/vnd.cyclonedx+json", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/metrics")
def metrics(project: str = Prj, c: Container = Ctx):
    return c.vuln.metrics(project)
