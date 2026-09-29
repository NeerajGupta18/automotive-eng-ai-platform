from __future__ import annotations

import json
import os
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.api.deps import get_actor, get_container
from app.config import validate
from app.container import Container
from app.core.db import SCHEMA_VERSION

router = APIRouter(tags=["Admin"])
Ctx, Usr = Depends(get_container), Depends(get_actor)


class UserIn(BaseModel):
    name: str
    role: str = "user"
    ttl_days: int | None = None


class MemberIn(BaseModel):
    role: str


@router.get("/me")
def me(request: Request, c: Container = Ctx, user: str = Usr):
    auth = getattr(request.state, "auth", None)
    if c.settings.auth_mode == "enforced" and auth:
        return {"name": auth["name"], "role": auth["role"], "auth": "enforced", "memberships": c.auth.memberships(auth["name"])}
    return {"name": user, "role": "admin", "auth": "off", "memberships": []}


@router.post("/admin/users", status_code=201)
def create_user(b: UserIn, c: Container = Ctx, user: str = Usr):
    u, token = c.auth.create_user(b.name, b.role, user, b.ttl_days)
    return {"user": u, "token": token, "note": "Store this token now: it cannot be shown again."}


@router.get("/admin/users")
def users(c: Container = Ctx):
    return c.auth.users()


@router.post("/admin/users/{name}/rotate")
def rotate(name: str, c: Container = Ctx, user: str = Usr):
    u, token = c.auth.rotate(name, user)
    return {"user": u, "token": token, "note": "The previous token no longer works."}


@router.post("/admin/users/{name}/disable")
def disable(name: str, c: Container = Ctx, user: str = Usr):
    return c.auth.set_disabled(name, True, user)


@router.post("/admin/users/{name}/enable")
def enable(name: str, c: Container = Ctx, user: str = Usr):
    return c.auth.set_disabled(name, False, user)


@router.get("/projects/{project}/members")
def members(project: str, c: Container = Ctx):
    if not c.db.project_exists(project):
        raise HTTPException(404, f"Project '{project}' not found")
    return c.auth.members(project)


@router.put("/projects/{project}/members/{name}")
def set_member(project: str, name: str, b: MemberIn, c: Container = Ctx, user: str = Usr):
    return c.auth.set_member(project, name, b.role, user)


@router.delete("/projects/{project}/members/{name}")
def remove_member(project: str, name: str, c: Container = Ctx, user: str = Usr):
    c.auth.remove_member(project, name, user)
    return {"removed": name}


@router.get("/admin/audit")
def audit(project: str | None = None, actor: str | None = None, action: str | None = None, limit: int = Query(200, le=5000), c: Container = Ctx):
    return c.db.audit_log(project, limit, actor, action)


@router.get("/admin/audit/verify")
def audit_verify(c: Container = Ctx):
    return c.db.verify_audit()


@router.get("/admin/audit/export", response_class=PlainTextResponse)
def audit_export(c: Container = Ctx, user: str = Usr):
    rows = c.db._q("SELECT * FROM audit ORDER BY id")
    c.db.audit(user, "audit.export", None, None, {"rows": len(rows)})
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"


@router.get("/system/info")
def system_info(request: Request, c: Container = Ctx):
    s = c.settings
    _, warnings = validate(s)
    return {"schema_version": SCHEMA_VERSION, "migrations_applied": c.db.migrations_applied, "auth_mode": s.auth_mode,
            "llm": c.llm.name, "embeddings": s.embed_backend, "vector_store": s.vector_backend, "warnings": warnings,
            "limits": {"rate_limit_per_min": s.rate_limit_per_min, "auth_fail_limit_per_min": s.auth_fail_limit_per_min,
                       "max_request_mb": s.max_request_mb, "max_upload_mb": s.max_upload_mb, "token_ttl_days": s.token_ttl_days},
            "uptime_seconds": round(time.time() - c.metrics.started)}


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(c: Container = Ctx):
    n = lambda sql: c.db._q(sql)[0]["n"]
    return PlainTextResponse(c.metrics.render({"aeai_projects": n("SELECT count(*) AS n FROM projects"), "aeai_users": n("SELECT count(*) AS n FROM users"),
                                               "aeai_records": n("SELECT count(*) AS n FROM records"), "aeai_audit_rows": n("SELECT count(*) AS n FROM audit")}),
                             media_type="text/plain; version=0.0.4")


@router.get("/ready")
def ready(c: Container = Ctx):
    checks = {}
    try:
        checks["database"] = c.db._q("SELECT 1 AS x")[0]["x"] == 1
    except Exception:                                    # noqa: BLE001
        checks["database"] = False
    try:
        probe = c.settings.data_dir / f".ready-{uuid.uuid4().hex[:6]}"
        probe.write_text("ok")
        probe.unlink()
        checks["storage_writable"] = True
    except OSError:
        checks["storage_writable"] = False
    try:
        checks["embedder"] = len(c.embedder.embed(["ping"])) == 1
    except Exception:                                    # noqa: BLE001
        checks["embedder"] = False
    ok = all(checks.values())
    return JSONResponse({"ready": ok, "checks": checks}, 200 if ok else 503)
