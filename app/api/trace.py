from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from app.api.deps import get_container, get_project
from app.container import Container
from app.core import tracequery

router = APIRouter(prefix="/projects/{project}/trace", tags=["Traceability"])


@router.get("/summary")
def summary(project: str = Depends(get_project), c: Container = Depends(get_container)):
    return tracequery.summary(c.db, project)


@router.get("/suspect")
def suspect(project: str = Depends(get_project), c: Container = Depends(get_container)):
    return tracequery.suspects(c.db, project)


@router.get("/nodes/{ref:path}")
def nodes(ref: str, project: str = Depends(get_project), c: Container = Depends(get_container)):
    return tracequery.neighborhood(c.db, project, ref)


@router.get("/graph", response_class=PlainTextResponse)
def graph(around: str | None = Query(None, max_length=80), project: str = Depends(get_project), c: Container = Depends(get_container)):
    return tracequery.dot(c.db, project, around)
