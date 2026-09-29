"""UDS diagnostics: specification, request validation, test generation, simulation and export."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import common
from common import api, do, status_badge

common.setup("UDS diagnostics")
project = common.sidebar("Module: **UDS diagnostics**. Expected responses come from a deterministic oracle, not the LLM.")
st.title("UDS diagnostics: validation and test generation")
if not project:
    st.info("Create a project workspace in the sidebar.")
    st.stop()
D = f"/projects/{project}/uds"
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "samples" / "uds_bcm_spec.json"

specs = api("GET", f"{D}/specs") or []
docs = api("GET", f"/projects/{project}/documents") or []
ref_data = api("GET", "/uds/reference") or {"nrc": {}, "mutations": {}}
t_spec, t_val, t_tests, t_cov, t_sim, t_exp = st.tabs(
    ["1 · Specification", "2 · Validate requests", "3 · Tests", "4 · Coverage", "5 · Simulator", "6 · Export & report"])

# ---------------------------------------------------------------- specification
with t_spec:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Extract from a specification document")
        st.caption("Upload a Markdown/PDF diagnostic specification on the HLD page first (tables: SID, DID, RID, DTC, sessions, levels).")
        if docs:
            doc = st.selectbox("Document", [d["id"] for d in docs], format_func=lambda d: next(x["name"] for x in docs if x["id"] == d))
            if st.button("Extract specification"):
                do("POST", f"{D}/specs/from-document", json={"doc_id": doc})
        else:
            st.info("No documents in this project yet.")
    with c2:
        st.subheader("Or paste the specification as JSON")
        with st.form("spec_json"):
            name = st.text_input("Name", "BCM diagnostics")
            txt = st.text_area("Specification JSON", SAMPLE.read_text() if SAMPLE.exists() else "{}", height=140)
            if st.form_submit_button("Create specification"):
                try:
                    do("POST", f"{D}/specs", json={"name": name, "spec": json.loads(txt)})
                except json.JSONDecodeError as e:
                    st.error(f"Invalid JSON: {e}")
    if not specs:
        for t in (t_val, t_tests, t_cov, t_sim, t_exp):
            with t:
                st.info("Create a specification first (tab 1).")
        st.stop()
    with st.sidebar:
        ref = st.selectbox("Specification", [s["ref"] for s in specs],
                           format_func=lambda r: f"{r} · {next(s['payload']['name'] for s in specs if s['ref'] == r)}")
    rec = next(s for s in specs if s["ref"] == ref)
    sp = rec["payload"]["spec"]
    st.subheader(f"{ref} · {rec['payload']['name']} · {status_badge(rec['status'])} (rev {rec['rev']})")
    m = st.columns(6)
    for col, (lbl, n) in zip(m, [("Services", len(sp["services"])), ("DIDs", len(sp["dids"])), ("Routines", len(sp["routines"])),
                                 ("DTCs", len(sp["dtcs"])), ("Security levels", len(sp["security_levels"])), ("Sessions", len(sp["sessions"]))]):
        col.metric(lbl, n)
    for w in rec["payload"]["warnings"]:
        st.warning(w)
    issues = rec["payload"]["issues"]
    if issues:
        st.dataframe(pd.DataFrame(issues), hide_index=True, width="stretch")
    else:
        st.success("No consistency issues in the specification")
    if rec["status"] in ("proposed", "assessed", "suspect"):
        b1, b2, _ = st.columns([1, 1, 4])
        if b1.button("✅ Approve specification"):
            do("POST", f"{D}/specs/{ref}/decision", json={"decision": "approved"})
        if b2.button("❌ Reject specification"):
            do("POST", f"{D}/specs/{ref}/decision", json={"decision": "rejected"})
    with st.expander("Services, DIDs and routines"):
        st.dataframe(pd.DataFrame([{"SID": f"0x{s['sid']:02X}", "service": s["name"], "sessions": ", ".join(str(x) for x in s["sessions"]), "modelled": s["modelled"]} for s in sp["services"]]), hide_index=True, width="stretch")
        st.dataframe(pd.DataFrame([{"DID": f"0x{d['did']:04X}", "name": d["name"], "len": d["length"], "read": d["read"], "write": d["write"], "sessions": str(d["sessions"]), "security": d["security"], "range": str(d["range"] or "")} for d in sp["dids"]]), hide_index=True, width="stretch")
        st.dataframe(pd.DataFrame([{"RID": f"0x{r['rid']:04X}", "name": r["name"], "controls": str(r["controls"]), "sessions": str(r["sessions"]), "security": r["security"], "option bytes": r["option_length"]} for r in sp["routines"]]), hide_index=True, width="stretch")
    if rec["payload"]["source_doc"]:
        q = st.text_input("Ask the specification document (cited answer)", key="uds_q")
        if st.button("Ask", disabled=not q):
            a = api("POST", f"{D}/specs/{ref}/ask", json={"question": q})
            if a:
                (st.warning if a["refused"] else st.info)(a["answer"])
                for c in a["citations"]:
                    st.caption(f"[{c['label']}] {c['doc_name']} · p.{c['page']} · {c['section']}")

# ---------------------------------------------------------------- validate
with t_val:
    st.caption("Enter one request per line (hex). Requests run in order, the ECU state carries over. Use <KEY> / <BADKEY> after a seed request.")
    c1, c2 = st.columns(2)
    session = c1.selectbox("Start session", [s["name"] for s in sp["sessions"]])
    unlock = c2.selectbox("Security level already unlocked", [None] + [l["level"] for l in sp["security_levels"]], format_func=lambda x: "locked" if x is None else f"level {x}")
    reqs = st.text_area("Requests", "22 F1 90\n22 01 00\n10 03\n27 01\n27 02 <KEY>\n22 01 00", height=140, key="val_reqs")
    if st.button("Validate"):
        r = api("POST", f"{D}/specs/{ref}/validate", json={"requests": [l for l in reqs.splitlines() if l.strip()], "session": session, "unlock": unlock})
        if r:
            st.dataframe(pd.DataFrame([{"request": x["request"], "expected response": x["response"] or "(no response)", "type": x["kind"],
                                        "NRC": f"0x{x['nrc']:02X} {x['nrc_name']}" if x["nrc"] else "", "rule": x["rule"],
                                        "state": f"session {x['state_after']['session']}, level {x['state_after']['unlocked']}", "why": x["why"]} for x in r["results"]]),
                         hide_index=True, width="stretch")
    with st.expander("NRC reference"):
        st.dataframe(pd.DataFrame([{"NRC": k, "name": v} for k, v in ref_data["nrc"].items()]), hide_index=True, width="stretch")

# ---------------------------------------------------------------- tests
with t_tests:
    if st.button("⚙️ Generate / refresh tests from the specification"):
        r = api("POST", f"{D}/specs/{ref}/generate")
        if r:
            st.success(f"{r['created']} new · {len(r['changed'])} changed · {len(r['orphaned'])} orphaned · {r['unchanged']} unchanged")
            if not r["spec_approved"]:
                st.warning("The specification is not approved yet; tests cannot be approved until it is.")
    tests = api("GET", f"{D}/tests", params={"spec": ref}) or []
    f1, f2 = st.columns(2)
    cat = f1.multiselect("Category", ["positive", "negative", "boundary", "session", "security", "timing"])
    sta = f2.multiselect("Status", ["proposed", "approved", "suspect", "rejected"], default=["proposed", "suspect", "approved"])
    shown = [t for t in tests if (not cat or t["payload"]["category"] in cat) and t["status"] in sta]
    st.caption(f"{len(shown)} of {len(tests)} tests")
    st.dataframe(pd.DataFrame([{"id": t["ref"], "element": t["payload"]["element"], "category": t["payload"]["category"], "title": t["payload"]["title"],
                                "expected": t["payload"]["expected_kind"] + (f" NRC 0x{t['payload']['expected_nrc']:02X}" if t["payload"]["expected_nrc"] else ""),
                                "status": t["status"]} for t in shown]), hide_index=True, width="stretch", height=280)
    if shown:
        sel = st.selectbox("Inspect a test", [t["ref"] for t in shown], format_func=lambda r: f"{r} · {next(t['payload']['title'] for t in shown if t['ref'] == r)}")
        t = next(x for x in shown if x["ref"] == sel)
        p = t["payload"]
        st.markdown(f"**{t['ref']}** · {status_badge(t['status'])} · precondition: session {p['pre']['session']}" + (f", security level {p['pre']['unlock']} unlocked" if p["pre"]["unlock"] else ""))
        if p.get("changed_reason"):
            st.warning(p["changed_reason"])
        st.dataframe(pd.DataFrame([{"step": i, "request": s.get("req", f"wait {s.get('wait_ms')} ms"), "expected": " ".join(s.get("expect", [])) or ("(no response)" if "req" in s else ""), "rule": s.get("rule", ""), "why": s.get("why", "")} for i, s in enumerate(p["steps"], 1)]), hide_index=True, width="stretch")
        b1, b2, b3 = st.columns([1, 1, 3])
        if t["status"] in ("proposed", "assessed", "suspect") and b1.button("✅ Approve test"):
            do("POST", f"{D}/tests/{sel}/decision", json={"decision": "approved"})
        if t["status"] in ("proposed", "assessed", "suspect") and b2.button("❌ Reject test"):
            do("POST", f"{D}/tests/{sel}/decision", json={"decision": "rejected"})
        with b3:
            with st.form(f"link_{sel}"):
                req_ref = st.text_input("Use as verification evidence for requirement (FSR/TSR/CSR ref)", key=f"lr_{sel}")
                if st.form_submit_button("Link to requirement"):
                    do("POST", f"{D}/tests/{sel}/link", json={"requirement": req_ref})
        pend = [x for x in shown if x["status"] in ("proposed", "suspect")]
        if pend:
            ok = st.checkbox(f"I reviewed the {len(pend)} listed tests and approve them as a group", key="bulk_ok")
            if st.button("Approve all listed tests", disabled=not ok):
                for x in pend:
                    api("POST", f"{D}/tests/{x['ref']}/decision", json={"decision": "approved", "comment": "group approval"})
                st.rerun()

# ---------------------------------------------------------------- coverage
with t_cov:
    cov = api("GET", f"{D}/specs/{ref}/coverage")
    if cov:
        c1, c2, c3 = st.columns(3)
        c1.metric("Element coverage (positive + negative)", f"{cov['coverage']:.0%}")
        c2.metric("Approved tests only", f"{cov['approved_only']:.0%}")
        c3.metric("NRCs missing", len(cov["nrc_missing"]))
        st.dataframe(pd.DataFrame(cov["elements"]), hide_index=True, width="stretch")
        st.write("**NRCs exercised:** " + (", ".join(cov["nrc_exercised"]) or "none"))
        if cov["nrc_missing"]:
            st.warning("Not exercised: " + ", ".join(cov["nrc_missing"]))
    comp = api("GET", f"{D}/specs/{ref}/completeness")
    if comp:
        st.markdown("**Completeness**")
        for ch in comp["checks"]:
            st.write(("✅ " if ch["passed"] else "⬜ ") + ch["description"] + (f" · open: {', '.join(map(str, ch['failing'][:6]))}" if ch["failing"] else ""))

# ---------------------------------------------------------------- simulator
with t_sim:
    st.caption("Runs the generated tests against a simulated ECU. Inject a defect to see whether the suite detects it. "
               "The simulator shares the oracle with the generator, so this shows detection power, not that the specification is right.")
    mut = st.selectbox("Simulated implementation", list(ref_data["mutations"]), format_func=lambda k: f"{k}: {ref_data['mutations'][k]}")
    only = st.checkbox("Only approved tests", False)
    r1, r2 = st.columns(2)
    if r1.button("▶ Run tests"):
        r = api("POST", f"{D}/specs/{ref}/run", json={"mutation": mut, "only_approved": only})
        if r:
            st.metric("Passed", f"{r['passed']} / {r['total']}")
            if r["failed"]:
                st.error(f"{r['failed']} tests failed" + (" - defect detected" if r["defect_detected"] else ""))
                st.dataframe(pd.DataFrame([{"test": x["test"], "element": x["element"], "title": x["title"], "first failure": x["failures"][0]} for x in r["results"] if not x["passed"]]), hide_index=True, width="stretch")
            else:
                st.success("All tests passed" + (" - the injected defect was NOT detected" if mut != "none" else ""))
    if r2.button("🧪 Run all injected defects"):
        dm = api("POST", f"{D}/specs/{ref}/detection-matrix")
        if dm:
            st.dataframe(pd.DataFrame([{"implementation": k, "description": v["description"], "failing tests": v["failed"], "detected": v["detected"]} for k, v in dm.items()]), hide_index=True, width="stretch")

# ---------------------------------------------------------------- export
with t_exp:
    inc = st.checkbox("Include unapproved tests (draft export)", False)
    cols = st.columns(4)
    for i, (fmt, label, mime) in enumerate([("bundle", "⬇ Bundle (zip)", "application/zip"), ("python", "⬇ pytest file", "text/x-python"), ("harness", "⬇ Harness", "text/x-python"),
                                             ("capl", "⬇ CAPL (experimental)", "text/plain"), ("csv", "⬇ CSV", "text/csv"), ("json", "⬇ JSON", "application/json")]):
        r = common.raw_get(f"{D}/specs/{ref}/export", format=fmt, include_unapproved=inc)
        ext = "zip" if fmt == "bundle" else "py" if fmt in ("python", "harness") else "can" if fmt == "capl" else fmt
        if r is not None:
            cols[i % 4].download_button(label, r.content, file_name=f"uds_{ref}.{ext}", mime=mime, key=f"dl_{fmt}")
        else:
            cols[i % 4].caption(f"{fmt}: not available yet (approve tests first, or tick the draft option)")
    rep = api("GET", f"{D}/specs/{ref}/report", expect_json=False)
    if rep:
        with st.expander("Test specification report (Markdown)"):
            st.markdown(rep)
        st.download_button("⬇ Report (md)", rep, file_name=f"{ref}_uds_report.md", mime="text/markdown")
