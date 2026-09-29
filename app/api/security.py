"""Request guard: identity, authorization (RBAC), rate limits, size limits, security headers, logging, metrics.

Authorization is DENY-BY-DEFAULT: every request is classified into a permission from its method and path. Anything
that is not recognised as a read needs at least 'write', so a newly added endpoint can never be more open than
intended (tests enumerate every route to keep this true)."""
from __future__ import annotations

import logging
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.auth import RANK, ROLE_RANK, SlidingWindow
from app.core.observability import clean_id

log = logging.getLogger("aeai.access")
PUBLIC = {"/health", "/ready", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
APPROVE_RE = re.compile(r"(/decision|/accept-risk|/library/load)$|^/documents/[^/]+/status$")
READ_POST_RE = re.compile(r"^/hld/query$|^/uds/specs/[^/]+/(validate|ask|run|detection-matrix)$")
GLOBAL_ADMIN = ("/admin", "/metrics", "/system")
AUTHENTICATED_REFERENCE = {"/hara/asil-table", "/tara/rules", "/uds/reference", "/codereview/rules", "/me"}


def classify(method: str, path: str) -> tuple[str, str | None]:
    """-> (permission, project). permission: public | authenticated | global_admin | create_project | read | write | approve | admin"""
    if path in PUBLIC:
        return "public", None
    if path.startswith(GLOBAL_ADMIN):
        return "global_admin", None
    if path == "/projects":
        return ("authenticated" if method in ("GET", "HEAD") else "create_project"), None
    m = re.match(r"^/projects/([^/]+)(/.*)?$", path)
    if not m:
        return "authenticated", None
    project, rest = m.group(1), m.group(2) or ""
    if method in ("GET", "HEAD", "OPTIONS"):
        if rest.startswith("/members") or rest == "/audit":
            return ("admin" if rest.startswith("/members") else "approve"), project
        return "read", project
    if method == "DELETE" or rest.startswith("/members"):
        return "admin", project
    if APPROVE_RE.search(rest):
        return "approve", project
    if READ_POST_RE.match(rest):
        return "read", project
    return "write", project


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else None


def _json(status: int, detail: str, rid: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail, "request_id": rid}, status, headers=headers)


