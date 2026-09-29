"""Streamlit engineering UI. Talks to the FastAPI service over HTTP (clean separation:
the UI can be replaced by an enterprise portal without touching the backend)."""
from __future__ import annotations

import os

import pandas as pd
import requests
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")

st.set_page_config(page_title="Automotive Engineering AI", page_icon="🚗", layout="wide")


# ------------------------------------------------------------------ API helper
def api(method: str, path: str, *, expect_json: bool = True, **kw):
    headers = {"X-User": st.session_state.get("user", "anonymous")}
    try:
        r = requests.request(method, f"{API_URL}{path}", headers=headers, timeout=300, **kw)
    except requests.RequestException as e:
        st.error(f"Cannot reach the API at {API_URL}: {e}")
        return None
    if not r.ok:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        st.error(f"{r.status_code}: {detail}")
        return None
    return r.json() if expect_json else r.text


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("🚗 Automotive Engineering AI")
    st.session_state["user"] = st.text_input("Your name (audit log)", value=st.session_state.get("user", "engineer"))
    health = api("GET", "/health")
    if health:
        st.caption(f"LLM: **{health['llm']}** · Embeddings: **{health['embeddings']}** · Vector DB: **{health['vector_store']}**")
    projects = api("GET", "/projects") or []
    names = [p["name"] for p in projects]
    project = st.selectbox("Project workspace", names) if names else None
    with st.expander("➕ New project"):
        new_name = st.text_input("Project name", key="new_project")
        if st.button("Create project") and new_name:
            if api("POST", "/projects", json={"name": new_name}):
                st.rerun()
    st.divider()
    st.caption("Module: **AUTOSAR HLD Analysis** (Stage 1). HARA, TARA, UDS and Code Review modules follow.")
    st.info("AI output is advisory. Every answer and finding requires engineer review before use.")

if not project:
    st.header("Welcome")
    st.write("Create a project workspace in the sidebar to begin.")
    st.stop()

docs = api("GET", f"/projects/{project}/documents") or []
doc_label = {d["id"]: f"{d['name']} · v{d['version']} · {d['status']}" for d in docs}

st.header(f"Project: {project}")
tabs = st.tabs(["📄 Documents", "💬 Ask", "🧩 Entities", "⚠️ Issues", "🕸️ Graph & impact", "🔁 Compare",
                "✅ Review queue", "🧾 Audit"])


def pick_doc(label: str, key: str) -> str | None:
    if not docs:
        st.info("Upload a document first.")
        return None
    return st.selectbox(label, list(doc_label), format_func=doc_label.get, key=key)


# ------------------------------------------------------------------ documents
with tabs[0]:
    st.subheader("Upload an HLD document")
    c1, c2, c3 = st.columns([3, 1, 1])
    f = c1.file_uploader("PDF, Markdown or text", type=["pdf", "md", "txt"], key="upload")
    ver = c2.text_input("Version label", "1.0")
    status = c3.selectbox("Status", ["approved", "draft"], help="Only approved documents are searched by default")
    if st.button("Ingest document", disabled=f is None):
        with st.spinner("Parsing, chunking and embedding…"):
            res = api("POST", f"/projects/{project}/documents", files={"file": (f.name, f.getvalue())},
                      data={"version": ver, "status": status})
        if res:
            st.success(f"Ingested {res['name']}: {res['pages']} pages, {res['chunks']} chunks")
            st.rerun()
    st.subheader("Documents in this project")
    if docs:
        st.dataframe(pd.DataFrame(docs)[["id", "name", "version", "status", "pages", "chunks", "uploaded_by", "uploaded_at"]],
                     width="stretch", hide_index=True)
        c1, c2, c3 = st.columns([3, 1, 1])
        sel = c1.selectbox("Manage document", list(doc_label), format_func=doc_label.get, key="manage")
        new_status = c2.selectbox("Set status", ["approved", "draft"], key="set_status")
        if c2.button("Apply status"):
            api("POST", f"/projects/{project}/documents/{sel}/status", params={"status": new_status}) and st.rerun()
        if c3.button("🗑 Delete", type="secondary"):
            api("DELETE", f"/projects/{project}/documents/{sel}") and st.rerun()

