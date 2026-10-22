"""8D report of a field issue (the automotive problem-solving format: D1 team ... D8 closure), filled from the
investigation, the confirmed decisions and the verification evidence. A draft until the issue is closed."""
from __future__ import annotations

from app.modules.field import investigate as I
from app.modules.field.triage import SYMPTOMS

ROLES = {"implementation_defect": "AEB software team", "calibration_regression": "Calibration engineer",
         "sensor_false_target": "Radar sensor supplier", "correct_intervention": "Customer service",
         "system_limitation": "Customer service / product owner", "insufficient_data": "Dealer service / field quality",
         "needs_engineer": "ADAS function owner"}
CONTAINMENT = {
    "calibration_regression": "Stop further over-the-air rollout of the build until the corrected calibration is released; inform dealers.",
    "implementation_defect": "Roll the affected vehicles back to the last validated build over the air; stop the pilot rollout.",
    "sensor_false_target": "Inform dealers and the sensor supplier; monitor the fleet for the same signature; no software rollback "
                           "(the validated build behaves the same).",
    "correct_intervention": "None needed: the function worked as designed. Explain the intervention to the customer.",
    "system_limitation": "None technical: explain the specified limits to the customer (owner's manual).",
    "insufficient_data": "None yet: obtain the event recording.",
    "needs_engineer": "To be decided by the ADAS function owner.",
}


def _c(x) -> str:
    return str(x if x is not None else "").replace("|", "\\|").replace("\n", " ")


