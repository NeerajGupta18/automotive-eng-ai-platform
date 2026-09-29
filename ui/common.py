"""Helpers shared by all Streamlit pages: API access and the project sidebar."""
from __future__ import annotations

import os

import requests
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")


def setup(title: str) -> None:
    st.set_page_config(page_title=f"{title} · Automotive Engineering AI", page_icon="🚗", layout="wide")


def _headers() -> dict:
    """Bearer token when signed in (AUTH_MODE=enforced); the X-User header is only used in development mode."""
    token = st.session_state.get("token") or os.getenv("API_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {"X-User": st.session_state.get("user", "engineer")}


def api(method: str, path: str, *, expect_json: bool = True, quiet: bool = False, **kw):
    try:
        r = requests.request(method, f"{API_URL}{path}", headers=_headers(), timeout=300, **kw)
    except requests.RequestException as e:
        if not quiet:
            st.error(f"Cannot reach the API at {API_URL}: {e}")
        return None
    if not r.ok:
        if not quiet:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            st.error(f"{r.status_code}: {detail}")
        return None
    return r.json() if expect_json else r.text


def raw_get(path: str, **params):
    """GET returning the response (for downloads); None on failure."""
    try:
        r = requests.get(f"{API_URL}{path}", params=params, headers=_headers(), timeout=60)
    except requests.RequestException:
        return None
    return r if r.ok else None


def do(method: str, path: str, **kw) -> None:
    """Call the API and refresh the page on success."""
    if api(method, path, **kw) is not None:
        st.rerun()


def sidebar(module_note: str) -> str | None:
    with st.sidebar:
        st.title("🚗 Automotive Engineering AI")
        health = api("GET", "/health", quiet=True)
        if health and health.get("auth") == "enforced":
            st.session_state["token"] = st.text_input("API token", type="password", value=st.session_state.get("token", os.getenv("API_TOKEN", "")))
            me = api("GET", "/me", quiet=True) if st.session_state["token"] else None
            if not me:
                st.warning("Sign in with your API token (ask a platform administrator).")
                st.stop()
            st.session_state["user"], st.session_state["me"] = me["name"], me
            st.success(f"Signed in as **{me['name']}** ({me['role']})")
            health = api("GET", "/health", quiet=True) or health
        else:
            st.session_state["user"] = st.text_input("Your name (audit log, four-eyes approval)", value=st.session_state.get("user", "engineer"))
            st.session_state["me"] = {"name": st.session_state["user"], "role": "admin", "auth": "off"}
            st.caption("⚠ Development mode: identities are not authenticated.")
        if health and "llm" in health:
            st.caption(f"LLM: **{health['llm']}** · Embeddings: **{health['embeddings']}** · Vector DB: **{health['vector_store']}**")
        projects = api("GET", "/projects") or []
        names = [p["name"] for p in projects]
        project = None
        if names:
            idx = names.index(st.session_state["project"]) if st.session_state.get("project") in names else 0
            project = st.selectbox("Project workspace", names, index=idx)
            st.session_state["project"] = project
        with st.expander("➕ New project"):
            new = st.text_input("Project name", key="new_project")
            if st.button("Create project") and new:
                do("POST", "/projects", json={"name": new})
        st.divider()
        st.caption(module_note)
        st.info("AI output is advisory. Approvals need a second person (four-eyes) and are logged.")
    return project


def status_badge(status: str) -> str:
    return {"approved": "🟢", "accepted": "🟢", "assessed": "🟡", "proposed": "⚪", "suspect": "🟠",
            "rejected": "🔴"}.get(status, "⚪") + f" {status}"