# ------------------------------------------------------------------ Q&A
with tabs[1]:
    st.subheader("Ask about the architecture")
    q = st.text_input("Question", placeholder="Which component provides the vehicle speed?", key="question")
    c1, c2 = st.columns(2)
    k = c1.slider("Sections to retrieve", 1, 10, 5)
    draft = c2.checkbox("Include draft documents", False)
    if st.button("Ask", disabled=not q):
        with st.spinner("Retrieving evidence and generating a cited answer…"):
            st.session_state["answer"] = api("POST", f"/projects/{project}/hld/query",
                                             json={"question": q, "k": k, "include_draft": draft})
    a = st.session_state.get("answer")
    if a:
        if a["refused"]:
            st.warning(a["answer"])
        else:
            st.markdown(a["answer"])
            m1, m2, m3 = st.columns(3)
            m1.metric("Top similarity", a["confidence"])
            m2.metric("Confidence", a["confidence_label"])
            m3.metric("Citations valid", "yes" if a["grounded"] else "NO – verify")
            st.caption("Status: **needs engineer review** — verify against the cited sections before use.")
            for c in a["citations"]:
                with st.expander(f"[{c['label']}] {c['doc_name']} v{c['version']} · p.{c['page']} · {c['section']}  (score {c['score']})"):
                    st.code(c["snippet"], language="markdown")

# ------------------------------------------------------------------ entities
with tabs[2]:
    doc = pick_doc("Document", "ent_doc")
    if doc:
        c1, c2 = st.columns([1, 1])
        refresh = c1.button("Re-run extraction")
        use_llm = c2.checkbox("LLM fallback if no taxonomy match", False)
        ex = api("GET", f"/projects/{project}/hld/{doc}/entities", params={"refresh": refresh, "use_llm": use_llm})
        if ex:
            s = ex["summary"]
            cols = st.columns(6)
            for col, key in zip(cols, ["components", "interfaces", "ports", "signals", "dependencies", "issues"]):
                col.metric(key.title(), s[key])
            st.markdown("**Components**")
            for c in ex["components"]:
                with st.expander(f"{c['name']} — {c['type'] or 'type n/a'}  ·  p.{c['source']['page']}"):
                    st.write(c["description"] or "_no description_")
                    if c["runnables"]:
                        st.caption("Runnables: " + ", ".join(c["runnables"]))
                    if c["ports"]:
                        st.dataframe(pd.DataFrame(c["ports"])[["name", "direction", "interface", "page"]],
                                     hide_index=True, width="stretch")
            st.markdown("**Interfaces**")
            st.dataframe(pd.DataFrame([{"name": i["name"], "type": i["type"],
                                        "elements": ", ".join(f"{e['name']}:{e['type']}" for e in i["elements"]),
                                        "page": i["source"]["page"]} for i in ex["interfaces"]]),
                         hide_index=True, width="stretch")
            d1, d2 = st.columns(2)
            csv = api("GET", f"/projects/{project}/hld/{doc}/export", params={"format": "csv"}, expect_json=False)
            js = api("GET", f"/projects/{project}/hld/{doc}/export", params={"format": "json"}, expect_json=False)
            if csv:
                d1.download_button("⬇ Ports (CSV)", csv, file_name=f"{doc}_ports.csv", mime="text/csv")
            if js:
                d2.download_button("⬇ Full extraction (JSON)", js, file_name=f"{doc}_extraction.json", mime="application/json")

# ------------------------------------------------------------------ issues
with tabs[3]:
    doc = pick_doc("Document", "iss_doc")
    if doc:
        issues = api("GET", f"/projects/{project}/hld/{doc}/issues") or []
        sev = st.multiselect("Severity", ["error", "warning", "info"], ["error", "warning"])
        rows = [i for i in issues if i["severity"] in sev]
        st.caption(f"{len(rows)} of {len(issues)} findings · error/warning items are queued in the Review tab")
        if rows:
            st.dataframe(pd.DataFrame([{"severity": i["severity"], "rule": i["rule"], "entity": i["entity"],
                                        "message": i["message"], "section": i["source"]["section"],
                                        "page": i["source"]["page"]} for i in rows]),
                         hide_index=True, width="stretch")
        else:
            st.success("No findings for the selected severities.")

