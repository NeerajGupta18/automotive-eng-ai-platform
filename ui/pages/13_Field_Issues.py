"""Phase 6: field issues. Customer complaints with vehicle recordings, investigated by the engineering agent:
replay on the field software, reconstruction, root cause, verified fix, hazard rating, corrective actions, 8D."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt
import pandas as pd
import streamlit as st

import common
from common import api, raw_get

CLASS_ICON = {"implementation_defect": "🐞", "calibration_regression": "🎛️", "sensor_false_target": "👻", "correct_intervention": "✅",
              "system_limitation": "📏", "insufficient_data": "❔", "needs_engineer": "🧑‍🔧"}
SHORT = {"implementation_defect": "🐞 code defect", "calibration_regression": "🎛️ calibration", "sensor_false_target": "👻 phantom object",
         "correct_intervention": "✅ correct braking", "system_limitation": "📏 outside limits", "insufficient_data": "❔ no data",
         "needs_engineer": "🧑‍🔧 engineer"}
STATUS = {"open": "open", "actions_taken": "actions taken", "approved": "closed (resolved)", "rejected": "closed (no fix)"}
RUN = {"waiting": "⏸️ waiting for a person", "completed": "✅ completed", "failed": "❗ failed", "cancelled": "⏹ cancelled", "running": "⏳ running"}

common.setup("Field Issues")
project = common.sidebar("Module: **Field issues** (Phase 6). Customer complaints with vehicle recordings are investigated by the "
                         "engineering agent; people confirm the hazard rating and the actions.")
st.title("Field issues: from complaint to verified fix")
if not project:
    st.info("Create a project workspace in the sidebar.")
    st.stop()
P = f"/projects/{project}"
F = f"{P}/field"

ov = api("GET", f"{F}/overview") or {}
m = st.columns(5)
m[0].metric("Complaints", ov.get("complaints", 0))
m[1].metric("Issues", ov.get("issues", 0))
m[2].metric("Open", ov.get("open", 0))
m[3].metric("Waiting for a person", ov.get("waiting_for_people", 0))
m[4].metric("Builds in the registry", ov.get("builds", 0))

t_inbox, t_inv, t_close, t_more = st.tabs(["1 · Inbox", "2 · Investigation", "3 · Verification and 8D", "4 · Builds and agent quality"])

# ============================================================================ 1 inbox
with t_inbox:
    issues = api("GET", f"{F}/issues") or []
    if not ov.get("complaints"):
        st.info("No complaints yet. Load the demo: six dealer complaints with event recordings from three AEB builds (synthetic).")
        if st.button("Load the demo complaints", type="primary"):
            with st.spinner("Filing complaints; the agent investigates each new issue…"):
                r = api("POST", f"{F}/samples/load")
            if r:
                st.success(f"{len(r['complaints'])} complaints → {len(r['issues'])} issues; {len(r['runs'])} investigation(s) started.")
                st.rerun()
    if issues:
        st.subheader("Issues (complaints grouped by symptom and software build)")
        st.dataframe(pd.DataFrame([{"issue": i["ref"], "title": i["title"], "complaints": i["complaints"], "status": STATUS.get(i["status"], i["status"]),
                                    "root cause": (CLASS_ICON.get(i["class"], "") + " " + (i["class"] or "").replace("_", " ")) if i["class"] else "–",
                                    "agent": f"{i['run']} {RUN.get(i['run_status'], '')}" if i["run"] else "–",
                                    "waiting at": i["waiting_for"]["tool"] if i["waiting_for"] else "", "ASIL": i["asil"] or ""} for i in issues]),
                     hide_index=True, width="stretch")
    with st.expander("File a complaint", expanded=not issues):
        builds = api("GET", f"{F}/builds", quiet=True) or []
        with st.form("complaint"):
            text = st.text_area("What the customer reported", placeholder="Car braked hard on its own under a flyover at 80 km/h…", max_chars=4000)
            c1, c2, c3 = st.columns(3)
            vin = c1.text_input("VIN", max_chars=20)
            build = c2.selectbox("Software build", [""] + [b["payload"]["build"]["version"] for b in builds],
                                 format_func=lambda x: x or "from the recording")
            dealer = c3.text_input("Dealer", max_chars=80)
            up = st.file_uploader("Event recording (CSV from the data recorder)", type=["csv"])
            go = st.form_submit_button("File complaint", type="primary")
        if go:
            files = {"file": (up.name, up.getvalue(), "text/csv")} if up else None
            with st.spinner("Filing; a new issue starts the agent's investigation…"):
                r = api("POST", f"{F}/complaints", data={"text": text, "vin": vin, "build": build, "dealer": dealer}, files=files)
            if r:
                st.success(f"{r['complaint']['ref']} filed → {r['issue']['ref']}" + (f"; investigation {r['run']['ref']} {r['run']['status']}" if r.get("run") else ""))
                st.session_state["field_issue"] = r["issue"]["ref"]
        st.caption("Recording format: data/samples/field/logs/*.csv (one row per 10 ms: speed, radar object, driver brake, AEB state and "
                   "outputs). The sample files can be uploaded here.")
    comps = api("GET", f"{F}/complaints") or []
    if comps:
        st.subheader("Complaints")
        st.dataframe(pd.DataFrame([{"complaint": c["ref"], "issue": c["payload"].get("issue"), "build": c["payload"]["build"],
                                    "symptom": c["payload"]["symptom"].replace("_", " "), "where": c["payload"]["understood"]["location"],
                                    "km/h": c["payload"]["understood"]["speed_kmh"], "recording": "📈" if c["payload"].get("log") else "–",
                                    "text": c["payload"]["text"], "reported": (c["payload"].get("reported_at") or "")[:10]} for c in comps]),
                     hide_index=True, width="stretch")


def pick_issue(key: str) -> str | None:
    refs = sorted(i["ref"] for i in issues)
    if not refs:
        st.info("No issues yet (tab 1).")
        return None
    cur = st.session_state.get("field_issue") or next((i["ref"] for i in sorted(issues, key=lambda x: x["ref"]) if i["waiting_for"]), refs[0])
    sel = st.selectbox("Issue", refs, index=refs.index(cur) if cur in refs else 0, key=key,
                       format_func=lambda r: next(f"{i['ref']} · {i['title']} ({i['complaints']} complaint(s))" for i in issues if i["ref"] == r))
    st.session_state["field_issue"] = sel
    return sel


def checkpoint(run: dict) -> None:
    st_ = run["payload"]["steps"][-1]
    a = st_.get("args") or {}
    st.warning(f"**Human checkpoint {st_['n']}: `{st_['tool']}`** · this step changes something, so it waits for a person and runs under "
               f"**your** name. {st_['why']}")
    if st_["tool"] == "record_safety_assessment":
        c = st.columns(4)
        c[0].metric("Severity", f"S{a.get('severity')}")
        c[1].metric("Exposure", f"E{a.get('exposure')}")
        c[2].metric("Controllability", f"C{a.get('controllability')}")
        c[3].metric("Escalate", "yes" if a.get("escalate") else "no")
        st.caption(a.get("rationale", ""))
    else:
        st.markdown(a.get("summary", ""))
        if a.get("calibration"):
            st.markdown("Calibration to verify: " + ", ".join(f"`{k} = {v:g}`" for k, v in a["calibration"].items()))
        if a.get("rollback_to"):
            st.markdown(f"Containment: roll back to **AEB {a['rollback_to']}**")
        if a.get("propose_scenarios"):
            st.markdown("The reconstructed field situations are proposed as regression scenarios (a reviewer approves them).")
        if a.get("tickets"):
            st.dataframe(pd.DataFrame([{"ticket": t["summary"], "priority": t["priority"]} for t in a["tickets"]]), hide_index=True, width="stretch")
    cm = st.text_input("Comment (optional)", key=f"fcm_{run['ref']}_{st_['n']}")
    b1, b2 = st.columns(2)
    if b1.button("Confirm and run this step", key=f"frun_{run['ref']}_{st_['n']}", type="primary"):
        if api("POST", f"{P}/agent/runs/{run['ref']}/steps/{st_['n']}/confirm", json={"decision": "run", "comment": cm}):
            st.rerun()
    if b2.button("Skip it", key=f"fskip_{run['ref']}_{st_['n']}"):
        if api("POST", f"{P}/agent/runs/{run['ref']}/steps/{st_['n']}/confirm", json={"decision": "skip", "comment": cm}):
            st.rerun()


def signals_chart(cref: str) -> None:
    sig = api("GET", f"{F}/complaints/{cref}/trace", quiet=True)
    if not sig:
        st.caption("No recording for this complaint.")
        return
    frames = [pd.DataFrame(sig["recorded"]).assign(source="recorded in the vehicle")]
    if sig.get("reconstructed"):
        frames.append(pd.DataFrame(sig["reconstructed"]).assign(source="reconstructed (field build)"))
    if sig.get("with_fix"):
        frames.append(pd.DataFrame(sig["with_fix"]).assign(source="reconstructed with the fix"))
    df = pd.concat(frames)
    st.altair_chart(alt.Chart(df).mark_line().encode(x=alt.X("t", title="time (s)"), y=alt.Y("brake_req", title="m/s²"), color="source",
                                                     strokeDash="source")
                    .properties(title="AEB brake request", height=230), use_container_width=True)
    g1, g2 = st.columns(2)
    g1.altair_chart(alt.Chart(df).mark_line().encode(x=alt.X("t", title="time (s)"), y=alt.Y("ego_kmh", title="km/h"),
                                                     color=alt.Color("source", legend=None), strokeDash=alt.StrokeDash("source", legend=None))
                    .properties(title="Vehicle speed", height=230), use_container_width=True)
    rec = pd.DataFrame(sig["recorded"]).dropna(subset=["obj_dist_m"])
    if not rec.empty:
        g2.altair_chart(alt.Chart(rec).mark_point(size=10, filled=True, color="#c0392b")
                        .encode(x=alt.X("t", title="time (s)", scale=alt.Scale(domain=[float(df["t"].min()), float(df["t"].max())])),
                                y=alt.Y("obj_dist_m", title="m"))
                        .properties(title="Object reported by the radar (distance)", height=230), use_container_width=True)
    if sig.get("fix"):
        st.caption("Fix calibration: " + ", ".join(f"{k} = {v:g}" for k, v in sig["fix"].items()))


# ============================================================================ 2 investigation
with t_inv:
    ref = pick_issue("inv_pick")
    if ref:
        data = api("GET", f"{F}/issues/{ref}") or {}
        s = data.get("summary", {})
        run = data.get("run")
        c = st.columns(4)
        c[0].metric("Status", STATUS.get(s.get("status"), s.get("status")))
        c[1].metric("Root cause", SHORT.get(s.get("class"), "–"))
        c[2].metric("Agent run", f"{s.get('run') or '–'}")
        c[2].caption(RUN.get(s.get("run_status"), ""))
        c[3].metric("Hazard rating", f"ASIL {s['asil']}" if s.get("asil") else "not confirmed")
        if s.get("new_evidence"):
            st.info("New complaints joined this issue after the last investigation.")
        if run and run["status"] == "waiting":
            checkpoint(run)
        if run:
            o = run["payload"].get("outcome")
            if o:
                (st.success if run["status"] == "completed" else st.warning)(o["summary"])
            with st.expander(f"What the agent did ({len(run['payload']['steps'])} steps; read steps ran on their own)", expanded=not o):
                for stp in run["payload"]["steps"]:
                    icon = {"done": "✅", "awaiting_confirmation": "⏸️", "skipped": "⏭", "error": "❗", "blocked": "🛡️"}.get(stp["status"], "•")
                    st.markdown(f"{icon} **{stp['n']}. `{stp['tool']}`** ({stp.get('kind')}) · {stp['why']}"
                                + (f" · {stp['ms']} ms" if stp.get("ms") is not None else "") + (f" · by {stp['by']}" if stp.get("by") else ""))
            if o:
                st.markdown("**Findings**\n" + "\n".join(f"- {f}" for f in o["findings"]))
        an = api("GET", f"{F}/issues/{ref}/analysis") or {}
        rows = an.get("complaints", [])
        if rows:
            st.subheader("Root cause per complaint")
            st.dataframe(pd.DataFrame([{"complaint": r["ref"], "class": CLASS_ICON.get(r["diagnosis"]["class"], "") + " " + r["diagnosis"]["class"].replace("_", " "),
                                        "confidence": r["diagnosis"]["confidence"],
                                        "reproduced": f"{r['diagnosis']['reproduced']['state_agreement']:.1%}" if r["diagnosis"].get("reproduced") else "–",
                                        "reconstruction": ("matched" if r["diagnosis"]["reconstruction"]["matched"] else "partial") if r["diagnosis"].get("reconstruction") else "–",
                                        "without AEB": ("collision" if (r["diagnosis"].get("counterfactual") or {}).get("collision") else "no collision")
                                        if r["diagnosis"].get("counterfactual") else "–",
                                        "customer said": r["text"]} for r in rows]), hide_index=True, width="stretch")
            cref = st.selectbox("Complaint", [r["ref"] for r in rows], key="inv_c")
            row = next(r for r in rows if r["ref"] == cref)
            st.markdown("\n".join(f"- {e}" for e in row["diagnosis"]["evidence"]))
            signals_chart(cref)
        fx = an.get("fix")
        if fx:
            st.subheader("Fix search" if fx["kind"] != "rollback" else "Containment: rollback search")
            st.markdown(fx["summary"])
            if fx.get("candidates"):
                st.dataframe(pd.DataFrame(fx["candidates"]), hide_index=True, width="stretch")

# ============================================================================ 3 verification, closure, 8D
with t_close:
    ref = pick_issue("close_pick")
    if ref:
        data = api("GET", f"{F}/issues/{ref}") or {}
        p, s = data.get("payload", {}), data.get("summary", {})
        act = p.get("actions")
        if not act:
            st.info("The corrective actions have not been confirmed yet (agent checkpoint on tab 2).")
        else:
            st.markdown(f"Actions confirmed by **{act['by']}**: {act.get('summary', '')}")
            scns = act.get("scenarios") or []
            if scns:
                st.markdown("**1. Field scenarios for the regression library** (approved by a reviewer other than the person who proposed them)")
                sc_rows = []
                for x in scns:
                    rec = api("GET", f"{P}/adas/scenarios/{x['ref']}", quiet=True) or {}
                    sc_rows.append({"scenario": x["ref"], "id": x["id"], "status": rec.get("status"), "title": rec.get("payload", {}).get("title")})
                st.dataframe(pd.DataFrame(sc_rows), hide_index=True, width="stretch")
                if any(r["status"] == "proposed" for r in sc_rows) and st.button("Approve these scenarios (as reviewer)"):
                    for r in sc_rows:
                        if r["status"] == "proposed":
                            api("POST", f"{P}/adas/scenarios/{r['scenario']}/decision", json={"decision": "approved", "comment": "field scenario reviewed"})
                    st.rerun()
            if act.get("calibration"):
                st.markdown("**2. Verification campaign** with " + ", ".join(f"`{k} = {v:g}`" for k, v in act["calibration"].items()))
                ver = p.get("verification")
                if ver:
                    camp = api("GET", f"{P}/adas/campaigns/{ver['campaign']}", quiet=True) or {}
                    cp = camp.get("payload", {})
                    st.markdown(f"{ver['campaign']} on {cp.get('target', '').upper()}: **{'PASSED' if cp.get('verdict', {}).get('passed') else 'FAILED'}**, "
                                f"status **{camp.get('status')}**" + (f" (released by {camp.get('approved_by')})" if camp.get("status") == "approved" else ""))
                    if camp.get("status") == "proposed":
                        if cp.get("verdict", {}).get("blocking_not_passed"):
                            st.warning("Not releasable: " + ", ".join(cp["verdict"]["blocking_not_passed"]) + " not passed (close the coverage gaps on the ADAS page).")
                        if st.button("Release the verification campaign (second person)"):
                            if api("POST", f"{P}/adas/campaigns/{ver['campaign']}/decision", json={"decision": "approved", "comment": f"verification of {ref} reviewed"}):
                                st.rerun()
                if st.button("Run the verification campaign" if not ver else "Run it again", type="primary" if not ver else "secondary"):
                    with st.spinner("SIL campaign on every approved scenario…"):
                        if api("POST", f"{F}/issues/{ref}/verify"):
                            st.rerun()
            if s.get("status") not in ("approved", "rejected"):
                st.markdown("**3. Close the issue** (a second person; a technical fix needs the released verification)")
                with st.form("close"):
                    dec = st.radio("Decision", ["approved", "rejected"], horizontal=True,
                                   format_func=lambda x: {"approved": "resolved", "rejected": "closed without a fix"}[x])
                    cm = st.text_input("What was checked", max_chars=1000)
                    if st.form_submit_button("Close issue"):
                        if api("POST", f"{F}/issues/{ref}/decision", json={"decision": dec, "comment": cm}):
                            st.rerun()
        st.divider()
        rep = raw_get(f"{F}/issues/{ref}/report", format="md")
        if rep is not None:
            d1, d2 = st.columns(2)
            d1.download_button("⬇ 8D report (.md)", rep.content, file_name=f"{ref}_8D.md")
            js = raw_get(f"{F}/issues/{ref}/report", format="json")
            if js is not None:
                d2.download_button("⬇ Investigation data (.json)", js.content, file_name=f"{ref}_investigation.json")
            with st.expander("8D report", expanded=True):
                st.markdown(rep.text)

# ============================================================================ 4 builds and agent quality
with t_more:
    builds = api("GET", f"{F}/builds") or []
    st.subheader("Software builds in the field")
    if builds:
        st.dataframe(pd.DataFrame([{"version": b["payload"]["build"]["version"], "firmware": b["payload"]["build"]["firmware"],
                                    "calibration": ", ".join(f"{k}={v}" for k, v in b["payload"]["build"]["calibration"].items()) or "default",
                                    "vehicles": b["payload"]["build"]["vehicles"], "validated": b["payload"]["build"]["validated"],
                                    "evidence": b["payload"]["build"]["validation_evidence"], "notes": b["payload"]["build"]["notes"]} for b in builds]),
                     hide_index=True, width="stretch")
    else:
        st.caption("No builds registered (loading the demo complaints registers the sample builds).")
    st.subheader("How well does the agent find the root cause?")
    ev = api("GET", f"{F}/evaluation", quiet=True)
    if ev:
        k = st.columns(4)
        k[0].metric("Root cause correct", f"{ev['metrics']['root_cause_accuracy']:.0%}")
        k[0].caption(f"{ev['cases']} labelled cases")
        k[1].metric("Reproduced", f"{ev['metrics']['reproduction_rate']:.0%}")
        k[2].metric("Verified fix / rollback", f"{ev['metrics']['fix_success_rate']:.0%}")
        k[3].metric("False defects", ev["metrics"]["false_defects"])
        st.dataframe(pd.DataFrame([{"case": r["case"], "build": r["build"], "expected": r["expected"], "agent": r["got"], "correct": r["correct"],
                                    "parameter": r["culprits"], "fix": r.get("fix", ""), "reproduced": r.get("reproduced"), "s": r["seconds"]}
                                   for r in ev["rows"]]), hide_index=True, width="stretch")
        st.caption(ev.get("note", ""))
    else:
        st.caption("Run `python scripts/eval_field_agent.py` to measure the agent on the labelled cases in eval/field.")
