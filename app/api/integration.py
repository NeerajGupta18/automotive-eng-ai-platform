from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import get_actor, get_container, get_project
from app.container import Container

router = APIRouter(prefix="/projects/{project}/integrations", tags=["Enterprise integration"])
Ctx, Prj, Usr = Depends(get_container), Depends(get_project), Depends(get_actor)
MAX_REQIF = 20 * 1024 * 1024


class WebhookIn(BaseModel):
    url: str = Field(..., max_length=500)
    events: list[str] = []
    description: str = Field("", max_length=200)


class TicketIn(BaseModel):
    summary: str = Field(..., min_length=5, max_length=250)
    description: str = Field("", max_length=30000)
    source_ref: str = Field("", max_length=40)
    priority: str = "Medium"


class DecisionIn(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected)$")
    comment: str = ""


class LinkIn(BaseModel):
    xr_ref: str
    target_ref: str


@router.get("/config")
def config(project: str = Prj, c: Container = Ctx):
    """Live or dry-run mode, webhook policy and Jira status (credentials are never returned)."""
    return c.integration.config()


@router.get("/webhooks")
def webhooks(project: str = Prj, c: Container = Ctx):
    return c.integration.webhooks(project)


@router.post("/webhooks", status_code=201)
def add_webhook(b: WebhookIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Project administrators only. The signing secret is returned once."""
    return c.integration.add_webhook(project, b.url, b.events, b.description, user)


@router.delete("/webhooks/{wid}")
def remove_webhook(wid: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.integration.remove_webhook(project, wid, user)


@router.post("/webhooks/{wid}/ping")
def ping(wid: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.integration.ping(project, wid, user)


@router.get("/deliveries")
def deliveries(limit: int = 100, project: str = Prj, c: Container = Ctx):
    return c.integration.deliveries(project, limit)


@router.post("/dispatch")
def dispatch(project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Queue new events for this project's webhooks and attempt due deliveries now."""
    return c.integration.dispatch(project, user)


@router.get("/tickets")
def tickets(project: str = Prj, c: Container = Ctx):
    return c.integration.tickets(project)


@router.post("/tickets", status_code=201)
def create_ticket(b: TicketIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.integration.create_ticket(project, b.summary, b.description, b.source_ref, user, b.priority)


@router.post("/tickets/{ref}/send")
def send_ticket(ref: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.integration.send_ticket(project, ref, user)


@router.get("/reqif/export")
def reqif_export(project: str = Prj, c: Container = Ctx, user: str = Usr):
    body, name = c.integration.export_reqif(project, user)
    return Response(body, media_type="application/xml", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/reqif/import", status_code=201)
async def reqif_import(file: UploadFile = File(...), project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Import a partner's ReqIF (.reqif or .reqifz): new, modified and removed requirements are detected."""
    content = await file.read(MAX_REQIF + 1)
    if len(content) > MAX_REQIF:
        from app.core.errors import Invalid
        raise Invalid("ReqIF file larger than 20 MB")
    return c.integration.import_reqif(project, file.filename or "import.reqif", content, user)


@router.get("/reqif/imports")
def reqif_imports(project: str = Prj, c: Container = Ctx):
    return c.integration.imports(project)


@router.get("/reqif/requirements")
def external(status: str | None = None, project: str = Prj, c: Container = Ctx):
    return c.integration.external(project, status)


@router.post("/reqif/requirements/{ref}/decision")
def decide_external(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Accept (or reject) an imported or changed customer requirement. Reviewer role."""
    return c.integration.decide_external(project, ref, b.decision, b.comment, user)


@router.post("/reqif/links")
def link(b: LinkIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Customer requirement -> platform item that satisfies it (changes to the customer requirement then flag the item)."""
    return c.integration.link_external(project, b.xr_ref, b.target_ref, user)
