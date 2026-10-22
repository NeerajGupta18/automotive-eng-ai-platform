"""Validation campaign for the AEB function.

  run_scenarios   closed-loop simulation of every scenario with one controller (model = MIL, C code = SIL)
  evaluate        every requirement against the scenarios its selector matches -> pass / fail / gaps
  back_to_back    ISO 26262-6 lists back-to-back comparison between model and code: the inputs recorded
                  in the model run are replayed into the compiled C code and the outputs compared step by step
  mutation_test   small deliberate defects ("mutants") are put into the C source one at a time; a good
                  scenario set makes some release-blocking requirement fail (the mutant is "killed").
                  Survivors show where the requirements or scenarios are too weak; whether back-to-back
                  would still have noticed the change is reported next to it.

Everything here is pure Python with no database: the API service and the CI script share it."""
from __future__ import annotations

import dataclasses
import time

from app.modules.adas import requirements as rq
from app.modules.adas import sil
from app.modules.adas.model import CAL, AebModel, Calibration
from app.modules.adas.sim import simulate

B2B_BRAKE_TOL = 1e-3          # m/s^2: float (C) against double (Python)
B2B_TTC_TOL = 1e-3            # relative

MUTANTS = [
    {"id": "M01", "description": "Object confirmed one cycle later (>= became >)",
     "find": "confirmed = (ctx->confirm >= c->confirm_cycles) ? 1u : 0u;",
     "replace": "confirmed = (ctx->confirm > c->confirm_cycles) ? 1u : 0u;"},
    {"id": "M02", "description": "New full braking needs TTC and a_req together (|| became &&)",
     "find": "if ((v <= c->v_max_mps) && ((ttc < c->ttc_full_s) || (areq > c->areq_full_mps2))) {",
     "replace": "if ((v <= c->v_max_mps) && ((ttc < c->ttc_full_s) && (areq > c->areq_full_mps2))) {"},
    {"id": "M03", "description": "Deceleration of the vehicle ahead ignored in a_req",
     "find": "if (a_tgt < c->a_tgt_min_mps2) {", "replace": "if (a_tgt < 1000.0f) {"},
    {"id": "M04", "description": "No brake hold at standstill",
     "find": "a = c->a_hold_mps2;", "replace": "a = 0.0f;"},
    {"id": "M05", "description": "Driver brake request not arbitrated (AEB request only)",
     "find": "out->brake_req_mps2 = aeb_maxf(aeb, in->driver_brake_mps2);", "replace": "out->brake_req_mps2 = aeb;"},
    {"id": "M06", "description": "Object confirmation 50 ms instead of 150 ms",
     "find": "cal->confirm_cycles = 15u;", "replace": "cal->confirm_cycles = 5u;"},
    {"id": "M07", "description": "Stop gap added instead of subtracted in a_req",
     "find": "float d_eff = aeb_maxf(distance - c->stop_gap_m, 0.1f);", "replace": "float d_eff = aeb_maxf(distance + c->stop_gap_m, 0.1f);"},
    {"id": "M08", "description": "Warning flag not set during partial braking",
     "find": "out->fcw = ((ctx->state == AEB_STATE_WARN) || (ctx->state == AEB_STATE_PARTIAL) ||",
     "replace": "out->fcw = ((ctx->state == AEB_STATE_WARN) || (ctx->state == AEB_STATE_WARN) ||"},
    {"id": "M09", "description": "Braking released below 10 km/h too (speed condition dropped)",
     "find": "((in->closing_mps < c->release_closing_mps) && (in->ego_speed_mps >= c->v_min_mps))",
     "replace": "(in->closing_mps < c->release_closing_mps)"},
    {"id": "M10", "description": "Lower speed limit (10 km/h) not applied",
     "find": "} else if ((confirmed != 0u) && (in->ego_speed_mps >= ctx->cal.v_min_mps)) {",
     "replace": "} else if ((confirmed != 0u) && (in->ego_speed_mps >= 0.0f)) {"},
    {"id": "M11", "description": "Upper speed limit (85 km/h) not applied to new full braking",
     "find": "if ((v <= c->v_max_mps) && ((ttc < c->ttc_full_s) || (areq > c->areq_full_mps2))) {",
     "replace": "if ((v <= 1000.0f) && ((ttc < c->ttc_full_s) || (areq > c->areq_full_mps2))) {"},
]


def mutate(c_text: str, mutant: dict) -> str:
    n = c_text.count(mutant["find"])
    if n != 1:
        raise ValueError(f"{mutant['id']}: the code to mutate occurs {n} times (expected once); update MUTANTS")
    return c_text.replace(mutant["find"], mutant["replace"])