class BodyLimit:
    """Pure ASGI: rejects oversized bodies by Content-Length and by counting streamed bytes (chunked uploads)."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max <= 0:
            return await self.app(scope, receive, send)
        cl = dict(scope["headers"]).get(b"content-length")
        state = {"over": False, "sent": False, "n": 0}
        body = b'{"detail":"Request body too large"}'

        async def reject():
            if not state["sent"]:
                state["sent"] = True
                await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})

        if cl and cl.isdigit() and int(cl) > self.max:
            return await reject()

        async def counting_receive():
            msg = await receive()
            if msg["type"] == "http.request":
                state["n"] += len(msg.get("body", b""))
                if state["n"] > self.max:
                    state["over"] = True
                    raise RequestTooLarge()
            return msg

        async def guarded_send(msg):
            if state["over"]:                       # whatever the framework made of the aborted read (400/500), the answer is 413
                return await reject()
            state["sent"] = state["sent"] or msg["type"] == "http.response.start"
            await send(msg)
        try:
            await self.app(scope, counting_receive, guarded_send)
        except RequestTooLarge:
            await reject()


class RequestTooLarge(Exception):
    pass


def install(app: FastAPI, settings) -> None:
    limiter = SlidingWindow()
    app.state.limiter = limiter

    @app.middleware("http")
    async def guard(request: Request, call_next):
        from app.api.deps import get_container
        t0 = time.perf_counter()
        rid = clean_id(request.headers.get("x-request-id"), 40) or uuid.uuid4().hex[:16]
        request.state.request_id, request.state.auth_user, request.state.auth = rid, None, None
        cont = get_container(request)
        cfg, met = cont.settings, cont.metrics
        enforced = cfg.auth_mode == "enforced"
        method, path = request.method, request.url.path
        ip = request.client.host if request.client else "unknown"
        perm, project = classify(method, path)
        response = None
        try:
            user = None
            if enforced:
                token = _bearer(request)
                if perm == "public":
                    user = cont.auth.authenticate(token) if token else None      # optional: unlocks the detailed /health
                else:
                    ok, retry = limiter.hit(f"authfail:{ip}", cfg.auth_fail_limit_per_min, count=False)
                    if not ok:
                        met.inc("aeai_rate_limited_total", {"kind": "auth_failures"})
                        response = _json(429, "Too many failed authentication attempts", rid, {"Retry-After": str(retry)})
                    else:
                        user = cont.auth.authenticate(token) if token else None
                        if user is None:
                            limiter.hit(f"authfail:{ip}", cfg.auth_fail_limit_per_min)
                            met.inc("aeai_auth_events_total", {"outcome": "unauthenticated"})
                            cont.db.audit("anonymous", "auth.failed", None, None, {"path": path[:120], "ip": ip})
                            response = _json(401, "Authentication required: send 'Authorization: Bearer <token>'", rid, {"WWW-Authenticate": "Bearer"})
                if user:
                    request.state.auth_user, request.state.auth = user["name"], user
            if response is None and enforced and perm != "public":
                response = _authorize(request, cont, user, perm, project, met, rid)
            if response is None:
                who = request.state.auth_user or (ip if not enforced else "anonymous")
                ok, retry = limiter.hit(f"user:{who}", cfg.rate_limit_per_min)
                if not ok:
                    met.inc("aeai_rate_limited_total", {"kind": "requests"})
                    response = _json(429, "Rate limit exceeded", rid, {"Retry-After": str(retry)})
            if response is None:
                response = await call_next(request)
        except RequestTooLarge:
            response = _json(413, "Request body too large", rid)
        except Exception:                                    # noqa: BLE001 - never leak internals to the client
            log.exception("unhandled error", extra={"x_request_id": rid, "x_path": path})
            response = _json(500, "Internal server error", rid)
        route = getattr(request.scope.get("route"), "path", None) or ("unmatched" if response.status_code == 404 else path if perm == "public" else "guarded")
        ms = (time.perf_counter() - t0) * 1000
        met.inc("aeai_http_requests_total", {"method": method, "route": route, "status": str(response.status_code)})
        met.observe("aeai_http_request_duration_seconds", ms / 1000, {"route": route})
        h = response.headers
        h["X-Request-ID"], h["X-Content-Type-Options"], h["Referrer-Policy"] = rid, "nosniff", "no-referrer"
        h["X-Frame-Options"], h["Cache-Control"] = "DENY", "no-store"
        if not path.startswith(("/docs", "/redoc")):
            h["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        log.info(f"{method} {route} {response.status_code} {ms:.1f}ms user={request.state.auth_user or '-'} rid={rid}",
                 extra={"x_request_id": rid, "x_user": request.state.auth_user, "x_method": method, "x_route": route,
                        "x_status": response.status_code, "x_ms": round(ms, 1), "x_ip": ip})
        return response


def _authorize(request, cont, user, perm, project, met, rid):
    def deny(code, msg, why):
        met.inc("aeai_auth_events_total", {"outcome": why})
        cont.db.audit(user["name"], "auth.denied", project, None, {"method": request.method, "path": request.url.path[:120], "needed": perm, "why": why})
        return _json(code, msg, rid)
    if perm == "authenticated":
        return None
    if perm == "create_project":
        return None
    if perm == "global_admin":
        return None if user["role"] == "admin" else deny(403, "Platform administrator role required", "forbidden")
    role = cont.auth.project_role(user, project)
    if role is None:
        return deny(404, f"Project '{project}' not found", "not_member")           # existence is not revealed to non-members
    if ROLE_RANK[role] < RANK[perm]:
        return deny(403, f"This action needs the '{ {'read': 'viewer', 'write': 'engineer', 'approve': 'reviewer', 'admin': 'admin'}[perm] }' role in project '{project}' (you are '{role}')", "forbidden")
    return None
