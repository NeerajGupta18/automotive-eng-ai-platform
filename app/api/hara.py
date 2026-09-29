from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import get_actor, get_container, get_project
from app.container import Container
from app.modules.hara import rules

router = APIRouter(prefix="/projects/{project}/hara", tags=["HARA"])
public = APIRouter(prefix="/hara", tags=["HARA"])


class FunctionIn(BaseModel):
    name: str
    description: str = ""
    guidewords: list[str] | None = None


class ItemIn(BaseModel):
    name: str
    description: str = ""
    functions: list[FunctionIn]
    hld_doc_id: str | None = None


class LibraryIn(BaseModel):
    library: dict | None = None


class HazardIn(BaseModel):
    function_id: str
    guideword: str
    description: str


class EventIn(BaseModel):
    situation: str


class AssessIn(BaseModel):
    S: int
    E: int
    C: int
    rationale_S: str = ""
    rationale_E: str = ""
    rationale_C: str = ""


class DecisionIn(BaseModel):
    decision: str
    comment: str = ""


class GoalIn(BaseModel):
    text: str | None = None
    safe_state: str | None = None
    ftti_ms: int | None = None


class ReqIn(BaseModel):
    text: str | None = None
    allocated_to: str | None = None
    verification: list[str] | None = None


@public.get("/asil-table")
def asil_table():
    return {"ruleset": rules.RULESET_VERSION, "table": rules.asil_table(), "guidewords": rules.GUIDEWORDS,
            "severity": rules.SEVERITY, "exposure": rules.EXPOSURE, "controllability": rules.CONTROLLABILITY}


@router.post("/library/load", status_code=201)
def load_library(b: LibraryIn, project: str = Depends(get_project), c: Container = Depends(get_container),
                 user: str = Depends(get_actor)):
    return c.hara.load_library(project, user, b.library)


@router.get("/library")
def library(project: str = Depends(get_project), c: Container = Depends(get_container)):
    return c.hara.library(project) or {"entries": []}


@router.post("/items", status_code=201)
def create_item(b: ItemIn, project: str = Depends(get_project), c: Container = Depends(get_container),
                user: str = Depends(get_actor)):
    return c.hara.create_item(project, b.name, b.description, [f.model_dump() for f in b.functions], b.hld_doc_id, user)


@router.get("/items")
def items(project: str = Depends(get_project), c: Container = Depends(get_container)):
    return c.hara.items(project)


@router.post("/items/{item}/suggest-hazards")
def suggest(item: str, project: str = Depends(get_project), c: Container = Depends(get_container),
            user: str = Depends(get_actor)):
    return c.hara.suggest_hazards(project, item, user)


@router.post("/items/{item}/hazards", status_code=201)
def add_hazard(item: str, b: HazardIn, project: str = Depends(get_project), c: Container = Depends(get_container),
               user: str = Depends(get_actor)):
    return c.hara.add_hazard(project, item, b.function_id, b.guideword, b.description, user)


@router.get("/hazards")
def hazards(item: str | None = None, status: str | None = None, project: str = Depends(get_project),
            c: Container = Depends(get_container)):
    return c.hara.hazards(project, item, status)


@router.post("/hazards/{ref}/decision")
def hazard_decision(ref: str, b: DecisionIn, project: str = Depends(get_project), c: Container = Depends(get_container),
                    user: str = Depends(get_actor)):
    return c.hara.decide_hazard(project, ref, b.decision, b.comment, user)


@router.post("/hazards/{ref}/propose-events")
def propose_events(ref: str, project: str = Depends(get_project), c: Container = Depends(get_container),
                   user: str = Depends(get_actor)):
    return c.hara.propose_events(project, ref, user)


@router.post("/hazards/{ref}/events", status_code=201)
def add_event(ref: str, b: EventIn, project: str = Depends(get_project), c: Container = Depends(get_container),
              user: str = Depends(get_actor)):
    return c.hara.add_event(project, ref, b.situation, user)