# ----------------------------------------------------------------------------- running and judging
def run_scenarios(scenarios: list[dict], controller, stop_on=None) -> list[dict]:
    """-> [{id, metrics}] in scenario order. stop_on(scn, metrics) -> True ends the run early."""
    out = []
    for s in scenarios:
        m = simulate(s, controller)["metrics"]
        out.append({"id": s["id"], "metrics": m})
        if stop_on and stop_on(s, m):
            break
    return out


def evaluate(requirements: list[dict], scenarios: list[dict], results: list[dict]) -> list[dict]:
    by_id = {r["id"]: r["metrics"] for r in results}
    out = []
    for req in requirements:
        rows = []
        for s in scenarios:
            if rq.applies(req, s) and s["id"] in by_id:
                rows.append({"scenario": s["id"], **rq.judge(req, by_id[s["id"]])})
        verdicts = [r["verdict"] for r in rows]
        out.append({"id": req["id"], "title": req["title"], "release_blocking": req["release_blocking"],
                    "category": req["category"], "check": req["check"],
                    "verdict": rq.summarize(verdicts, bool(rows)), "scenarios": rows,
                    "counts": {v: verdicts.count(v) for v in ("pass", "fail", "n/a")}})
    return out


def verdict_of(req_results: list[dict]) -> dict:
    blocking = [r for r in req_results if r["release_blocking"]]
    bad = [r["id"] for r in blocking if r["verdict"] != "pass"]
    return {"passed": not bad, "blocking_not_passed": bad, "blocking_total": len(blocking),
            "exploratory_findings": [r["id"] for r in req_results if not r["release_blocking"] and r["verdict"] == "fail"],
            "requirements": {v: sum(1 for r in req_results if r["verdict"] == v)
                             for v in ("pass", "fail", "not_verified", "not_covered")}}


# ----------------------------------------------------------------------------- back-to-back
def record(scenarios: list[dict], cal: Calibration = CAL) -> dict[str, list]:
    """Inputs and outputs of the model for every step (the reference for back-to-back)."""
    m = AebModel(cal)
    return {s["id"]: simulate(s, m, keep_series=True)["series"] for s in scenarios}


def replay(series: list, controller) -> dict:
    controller.reset()
    st = fcw = 0
    first = None
    brake_d = ttc_d = 0.0
    for k, (inp, ref) in enumerate(series):
        o = controller.step(*inp)
        bad = False
        if o["state"] != ref[1]:
            st, bad = st + 1, True
        if o["fcw"] != ref[2]:
            fcw, bad = fcw + 1, True
        d = abs(o["brake_req"] - ref[0])
        brake_d = max(brake_d, d)
        bad = bad or d > B2B_BRAKE_TOL
        if ref[3] < 50.0:
            ttc_d = max(ttc_d, abs(o["ttc"] - ref[3]) / max(abs(ref[3]), 1e-3))
        if bad and first is None:
            first = k
    ok = st == 0 and fcw == 0 and brake_d <= B2B_BRAKE_TOL
    return {"steps": len(series), "state_mismatch": st, "fcw_mismatch": fcw, "max_brake_diff": round(brake_d, 6),
            "max_ttc_rel_diff": round(ttc_d, 6), "first_mismatch_t": round(first * 0.01, 2) if first is not None else None,
            "passed": ok}


def calibration_check(controller, cal: Calibration | None) -> list[dict]:
    """The C default calibration must equal the model's (only when the defaults are what is tested)."""
    if cal is not None:
        return []
    c_cal, out = controller.default_calibration(), []
    for f in dataclasses.fields(CAL):
        py, cv = getattr(CAL, f.name), c_cal[f.name]
        if abs(float(py) - float(cv)) > 1e-5 * max(1.0, abs(float(py))):
            out.append({"parameter": f.name, "model": py, "c_code": round(float(cv), 6)})
    return out


def back_to_back(recorded: dict[str, list], controller, cal: Calibration | None = None) -> dict:
    rows = [{"scenario": sid, **replay(series, controller)} for sid, series in recorded.items()]
    cal_diff = calibration_check(controller, cal)
    return {"passed": all(r["passed"] for r in rows) and not cal_diff, "calibration_mismatch": cal_diff,
            "scenarios": len(rows), "steps": sum(r["steps"] for r in rows),
            "mismatching_scenarios": [r["scenario"] for r in rows if not r["passed"]],
            "max_brake_diff": max((r["max_brake_diff"] for r in rows), default=0.0),
            "max_ttc_rel_diff": max((r["max_ttc_rel_diff"] for r in rows), default=0.0),
            "tolerances": {"brake_mps2": B2B_BRAKE_TOL, "state_and_warning": "exact"}, "rows": rows}


