"""Secure code review: sources, static-analysis logs, findings triage, verified fixes, quality gate."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import common
from common import api, do, status_badge

common.setup("Secure code review")
project = common.sidebar("Module: **Secure code review**. Rule findings are deterministic; LLM findings (opt-in) must quote real code.")
st.title("Secure code review")
if not project:
    st.info("Create a project workspace in the sidebar.")
    st.stop()
C = f"/projects/{project}/codereview"
SEV = ["critical", "high", "medium", "low", "info"]

sources = api("GET", f"{C}/sources") or []
docs = api("GET", f"/projects/{project}/documents") or []
health = api("GET", "/health") or {}
t_in, t_find, t_gate, t_rep = st.tabs(["1 · Sources & analysis", "2 · Findings", "3 · Metrics & quality gate", "4 · Reports & rules"])

# ---------------------------------------------------------------- inputs
with t_in:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("C sources")
        files = st.file_uploader("Upload .c / .h files (max 1 MB each)", type=["c", "h"], accept_multiple_files=True, key="src_files")
        ver = st.text_input("Version label", "1.0", key="src_ver")
        if st.button("Upload sources", disabled=not files):
            for f in files:
                r = api("POST", f"{C}/sources", files={"file": (f.name, f.getvalue())}, data={"version": ver})
                if r:
                    st.success(f"{f.name}: " + ("unchanged" if r.get("unchanged") else f"revision {r['rev']}"))
            st.rerun()
        if sources:
            st.dataframe(pd.DataFrame([{"ref": s["ref"], "file": s["payload"]["name"], "rev": s["rev"], "version": s["payload"]["version"], "lines": s["payload"]["loc"]} for s in sources]), hide_index=True, width="stretch")
    with c2:
        st.subheader("Static-analysis logs")
        st.caption("cppcheck XML, SARIF 2.1.0 or compiler-style text. A log older than its source revision is ignored as stale.")
        lf = st.file_uploader("Upload a log", type=["xml", "sarif", "json", "txt", "log"], key="log_file")
        if st.button("Upload log", disabled=lf is None):
            r = api("POST", f"{C}/logs", files={"file": (lf.name, lf.getvalue())})
            if r:
                st.success(f"{r['payload']['kind']}: {r['payload']['count']} entries" + (" (duplicate)" if r["duplicate"] else ""))
                st.rerun()
        for l in api("GET", f"{C}/logs") or []:
            st.caption(f"{l['ref']} · {l['payload']['kind']} · {l['payload']['count']} entries · {', '.join(l['payload']['tools'])}")
    st.subheader("Analyze")
    d1, d2 = st.columns(2)
    hld = d1.selectbox("Linked HLD (maps functions to components and ASIL)", [None] + [d["id"] for d in docs],
                       format_func=lambda d: "none" if d is None else next(x["name"] for x in docs if x["id"] == d))
    if d1.button("Save HLD context"):
        do("PUT", f"{C}/context", json={"hld_doc_id": hld})
    use_llm = d2.checkbox("Also run an LLM review (opt-in)", False)
    external = False
    backend = health.get("llm", "mock")
    if use_llm and backend not in ("mock", "ollama"):
        external = d2.checkbox(f"⚠ '{backend}' is not a local model: I confirm source code may leave this machine", False)
    if st.button("🔎 Analyze all sources", disabled=not sources):
        r = api("POST", f"{C}/analyze", json={"llm": use_llm, "allow_external": external})
        if r:
            m = st.columns(6)
            for col, k in zip(m, ["new", "unchanged", "fixed", "reopened", "tool_findings", "corroborated"]):
                col.metric(k.replace("_", " "), r[k])
            for w in r["warnings"] + r["stale_logs"]:
                st.warning(w)
            if r["unmatched_log_files"]:
                st.info("Log entries for files that are not uploaded: " + ", ".join(r["unmatched_log_files"]))
            if use_llm:
                st.info(f"LLM: {r['llm_findings']} findings kept, {r['llm_dropped']} dropped because the quoted evidence did not match the code")

# ---------------------------------------------------------------- findings
with t_find:
    f1, f2, f3, f4 = st.columns(4)
    sev = f1.multiselect("Severity", SEV)
    sta = f2.multiselect("Status", ["proposed", "assessed", "approved", "waived", "rejected", "fixed"], default=["proposed", "assessed", "approved"])
    org = f3.selectbox("Origin", ["", "rules", "tool", "llm"])
    src = f4.selectbox("Source", [""] + [s["ref"] for s in sources], format_func=lambda r: "all" if not r else next(s["payload"]["name"] for s in sources if s["ref"] == r))
    allf = api("GET", f"{C}/findings", params={k: v for k, v in {"origin": org, "source": src}.items() if v}) or []
    shown = [f for f in allf if (not sev or f["payload"]["severity"] in sev) and f["status"] in sta]
    st.caption(f"{len(shown)} of {len(allf)} findings")
    if shown:
        st.dataframe(pd.DataFrame([{"ref": f["ref"], "severity": f["payload"]["severity"], "file": f["payload"]["file"], "line": f["payload"]["line"],
                                    "function": f["payload"]["function"], "rule": f["payload"]["rule"], "CWE": f["payload"]["cwe"], "origin": f["payload"]["origin"],
                                    "confirmed by": ", ".join(f["payload"]["corroborated_by"]), "ASIL": f["payload"]["asil"], "status": f["status"]} for f in shown]),
                     hide_index=True, width="stretch", height=260)
        sel = st.selectbox("Inspect a finding", [f["ref"] for f in shown], format_func=lambda r: f"{r} · {next(f['payload']['message'] for f in shown if f['ref'] == r)[:80]}")
        f = next(x for x in shown if x["ref"] == sel)
        p = f["payload"]
        st.markdown(f"### {f['ref']} · {p['severity'].upper()} · {p['title']} · {status_badge(f['status'])}")
        st.write(p["message"])
        meta = [f"CWE {p['cwe'][4:]}" if p["cwe"] else None, f"CERT {p['cert']}" if p["cert"] else None, f"MISRA-style ref {p['misra']} (verify against your licensed copy)" if p["misra"] else None,
                f"confidence {p['confidence']}", f"origin {p['origin']}", f"component {p['component']} · ASIL {p['asil']}" if p["component"] else None]
        st.caption(" · ".join(x for x in meta if x))
        if p["origin"] == "llm":
            st.warning("LLM finding: unverified. Confirm it against the code before acting on it.")
        if p.get("reopened"):
            st.warning("This finding was fixed earlier and has reappeared (regression).")
        if p["why"]:
            st.info(f"**Why it matters:** {p['why']}  \n**How to fix:** {p['fix_hint']}")
        srcrec = api("GET", f"{C}/sources/{p['source']}")
        if srcrec:
            lines = srcrec["payload"]["text"].split("\n")
            lo, hi = max(0, p["line"] - 7), min(len(lines), p["line"] + 6)
            st.code("\n".join(f"{'>>' if i + 1 == p['line'] else '  '} {i + 1:4d}  {lines[i]}" for i in range(lo, hi)), language="c")
        if p["origin"] == "rules" and st.button("Show a verified fix", key=f"fx_{sel}"):
            fx = api("GET", f"{C}/findings/{sel}/fix")
            if fx:
                if fx["available"]:
                    (st.success if fx["verified"] else st.error)("Verified by re-analysis: the finding disappears and nothing new appears" if fx["verified"] else f"NOT verified: {fx['verification']['introduced']}")
                    st.caption(fx["description"] + " Review the patch and apply it yourself; nothing is changed automatically.")
                    st.code(fx["diff"], language="diff")
                else:
                    st.info(fx["reason"])
        if f["status"] == "proposed":
            with st.form(f"tri_{sel}"):
                disp = st.radio("Decision", ["confirmed", "false_positive", "waived"], horizontal=True, key=f"d_{sel}")
                why = st.text_input("Justification (required for false positive / waiver)", key=f"w_{sel}")
                if st.form_submit_button("Record decision"):
                    do("POST", f"{C}/findings/{sel}/triage", json={"disposition": disp, "comment": why})
        elif f["status"] == "assessed" and p["pending"]:
            st.warning(f"{p['pending']} proposed by {f['updated_by']}: \"{p['decision_comment']}\". A second person must approve it.")
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("✅ Approve decision", key=f"ap_{sel}"):
                do("POST", f"{C}/findings/{sel}/decision", json={"decision": "approved"})
            if b2.button("↩ Send back", key=f"sb_{sel}"):
                do("POST", f"{C}/findings/{sel}/decision", json={"decision": "rejected", "comment": "returned"})
        if f["status"] in ("approved", "rejected", "waived"):
            with st.form(f"ro_{sel}"):
                rc = st.text_input("Reason to reopen", key=f"rc_{sel}")
                if st.form_submit_button("Reopen"):
                    do("POST", f"{C}/findings/{sel}/reopen", json={"comment": rc})
        with st.form(f"lk_{sel}"):
            rq = st.text_input("This violates requirement (FSR/TSR/CSR ref)", key=f"lr_{sel}")
            if st.form_submit_button("Link to requirement"):
                do("POST", f"{C}/findings/{sel}/link", json={"requirement": rq})

# ---------------------------------------------------------------- metrics and gate
with t_gate:
    fail_on = st.selectbox("Fail the gate on severity", SEV[:4], index=1)
    g = api("GET", f"{C}/gate", params={"fail_on": fail_on})
    if g:
        (st.success if g["passed"] else st.error)("Quality gate " + ("PASSED" if g["passed"] else f"FAILED: {len(g['blockers'])} open findings at or above '{fail_on}'"))
        if g["blockers"]:
            st.dataframe(pd.DataFrame(g["blockers"]), hide_index=True, width="stretch")
    m = api("GET", f"{C}/metrics")
    if m and m["findings"]:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Open findings / KLOC", m["open_per_kloc"])
        c2.metric("Fixed", m["fixed"])
        c3.metric("Reviewer precision", "n/a" if m["precision"]["precision"] is None else f"{m['precision']['precision']:.0%}")
        c4.metric("Open in ASIL components", m["safety_relevant_open"])
        st.write("**Open by severity:** " + ", ".join(f"{k}: {v}" for k, v in m["open_by_severity"].items()))
        if m["precision_by_rule"]:
            st.dataframe(pd.DataFrame([{"rule": k, **v} for k, v in m["precision_by_rule"].items()]), hide_index=True, width="stretch")
        st.caption("Precision = confirmed / (confirmed + false positives) from reviewer decisions; use it to tune or drop noisy rules.")

# ---------------------------------------------------------------- reports
with t_rep:
    cols = st.columns(4)
    for col, (fmt, label, mime) in zip(cols, [("md", "⬇ Report (md)", "text/markdown"), ("csv", "⬇ CSV", "text/csv"), ("json", "⬇ JSON", "application/json"), ("sarif", "⬇ SARIF", "application/json")]):
        r = common.raw_get(f"{C}/report", format=fmt)
        if r is not None:
            col.download_button(label, r.text, file_name=f"code_review.{fmt}", mime=mime, key=f"dl_{fmt}")
    with st.expander("Rule catalogue"):
        rules = api("GET", "/codereview/rules") or []
        st.dataframe(pd.DataFrame([{"id": r["id"], "title": r["title"], "severity": r["severity"], "category": r["category"], "CWE": r["cwe"], "CERT": r["cert"], "MISRA-style": r["misra"], "confidence": r["confidence"]} for r in rules]), hide_index=True, width="stretch")
        st.caption("Original rule descriptions mapped to public CWE/CERT identifiers. MISRA numbers are informational and must be checked against your licensed MISRA C copy. This is not a MISRA compliance checker.")
