"""Validation report (Markdown) for a campaign record; shared by the API and the CI script."""
from __future__ import annotations


def _c(text) -> str:
    """Markdown table cell: pipes and line breaks would break the table."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def markdown(project: str, rec: dict) -> str:
    p = rec["payload"]
    v = p["verdict"]
    fw = p.get("firmware") or {}
    changes = p["calibration_changes"]
    cal_txt = "default" if not changes else ", ".join(f"{k} {x['default']} → {x['value']}" for k, x in changes.items())
    status = {"proposed": "awaiting release decision", "approved": f"RELEASED by {rec['approved_by']} at {(rec['approved_at'] or '')[:16]}",
              "rejected": "rejected", "suspect": f"SUSPECT: {p.get('suspect_reason', '')}"}.get(rec["status"], rec["status"])
    L = [f"# AEB virtual validation report: {rec['ref']}", "",
         f"Project **{project}** · run by **{p['run_by']}** at {p['run_at'][:16]} · {p['seconds']} s · status: **{status}**", "",
         "## Verdict", "",
         f"**{'PASSED' if v['passed'] else 'FAILED'}** on the **{'compiled C code (SIL)' if p['target'] == 'sil' else 'Python model (MIL) only'}**. "
         f"Release-blocking requirements passed: {v.get('blocking_total', 0) - len(v['blocking_not_passed'])} of {v.get('blocking_total', 0)}. "
         f"All requirements: {v['requirements']['pass']} pass, {v['requirements']['fail']} fail, "
         f"{v['requirements']['not_verified']} not verified, {v['requirements']['not_covered']} not covered.", ""]
    if v["exploratory_findings"]:
        L += [f"Exploratory (SOTIF) findings, to be analysed as triggering conditions: **{', '.join(v['exploratory_findings'])}**", ""]
    if v["blocking_not_passed"]:
        L += [f"Release-blocking requirements not passed: **{', '.join(v['blocking_not_passed'])}**", ""]
    if rec.get("stale"):
        L += [f"⚠ Out of date: {'; '.join(rec['stale'])}.", ""]
    L += ["## What was tested", "",
          f"- Firmware: {('version ' + fw.get('version', '?') + ', source hash `' + fw.get('hash', '')[:16] + '`') if fw else 'not compiled (model only)'}",
          f"- Calibration: {cal_txt}",
          f"- {len(p['scenario_set'])} approved scenarios, {len(p['requirement_set'])} approved requirements", ""]
    if p.get("note"):
        L += [f"Note: {p['note']}", ""]
    L += ["## Requirements", "", "| Requirement | Release-blocking | Verdict | Scenarios pass / fail / n.a. | Failures |", "|---|---|---|---|---|"]
    for r in p["requirements"]:
        fails = "; ".join(f"{x['scenario']}: {x['detail']}" for x in r["scenarios"] if x["verdict"] == "fail")[:300]
        L.append(f"| {r['id']} {_c(r['title'])} | {'yes' if r['release_blocking'] else 'no (exploratory)'} | {r['verdict']} | "
                 f"{r['counts']['pass']} / {r['counts']['fail']} / {r['counts']['n/a']} | {_c(fails)} |")
    b = p.get("back_to_back")
    L += ["", "## Back-to-back (C code against the model)", ""]
    if b:
        L += [f"{'PASSED' if b['passed'] else 'FAILED'}: {b['scenarios']} scenarios, {b['steps']} time steps replayed; "
              f"state and warning compared exactly, brake request within {b['tolerances']['brake_mps2']} m/s² "
              f"(largest difference {b['max_brake_diff']}); TTC largest relative difference {b['max_ttc_rel_diff']}."]
        if b.get("calibration_mismatch"):
            L.append(f"Calibration mismatch between C defaults and the model: {b['calibration_mismatch']}")
        if b["mismatching_scenarios"]:
            L.append(f"Mismatching scenarios: {', '.join(b['mismatching_scenarios'])}")
    else:
        L.append("Not run (needs the compiled C code).")
    m = p.get("mutation")
    L += ["", "## Mutation testing", ""]
    if m:
        L += [f"{m['killed']} of {m['killed'] + m['survived']} mutants killed by the release-blocking requirements "
              f"(score {m['score']:.0%}); back-to-back would have caught {m['survivors_caught_by_b2b']} of the "
              f"{m['survived']} survivors.", "", "| Mutant | Change | Result |", "|---|---|---|"]
        for x in m["rows"]:
            res = (f"killed by {', '.join(x['killed_by'])} ({x['scenario']})" if x["status"] == "killed" else
                   f"survived{' (back-to-back detects it)' if x.get('b2b_detected') else ' (not detected)'}" if x["status"] == "survived"
                   else f"error: {x.get('detail', '')}")
            L.append(f"| {x['id']} | {_c(x['description'])} | {_c(res)} |")
    else:
        L.append("Not run in this campaign.")
    L += ["", "## Scenario results", "", "| Scenario | Collision | Impact km/h | Stop gap m | Warning s | Braking s | Full s |", "|---|---|---|---|---|---|---|"]
    for s in p["scenarios"]:
        mt = s["metrics"]
        L.append(f"| {s['id']} | {'YES' if mt['collided'] else 'no'} | {mt['impact_kmh'] if mt['collided'] else ''} | "
                 f"{mt['stop_gap_m'] if mt['stop_gap_m'] is not None else ''} | {mt['fcw_t'] if mt['fcw_t'] is not None else ''} | "
                 f"{mt['brake_t'] if mt['brake_t'] is not None else ''} | {mt['full_t'] if mt['full_t'] is not None else ''} |")
    if p["notes"]:
        L += ["", "## Notes", ""] + [f"- {n}" for n in p["notes"]]
    L += ["", "## Limits", "",
          "1-D longitudinal simulation (100 % overlap, straight road, simplified brake and sensor models). The scenario "
          "grids follow the Euro NCAP AEB Car-to-Car families but this is not an NCAP assessment. Results support "
          "engineering decisions and regression testing; they do not replace HIL, vehicle and proving-ground testing.", ""]
    return "\n".join(L)

