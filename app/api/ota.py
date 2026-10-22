from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.deps import get_actor, get_container, get_project
from app.container import Container

router = APIRouter(prefix="/projects/{project}/ota", tags=["OTA software updates"])
Ctx, Prj, Usr = Depends(get_container), Depends(get_project), Depends(get_actor)


class KeyIn(BaseModel):
    key_id: str
    owner: str
    public_key_pem: str
    valid_from: str = ""
    valid_until: str = ""


class DecisionIn(BaseModel):
    decision: str
    comment: str = ""


class RevokeIn(BaseModel):
    reason: str


@router.post("/keys", status_code=201)
def add_key(b: KeyIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Propose a trusted ECDSA P-256 signing key. It becomes active only after a second person approves it."""
    return c.ota.add_key(project, b.key_id, b.owner, b.public_key_pem, user, b.valid_from, b.valid_until)


@router.get("/keys")
def keys(project: str = Prj, c: Container = Ctx):
    return c.ota.keys(project)


@router.post("/keys/{ref}/decision")
def decide_key(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.ota.decide_key(project, ref, b.decision, b.comment, user)


@router.post("/keys/{ref}/revoke")
def revoke_key(ref: str, b: RevokeIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Immediate revocation (e.g. key compromise); unreleased packages are re-verified."""
    return c.ota.revoke_key(project, ref, b.reason, user)


@router.post("/packages", status_code=201)
async def add_package(file: UploadFile = File(...), project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.ota.add_package(project, file.filename or "package.zip", await file.read(), user)


@router.get("/packages")
def packages(project: str = Prj, c: Container = Ctx):
    return c.ota.packages(project)


@router.get("/packages/{ref}")
def package(ref: str, project: str = Prj, c: Container = Ctx):
    return c.ota.package(project, ref)


@router.post("/packages/{ref}/verify")
def verify(ref: str, project: str = Prj, c: Container = Ctx, user: str = Usr):
    return c.ota.verify_package(project, ref, user)


@router.post("/packages/{ref}/decision")
def decide_package(ref: str, b: DecisionIn, project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Release ('approved') or reject an update. Four-eyes; blocked while any check fails."""
    return c.ota.decide_package(project, ref, b.decision, b.comment, user)


@router.get("/packages/{ref}/report")
def report(ref: str, format: str = Query("md"), project: str = Prj, c: Container = Ctx):
    body, mime = c.ota.report(project, ref, format)
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{ref}_readiness.{format}"'})


@router.get("/self-test")
def self_test(project: str = Prj, c: Container = Ctx, user: str = Usr):
    """Run 17 attack scenarios against the verifier with a throw-away key (changes no data)."""
    return c.ota.self_test(project, user)
