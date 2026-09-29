"""Cybersecurity: TARA (threat analysis and risk assessment) assistant."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import common
from common import api, do, status_badge

common.setup("Cybersecurity TARA")
project = common.sidebar("Module: **Cybersecurity (TARA)**. Feasibility and risk are computed by rules, never by the LLM.")
st.title("Cybersecurity: threat analysis and risk assessment")
if not project:
    st.info("Create a project workspace in the sidebar.")
    st.stop()
T = f"/projects/{project}/tara"

systems = api("GET", f"{T}/systems") or []
docs = api("GET", f"/projects/{project}/documents") or []
rules = api("GET", "/tara/rules") or {}
t_setup, t_surface, t_threat, t_goal, t_cov, t_trace = st.tabs(
    ["1 · System & library", "2 · Attack surface", "3 · Assets & threats", "4 · Goals & controls", "5 · Coverage", "6 · Traceability & report"])

SAMPLE = Path(__file__).resolve().parents[2] / "data" / "samples" / "tara_system_bcm.json"

# ---------------------------------------------------------------- setup
with t_setup:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Threat library")
        st.caption("Approved patterns and controls with owner and review date. Bundled data is synthetic and high level.")
        if st.button("Load bundled synthetic library"):
            do("POST", f"{T}/library/load", json={})
        lib = api("GET", f"{T}/library")
        if lib and lib.get("patterns"):
            stale = sum(p["stale"] for p in lib["patterns"]) + sum(c["stale"] for c in lib["controls"])
            st.write(f"{len(lib['patterns'])} threat patterns · {len(lib['controls'])} controls · {stale} past review date")
    with c2:
        st.subheader("Create a system")
        with st.form("sys_form"):
            name = st.text_input("System name", "BCM system")
            arch_text = st.text_area("Architecture JSON (components, data_flows)", SAMPLE.read_text() if SAMPLE.exists() else "{}", height=180)
            if st.form_submit_button("Create system"):
                try:
                    do("POST", f"{T}/systems", json={"name": name, "architecture": json.loads(arch_text)})
                except json.JSONDecodeError as e:
                    st.error(f"Invalid JSON: {e}")
    if not systems:
        for t in (t_surface, t_threat, t_goal, t_cov, t_trace):
            with t:
                st.info("Create a system first (tab 1).")
        st.stop()
    with st.sidebar:
        system = st.selectbox("System", [s["ref"] for s in systems], format_func=lambda r: f"{r} · {next(s['payload']['name'] for s in systems if s['ref'] == r)}")
    arch = next(s for s in systems if s["ref"] == system)["payload"]["architecture"]
    st.subheader("Architecture inputs")
    i1, i2 = st.columns(2)
    with i1:
        up = st.file_uploader("Import communication matrix (CSV: message, sender, receivers, bus)", type=["csv"])
        if up is not None and st.button("Import matrix"):
            do("POST", f"{T}/systems/{system}/import/comm-matrix", files={"file": (up.name, up.getvalue(), "text/csv")})
    with i2:
        if docs:
            hd = st.selectbox("Import components/flows from HLD", [d["id"] for d in docs], format_func=lambda d: next(x["name"] for x in docs if x["id"] == d))
            if st.button("Import HLD architecture"):
                do("POST", f"{T}/systems/{system}/import/hld", json={"doc_id": hd})
    st.write(f"{len(arch['components'])} components · {len(arch['data_flows'])} data flows")

# ---------------------------------------------------------------- attack surface
with t_surface:
    s = api("GET", f"{T}/systems/{system}/attack-surface")
    if s:
        st.dataframe(pd.DataFrame(s["surface"]), hide_index=True, width="stretch")
        st.markdown("**Trust boundary crossings**")
        st.dataframe(pd.DataFrame(s["boundary_crossings"]), hide_index=True, width="stretch")
    zones: dict[str, list[str]] = {}
    for c in arch["components"]:
        zones.setdefault(c["zone"], []).append(c["name"])
    dot = ["digraph arch {", "rankdir=LR; node [shape=box, style=rounded];"]
    for i, (z, names) in enumerate(zones.items()):
        dot.append(f'subgraph cluster_{i} {{ label="{z}"; style=dashed; ' + " ".join(f'"{n}";' for n in names) + "}")
    dot += [f'"{f["from"]}" -> "{f["to"]}" [label="{f["name"]} ({f["channel"]})", fontsize=9];' for f in arch["data_flows"]] + ["}"]
    st.graphviz_chart("\n".join(dot), width="stretch")

# ---------------------------------------------------------------- assets & threats
with t_threat:
    if st.button("🔎 Analyze: derive assets and threat scenarios from the library"):
        r = api("POST", f"{T}/systems/{system}/analyze")
        if r:
            st.success(f"{r['assets_created']} assets and {r['threats_created']} threat scenarios created")
            for w in r["warnings"]:
                st.warning(w)
            if r["assets_without_threats"]:
                st.info(f"No library pattern matched: {', '.join(r['assets_without_threats'])}. Add threats manually or extend the library.")
    threats = api("GET", f"{T}/threats", params={"system": system}) or []
    assets = api("GET", f"{T}/assets", params={"system": system}) or []
    with st.expander(f"Assets ({len(assets)})"):
        st.dataframe(pd.DataFrame([{"ref": a["ref"], **{k: (", ".join(v) if isinstance(v, list) else v) for k, v in a["payload"].items() if k in ("kind", "name", "component", "interfaces", "properties", "safety_relevant")}} for a in assets]),
                     hide_index=True, width="stretch")
    flt = st.multiselect("Filter by status", ["proposed", "assessed", "approved", "suspect", "rejected"], default=["proposed", "assessed", "suspect"])
    for t in [t for t in threats if t["status"] in flt]:
        p = t["payload"]
        risk = f"risk {p['risk']}" if p.get("risk") else "not assessed"
        with st.expander(f"{t['ref']} · {p['asset_name']} · {p['name']} · {p['stride']} · {risk} · {status_badge(t['status'])}"):
            st.write(p["description"])
            st.caption(f"Damage: {p['damage']} · Interface: {p['interface']} ({p['exposure']}) · Source: {p['source']['library'] or 'manual'}"
                       + (" · ⚠ library entry past review date" if p["source"]["stale"] else ""))
            if p.get("attack_summary"):
                st.caption(f"Attack path (summary): {p['attack_summary']}")
            sug = p.get("suggested") or {}
            imp0, ap0 = (p.get("impact") or sug.get("impact") or {}), (p.get("ap") or sug.get("ap") or {})
            if sug.get("note"):
                st.caption(sug["note"])
            with st.form(f"as_{t['ref']}"):
                st.markdown("**Impact**")
                cols = st.columns(4)
                impact = {c: cols[i].selectbox(c, rules["impact_levels"], index=rules["impact_levels"].index(imp0.get(c, "negligible")), key=f"i_{c}_{t['ref']}")
                          for i, c in enumerate(rules["impact_categories"])}
                st.markdown("**Attack potential**")
                cols = st.columns(5)
                ap = {k: cols[i].selectbox(k.replace("_", " "), list(opts), index=list(opts).index(ap0.get(k, list(opts)[0])), key=f"ap_{k}_{t['ref']}")
                      for i, (k, opts) in enumerate(rules["attack_potential"].items())}
                why = st.text_input("Rationale", p.get("rationale", ""), key=f"why_{t['ref']}")
                link = st.text_input("Linked HARA hazardous event (optional, e.g. HE-001)", p.get("safety_link") or "", key=f"sl_{t['ref']}")
                if st.form_submit_button("Save assessment"):
                    do("PUT", f"{T}/threats/{t['ref']}/assessment", json={"impact": impact, "ap": ap, "rationale": why, "safety_link": link or None})
            if p.get("risk"):
                st.success(f"Impact **{p['impact_rating']}** · attack potential {p['ap_sum']} → feasibility **{p['feasibility']}** · risk value **{p['risk']}** "
                           f"({'mitigation required' if p['mitigation_required'] else 'below mitigation threshold'})")
                if p.get("safety_warning"):
                    st.warning(p["safety_warning"])
                with st.form(f"tr_{t['ref']}"):
                    tr = st.selectbox("Treatment", rules["treatments"], index=rules["treatments"].index(p["treatment"]) if p.get("treatment") else 0, key=f"trs_{t['ref']}")
                    just = st.text_input("Justification (required to retain a high risk)", p.get("treatment_justification", ""), key=f"trj_{t['ref']}")
                    if st.form_submit_button("Save treatment"):
                        do("PUT", f"{T}/threats/{t['ref']}/treatment", json={"decision": tr, "justification": just})
                if p.get("risk_acceptance") == "pending" and st.button("Accept retained risk (second person)", key=f"acc_{t['ref']}"):
                    do("POST", f"{T}/threats/{t['ref']}/accept-risk", json={})
            if t["status"] in ("assessed", "proposed"):
                b1, b2, _ = st.columns([1, 1, 4])
                if b1.button("✅ Approve", key=f"ta_{t['ref']}"):
                    do("POST", f"{T}/threats/{t['ref']}/decision", json={"decision": "approved"})
                if b2.button("❌ Reject", key=f"tj_{t['ref']}"):
                    do("POST", f"{T}/threats/{t['ref']}/decision", json={"decision": "rejected"})

# ---------------------------------------------------------------- goals & controls
with t_goal:
    if st.button("🎯 Derive cybersecurity goals (approved, treated, risk ≥ threshold)"):
        do("POST", f"{T}/systems/{system}/derive-goals")
    goals = api("GET", f"{T}/goals", params={"system": system}) or []
    reqs = api("GET", f"{T}/requirements", params={"system": system}) or []
    if not goals:
        st.info(f"No goals yet. Approve threats with risk ≥ {rules.get('mitigation_threshold', 3)} and a reduce/avoid/share treatment.")
    for g in goals:
        p = g["payload"]
        with st.expander(f"{g['ref']} · risk {p['risk']} · {p['text']} · {status_badge(g['status'])}", expanded=g["status"] == "suspect"):
            if p.get("suspect_reason"):
                st.warning(p["suspect_reason"])
            st.caption(f"Threats: {', '.join(p['threats'])}")
            b1, b2, _ = st.columns([1, 2, 3])
            if g["status"] in ("proposed", "assessed", "suspect") and b1.button("✅ Approve goal", key=f"cga_{g['ref']}"):
                do("POST", f"{T}/goals/{g['ref']}/decision", json={"decision": "approved"})
            if b2.button("🛡 Recommend controls", key=f"rc_{g['ref']}"):
                do("POST", f"{T}/goals/{g['ref']}/recommend-controls")
            for r in [r for r in reqs if r["payload"]["goal"] == g["ref"]]:
                rp = r["payload"]
                st.markdown(f"**{r['ref']}** · {rp['control_name']} · {status_badge(r['status'])}")
                st.caption(f"Source: {rp['source']['library']} (reviewed {rp['source']['review_date']}) · Assumptions: {rp['assumptions']} · Alternatives: {rp['alternatives']} · Suggested allocation: {rp['suggested_allocation']}")
                for q in rp["quality"]:
                    (st.error if q["severity"] == "error" else st.warning)(f"{q['rule']}: {q['message']}")
                with st.form(f"csr_{r['ref']}"):
                    txt = st.text_area("Requirement text", rp["text"], key=f"ct_{r['ref']}")
                    al = st.text_input("Allocated to", rp["allocated_to"], key=f"cal_{r['ref']}")
                    ver = st.text_input("Verification references (comma separated)", ", ".join(rp["verification"]), key=f"cv_{r['ref']}")
                    if st.form_submit_button("Save requirement"):
                        do("PUT", f"{T}/requirements/{r['ref']}", json={"text": txt, "allocated_to": al, "verification": ver.split(",")})
                if r["status"] in ("proposed", "assessed", "suspect") and st.button("✅ Approve requirement", key=f"cra_{r['ref']}"):
                    do("POST", f"{T}/requirements/{r['ref']}/decision", json={"decision": "approved"})

# ---------------------------------------------------------------- coverage
with t_cov:
    cov = api("GET", f"{T}/systems/{system}/coverage")
    if cov:
        st.metric("Checklist coverage", f"{cov['coverage']:.0%}", f"{cov['gaps']} open gaps", delta_color="inverse")
        st.warning(cov["note"])
        st.dataframe(pd.DataFrame([{**r, "threats": ", ".join(r["threats"])} for r in cov["rows"]]), hide_index=True, width="stretch")
        gaps = [r for r in cov["rows"] if r["status"] == "missing"]
        if gaps:
            with st.form("na_form"):
                st.markdown("**Declare a gap as not applicable** (needs justification and a second person's approval)")
                g = st.selectbox("Gap", range(len(gaps)), format_func=lambda i: f"{gaps[i]['component']} / {gaps[i]['interface']} / {gaps[i]['stride']}")
                why = st.text_input("Justification")
                if st.form_submit_button("Submit"):
                    do("POST", f"{T}/systems/{system}/coverage/na", json={"component": gaps[g]["component"], "interface": gaps[g]["interface"], "stride": gaps[g]["stride"], "justification": why})
        for r in [r for r in cov["rows"] if r["status"] == "na_pending"]:
            if st.button(f"Approve N/A {r['na_ref']} ({r['component']}/{r['interface']}/{r['stride']})", key=f"na_{r['na_ref']}"):
                do("POST", f"{T}/na/{r['na_ref']}/decision", json={"decision": "approved"})

# ---------------------------------------------------------------- trace & report
with t_trace:
    comp = api("GET", f"{T}/systems/{system}/completeness")
    if comp:
        c1, c2 = st.columns([1, 3])
        c1.metric("Traceability completeness", f"{comp['traceability_completeness']:.0%}")
        c1.write("✅ complete" if comp["complete"] else "⚠️ not complete")
        with c2:
            for ch in comp["checks"]:
                st.write(("✅ " if ch["passed"] else "⬜ ") + ch["description"] + (f" · open: {', '.join(ch['failing'][:6])}" if ch["failing"] else ""))
    rows = api("GET", f"{T}/systems/{system}/trace") or []
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    m = api("GET", f"{T}/systems/{system}/metrics")
    if m:
        with st.expander("Metrics"):
            st.json(m)
    d1, d2, d3 = st.columns(3)
    for col, fmt, mime in ((d1, "md", "text/markdown"), (d2, "csv", "text/csv"), (d3, "json", "application/json")):
        body = api("GET", f"{T}/systems/{system}/report", params={"format": fmt}, expect_json=False)
        if body:
            col.download_button(f"⬇ Report ({fmt})", body, file_name=f"{system}_tara.{fmt}", mime=mime)
    ref = st.text_input("Show revision history for record (e.g. TS-001)")
    if ref:
        hist = api("GET", f"{T}/records/{ref}/history") or []
        st.dataframe(pd.DataFrame([{"rev": h["rev"], "time": h["ts"], "actor": h["actor"], "event": h["event"], "status": h["status"]} for h in hist]), hide_index=True, width="stretch")