# ----------------------------------------------------------------------------- mutation testing
def mutation_test(scenarios: list[dict], requirements: list[dict], baseline: list[dict], recorded: dict[str, list],
                  cal: Calibration | None = None, cache_dir=None, mutants: list[dict] | None = None) -> dict:
    """baseline: requirement results of the unmutated code. A mutant is killed when a release-blocking
    requirement that passed on the unmutated code fails."""
    c_text, _ = sil.sources()
    passed_before = {r["id"] for r in baseline if r["release_blocking"] and r["verdict"] == "pass"}
    blocking = [r for r in requirements if r["id"] in passed_before]
    rows = []
    for mt in mutants or MUTANTS:
        t0 = time.perf_counter()
        row = {"id": mt["id"], "description": mt["description"]}
        try:
            lib = sil.build(mutate(c_text, mt), cache_dir)
            ctrl = sil.SilController(lib["path"], cal)
        except (sil.SilUnavailable, ValueError, OSError) as e:
            rows.append({**row, "status": "error", "detail": str(e)[:300]})
            continue

        def fails(s, m):
            return any(rq.applies(r, s) and rq.judge(r, m)["verdict"] == "fail" for r in blocking)

        res = run_scenarios(scenarios, ctrl, stop_on=fails)
        killed_by = []
        if res and fails(next(s for s in scenarios if s["id"] == res[-1]["id"]), res[-1]["metrics"]):
            s_last = next(s for s in scenarios if s["id"] == res[-1]["id"])
            killed_by = [r["id"] for r in blocking if rq.applies(r, s_last) and rq.judge(r, res[-1]["metrics"])["verdict"] == "fail"]
            row.update(status="killed", killed_by=killed_by, scenario=res[-1]["id"])
        else:
            b2b = back_to_back(recorded, ctrl, cal)
            row.update(status="survived", b2b_detected=not b2b["passed"],
                       b2b_first=(b2b["mismatching_scenarios"] or [None])[0])
        row["seconds"] = round(time.perf_counter() - t0, 2)
        rows.append(row)
    valid = [r for r in rows if r["status"] in ("killed", "survived")]
    killed = sum(1 for r in valid if r["status"] == "killed")
    return {"mutants": len(rows), "killed": killed, "survived": len(valid) - killed,
            "errors": len(rows) - len(valid), "score": round(killed / len(valid), 3) if valid else None,
            "survivors_caught_by_b2b": sum(1 for r in valid if r.get("b2b_detected")), "rows": rows}


# ----------------------------------------------------------------------------- one call for CI and the service
def run_campaign(scenarios: list[dict], requirements: list[dict], *, target: str = "auto", cal: Calibration = CAL,
                 do_b2b: bool = True, do_mutation: bool = False, cache_dir=None) -> dict:
    """target: model | sil | auto (SIL when a library or compiler is available, else model).
    With the default calibration the C code runs on its own compiled-in defaults (checked against the model)."""
    t0 = time.perf_counter()
    notes, build = [], None
    sil_ctrl = None
    sil_cal = None if cal == CAL else cal
    if target in ("sil", "auto"):
        try:
            build = sil.build(None, cache_dir)
            sil_ctrl = sil.SilController(build["path"], sil_cal)
        except sil.SilUnavailable as e:
            if target == "sil":
                raise
            notes.append(f"SIL unavailable, model only: {e}")
    ctrl = sil_ctrl or AebModel(cal)
    results = run_scenarios(scenarios, ctrl)
    req_results = evaluate(requirements, scenarios, results)
    out = {"target": "sil" if sil_ctrl else "model", "scenarios": results, "requirements": req_results,
           "verdict": verdict_of(req_results), "notes": notes,
           "firmware": ({"hash": build["hash"], "version": sil_ctrl.version, "cached_build": build["cached"],
                         "compiler_warnings": build["warnings"]} if sil_ctrl else None)}
    recorded = None
    if sil_ctrl and (do_b2b or do_mutation):
        recorded = record(scenarios, cal)
    if sil_ctrl and do_b2b:
        out["back_to_back"] = back_to_back(recorded, sil_ctrl, sil_cal)
        if not out["back_to_back"]["passed"]:
            out["verdict"]["passed"] = False
            out["verdict"]["back_to_back_failed"] = out["back_to_back"]["mismatching_scenarios"]
    elif do_b2b:
        out["back_to_back"] = None
        notes.append("Back-to-back skipped: needs the compiled C code (SIL)")
    if do_mutation:
        if sil_ctrl:
            out["mutation"] = mutation_test(scenarios, requirements, req_results, recorded, sil_cal, cache_dir)
        else:
            out["mutation"] = None
            notes.append("Mutation testing skipped: needs a C compiler")
    out["seconds"] = round(time.perf_counter() - t0, 2)
    return out