# ------------------------------------------------------------------ graph & impact
with tabs[4]:
    doc = pick_doc("Document", "gr_doc")
    if doc:
        dot = api("GET", f"/projects/{project}/hld/{doc}/graph", expect_json=False)
        if dot:
            st.graphviz_chart(dot, width="stretch")
            st.caption("Arrows show data flow: provider → consumer, labelled with the port interface.")
        ex = api("GET", f"/projects/{project}/hld/{doc}/entities")
        if ex and ex["components"]:
            comp = st.selectbox("Impact analysis for component", [c["name"] for c in ex["components"]])
            imp = api("GET", f"/projects/{project}/hld/{doc}/impact/{comp}")
            if imp:
                c1, c2 = st.columns(2)
                c1.markdown("**Downstream (affected if this changes)**")
                c1.dataframe(pd.DataFrame([{"component": k, "hops": v} for k, v in imp["downstream_impacted"].items()]),
                             hide_index=True, width="stretch")
                c2.markdown("**Upstream (this relies on)**")
                c2.dataframe(pd.DataFrame([{"component": k, "hops": v} for k, v in imp["upstream_dependencies"].items()]),
                             hide_index=True, width="stretch")

# ------------------------------------------------------------------ compare
with tabs[5]:
    if len(docs) < 2:
        st.info("Upload at least two revisions to compare.")
    else:
        c1, c2 = st.columns(2)
        a_id = c1.selectbox("Older revision", list(doc_label), format_func=doc_label.get, key="cmp_a")
        b_id = c2.selectbox("Newer revision", list(doc_label), format_func=doc_label.get, key="cmp_b", index=len(docs) - 1)
        if st.button("Compare revisions"):
            r = api("GET", f"/projects/{project}/hld/compare", params={"a": a_id, "b": b_id})
            if r:
                st.json({k: v for k, v in r.items() if v and k not in ("from", "to")})

# ------------------------------------------------------------------ review queue
with tabs[6]:
    items = api("GET", f"/projects/{project}/reviews", params={"status": "pending"}) or []
    st.subheader(f"{len(items)} pending findings")
    for it in items:
        p = it["payload"]
        with st.expander(f"[{p['severity'].upper()}] {p['rule']} · {p['entity']} — {p['doc_name']}"):
            st.write(p["message"])
            st.caption(f"Section: {p['source']['section']} · page {p['source']['page']}")
            comment = st.text_input("Comment", key=f"cm_{it['id']}")
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("✅ Confirm", key=f"ok_{it['id']}"):
                api("POST", f"/projects/{project}/reviews/{it['id']}/decision", json={"decision": "approved", "comment": comment}) and st.rerun()
            if b2.button("❌ False positive", key=f"no_{it['id']}"):
                api("POST", f"/projects/{project}/reviews/{it['id']}/decision", json={"decision": "rejected", "comment": comment}) and st.rerun()
    done = api("GET", f"/projects/{project}/reviews") or []
    decided = [r for r in done if r["status"] != "pending"]
    if decided:
        st.subheader("Decided")
        st.dataframe(pd.DataFrame([{"id": r["id"], "status": r["status"], "rule": r["payload"]["rule"],
                                    "entity": r["payload"]["entity"], "reviewer": r["reviewer"],
                                    "comment": r["comment"]} for r in decided]),
                     hide_index=True, width="stretch")
        confirmed = sum(r["status"] == "approved" for r in decided)
        st.caption(f"Finding precision so far: **{confirmed}/{len(decided)}** confirmed by reviewers")

# ------------------------------------------------------------------ audit
with tabs[7]:
    log = api("GET", f"/projects/{project}/audit") or []
    if log:
        st.dataframe(pd.DataFrame(log)[["ts", "actor", "action", "entity", "detail"]],
                     hide_index=True, width="stretch")
