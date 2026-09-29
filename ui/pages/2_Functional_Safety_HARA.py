"""Functional Safety: HARA and safety requirement management."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import common
from common import api, do, status_badge

common.setup("Functional Safety HARA")
project = common.sidebar("Module: **Functional Safety (HARA)**. ASIL comes from a deterministic rule set, never from the LLM.")
st.title("Functional Safety: HARA and safety requirements")
if not project:
    st.info("Create a project workspace in the sidebar.")
    st.stop()
H = f"/projects/{project}/hara"

items = api("GET", f"{H}/items") or []
docs = api("GET", f"/projects/{project}/documents") or []
t_setup, t_haz, t_ev, t_goal, t_trace, t_hist = st.tabs(
    ["1 · Item & library", "2 · Hazards", "3 · Events & ASIL", "4 · Goals & requirements", "5 · Traceability & report", "History"])

# ---------------------------------------------------------------- setup
with t_setup:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Approved hazard library")
        st.caption("Provides similar approved hazards and suggested S/E/C values. Bundled data is synthetic training data.")
        if st.button("Load bundled synthetic library"):
            do("POST", f"{H}/library/load", json={})
        lib = api("GET", f"{H}/library") or {}
        st.write(f"{len(lib.get('entries', []))} library entries loaded")
    with c2:
        st.subheader("Define an item")
        with st.form("item_form"):
            name = st.text_input("Item name", "BCM central locking")
            desc = st.text_area("Description", "Locks and unlocks all vehicle doors.")
            funcs = st.text_area("Functions (one per line: name | description)",
                                 "Central door locking | locks and unlocks all doors\nAutomatic locking above speed threshold | locks doors when speed exceeds a threshold")
            hld = st.selectbox("Linked HLD document (optional, checks requirement allocation)",
                               [None] + [d["id"] for d in docs], format_func=lambda d: "none" if d is None else next(x["name"] for x in docs if x["id"] == d))
            if st.form_submit_button("Create item"):
                fl = [{"name": l.split("|")[0].strip(), "description": (l.split("|") + [""])[1].strip()} for l in funcs.splitlines() if l.strip()]
                do("POST", f"{H}/items", json={"name": name, "description": desc, "functions": fl, "hld_doc_id": hld})
    if items:
        st.dataframe(pd.DataFrame([{"ref": i["ref"], "name": i["payload"]["name"], "functions": ", ".join(f["name"] for f in i["payload"]["functions"]),
                                    "hld": i["payload"]["hld_doc_id"] or ""} for i in items]), hide_index=True, width="stretch")
    with st.expander("ASIL determination table (rule set)"):
        tbl = api("GET", "/hara/asil-table")
        if tbl:
            st.caption(f"Rule set: {tbl['ruleset']}. Verify against your licensed standard copy.")
            st.dataframe(pd.DataFrame(tbl["table"]).pivot_table(index=["S", "E"], columns="C", values="asil", aggfunc="first"), width="stretch")

if not items:
    for t in (t_haz, t_ev, t_goal, t_trace, t_hist):
        with t:
            st.info("Define an item first (tab 1).")
    st.stop()

with st.sidebar:
    item = st.selectbox("HARA item", [i["ref"] for i in items], format_func=lambda r: f"{r} · {next(i['payload']['name'] for i in items if i['ref'] == r)}")
hazards = api("GET", f"{H}/hazards", params={"item": item}) or []
events = api("GET", f"{H}/events", params={"item": item}) or []
goals = api("GET", f"{H}/goals", params={"item": item}) or []
reqs = api("GET", f"{H}/requirements", params={"item": item}) or []

# ---------------------------------------------------------------- hazards
with t_haz:
    if st.button("💡 Suggest hazards (guidewords × functions + similar library hazards)"):
        do("POST", f"{H}/items/{item}/suggest-hazards")
    st.caption(f"{sum(h['status'] == 'proposed' for h in hazards)} awaiting triage")
    for h in hazards:
        p = h["payload"]
        with st.expander(f"{h['ref']} · {p['function']} · {p['malfunction']} · {status_badge(h['status'])}"):
            st.write(p["description"])
            for s in p["similar"]:
                st.caption(f"Similar approved hazard {s['id']} (score {s['score']}): {s['hazard']} · S{s['S']} E{s['E']} C{s['C']} · {s['situation']}")
            if h["status"] == "proposed":
                b1, b2, _ = st.columns([1, 1, 4])
                if b1.button("✅ Accept", key=f"ha_{h['ref']}"):
                    do("POST", f"{H}/hazards/{h['ref']}/decision", json={"decision": "accepted"})
                if b2.button("❌ Reject", key=f"hr_{h['ref']}"):
                    do("POST", f"{H}/hazards/{h['ref']}/decision", json={"decision": "rejected"})

# ---------------------------------------------------------------- events
with t_ev:
    accepted = [h for h in hazards if h["status"] == "accepted"]
    if not accepted:
        st.info("Accept at least one hazard first.")
    for h in accepted:
        st.markdown(f"#### {h['ref']} · {h['payload']['function']} · {h['payload']['malfunction']}")
        if st.button("Propose hazardous events", key=f"pe_{h['ref']}"):
            do("POST", f"{H}/hazards/{h['ref']}/propose-events")
        for e in [e for e in events if e["payload"]["hazard"] == h["ref"]]:
            p = e["payload"]
            head = f"{e['ref']} · {p['situation']} · " + (f"S{p['S']} E{p['E']} C{p['C']} → ASIL {p['asil']}" if p["asil"] else "not assessed") + f" · {status_badge(e['status'])}"
            with st.expander(head):
                sug = p.get("suggested")
                if sug:
                    st.info(f"Library suggestion from {sug['source']}: S{sug['S']} E{sug['E']} C{sug['C']}. A suggestion is a starting point, not a decision.")
                if e["status"] != "rejected":
                    with st.form(f"assess_{e['ref']}"):
                        c1, c2, c3 = st.columns(3)
                        S = c1.selectbox("Severity S", [0, 1, 2, 3], index=(p["S"] if p["S"] is not None else (sug or {}).get("S", 1)), key=f"S_{e['ref']}")
                        E = c2.selectbox("Exposure E", [0, 1, 2, 3, 4], index=(p["E"] if p["E"] is not None else (sug or {}).get("E", 1)), key=f"E_{e['ref']}")
                        C = c3.selectbox("Controllability C", [0, 1, 2, 3], index=(p["C"] if p["C"] is not None else (sug or {}).get("C", 1)), key=f"C_{e['ref']}")
                        rs = st.text_input("Rationale for S", p["rationale"]["S"], key=f"rs_{e['ref']}")
                        re_ = st.text_input("Rationale for E", p["rationale"]["E"], key=f"re_{e['ref']}")
                        rc = st.text_input("Rationale for C", p["rationale"]["C"], key=f"rc_{e['ref']}")
                        if st.form_submit_button("Save assessment"):
                            do("PUT", f"{H}/events/{e['ref']}/assessment", json={"S": S, "E": E, "C": C, "rationale_S": rs, "rationale_E": re_, "rationale_C": rc})
                if p["asil"]:
                    st.caption(p["asil_rationale"])
                if e["status"] in ("assessed", "proposed"):
                    b1, b2, _ = st.columns([1, 1, 4])
                    if b1.button("✅ Approve", key=f"ea_{e['ref']}"):
                        do("POST", f"{H}/events/{e['ref']}/decision", json={"decision": "approved"})
                    if b2.button("❌ Reject", key=f"er_{e['ref']}"):
                        do("POST", f"{H}/events/{e['ref']}/decision", json={"decision": "rejected"})

# ---------------------------------------------------------------- goals & requirements
with t_goal:
    if st.button("🎯 Derive safety goals from approved events"):
        do("POST", f"{H}/items/{item}/derive-goals")
    if not goals:
        st.info("No goals yet. Approve ASIL-rated hazardous events, then derive goals.")
    for g in goals:
        p = g["payload"]
        with st.expander(f"{g['ref']} · ASIL {p['asil']} · {p['text']} · {status_badge(g['status'])}", expanded=g["status"] == "suspect"):
            if g["payload"].get("suspect_reason"):
                st.warning(g["payload"]["suspect_reason"])
            st.caption(f"Derived from events: {', '.join(p['events'])}")
            with st.form(f"goal_{g['ref']}"):
                ss = st.text_input("Safe state", p["safe_state"])
                ftti = st.number_input("FTTI (ms)", 0, 60000, int(p["ftti_ms"] or 0))
                if st.form_submit_button("Save goal"):
                    do("PUT", f"{H}/goals/{g['ref']}", json={"safe_state": ss, "ftti_ms": int(ftti) if ftti else None})
            b1, b2, b3 = st.columns([1, 1, 2])
            if g["status"] in ("proposed", "assessed", "suspect") and b1.button("✅ Approve goal", key=f"ga_{g['ref']}"):
                do("POST", f"{H}/goals/{g['ref']}/decision", json={"decision": "approved"})
            if b3.button("📝 Draft FSR + TSR", key=f"dr_{g['ref']}"):
                do("POST", f"{H}/goals/{g['ref']}/draft-requirements")
            for r in [r for r in reqs if r["payload"]["goal"] == g["ref"]]:
                rp = r["payload"]
                st.markdown(f"**{r['ref']}** ({rp['level']}, ASIL {rp['asil']}) · {status_badge(r['status'])}")
                for q in rp["quality"]:
                    (st.error if q["severity"] == "error" else st.warning)(f"{q['rule']}: {q['message']}")
                with st.form(f"req_{r['ref']}"):
                    txt = st.text_area("Requirement text", rp["text"], key=f"rt_{r['ref']}")
                    al = st.text_input("Allocated to (architecture element)", rp["allocated_to"], key=f"ral_{r['ref']}")
                    ver = st.text_input("Verification references (comma separated)", ", ".join(rp["verification"]), key=f"rv_{r['ref']}")
                    if st.form_submit_button("Save requirement"):
                        do("PUT", f"{H}/requirements/{r['ref']}", json={"text": txt, "allocated_to": al, "verification": [v for v in ver.split(",")]})
                if rp["allocation_check"] == "not_in_architecture":
                    st.error("Allocation target is not a component in the linked HLD")
                if r["status"] in ("proposed", "assessed", "suspect") and st.button("✅ Approve requirement", key=f"ra_{r['ref']}"):
                    do("POST", f"{H}/requirements/{r['ref']}/decision", json={"decision": "approved"})

# ---------------------------------------------------------------- trace & report
with t_trace:
    comp = api("GET", f"{H}/items/{item}/completeness")
    if comp:
        c1, c2 = st.columns([1, 3])
        c1.metric("Traceability completeness", f"{comp['traceability_completeness']:.0%}")
        c1.write("✅ complete" if comp["complete"] else "⚠️ not complete")
        with c2:
            for ch in comp["checks"]:
                st.write(("✅ " if ch["passed"] else "⬜ ") + ch["description"] + (f" · open: {', '.join(ch['failing'])}" if ch["failing"] else ""))
    rows = api("GET", f"{H}/items/{item}/trace") or []
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    m = api("GET", f"{H}/items/{item}/metrics")
    if m:
        with st.expander("Metrics"):
            st.json(m)
    dot = api("GET", f"{H}/graph", expect_json=False)
    if dot and "->" in dot:
        st.graphviz_chart(dot, width="stretch")
    d1, d2, d3 = st.columns(3)
    for col, fmt, mime in ((d1, "md", "text/markdown"), (d2, "csv", "text/csv"), (d3, "json", "application/json")):
        body = api("GET", f"{H}/items/{item}/report", params={"format": fmt}, expect_json=False)
        if body:
            col.download_button(f"⬇ Report ({fmt})", body, file_name=f"{item}_hara.{fmt}", mime=mime)

# ---------------------------------------------------------------- history
with t_hist:
    ref = st.selectbox("Record", [r["ref"] for r in hazards + events + goals + reqs] or [""], key="hist_ref")
    if ref:
        hist = api("GET", f"{H}/records/{ref}/history") or []
        st.dataframe(pd.DataFrame([{"rev": h["rev"], "time": h["ts"], "actor": h["actor"], "event": h["event"], "status": h["status"]} for h in hist]),
                     hide_index=True, width="stretch")
