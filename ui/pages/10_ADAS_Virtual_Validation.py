"""Phase 4: ADAS virtual validation of the AEB function (requirements, scenarios, MIL/SIL campaigns, release)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt
import pandas as pd
import streamlit as st

import common
from common import api, do, raw_get, status_badge

VERDICT = {"pass": "✅ pass", "fail": "❌ FAIL", "n/a": "– n/a", "not_verified": "⚠️ not verified", "not_covered": "🕳 not covered"}
CAL_UI = ["ttc_warn_s", "ttc_partial_s", "ttc_full_s", "areq_full_mps2", "a_full_mps2", "confirm_cycles"]

common.setup("ADAS Virtual Validation")
project = common.sidebar("Module: **ADAS virtual validation** (Phase 4). AEB requirements, Euro NCAP-style and SOTIF "
                         "scenarios, model- and software-in-the-loop campaigns, release with four-eyes.")
st.title("ADAS virtual validation: Autonomous Emergency Braking")
if not project:
    st.info("Create a project workspace in the sidebar.")
    st.stop()
A = f"/projects/{project}/adas"
stt = api("GET", f"{A}/status")
if not stt:
    st.stop()
ref_data = api("GET", f"{A}/reference") or {"calibration": [], "families": [], "states": {}}
CAL = {c["name"]: c for c in ref_data["calibration"]}

k = st.columns(5)
k[0].metric("Requirements approved", stt["requirements"]["approved"], help="AEB functional and SOTIF requirements")
k[1].metric("Scenarios approved", stt["scenarios"]["approved"], delta=f"{stt['scenarios']['proposed']} proposed" if stt["scenarios"]["proposed"] else None,
            delta_color="off")
sil = stt["sil"]
k[2].metric("C code (SIL)", "ready" if sil["available"] else "unavailable",
            help=f"firmware {sil['firmware_version']} · source hash {sil['firmware_hash'][:12]} · "
                 f"{'prebuilt library' if sil['prebuilt'] else 'compiler: ' + str(sil['compiler'])}")
lc = stt["latest_campaign"]
k[3].metric("Latest campaign", ("PASSED" if lc["passed"] else "FAILED") if lc else "–", help=lc["ref"] if lc else None)
rel = stt["current_release"]
k[4].metric("Released", rel["ref"] if rel else "nothing yet", help=f"approved by {rel['approved_by']}" if rel else None)
if not sil["available"]:
    st.warning("No C compiler and no prebuilt library on this server: campaigns run on the Python model only and cannot be "
               "released. Run `python scripts/sil_run.py campaign` locally or in CI for SIL evidence.")
if not stt["library_loaded"]:
    st.info("Start by loading the AEB requirement set and the scenario catalog (reviewer permission).")
    if st.button("Load AEB requirements and scenario catalog", type="primary"):
        do("POST", f"{A}/library/load")
    st.stop()


def cal_controls(key: str) -> dict:
    """Calibration what-if: returns only the values that differ from the default."""
    out = {}
    with st.expander("Calibration what-if (optional)"):
        st.caption("Change thresholds to see the effect on safety and nuisance braking. The firmware is unchanged; "
                   "the calibration is passed to the compiled code at start-up, like a calibration data set.")
        cols = st.columns(3)
        for i, name in enumerate(CAL_UI):
            c = CAL.get(name)
            if not c:
                continue
            col = cols[i % 3]
            if name == "confirm_cycles":
                v = col.slider(f"{name} ({c['unit']})", int(c["min"]), min(int(c["max"]), 60), int(c["default"]), key=f"{key}_{name}",
                               help=c["meaning"])
            else:
                v = col.slider(f"{name} ({c['unit']})", float(c["min"]), float(c["max"]), float(c["default"]), step=0.1,
                               key=f"{key}_{name}", help=c["meaning"])
            if abs(float(v) - float(c["default"])) > 1e-9:
                out[name] = v
        if out:
            st.caption("Changed: " + ", ".join(f"{n} {CAL[n]['default']} → {v}" for n, v in out.items()))
    return out


t_req, t_scn, t_run, t_cmp, t_fw = st.tabs(["1 · Requirements & coverage", "2 · Scenarios", "3 · Simulate a scenario",
                                            "4 · Validation campaign & release", "5 · Firmware & SIL"])

# ------------------------------------------------------------------ 1 requirements and coverage
with t_req:
    cov = api("GET", f"{A}/coverage") or {"requirements": [], "gaps": [], "covered": 0, "total": 0}
    st.markdown(f"**{cov['covered']} of {cov['total']}** requirements have at least one approved scenario.")
    if cov["gaps"]:
        st.warning(f"Not covered by any approved scenario: **{', '.join(cov['gaps'])}**. A campaign cannot pass while a "
                   f"release-blocking requirement is not covered.")
    if st.button("Propose scenarios for uncovered boundaries (boundary-value analysis)",
                 help="ISO 26262-6 lists boundary-value analysis for deriving test cases. Proposals need approval."):
        r = api("POST", f"{A}/scenarios/propose")
        if r:
            st.success(r["message"])
            for p in r["proposals"]:
                st.caption(f"{p['ref']} {p['id']}: {p['reason']}")
    st.dataframe(pd.DataFrame([{"ref": r["ref"], "requirement": r["id"], "title": r["title"], "status": status_badge(r["status"]),
                                "release-blocking": "yes" if r["release_blocking"] else "exploratory", "check": r["check"],
                                "approved scenarios": r["approved_scenarios"], "proposed": r["proposed_scenarios"]}
                               for r in cov["requirements"]]), hide_index=True, width="stretch")
    reqs = api("GET", f"{A}/requirements") or []
    with st.expander("Requirement details, review and change"):
        sel = st.selectbox("Requirement", [r["ref"] for r in reqs],
                           format_func=lambda x: next(r["payload"]["title"] for r in reqs if r["ref"] == x))
        rec = next((r for r in reqs if r["ref"] == sel), None)
        if rec:
            p = rec["payload"]
            st.markdown(f"{status_badge(rec['status'])} · **{p['category']}** · origin {p.get('origin', '')}  \n{p['text']}")
            st.caption(f"Check: `{p['check']}` {p['params'] or ''} · applies to {p['applies']}")
            if p.get("suspect_reason"):
                st.warning(p["suspect_reason"])
            editable = {k: p[k] for k in ("title", "text", "category", "release_blocking", "check", "params", "applies")}
            txt = st.text_area("Edit (JSON). Saving an approved requirement makes it need re-approval and flags released "
                               "campaigns that used it.", json.dumps(editable, indent=2), height=220, key=f"edit_{sel}")
            b1, b2, b3 = st.columns(3)
            if b1.button("Save change", key=f"save_{sel}"):
                try:
                    do("PUT", f"{A}/requirements/{sel}", json={"requirement": json.loads(txt)})
                except json.JSONDecodeError as e:
                    st.error(f"Invalid JSON: {e}")
            if rec["status"] in ("proposed", "assessed", "suspect"):
                cm = st.text_input("Review comment", key=f"rc_{sel}")
                if b2.button("Approve", key=f"ra_{sel}"):
                    do("POST", f"{A}/requirements/{sel}/decision", json={"decision": "approved", "comment": cm})
                if b3.button("Reject", key=f"rr_{sel}"):
                    do("POST", f"{A}/requirements/{sel}/decision", json={"decision": "rejected", "comment": cm})

# ------------------------------------------------------------------ 2 scenarios
with t_scn:
    scns = api("GET", f"{A}/scenarios") or []
    pending = [s for s in scns if s["status"] in ("proposed", "suspect")]
    st.subheader("Describe a scenario in plain language")
    c1, c2 = st.columns([4, 1])
    text = c1.text_input("Description", placeholder="Truck stopped in fog at 60 km/h on a wet road",
                         help="Speeds in km/h, gaps in m, deceleration in m/s². Conditions: wet, snow, fog, latency in ms, phantom detection.")
    use_llm = c2.checkbox("Use the LLM", help="Only with a real LLM backend. Its values are accepted only within bounds and "
                                               "if the number appears in your description.")
    if st.button("Create proposal") and text:
        r = api("POST", f"{A}/scenarios/from-text", json={"text": text, "use_llm": use_llm})
        if r:
            sc = r["scenario"]
            st.success(f"{sc['ref']} proposed as family **{sc['payload']['family']}**. A second person must approve it.")
            st.markdown("**Understood:** " + "; ".join(r.get("understood", [])))
            if r.get("assumptions"):
                st.markdown("**Assumed:** " + "; ".join(r["assumptions"]))
            if r.get("not_modelled"):
                st.markdown("**Not represented by the simulation:** " + "; ".join(r["not_modelled"]))
            if r.get("llm"):
                st.caption(f"LLM: {r['llm']}")
            st.json(sc["payload"]["scenario"], expanded=False)
    st.subheader(f"Proposals waiting for review ({len(pending)})")
    for s in pending:
        p = s["payload"]
        with st.expander(f"{status_badge(s['status'])} {p['title']} · {p['origin']} · by {s['updated_by']}"):
            for key, label in (("proposal_reason", "Why"), ("description", "Description"), ("understood", "Understood"),
                               ("assumptions", "Assumed"), ("not_modelled", "Not modelled"), ("llm", "LLM")):
                if p.get(key):
                    st.markdown(f"**{label}:** " + ("; ".join(p[key]) if isinstance(p[key], list) else str(p[key])))
            st.json(p["scenario"], expanded=False)
            cm = st.text_input("Comment", key=f"sc_c_{s['ref']}")
            a1, a2 = st.columns(2)
            if a1.button("Approve", key=f"sc_a_{s['ref']}"):
                do("POST", f"{A}/scenarios/{s['ref']}/decision", json={"decision": "approved", "comment": cm})
            if a2.button("Reject", key=f"sc_r_{s['ref']}"):
                do("POST", f"{A}/scenarios/{s['ref']}/decision", json={"decision": "rejected", "comment": cm})
    st.subheader("All scenarios")
    fams = sorted({s["payload"]["family"] for s in scns})
    f1, f2 = st.columns(2)
    fam = f1.selectbox("Family", [""] + fams, format_func=lambda x: x or "all")
    stf = f2.selectbox("Status", ["", "approved", "proposed", "suspect", "rejected"], format_func=lambda x: x or "all")
    rows = []
    for s in scns:
        sc = s["payload"]["scenario"]
        if (fam and s["payload"]["family"] != fam) or (stf and s["status"] != stf):
            continue
        rows.append({"ref": s["ref"], "id": sc["id"], "family": s["payload"]["family"], "status": status_badge(s["status"]),
                     "origin": s["payload"]["origin"], "ego km/h": sc["ego_kmh"], "target": sc["target"],
                     "target km/h": sc.get("target_kmh"), "gap m": sc.get("gap_m"), "friction": sc.get("mu"),
                     "radar range m": sc.get("sensor_range_m"), "latency s": sc.get("latency_s"), "source": sc.get("source")})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    r = raw_get(f"{A}/openscenario.zip")
    if r is not None:
        st.download_button("⬇ Approved scenarios as ASAM OpenSCENARIO 1.1 (.zip with OpenDRIVE road)", r.content,
                           file_name=f"{project}_aeb_scenarios.zip", mime="application/zip")

# ------------------------------------------------------------------ 3 one scenario
with t_run:
    scns = api("GET", f"{A}/scenarios") or []
    pick = st.selectbox("Scenario", [s["ref"] for s in scns], format_func=lambda x: next(f"{s['payload']['title']} ({s['status']})"
                                                                                       for s in scns if s["ref"] == x))
    target = st.radio("Run on", ["model", "sil"], horizontal=True, disabled=not sil["available"],
                      format_func=lambda x: {"model": "Python model (MIL)", "sil": "Compiled C code (SIL)"}[x])
    overrides = cal_controls("run")
    if st.button("Simulate", type="primary") and pick:
        st.session_state["adas_run"] = api("POST", f"{A}/scenarios/{pick}/run", json={"target": target, "calibration": overrides})
    res = st.session_state.get("adas_run")
    if res and res["scenario"]["id"] == next((s["payload"]["scenario"]["id"] for s in scns if s["ref"] == pick), None):
        m = res["metrics"]
        c = st.columns(6)
        c[0].metric("Collision", f"YES {m['impact_kmh']} km/h" if m["collided"] else "no")
        c[1].metric("Stopped gap", f"{m['stop_gap_m']} m" if m["stop_gap_m"] is not None else "–")
        c[2].metric("Closest gap", f"{m['min_gap_m']:.2f} m" if m["min_gap_m"] is not None else "–")
        c[3].metric("Warning at", f"{m['fcw_t']} s" if m["fcw_t"] is not None else "–")
        c[4].metric("Braking at", f"{m['brake_t']} s" if m["brake_t"] is not None else "–")
        c[5].metric("Full braking at", f"{m['full_t']} s" if m["full_t"] is not None else "–")
        if res["calibration_changes"]:
            st.caption("Calibration what-if: " + ", ".join(f"{k} {v['default']} → {v['value']}" for k, v in res["calibration_changes"].items()))
        df = pd.DataFrame(res["trace"])
        if not df.empty:
            states = {int(k): v for k, v in res["states"].items()}
            df["AEB state"] = df["state"].map(states)
            g1, g2 = st.columns(2)
            speed = df.melt(id_vars="t", value_vars=[c for c in ("v_ego_kmh", "v_target_kmh") if df[c].notna().any()],
                            var_name="signal", value_name="km/h")
            g1.altair_chart(alt.Chart(speed).mark_line().encode(x=alt.X("t", title="time (s)"), y="km/h", color="signal")
                            .properties(title="Speeds", height=260), use_container_width=True)
            if df["gap"].notna().any():
                g2.altair_chart(alt.Chart(df).mark_line(color="#666").encode(x=alt.X("t", title="time (s)"), y=alt.Y("gap", title="gap (m)"))
                                .properties(title="Gap to the object", height=260), use_container_width=True)
            br = df.melt(id_vars=["t", "AEB state"], value_vars=["brake_req", "decel"], var_name="signal", value_name="m/s²")
            st.altair_chart(alt.Chart(br).mark_line().encode(x=alt.X("t", title="time (s)"), y="m/s²", color="signal",
                                                             tooltip=["t", "signal", "m/s²", "AEB state"])
                            .properties(title="Brake request and actual deceleration", height=240), use_container_width=True)
        if res["verdicts"]:
            st.dataframe(pd.DataFrame([{"requirement": v["requirement"], "title": v["title"], "verdict": VERDICT[v["verdict"]],
                                        "detail": v["detail"], "release-blocking": v["release_blocking"]} for v in res["verdicts"]]),
                         hide_index=True, width="stretch")
        r = raw_get(f"{A}/scenarios/{pick}/openscenario")
        if r is not None:
            st.download_button("⬇ This scenario as OpenSCENARIO (.xosc)", r.content, file_name=f"{res['scenario']['id']}.xosc")

# ------------------------------------------------------------------ 4 campaigns and release
with t_cmp:
    st.subheader("Run a validation campaign")
    o1, o2, o3 = st.columns(3)
    tgt = o1.selectbox("Target", ["auto", "sil", "model"], format_func=lambda x: {"auto": "auto (SIL if available)", "sil": "compiled C code (SIL)",
                                                                                  "model": "Python model (MIL)"}[x])
    b2b = o2.checkbox("Back-to-back: C code vs model", value=True)
    mut = o3.checkbox("Mutation testing (11 mutants, slower)", value=False)
    overrides_c = cal_controls("cmp")
    note = st.text_input("Note (e.g. what changed)", max_chars=500)
    if st.button("Run campaign", type="primary"):
        with st.spinner("Simulating every approved scenario…"):
            r = api("POST", f"{A}/campaigns", json={"target": tgt, "back_to_back": b2b, "mutation": mut,
                                                     "calibration": overrides_c, "note": note})
        if r:
            st.session_state["adas_cmp"] = r["ref"]
            st.rerun()
    camps = api("GET", f"{A}/campaigns") or []
    if camps:
        st.dataframe(pd.DataFrame([{"ref": c["ref"], "status": status_badge(c["status"]), "result": "PASSED" if c["passed"] else "FAILED",
                                    "target": c["target"], "not passed": ", ".join(c["blocking_not_passed"]),
                                    "SOTIF findings": ", ".join(c["exploratory_findings"]),
                                    "back-to-back": {True: "✅", False: "❌", None: "–"}[c["back_to_back"]],
                                    "mutation score": f"{c['mutation_score']:.0%}" if c["mutation_score"] is not None else "–",
                                    "calibration": "changed" if c["calibration_changes"] else "default",
                                    "out of date": "; ".join(c["stale"]) or c["suspect_reason"], "run by": c["run_by"],
                                    "released by": c["approved_by"] or ""} for c in camps]), hide_index=True, width="stretch")
        refs = [c["ref"] for c in camps]
        cur = st.session_state.get("adas_cmp")
        sel = st.selectbox("Campaign details", refs, index=refs.index(cur) if cur in refs else 0)
        cd = api("GET", f"{A}/campaigns/{sel}")
        if cd:
            p = cd["payload"]
            v = p["verdict"]
            (st.success if v["passed"] else st.error)(
                f"**{p['title']}** · release-blocking requirements passed: "
                f"{v.get('blocking_total', 0) - len(v['blocking_not_passed'])} of {v.get('blocking_total', 0)} · "
                f"exploratory (SOTIF) findings: {len(v['exploratory_findings'])} · {len(p['scenario_set'])} scenarios · {p['seconds']} s")
            if v["blocking_not_passed"]:
                st.markdown(f"Release-blocking not passed: **{', '.join(v['blocking_not_passed'])}**")
            if v["exploratory_findings"]:
                st.info(f"SOTIF findings (exploratory, analyse as triggering conditions): **{', '.join(v['exploratory_findings'])}**")
            if cd["stale"]:
                st.warning("Out of date: " + "; ".join(cd["stale"]))
            for n in p["notes"]:
                st.caption(n)
            st.dataframe(pd.DataFrame([{"requirement": r["id"], "title": r["title"], "verdict": VERDICT[r["verdict"]],
                                        "release-blocking": r["release_blocking"], "pass": r["counts"]["pass"], "fail": r["counts"]["fail"],
                                        "n/a": r["counts"]["n/a"],
                                        "first failure": next((f"{x['scenario']}: {x['detail']}" for x in r["scenarios"] if x["verdict"] == "fail"), "")}
                                       for r in p["requirements"]]), hide_index=True, width="stretch")
            bb = p.get("back_to_back")
            if bb:
                (st.success if bb["passed"] else st.error)(
                    f"Back-to-back {'passed' if bb['passed'] else 'FAILED'}: {bb['steps']:,} time steps in {bb['scenarios']} scenarios; "
                    f"state and warning identical, largest brake-request difference {bb['max_brake_diff']} m/s²"
                    + (f"; calibration mismatch {bb['calibration_mismatch']}" if bb.get("calibration_mismatch") else ""))
            mu = p.get("mutation")
            if mu:
                st.markdown(f"**Mutation testing:** {mu['killed']} of {mu['killed'] + mu['survived']} mutants killed by the "
                            f"release-blocking requirements (score **{mu['score']:.0%}**); back-to-back catches "
                            f"{mu['survivors_caught_by_b2b']} of {mu['survived']} survivors.")
                st.dataframe(pd.DataFrame([{"mutant": x["id"], "change": x["description"], "result": x["status"],
                                            "killed by": ", ".join(x.get("killed_by", [])), "scenario": x.get("scenario", ""),
                                            "back-to-back detects": {True: "yes", False: "no"}.get(x.get("b2b_detected"), "")}
                                           for x in mu["rows"]]), hide_index=True, width="stretch")
            with st.expander("Scenario results"):
                st.dataframe(pd.DataFrame([{"scenario": s["id"], "collision": s["metrics"]["collided"],
                                            "impact km/h": s["metrics"]["impact_kmh"] or None, "stop gap m": s["metrics"]["stop_gap_m"],
                                            "closest gap m": s["metrics"]["min_gap_m"], "warning s": s["metrics"]["fcw_t"],
                                            "braking s": s["metrics"]["brake_t"], "full s": s["metrics"]["full_t"]}
                                           for s in p["scenarios"]]), hide_index=True, width="stretch")
            d1, d2 = st.columns(2)
            for col, fmt in ((d1, "md"), (d2, "json")):
                rr = raw_get(f"{A}/campaigns/{sel}/report", format=fmt)
                if rr is not None:
                    col.download_button(f"⬇ Validation report (.{fmt})", rr.content, file_name=f"{sel}_validation.{fmt}")
            if cd["status"] == "proposed":
                st.markdown("**Release decision** (a second person; needs SIL evidence, every release-blocking requirement "
                            "passed, back-to-back passed and nothing changed since the run)")
                cm = st.text_input("Release comment (what you reviewed)", key=f"rel_{sel}")
                r1, r2 = st.columns(2)
                if r1.button("Release", key=f"rel_a_{sel}", type="primary"):
                    do("POST", f"{A}/campaigns/{sel}/decision", json={"decision": "approved", "comment": cm})
                if r2.button("Reject", key=f"rel_r_{sel}"):
                    do("POST", f"{A}/campaigns/{sel}/decision", json={"decision": "rejected", "comment": cm})
            elif cd["status"] == "approved":
                st.success(f"Released by {cd['approved_by']} at {(cd['approved_at'] or '')[:16]}: {p.get('decision_comment', '')}")
            elif cd["status"] == "suspect":
                st.warning(f"Release evidence no longer valid: {p.get('suspect_reason', '')}")

# ------------------------------------------------------------------ 5 firmware
with t_fw:
    fw = api("GET", f"{A}/firmware")
    if fw:
        s = fw["sil"]
        st.markdown(f"Firmware **{s['firmware_version']}** · source hash `{s['firmware_hash'][:16]}` · "
                    f"{'prebuilt SIL library found' if s['prebuilt'] else ('compiler ' + s['compiler']) if s['compiler'] else 'no compiler'}")
        if fw["review_findings"]:
            st.warning(f"{len(fw['review_findings'])} findings from the platform's code review analyzer")
            st.dataframe(pd.DataFrame(fw["review_findings"]), hide_index=True, width="stretch")
        else:
            st.success("Code review analyzer (Secure Code Review module): 0 findings on the AEB source "
                       "(no dynamic memory, no recursion, every function at or below complexity 10).")
        st.markdown("**How SIL works here:** the C file is compiled into a shared library (`cc -std=c99 -O2 -shared`) and called "
                    "through ctypes with the same inputs as the Python model, every 10 ms of simulated time. Builds are "
                    "cached by source hash, which every campaign records as evidence. Only repository code and the "
                    "predefined mutants are ever compiled; nothing uploaded by users.")
        for f in fw["files"]:
            with st.expander(f"{f['name']} · {f['lines']} lines · sha256 {f['sha256'][:12]}"):
                st.code(f["text"], language="c")
        st.markdown("**Mutants used for mutation testing**")
        st.dataframe(pd.DataFrame(fw["mutants"]), hide_index=True, width="stretch")
