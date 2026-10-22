from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import get_actor, get_container, get_project
from app.container import Container

router = APIRouter(prefix="/projects/{project}/adas", tags=["ADAS virtual validation"])
Ctx, Prj, Usr = Depends(get_container), Depends(get_project), Depends(get_actor)


class DecisionIn(BaseModel):
    decision: str
    comment: str = ""


class RequirementIn(BaseModel):
    requirement: dict


class ScenarioIn(BaseModel):
    scenario: dict


class TextIn(BaseModel):
    text: str = Field(..., max_length=500)
    use_llm: bool = False


class RunIn(BaseModel):
    target: str = "model"
    calibration: dict = {}


class CampaignIn(BaseModel):
    target: str = "auto"
    calibration: dict = {}
    back_to_back: bool = True
    mutation: bool = False
    note: str = Field("", max_length=500)


@router.get("/status")
def status(project: str = Prj, c: Container = Ctx):
    """Library, record counts, SIL availability (compiler / prebuilt library), latest campaign and current release."""
    return c.adas.status(project)


@router.get("/reference")
def reference(project: str = Prj, c: Container = Ctx):
    return c.adas.reference()


@router.post("/library/load")
def load_library(project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Load the AEB requirement set and the scenario catalog (Euro NCAP-style grids + SOTIF edge cases) as approved."""
    return c.adas.load_library(project, user)


@router.get("/requirements")
def requirements(status: str | None = None, project: str = Prj, c: Container = Ctx):
    return c.adas.requirements(project, status)


@router.post("/requirements", status_code=201)
def create_requirement(b: RequirementIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.adas.create_requirement(project, b.requirement, user)


@router.put("/requirements/{ref}")
def edit_requirement(ref: str, b: RequirementIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Changing an approved requirement needs re-approval and makes released campaigns that used it suspect."""
    return c.adas.edit_requirement(project, ref, b.requirement, user)


@router.post("/requirements/{ref}/decision")
def decide_requirement(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.adas.decide_requirement(project, ref, b.decision, b.comment, user)


@router.get("/coverage")
def coverage(project: str = Prj, c: Container = Ctx):
    return c.adas.coverage(project)


@router.get("/scenarios")
def scenarios(status: str | None = None, family: str | None = None, project: str = Prj, c: Container = Ctx):
    return c.adas.scenarios(project, status, family)


@router.post("/scenarios", status_code=201)
def create_scenario(b: ScenarioIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.adas.create_scenario(project, b.scenario, user)


@router.post("/scenarios/propose")
def propose(project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Boundary-value analysis: proposes scenarios at requirement boundaries that nothing covers yet."""
    return c.adas.propose_scenarios(project, user)


@router.post("/scenarios/from-text", status_code=201)
def from_text(b: TextIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Plain-language description -> proposed scenario, with what was understood, assumed and not modelled."""
    return c.adas.scenario_from_text(project, b.text, user, b.use_llm)


@router.get("/scenarios/{ref}")
def scenario(ref: str, project: str = Prj, c: Container = Ctx):
    return c.adas.scenario(project, ref)


@router.put("/scenarios/{ref}")
def edit_scenario(ref: str, b: ScenarioIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.adas.edit_scenario(project, ref, b.scenario, user)


@router.post("/scenarios/{ref}/decision")
def decide_scenario(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.adas.decide_scenario(project, ref, b.decision, b.comment, user)


@router.post("/scenarios/{ref}/run")
def run_scenario(ref: str, b: RunIn, project: str = Prj, c: Container = Ctx):
    """Simulate one scenario (model or SIL, optional calibration what-if) and return the time series. Stores nothing."""
    return c.adas.run_scenario(project, ref, b.target, b.calibration)


@router.get("/scenarios/{ref}/openscenario")
def openscenario(ref: str, project: str = Prj, c: Container = Ctx):
    body, name = c.adas.openscenario(project, ref)
    return Response(body, media_type="application/xml", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/openscenario.zip")
def openscenario_zip(project: str = Prj, c: Container = Ctx):
    return Response(c.adas.openscenario_zip(project), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{project}_aeb_scenarios.zip"'})


@router.post("/campaigns", status_code=201)
def run_campaign(b: CampaignIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Run every approved scenario (SIL when available), judge the requirements, back-to-back and optional mutation testing."""
    return c.adas.run_campaign(project, user, b.target, b.calibration, b.back_to_back, b.mutation, b.note)


@router.get("/campaigns")
def campaigns(project: str = Prj, c: Container = Ctx):
    return c.adas.campaigns(project)


@router.get("/campaigns/{ref}")
def get_campaign(ref: str, project: str = Prj, c: Container = Ctx):
    return c.adas.get_campaign(project, ref)


@router.post("/campaigns/{ref}/decision")
def decide_campaign(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Release ('approved') or reject. Four-eyes; blocked unless SIL evidence passed, back-to-back passed and nothing changed since."""
    return c.adas.decide_campaign(project, ref, b.decision, b.comment, user)


@router.get("/campaigns/{ref}/report")
def report(ref: str, format: str = Query("md"), project: str = Prj, c: Container = Ctx):
    body, mime = c.adas.report(project, ref, format)
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{ref}_validation.{format}"'})


@router.get("/firmware")
def firmware(project: str = Prj, c: Container = Ctx):
    """The AEB C source under test, the platform's code-review findings on it, and SIL build status."""
    return c.adas.firmware()