@router.get("/events")
def events(item: str | None = None, hazard: str | None = None, status: str | None = None,
           project: str = Depends(get_project), c: Container = Depends(get_container)):
    return c.hara.events(project, item, hazard, status)


@router.put("/events/{ref}/assessment")
def assess(ref: str, b: AssessIn, project: str = Depends(get_project), c: Container = Depends(get_container),
           user: str = Depends(get_actor)):
    return c.hara.assess_event(project, ref, b.S, b.E, b.C,
                               {"S": b.rationale_S, "E": b.rationale_E, "C": b.rationale_C}, user)


@router.post("/events/{ref}/decision")
def event_decision(ref: str, b: DecisionIn, project: str = Depends(get_project), c: Container = Depends(get_container),
                   user: str = Depends(get_actor)):
    return c.hara.decide_event(project, ref, b.decision, b.comment, user)


@router.post("/items/{item}/derive-goals")
def derive_goals(item: str, project: str = Depends(get_project), c: Container = Depends(get_container),
                 user: str = Depends(get_actor)):
    return c.hara.derive_goals(project, item, user)


@router.get("/goals")
def goals(item: str | None = None, project: str = Depends(get_project), c: Container = Depends(get_container)):
    return c.hara.goals(project, item)


@router.put("/goals/{ref}")
def update_goal(ref: str, b: GoalIn, project: str = Depends(get_project), c: Container = Depends(get_container),
                user: str = Depends(get_actor)):
    return c.hara.update_goal(project, ref, user, b.text, b.safe_state, b.ftti_ms)


@router.post("/goals/{ref}/decision")
def goal_decision(ref: str, b: DecisionIn, project: str = Depends(get_project), c: Container = Depends(get_container),
                  user: str = Depends(get_actor)):
    return c.hara.decide_goal(project, ref, b.decision, b.comment, user)


@router.post("/goals/{ref}/draft-requirements")
def draft_reqs(ref: str, project: str = Depends(get_project), c: Container = Depends(get_container),
               user: str = Depends(get_actor)):
    return c.hara.draft_requirements(project, ref, user)


@router.get("/requirements")
def requirements(item: str | None = None, project: str = Depends(get_project), c: Container = Depends(get_container)):
    return c.hara.requirements(project, item)


@router.put("/requirements/{ref}")
def update_req(ref: str, b: ReqIn, project: str = Depends(get_project), c: Container = Depends(get_container),
               user: str = Depends(get_actor)):
    return c.hara.update_requirement(project, ref, user, b.text, b.allocated_to, b.verification)


@router.post("/requirements/{ref}/decision")
def req_decision(ref: str, b: DecisionIn, project: str = Depends(get_project), c: Container = Depends(get_container),
                 user: str = Depends(get_actor)):
    return c.hara.decide_requirement(project, ref, b.decision, b.comment, user)


@router.get("/items/{item}/completeness")
def completeness(item: str, project: str = Depends(get_project), c: Container = Depends(get_container)):
    return c.hara.completeness(project, item)


@router.get("/items/{item}/trace")
def trace(item: str, project: str = Depends(get_project), c: Container = Depends(get_container)):
    return c.hara.trace_matrix(project, item)


@router.get("/items/{item}/metrics")
def metrics(item: str, project: str = Depends(get_project), c: Container = Depends(get_container)):
    return c.hara.metrics(project, item)


@router.get("/items/{item}/report")
def report(item: str, format: str = Query("md", pattern="^(md|json|csv)$"), project: str = Depends(get_project),
           c: Container = Depends(get_container), user: str = Depends(get_actor)):
    body, mime = c.hara.report(project, item, format, user)
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{item}_hara.{format}"'})


@router.get("/records/{ref}/history")
def history(ref: str, project: str = Depends(get_project), c: Container = Depends(get_container)):
    return c.hara.history(project, ref)


@router.get("/graph", response_class=PlainTextResponse)
def graph(project: str = Depends(get_project), c: Container = Depends(get_container)):
    return c.hara.trace.dot(project)
