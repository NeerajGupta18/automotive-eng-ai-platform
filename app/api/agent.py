from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import get_actor, get_container, get_project
from app.container import Container
from app.modules.agent import playbooks, tools

router = APIRouter(prefix="/projects/{project}", tags=["Release readiness and engineering agent"])
Ctx, Prj, Usr = Depends(get_container), Depends(get_project), Depends(get_actor)


class RunIn(BaseModel):
    goal: str = Field(..., min_length=3, max_length=1000)
    playbook: str = "auto"
    params: dict = {}
    planner: str = "rules"


class ConfirmIn(BaseModel):
    decision: str = Field(..., pattern="^(run|skip)$")
    comment: str = Field("", max_length=300)


@router.get("/readiness")
def readiness(project: str = Prj, c: Container = Ctx):
    """GO / NO-GO across every discipline, with owners and actions for each open item."""
    return c.readiness.evaluate(project)


@router.get("/readiness/report")
def readiness_report(format: str = Query("md", pattern="^(md|json)$"), project: str = Prj, c: Container = Ctx):
    body, mime = c.readiness.report(project, format)
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{project}_readiness.{format}"'})


@router.get("/agent/tools")
def agent_tools(project: str = Prj, c: Container = Ctx):
    """What the agent can do. There are no approval, release, delete or user-administration tools."""
    return {"tools": tools.describe(), "playbooks": playbooks.PLAYBOOKS, "llm_planner_available": c.agent.llm_available(),
            "max_steps": c.settings.agent_max_steps}


@router.post("/agent/runs", status_code=201)
def start_run(b: RunIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Start a run. Read steps execute immediately; the run stops at the first write step until a person confirms it."""
    return c.agent.start(project, b.goal, user, b.playbook, b.params, b.planner)


@router.get("/agent/runs")
def runs(project: str = Prj, c: Container = Ctx):
    return c.agent.runs(project)


@router.get("/agent/runs/{ref}")
def run(ref: str, project: str = Prj, c: Container = Ctx):
    return c.agent.run(project, ref)


@router.post("/agent/runs/{ref}/steps/{n}/confirm")
def confirm(ref: str, n: int, b: ConfirmIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Human checkpoint: 'run' executes the write step under your name, 'skip' leaves it out; then the run continues."""
    return c.agent.confirm(project, ref, n, b.decision, user, b.comment)


@router.post("/agent/runs/{ref}/cancel")
def cancel(ref: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.agent.cancel(project, ref, user)


@router.get("/agent/runs/{ref}/report")
def run_report(ref: str, project: str = Prj, c: Container = Ctx):
    return Response(c.agent.report(project, ref), media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{ref}_agent_report.md"'})
