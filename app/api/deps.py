from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from app.container import Container


def get_container(request: Request) -> Container:
    if request.app.state.container is None:      # built lazily on first request
        request.app.state.container = Container()
    return request.app.state.container


def get_actor(request: Request, x_user: str = Header(default="anonymous")) -> str:
    """Authenticated identity (token-bound) when AUTH_MODE=enforced; the X-User header only in development mode."""
    from app.core.observability import clean_id
    return getattr(request.state, "auth_user", None) or clean_id(x_user) or "anonymous"


def get_project(project: str, cont: Container = Depends(get_container)) -> str:
    if not cont.db.project_exists(project):
        raise HTTPException(404, f"Project '{project}' not found")
    return project
