"""Phase 2: SBOM-based vulnerability monitoring, OTA update verification and R155/R156 evidence."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import common
from common import api, do, status_badge

ICON = {"pass": "✅ pass", "fail": "❌ FAIL", "warn": "⚠️ warn", "info": "ℹ️ info", "skip": "⏭ skip"}
SEV_ICON = {"critical": "🟥 critical", "high": "🟧 high", "medium": "🟨 medium", "low": "🟩 low", "unknown": "⬜ unknown", "none": "⬜ none"}
STATE_TEXT = {"proposed": "in triage", "assessed": "awaiting 2nd person", "approved": "EXPLOITABLE", "rejected": "not affected / FP",
              "resolved": "resolved"}

common.setup("Vulnerabilities & OTA")
project = common.sidebar("Module: **Vulnerabilities & OTA** (Phase 2). SBOM monitoring, signed update verification, "
                         "UN R155/R156 evidence.")
st.title("Vulnerability monitoring and OTA update security")
if not project:
    st.info("Create a project workspace in the sidebar.")
    st.stop()
V, O, C = f"/projects/{project}/vuln", f"/projects/{project}/ota", f"/projects/{project}/compliance"
me = st.session_state.get("user", "engineer")

t_ecu, t_vuln, t_keys, t_pkg, t_test, t_ev = st.tabs(["1 · ECUs & SBOMs", "2 · Vulnerabilities", "3 · Signing keys",
                                                      "4 · Update packages", "5 · Attack self-test", "6 · R155 / R156 evidence"])

# ------------------------------------------------------------------ 1 ECUs and SBOMs
with t_ecu:
    feed = api("GET", f"{V}/library") or {}
    c1, c2 = st.columns([2, 1])
    with c1:
        st.subheader("Advisory feed (OSV format)")
        if feed.get("loaded"):
            st.write(f"**{feed['name']}** · {feed['advisories']} advisories ({feed.get('synthetic', 0)} synthetic) · "
                     f"loaded {feed['loaded_at'][:16]} by {feed['loaded_by']}" + (" · ⚠️ older than 7 days" if feed.get("stale") else ""))
        else:
            st.warning("No advisory feed loaded: nothing can be matched yet.")
    with c2:
        if st.button("Load bundled demo feed", help="4 public CVEs + 3 synthetic automotive advisories (reviewer permission)"):
            do("POST", f"{V}/library/load")
        ff = st.file_uploader("…or upload an OSV feed (.json / .zip)", type=["json", "zip"], key="feed_up")
        if ff and st.button("Load uploaded feed"):
            do("POST", f"{V}/library/upload", files={"file": (ff.name, ff.getvalue())})
    st.subheader("ECU software baseline")
    sb = st.file_uploader("Upload a CycloneDX SBOM (JSON) for an ECU software release", type=["json"], key="sbom_up")
    if sb and st.button("Upload SBOM and scan"):
        r = api("POST", f"{V}/sboms", files={"file": (sb.name, sb.getvalue())})
        if r:
            st.success(f"{r['sbom']['ref']}: {r['sbom']['ecu']} {r['sbom']['ecu_version']} · {r['sbom']['components']} components · "
                       f"{r['scan'].get('new', 0)} new findings, {r['scan'].get('resolved', 0)} resolved")
            for w in r["warnings"]:
                st.caption(f"⚠️ {w}")
    ecus = api("GET", f"{V}/ecus") or []
    if ecus:
        st.dataframe(pd.DataFrame([{"ECU": e["ecu"], "software": e["ecu_version"], "hardware": e["hardware_id"], "RXSWIN": e["rxswin"],
                                    "components": e["components"], "open findings": e["open_findings"],
                                    "critical": e["by_severity"]["critical"], "high": e["by_severity"]["high"],
                                    "SBOM": e["ref"], "source": e["source"]} for e in ecus]), hide_index=True, use_container_width=True)
    m = api("GET", f"{V}/metrics")
    if m:
        k = st.columns(5)
        k[0].metric("Open findings", m["open"])
        k[1].metric("Critical / high open", f"{m['open_by_severity']['critical']} / {m['open_by_severity']['high']}")
        k[2].metric("Not triaged", m["untriaged"])
        k[3].metric("Resolved by updates", m["resolved"])
        k[4].metric("Mean time to triage", f"{m['mean_hours_to_triage']} h" if m["mean_hours_to_triage"] is not None else "–")

# ------------------------------------------------------------------ 2 findings and triage
with t_vuln:
    ref_lists = api("GET", f"{V}/reference") or {"states": [], "justifications": [], "responses": []}
    f1, f2, f3 = st.columns(3)
    st_f = f1.selectbox("Status", ["", "proposed", "assessed", "approved", "rejected", "resolved"],
                        format_func=lambda s: "all" if not s else STATE_TEXT[s])
    sev_f = f2.selectbox("Severity", ["", "critical", "high", "medium", "low", "unknown"], format_func=lambda s: s or "all")
    ecu_f = f3.selectbox("ECU", [""] + [e["ecu"] for e in ecus], format_func=lambda s: s or "all")
    params = {k: v for k, v in (("status", st_f), ("severity", sev_f), ("ecu", ecu_f)) if v}
    fs = api("GET", f"{V}/findings", params=params) or []
    if not fs:
        st.info("No findings for this filter.")
    else:
        st.dataframe(pd.DataFrame([{"ref": f["ref"], "advisory": f["payload"]["advisory"], "severity": SEV_ICON.get(f["payload"]["severity"]),
                                    "CVSS": f["payload"]["score"], "ECU": f["payload"]["ecu"],
                                    "component": f"{f['payload']['component']} {f['payload']['component_version']}",
                                    "fixed in": ", ".join(f["payload"]["fixed_in"]), "state": STATE_TEXT.get(f["status"], f["status"])}
                                   for f in fs]), hide_index=True, use_container_width=True)
        sel = st.selectbox("Inspect a finding", [f["ref"] for f in fs],
                           format_func=lambda r: next(f["payload"]["title"] for f in fs if f["ref"] == r))
        f = next(x for x in fs if x["ref"] == sel)
        p = f["payload"]
        st.markdown(f"### {sel} · {SEV_ICON.get(p['severity'])} · CVSS {p['score'] if p['score'] is not None else '–'} · {STATE_TEXT.get(f['status'])}")
        if p.get("synthetic"):
            st.caption("Synthetic training advisory (fictional component).")
        st.write(p["summary"])
        st.caption(f"{p['component']} {p['component_version']} in {p['ecu']} {p['ecu_version']} ({p['sbom']}) · "
                   f"fixed in {', '.join(p['fixed_in']) or 'n/a'} · `{p['vector'] or 'no CVSS vector'}`")
        for u in p.get("references", []):
            st.markdown(f"- {u}")
        if f["status"] == "proposed":
            with st.form(f"triage_{sel}"):
                state = st.radio("Analysis", ref_lists["states"], horizontal=True,
                                 help="exploitable: remediation needed (flags the ECU's TARA threats). not_affected needs a VEX justification.")
                just = st.selectbox("VEX justification (for not_affected)", [None] + ref_lists["justifications"])
                resp = st.multiselect("Response", ref_lists["responses"])
                detail = st.text_area("Analysis detail / evidence")
                if st.form_submit_button("Save analysis"):
                    r = api("POST", f"{V}/findings/{sel}/triage", json={"state": state, "justification": just, "response": resp, "detail": detail})
                    if r:
                        if r.get("tara_suspect"):
                            st.warning(f"TARA items flagged for re-assessment: {', '.join(r['tara_suspect'])} (see Traceability)")
                        st.rerun()
        elif f["status"] == "assessed":
            st.info(f"{p.get('triaged_by')} proposed **{p.get('pending')}** ({p.get('justification') or '-'}): {p.get('detail')}. "
                    "A second person must confirm.")
            cmt = st.text_input("Comment", key=f"c2_{sel}")
            b1, b2 = st.columns(2)
            if b1.button("✅ Confirm (second person)"):
                do("POST", f"{V}/findings/{sel}/decision", json={"decision": "approved", "comment": cmt})
            if b2.button("↩ Return to triage"):
                do("POST", f"{V}/findings/{sel}/decision", json={"decision": "rejected", "comment": cmt})
        elif f["status"] in ("approved", "rejected"):
            st.write(f"**{p.get('state')}** by {p.get('triaged_by')}"
                     + (f", confirmed by {p.get('second_reviewer')}" if p.get("second_reviewer") else "")
                     + f": {p.get('detail') or ''}")
            why = st.text_input("Reason to reopen (min 10 characters)", key=f"ro_{sel}")
            if st.button("Reopen"):
                do("POST", f"{V}/findings/{sel}/reopen", json={"comment": why})
        else:
            st.success(p.get("resolution", "Resolved"))
    r = common.raw_get(f"{V}/vex", **({"ecu": ecu_f} if ecu_f else {}))
    if r:
        st.download_button("⬇ Export CycloneDX VEX", r.text, file_name=f"vex_{ecu_f or 'all'}.cdx.json", mime="application/json")

# ------------------------------------------------------------------ 3 keys
with t_keys:
    st.caption("A key becomes active only after a second person approves it. Revocation is immediate and re-verifies "
               "every unreleased package.")
    with st.expander("➕ Register a signing key (ECDSA P-256 public key, PEM)"):
        with st.form("key_form"):
            kid = st.text_input("Key id", "oem-ota-2027")
            owner = st.text_input("Owner (who controls the private key)", "OEM release HSM")
            pem_file = st.file_uploader("Public key file", type=["pem"])
            pem = st.text_area("…or paste the PEM")
            until = st.text_input("Valid until (ISO-8601, optional)", "2028-12-31T00:00:00Z")
            if st.form_submit_button("Propose key"):
                text = pem_file.getvalue().decode() if pem_file else pem
                do("POST", f"{O}/keys", json={"key_id": kid, "owner": owner, "public_key_pem": text, "valid_until": until})
    for k in api("GET", f"{O}/keys") or []:
        p = k["payload"]
        st.markdown(f"**{p['key_id']}** · {p['owner']} · {status_badge(k['status'])} · fingerprint `{p['fingerprint'][:24]}…` "
                    f"· valid until {p['valid_until'] or '–'} · proposed by {k['created_by']}"
                    + (f" · approved by {k['approved_by']}" if k.get("approved_by") else "")
                    + (f" · revoked: {p['revoked_reason']}" if k["status"] == "revoked" else ""))
        if k["status"] in ("proposed", "assessed"):
            a1, a2 = st.columns(2)
            if a1.button(f"✅ Approve {k['ref']}"):
                do("POST", f"{O}/keys/{k['ref']}/decision", json={"decision": "approved", "comment": "fingerprint verified"})
            if a2.button(f"❌ Reject {k['ref']}"):
                do("POST", f"{O}/keys/{k['ref']}/decision", json={"decision": "rejected", "comment": ""})
        elif k["status"] == "approved":
            why = st.text_input(f"Revocation reason for {k['ref']}", key=f"rv_{k['ref']}")
            if st.button(f"⛔ Revoke {k['ref']}"):
                do("POST", f"{O}/keys/{k['ref']}/revoke", json={"reason": why})

# ------------------------------------------------------------------ 4 packages
with t_pkg:
    up = st.file_uploader("Upload an update package (.zip with manifest.json, manifest.sig, payload, sbom.json)", type=["zip"], key="pkg_up")
    if up and st.button("Upload and verify"):
        r = api("POST", f"{O}/packages", files={"file": (up.name, up.getvalue())})
        if r:
            (st.success if r["payload"]["verdict"] != "fail" else st.error)(
                f"{r['ref']}: {r['payload']['title']} · verdict **{r['payload']['verdict'].upper()}**"
                + (f" · failed {', '.join(r['payload']['failed'])}" if r["payload"]["failed"] else ""))
    pkgs = api("GET", f"{O}/packages") or []
    if pkgs:
        st.dataframe(pd.DataFrame([{"ref": x["ref"], "update": x["payload"]["title"], "file": x["payload"]["filename"],
                                    "signed by": x["payload"]["signed_by"], "verdict": ICON.get(x["payload"]["verdict"]),
                                    "failed": ", ".join(x["payload"]["failed"]), "status": "RELEASED" if x["status"] == "approved" else x["status"],
                                    "uploaded by": x["payload"].get("uploaded_by")} for x in pkgs]), hide_index=True, use_container_width=True)
        sel = st.selectbox("Inspect a package", [x["ref"] for x in pkgs], format_func=lambda r: next(f"{r} · {x['payload']['title']}" for x in pkgs if x["ref"] == r))
        rec = api("GET", f"{O}/packages/{sel}")
        if rec:
            p = rec["payload"]
            st.markdown(f"### {sel} · {p['title']} · verdict {ICON[p['verdict']]}")
            st.dataframe(pd.DataFrame([{"check": f"{c['id']} {c['title']}", "result": ICON[c["status"]], "detail": c["detail"],
                                        "supports": c["relates_to"]} for c in p["checks"]]), hide_index=True, use_container_width=True)
            imp = p.get("impact") or {}
            if imp:
                with st.expander(f"Change impact (highest ASIL touched: {imp.get('max_asil') or 'none'})"):
                    for kname in ("requirements", "safety_and_cyber_goals", "tests", "threats", "code_findings", "open_vulnerabilities"):
                        st.write(f"**{kname.replace('_', ' ')}**: {', '.join(imp.get(kname) or []) or '–'}")
            if rec["status"] in ("proposed", "assessed"):
                cmt = st.text_area("Release comment (required when a check warns)", key=f"rel_{sel}")
                b1, b2, b3 = st.columns(3)
                if b1.button("🚀 Release update (second person)", disabled=p["verdict"] == "fail"):
                    do("POST", f"{O}/packages/{sel}/decision", json={"decision": "approved", "comment": cmt})
                if b2.button("❌ Reject package"):
                    do("POST", f"{O}/packages/{sel}/decision", json={"decision": "rejected", "comment": cmt})
                if b3.button("🔁 Re-verify"):
                    do("POST", f"{O}/packages/{sel}/verify")
                if p["verdict"] == "fail":
                    st.caption("Release is blocked while any check fails.")
            elif rec["status"] == "approved":
                st.success(f"Released {p.get('released_at', '')[:16]} by {p.get('released_by')} (uploaded by {p.get('uploaded_by')}). "
                           f"New baseline: {p.get('baseline_sbom') or '–'} · vulnerabilities resolved: {p.get('vulnerabilities_resolved', 0)}"
                           + (f" · re-verification flagged: {', '.join(p['reverify_needed'])}" if p.get("reverify_needed") else ""))
            d1, d2 = st.columns(2)
            for col, fmt, mime in ((d1, "md", "text/markdown"), (d2, "json", "application/json")):
                r = common.raw_get(f"{O}/packages/{sel}/report", format=fmt)
                if r:
                    col.download_button(f"⬇ Readiness report (.{fmt})", r.text, file_name=f"{sel}_readiness.{fmt}", mime=mime, key=f"dl_{fmt}")

# ------------------------------------------------------------------ 5 self-test
with t_test:
    st.markdown("The verifier is only trustworthy if a genuine package passes **and** every attack is caught by the check "
                "that is supposed to catch it. This builds packages with a throw-away key and changes no data.")
    if st.button("▶ Run attack self-test"):
        st.session_state["selftest"] = api("GET", f"{O}/self-test")
    res = st.session_state.get("selftest")
    if res:
        a, b, c = st.columns(3)
        a.metric("Attacks detected", f"{res['detected']} / {res['attacks']}")
        b.metric("Genuine package", "passes" if res["genuine_passes"] else "FAILS")
        c.metric("Overall", "✅ OK" if res["all_ok"] else "❌ PROBLEM")
        st.dataframe(pd.DataFrame([{"scenario": r["scenario"], "attack": r["attack"], "expected check": r["expected_check"],
                                    "result": ("✅ passes" if r["ok"] else "❌") if r["scenario"] == "genuine" else ("✅ detected" if r["detected"] else "❌ MISSED"),
                                    "verifier said": r["detail"]} for r in res["rows"]]), hide_index=True, use_container_width=True)

# ------------------------------------------------------------------ 6 evidence
with t_ev:
    st.caption("Coverage of the UN R155 Annex 5 threat groups from this project's records. Supports evidence preparation; "
               "it is not a compliance statement.")
    cov = api("GET", f"{C}/r155")
    if cov:
        color = {"addressed": "🟢", "in_progress": "🟡", "identified": "🟠", "monitored": "🟢", "gap": "🔴", "organisational": "⚪"}
        st.dataframe(pd.DataFrame([{"group": f"{c['id']} {c['title']}", "status": f"{color[c['status']]} {c['status']}",
                                    "TARA threats": len(c["threats"]), "evidence": " · ".join(c["evidence"]) or "–",
                                    "gaps": " · ".join(c["gaps"]) or "–"} for c in cov["categories"]]), hide_index=True, use_container_width=True)
    d1, d2 = st.columns(2)
    if d1.button("Build evidence pack"):
        st.session_state["ev_md"] = common.raw_get(f"{C}/evidence", format="md")
        st.session_state["ev_json"] = common.raw_get(f"{C}/evidence", format="json")
    if st.session_state.get("ev_md"):
        d1.download_button("⬇ Evidence pack (.md)", st.session_state["ev_md"].text, file_name=f"evidence_pack_{project}.md", mime="text/markdown")
        d2.download_button("⬇ Evidence pack (.json)", st.session_state["ev_json"].text, file_name=f"evidence_pack_{project}.json", mime="application/json")
        with st.expander("Preview"):
            st.markdown(st.session_state["ev_md"].text)
