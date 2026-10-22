from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.api.deps import get_actor, get_container, get_project
from app.container import Container
from app.core.errors import Invalid
from app.modules.compliance.r155 import CATEGORIES

router = APIRouter(prefix="/projects/{project}/compliance", tags=["Compliance evidence"])
Ctx, Prj, Usr = Depends(get_container), Depends(get_project), Depends(get_actor)


@router.get("/r155")
def r155(project: str = Prj, c: Container = Ctx):
    """Coverage of the UN R155 Annex 5 threat groups by TARA, vulnerability monitoring and OTA evidence."""
    return c.compliance.r155(project)


@router.get("/r155/categories")
def categories(project: str = Prj):
    return CATEGORIES


@router.get("/evidence")
def evidence(format: str = Query("md"), project: str = Prj, c: Container = Ctx, user: str = Usr):
    if format not in ("md", "json"):
        raise Invalid("format must be md or json")
    body, mime = c.compliance.render(c.compliance.evidence(project, user), format)
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="evidence_pack_{project}.{format}"'})
