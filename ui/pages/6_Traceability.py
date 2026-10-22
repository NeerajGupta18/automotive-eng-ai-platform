"""Cross-module traceability: impact analysis and the 'needs re-review' worklist."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import common
from common import api, raw_get, status_badge

common.setup("Traceability")
project = common.sidebar("Module: **Traceability**. One graph links hazards, requirements, threats, tests and code findings.")
st.title("Traceability and change impact")
if not project:
    st.info("Create a project workspace in the sidebar.")
    st.stop()
T = f"/projects/{project}/trace"

s = api("GET", f"{T}/summary")
if s:
    c = st.columns(4)
    c[0].metric("Graph nodes", s["nodes"])
    c[1].metric("Links", sum(s["links"].values()))
    c[2].metric("Items needing re-review", s["suspect"])
    c[3].metric("Requirements without verification", len(s["requirements_without_verification"]))
    with st.expander("Records and links by type"):
        st.json({"records": s["records"], "links": s["links"]})
    for label, key in (("Requirements without verification", "requirements_without_verification"), ("Requirements without allocation", "requirements_without_allocation"),
                       ("Requirements with open code-review findings", "requirements_with_open_findings"), ("Safety goals without any link", "unlinked_safety_goals")):
        if s[key]:
            st.warning(f"{label}: {', '.join(s[key])}")

st.subheader("Worklist: items made suspect by an upstream change")
sus = api("GET", f"{T}/suspect") or []
if sus:
    st.dataframe(pd.DataFrame([{"ref": n["ref"], "module": n["module"], "kind": n["kind"], "label": n["label"], "reason": n["suspect_reason"] or ""} for n in sus]), hide_index=True, width="stretch")
else:
    st.success("Nothing is waiting for re-review.")

st.subheader("Impact of a change")
ref = st.text_input("Reference (e.g. HE-001, FSR-002, TS-010, TC-005, F-012, AR-005)", key="trace_ref")
if ref:
    n = api("GET", f"{T}/nodes/{ref}")
    if n:
        st.markdown(f"**{n['node']['ref']}** · {n['node']['module']}/{n['node']['kind']} · {status_badge(n['node']['status'])} · {n['node']['label']}")
        c1, c2 = st.columns(2)
        for col, title, key in ((c1, "Upstream (this depends on)", "ancestors"), (c2, "Downstream (affected if this changes)", "descendants")):
            col.markdown(f"**{title}**")
            rows = [{"hops": x["hops"], "ref": x["ref"], "module": x["module"], "kind": x["kind"], "status": x["status"], "label": x["label"]} for x in n[key]]
            col.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch") if rows else col.caption("none")
        r = raw_get(f"{T}/graph", around=ref)
        if r is not None and "->" in r.text:
            st.graphviz_chart(r.text, width="stretch")
            st.caption("Orange outline = suspect. Colours: red HARA · blue TARA · green UDS tests · amber code review · purple ADAS validation.")
