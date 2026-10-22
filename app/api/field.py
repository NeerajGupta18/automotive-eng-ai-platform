from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import get_actor, get_container, get_project
from app.container import Container
from app.core.errors import Invalid
from app.modules.field import logs as L
from app.modules.field.service import SAMPLES

router = APIRouter(prefix="/projects/{project}/field", tags=["Field issues (Phase 6)"])
Ctx, Prj, Usr = Depends(get_container), Depends(get_project), Depends(get_actor)


class BuildIn(BaseModel):
    version: str = Field(..., max_length=30)
    ecu: str = Field("AEB", max_length=20)
    firmware: str = "reference"
    calibration: dict = {}
    vehicles: int = 0
    released_at: str = Field("", max_length=30)
    validated: bool = False
    validation_evidence: str = Field("", max_length=300)
    previous: str | None = None
    notes: str = Field("", max_length=500)


class ComplaintItem(BaseModel):
    text: str = Field(..., min_length=10, max_length=4000)
    vin: str = Field("", max_length=20)
    build: str = Field("", max_length=30)
    reported_at: str = Field("", max_length=40)
    dealer: str = Field("", max_length=80)
    log_name: str = Field("recording.csv", max_length=120)
    log_csv: str | None = Field(None, max_length=L.MAX_BYTES)


class ImportIn(BaseModel):
    complaints: list[ComplaintItem]
    investigate: bool = True


class DecisionIn(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected)$")
    comment: str = Field("", max_length=1000)


@router.get("/overview")
def overview(project: str = Prj, c: Container = Ctx):
    return c.field.metrics(project)


# ------------------------------------------------------------------------- builds
@router.get("/builds")
def builds(project: str = Prj, c: Container = Ctx):
    return c.field.builds(project)


@router.post("/builds", status_code=201)
def add_build(b: BuildIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Register a software build in the field. Firmware is the repository code or one of the predefined defect variants."""
    return c.field.add_build(project, b.model_dump(), user)


# ------------------------------------------------------------------------- complaints
@router.post("/complaints", status_code=201)
async def submit(text: str = Form(..., min_length=10, max_length=4000), vin: str = Form(""), build: str = Form(""),
                 reported_at: str = Form(""), dealer: str = Form(""), investigate: bool = Form(True),
                 file: UploadFile | None = File(None), project: str = Prj, c: Container = Ctx, user: str = Usr):
    """File one complaint with an optional event recording (CSV). A new issue starts the agent's investigation."""
    content = None
    if file is not None and file.filename:
        content = await file.read(L.MAX_BYTES + 1)
        if len(content) > L.MAX_BYTES:
            raise Invalid("recording larger than 3 MB")
    return c.field.submit(project, text, user, vin=vin, build=build, reported_at=reported_at, dealer=dealer,
                          log_name=file.filename if file is not None else None, log_content=content or None, investigate=investigate)


@router.post("/complaints/import", status_code=201)
def import_complaints(b: ImportIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Several complaints at once (e.g. the dealer system's daily export); investigations start after all are filed."""
    items = [{**x.model_dump(exclude={"log_csv"}), "log_content": x.log_csv.encode() if x.log_csv else None} for x in b.complaints]
    return c.field.import_batch(project, items, user, b.investigate)


@router.post("/samples/load", status_code=201)
def load_samples(investigate: bool = True, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Demo data: the sample build registry and six dealer complaints with recordings (synthetic)."""
    b = c.field.load_sample_builds(project, user)
    data = json.loads((SAMPLES / "complaints.json").read_text())
    items = [{**{k: x.get(k, "") for k in ("text", "vin", "build", "reported_at", "dealer")}, "log_name": x["log"].split("/")[-1],
              "log_content": (SAMPLES / x["log"]).read_bytes()} for x in data["complaints"]]
    return {"builds": b, **c.field.import_batch(project, items, user, investigate)}


@router.get("/complaints")
def complaints(project: str = Prj, c: Container = Ctx):
    return c.field.complaints(project)


@router.get("/complaints/{ref}")
def complaint(ref: str, project: str = Prj, c: Container = Ctx):
    return c.field.complaint(project, ref)


@router.get("/complaints/{ref}/trace")
def trace(ref: str, project: str = Prj, c: Container = Ctx):
    """Recorded signals and the closed-loop reconstruction (field build and recommended fix) for plots."""
    return c.field.signals(project, ref)


@router.get("/complaints/{ref}/recording")
def recording(ref: str, project: str = Prj, c: Container = Ctx):
    body, name = c.field.log_csv(project, ref)
    return Response(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{ref}_{name}"'})


# ------------------------------------------------------------------------- issues
@router.get("/issues")
def issues(project: str = Prj, c: Container = Ctx):
    return c.field.issues(project)


@router.get("/issues/{ref}")
def issue(ref: str, project: str = Prj, c: Container = Ctx):
    return c.field.issue(project, ref)


@router.get("/issues/{ref}/analysis")
def analysis(ref: str, project: str = Prj, c: Container = Ctx):
    """The full per-complaint diagnosis (what the agent's tools summarise)."""
    a = c.field.analysis(project, ref)
    fix = c.field.tool_fix(project, ref) if a["aggregate"]["class"] in ("calibration_regression", "sensor_false_target",
                                                                         "implementation_defect") else None
    return {**a, "fix": fix}


@router.post("/issues/{ref}/investigate", status_code=201)
def investigate(ref: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Start a new investigation run (e.g. after new complaints joined the issue)."""
    return c.field.investigate(project, ref, user)


@router.post("/issues/{ref}/verify", status_code=201)
def verify(ref: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Run the ADAS verification campaign with the fix calibration (after the field scenarios were approved)."""
    return c.field.verify(project, ref, user)


@router.post("/issues/{ref}/decision")
def decide(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Close the issue: 'approved' = resolved (needs the released verification for a fix), 'rejected' = closed without a fix."""
    return c.field.decide_issue(project, ref, b.decision, b.comment, user)


@router.get("/issues/{ref}/report")
def report(ref: str, format: str = Query("md", pattern="^(md|json)$"), project: str = Prj, c: Container = Ctx):
    body, mime = c.field.report(project, ref, format)
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{ref}_8D.{format}"'})


@router.get("/evaluation")
def evaluation(project: str = Prj, c: Container = Ctx):
    """Latest evaluation of the agent on the labelled cases (scripts/eval_field_agent.py), if it has been run."""
    from pathlib import Path
    f = Path(__file__).resolve().parents[2] / "eval" / "field" / "results" / "field_agent.json"
    if not f.is_file():
        from app.core.errors import NotFound
        raise NotFound("No evaluation results yet: run python scripts/eval_field_agent.py")
    return json.loads(f.read_text())