def eight_d(project: str, data: dict, a: dict, fix: dict | None, camp: dict | None) -> str:
    p, s = data["payload"], data["summary"]
    agg = a["aggregate"]
    act = p.get("actions") or {}
    cls = act.get("investigation", {}).get("class") or agg["class"]
    safety = p.get("safety")
    closed = data["status"] in ("approved", "rejected")
    run = data.get("run")
    L = [f"# 8D report {data['ref']}: {p['title']}", "",
         f"Project `{project}` · status **{'closed' if closed else 'open'}** ({data['status']}) · opened {(p.get('opened_at') or '')[:10]} · "
         f"{len(p['complaints'])} complaint(s) · investigation {run['ref'] if run else 'not started'}"
         + ("" if closed else " · **DRAFT**"), ""]
    # D1
    L += ["## D1 Team", "", "| Role | Who |", "|---|---|", "| Field quality engineer (owner) | " + _c(data.get("created_by")) + " |",
          f"| {ROLES.get(cls, 'ADAS function owner')} | (assigned through the tickets below) |",
          "| Safety manager | " + (_c(safety["confirmed_by"]) + " (hazard rating confirmed)" if safety else "not yet confirmed") + " |",
          "| Engineering agent | " + (f"run {run['ref']} (replay, reconstruction, diagnosis, fix search; no decisions)" if run else "-") + " |", ""]
    # D2
    comps = data["complaint_records"]
    builds = a.get("build") or {}
    locs = sorted({c["payload"]["understood"]["location"] for c in comps} - {"other"})
    speeds = [x["log_analysis"]["event"]["kmh"] for x in a["complaints"] if x["log_analysis"] and x["log_analysis"]["event"].get("kmh")]
    L += ["## D2 Problem description", "",
          f"- **What:** {SYMPTOMS.get(p['symptom'], p['symptom'])} reported by customers.",
          f"- **Which software:** AEB {p['build']}" + (f" ({builds.get('vehicles', 0):,} vehicles; {'validated' if builds.get('validated') else 'no validation evidence'})"
                                                       if builds else " (not in the build registry)") + ".",
          f"- **Where:** {', '.join(locs) or 'not stated'}." + ((f" Event speed{'s' if len(speeds) > 1 else ''} in the recordings: "
                                                                 + (f"{min(speeds):.0f}-{max(speeds):.0f}" if min(speeds) != max(speeds) else f"{speeds[0]:.0f}")
                                                                 + " km/h.") if speeds else ""),
          f"- **When:** {', '.join(sorted({(c['payload'].get('reported_at') or '')[:10] for c in comps if c['payload'].get('reported_at')})) or 'not stated'}.",
          f"- **How many:** {len(comps)} complaint(s), {sum(1 for c in comps if c['payload'].get('log'))} with an event recording.", "",
          "Customer words:", ""]
    L += [f"> {c['ref']}: {c['payload']['text'][:300]}" for c in comps[:4]] + [""]
    # D3
    L += ["## D3 Containment", "", CONTAINMENT.get(cls, "")]
    if act.get("rollback_to"):
        L.append(f"Rollback target confirmed: AEB {act['rollback_to']}.")
    L.append("")
    # D4
    L += ["## D4 Root cause", "", f"**Occurrence:** {I.CLASSES.get(cls, cls)}"
          + (f" (parameter(s): {', '.join(agg['culprits'])})" if agg.get("culprits") else "") + ".",
          f"Complaints by class: {', '.join(f'{k} {v}' for k, v in agg['counts'].items())}." if agg["counts"] else "", ""]
    for x in a["complaints"][:4]:
        L.append(f"**{x['ref']}** ({x['diagnosis']['confidence']} confidence)")
        L += [f"- {e}" for e in x["diagnosis"]["evidence"][:5]]
        L.append("")
    if act.get("escape"):
        L += ["**Escape point (why it was not caught):**", ""] + [f"- {f}" for f in act["escape"]] + [""]
    elif cls in I.MALFUNCTION:
        L += ["**Escape point:** determined when the corrective actions are confirmed.", ""]
    # D5
    L += ["## D5 Chosen corrective action and verification", ""]
    if fix:
        L += [fix["summary"], ""]
        if fix.get("candidates"):
            if fix["kind"] == "rollback":
                L += ["| Build | Validated | Free of the problem |", "|---|---|---|"]
                L += [f"| {c['version']} | {'yes' if c['validated'] else 'no'} | {c['free_of_problem']} |" for c in fix["candidates"]]
            else:
                L += ["| Parameter | Value | Fixes complaints | Margin cases | Regression | Brake onset change | Passes |", "|---|---|---|---|---|---|---|"]
                L += [f"| {c['parameter']} | {c['value']:g} | {c.get('fixes_complaints', '-')} | {c.get('fixes_margin_cases', '-')} | {_c(c.get('regression', '-'))} | "
                      f"{c.get('brake_onset_change_ms', 0):+.0f} ms | {'✅' if c.get('passes') else '-'} |" for c in fix["candidates"][:12] if c.get("valid", True)]
            L.append("")
    else:
        L += ["No technical correction: the function behaved as specified." if cls in ("correct_intervention", "system_limitation")
              else "Pending.", ""]
    # D6
    L += ["## D6 Implementation", ""]
    if act:
        L.append(f"Confirmed by **{act['by']}** on {act['at'][:16]}: {act.get('summary') or ''}")
        if act.get("calibration"):
            L.append("Calibration to verify and release: " + ", ".join(f"{k} = {v:g}" for k, v in act["calibration"].items()) + ".")
        L += [f"- Ticket {t['ref']}: {t['summary']} ({t['status']})" for t in act.get("tickets", [])]
        L += [f"- Regression scenario proposed: {x['ref']} ({x['id']})" for x in act.get("scenarios", [])]
        L += [f"- {n}" for n in act.get("notes", [])]
        if camp:
            cp = camp["payload"]
            L.append(f"- Verification campaign **{camp['ref']}** on {cp['target'].upper()}: {'PASSED' if cp['verdict']['passed'] else 'FAILED'}, "
                     f"{len(cp['scenario_set'])} scenarios, " + (f"released by {camp['approved_by']}" if camp["status"] == "approved"
                                                                 else f"status {camp['status']} (needs release by a second person)") + ".")
    else:
        L.append("Pending: the corrective actions wait for confirmation at the agent's checkpoint.")
    L.append("")
    # D7
    L += ["## D7 Prevent recurrence", ""]
    if cls == "calibration_regression":
        L.append("- Calibration-only changes get the same SIL regression campaign and release as code changes (release readiness RR07/RR12).")
    if cls == "implementation_defect":
        L.append("- Back-to-back against the reference model is mandatory before any pilot rollout.")
    if cls == "sensor_false_target":
        L.append("- The sensor supplier's correction is verified against the reconstructed field scenarios before it is accepted.")
    if act.get("scenarios"):
        L.append(f"- The reconstructed field situations become permanent regression scenarios: {', '.join(x['id'] for x in act['scenarios'])}.")
    if cls in ("correct_intervention", "system_limitation"):
        L.append("- Consider customer information on how AEB works and where its limits are.")
    if cls == "insufficient_data":
        L.append("- Dealers attach the event data recorder export to every AEB complaint.")
    L.append("")
    # D8
    L += ["## D8 Closure", ""]
    if closed:
        L.append(f"Closed ({'resolved' if data['status'] == 'approved' else 'without a fix'}) by **{data['approved_by'] or data['updated_by']}**: "
                 f"{p.get('decision_comment', '')}")
    else:
        L.append("Open. Closure needs a second person and, for a technical fix, the released verification campaign.")
    if safety:
        L += ["", f"Hazard rating confirmed by {safety['confirmed_by']}: S{safety['severity']} E{safety['exposure']} C{safety['controllability']} → "
                  f"**ASIL {safety['asil']}** ({safety['ruleset']})" + (" · escalated to the product safety committee" if safety.get("escalate") else "")]
    L += ["", "---", "Prepared with the platform's field-issue agent: recordings replayed on the field software (SIL), situations "
          "reconstructed and simulated, root cause by differential replay, fixes checked against every complaint and the regression "
          "library. The agent decides nothing: the hazard rating, the corrective actions, scenario approvals, the release of the "
          "verification campaign and the closure are decisions of the people named above.", ""]
    return "\n".join(x for x in L if x is not None)
