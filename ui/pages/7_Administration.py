"""Administration: users and tokens, project membership, tamper-evident audit log, system status."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import common
from common import api, do, raw_get

common.setup("Administration")
project = common.sidebar("Module: **Administration**")
st.title("Administration")
me = st.session_state.get("me", {})
if me.get("role") != "admin":
    st.warning("Only platform administrators can use this page.")
    st.stop()
if me.get("auth") == "off":
    st.info("Development mode (AUTH_MODE=off): users and roles are not enforced. Set AUTH_MODE=enforced for real access control.")
t_users, t_mem, t_audit, t_sys = st.tabs(["Users & tokens", "Project members", "Audit log", "System"])

with t_users:
    users = api("GET", "/admin/users") or []
    if users:
        st.dataframe(pd.DataFrame(users), hide_index=True, width="stretch")
    if st.session_state.get("new_token"):
        st.success("Token created. Copy it now: it cannot be shown again.")
        st.code(st.session_state.pop("new_token"))
    with st.form("new_user"):
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("User name")
        role = c2.selectbox("Platform role", ["user", "admin"])
        ttl = c3.number_input("Token lifetime (days, 0 = never expires)", 0, 3650, 90)
        if st.form_submit_button("Create user"):
            r = api("POST", "/admin/users", json={"name": name, "role": role, "ttl_days": int(ttl)})
            if r:
                st.session_state["new_token"] = r["token"]
                st.rerun()
    if users:
        sel = st.selectbox("Manage user", [u["name"] for u in users])
        b1, b2, b3, _ = st.columns([1, 1, 1, 3])
        if b1.button("🔄 Rotate token"):
            r = api("POST", f"/admin/users/{sel}/rotate")
            if r:
                st.session_state["new_token"] = r["token"]
                st.rerun()
        if b2.button("⛔ Disable"):
            do("POST", f"/admin/users/{sel}/disable")
        if b3.button("✅ Enable"):
            do("POST", f"/admin/users/{sel}/enable")

with t_mem:
    projects = api("GET", "/projects") or []
    if projects:
        pj = st.selectbox("Project", [p["name"] for p in projects], key="mem_project")
        mem = api("GET", f"/projects/{pj}/members") or []
        st.dataframe(pd.DataFrame(mem), hide_index=True, width="stretch") if mem else st.caption("No members yet.")
        st.caption("viewer: read · engineer: create and edit · reviewer: approve decisions · admin: manage members, delete.")
        with st.form("member"):
            c1, c2 = st.columns(2)
            u = c1.selectbox("User", [x["name"] for x in (api("GET", "/admin/users") or [])])
            r = c2.selectbox("Project role", ["viewer", "engineer", "reviewer", "admin"])
            if st.form_submit_button("Set role"):
                do("PUT", f"/projects/{pj}/members/{u}", json={"role": r})
    else:
        st.info("No projects yet.")

with t_audit:
    c1, c2, c3 = st.columns(3)
    f_actor, f_action, f_proj = c1.text_input("Actor"), c2.text_input("Action prefix (e.g. auth., hara.)"), c3.text_input("Project")
    rows = api("GET", "/admin/audit", params={k: v for k, v in {"actor": f_actor, "action": f_action, "project": f_proj}.items() if v} | {"limit": 500}) or []
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=320) if rows else st.caption("No entries.")
    if st.button("🔒 Verify audit chain"):
        v = api("GET", "/admin/audit/verify")
        if v:
            (st.success if v["ok"] else st.error)("Chain intact" if v["ok"] else f"CHAIN BROKEN at entry {v['first_bad_id']} (missing: {v['missing_ids']})")
            st.json(v)
            st.caption("Record the head hash somewhere outside this system (ticket, WORM storage): the chain proves consistency, an external anchor proves nothing was truncated.")
    exp = raw_get("/admin/audit/export")
    if exp is not None:
        st.download_button("⬇ Export audit log (JSON lines)", exp.text, file_name="audit.jsonl", mime="application/x-ndjson")

with t_sys:
    info = api("GET", "/system/info")
    if info:
        for w in info["warnings"]:
            st.warning(w)
        st.json(info)
    rd = raw_get("/ready")
    if rd is not None:
        st.write("Readiness:", rd.json())
