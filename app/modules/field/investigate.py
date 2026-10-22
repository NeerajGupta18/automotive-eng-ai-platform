"""The investigation steps of the field-issue agent. Deterministic engineering analysis on recorded data and
simulation; no LLM decides anything here.

  replay        open loop: the recorded inputs are fed into a build's controller (the C code of that build when
                available) and its outputs compared with the recorded outputs, cycle by cycle. Bit-exact agreement
                shows the recording, the software version and the calibration belong together
  reconstruct   closed loop: the situation is rebuilt as a scenario (phantom object, stationary / moving / braking
                vehicle, friction, sensor range, latency, driver braking) estimated from the recording, simulated with
                the field build and compared with the recording; if it does not match, variations are tried
  diagnose      root-cause class with evidence: implementation defect (code differs from the model with the same
                calibration), calibration regression (the last validated calibration would not have reacted, and which
                parameter it was), sensor false target (phantom track, same reaction with the validated build),
                correct intervention (without AEB the car would have hit the object), system limitation (speed window,
                friction, late detection) or insufficient data
  escape        why it was not caught: validation evidence of the build, whether the regression scenarios fail with
                its calibration, whether back-to-back would have seen a coding defect
  fix search    calibration candidates for the responsible parameter(s), each checked against every complaint (plus a
                margin case) and the whole regression library with its release-blocking requirements; for coding
                defects the newest earlier build that does not show the problem (rollback containment)
  safety        an S/E/C proposal for the hazard with rationale; the ASIL comes from the HARA rule table. A person
                confirms it"""
from __future__ import annotations

from app.modules.adas import requirements as rq
from app.modules.adas.authoring import family_for, validate_scenario
from app.modules.adas.model import CAL_INFO, calibration_changes, make_calibration
from app.modules.adas.sim import simulate
from app.modules.field import builds as B
from app.modules.field import logs as L
from app.modules.hara import rules as hara_rules

DT = 0.01
CLASSES = {
    "implementation_defect": "Implementation defect: the C code behaves differently from the model with the same calibration",
    "calibration_regression": "Calibration regression: the last validated calibration would not have behaved like this",
    "sensor_false_target": "Sensor false target: the radar reported an object that was not there; the function reacted as specified",
    "correct_intervention": "Correct intervention: without AEB the vehicle would have hit a real object",
    "system_limitation": "System limitation: outside what the function is specified to handle",
    "insufficient_data": "Insufficient data: the recording does not show or does not match the complained event",
    "needs_engineer": "Unexplained by the automatic analysis: an engineer must look at it",
}
MALFUNCTION = ("implementation_defect", "calibration_regression", "sensor_false_target")
PRIORITY = ("implementation_defect", "calibration_regression", "sensor_false_target", "system_limitation",
            "correct_intervention", "needs_engineer", "insufficient_data")


# ============================================================================================ simulation <-> logs
class _Recorder:
    name = "recorder"

    def __init__(self, ctrl):
        self.c, self.rows = ctrl, []

    def reset(self) -> None:
        self.c.reset()
        self.rows = []

    def step(self, *inp):
        o = self.c.step(*inp)
        self.rows.append((inp, o))
        return o


def sim_log(scn: dict, ctrl, header: dict | None = None) -> tuple[dict, dict]:
    """Closed-loop simulation recorded the way the vehicle's data recorder would: -> (log, metrics)."""
    rec = _Recorder(ctrl)
    m = simulate(scn, rec, trace_every=10_000)["metrics"]
    rows, n = rec.rows, len(rec.rows)
    cols = {c: [] for c in L.COLUMNS}
    for k, (inp, o) in enumerate(rows):
        v = inp[0]
        v_next = rows[k + 1][0][0] if k + 1 < n else None
        decel = (v - v_next) / DT if v_next is not None else (cols["decel_mps2"][-1] if cols["decel_mps2"] else 0.0)
        for c, val in zip(L.COLUMNS, (k * DT, v * 3.6, 1 if inp[1] else 0, inp[2] if inp[1] else 0.0, inp[3] if inp[1] else 0.0,
                                      inp[4], int(o["state"]), 1 if o["fcw"] else 0, float(o["brake_req"]), decel)):
            cols[c].append(val)
    trig = "crash" if m["collided"] else "aeb" if m["brake_t"] is not None else "manual"
    return {"header": {"trigger": trig, **(header or {})}, "rows": n, "cols": cols}, m


def log_rows(log: dict) -> list[tuple]:
    c = log["cols"]
    return [tuple(c[name][k] for name in L.COLUMNS) for k in range(log["rows"])]


def _with_outputs(log: dict, states: list, fcw: list, brake: list) -> dict:
    cols = dict(log["cols"])
    cols.update(aeb_state=states, fcw=fcw, brake_req_mps2=brake)
    return {"header": log["header"], "rows": log["rows"], "cols": cols}


# ============================================================================================ replay (open loop)
def replay(log: dict, ctrl) -> dict:
    ins, rec = L.inputs(log), L.recorded_outputs(log)
    ctrl.reset()
    states, fcws, brakes = [], [], []
    same_state = same_fcw = same_brake = 0
    first, max_bd = None, 0.0
    for k, (i, r) in enumerate(zip(ins, rec)):
        o = ctrl.step(*i)
        s, f, b = int(o["state"]), 1 if o["fcw"] else 0, float(o["brake_req"])
        states.append(s), fcws.append(f), brakes.append(b)
        ok_s, ok_f = s == r[0], bool(f) == r[1]
        same_state += ok_s
        same_fcw += ok_f
        bd = abs(b - r[2])
        max_bd = max(max_bd, bd)
        same_brake += bd <= 0.05
        if first is None and (not ok_s or not ok_f or bd > 0.05):
            first = {"t": round(log["cols"]["t_s"][k], 2), "recorded": {"state": L.STATE_NAMES[r[0]], "fcw": r[1], "brake_req": round(r[2], 2)},
                     "simulated": {"state": L.STATE_NAMES[s], "fcw": bool(f), "brake_req": round(b, 2)},
                     "inputs": {"ego_kmh": round(i[0] * 3.6, 1), "obj_valid": i[1], "obj_dist_m": round(i[2], 2), "closing_mps": round(i[3], 2),
                                "driver_brake": round(i[4], 2)}}
    n = log["rows"]
    rec_act = next((k for k in range(n) if rec[k][0] in (2, 3)), None)
    sim_act = next((k for k in range(n) if states[k] in (2, 3)), None)
    act_ok = (rec_act is None) == (sim_act is None) and (rec_act is None or abs(rec_act - sim_act) <= 2)
    agree = same_state / n
    return {"steps": n, "state_agreement": round(agree, 4), "warning_agreement": round(same_fcw / n, 4),
            "brake_agreement": round(same_brake / n, 4), "max_brake_diff": round(max_bd, 3), "first_difference": first,
            "recorded_activation_t": None if rec_act is None else round(rec_act * DT, 2),
            "simulated_activation_t": None if sim_act is None else round(sim_act * DT, 2),
            "reproduced": bool(agree >= 0.995 and same_fcw / n >= 0.995 and same_brake / n >= 0.995 and act_ok),
            "_outputs": (states, fcws, brakes)}


def replayed_analysis(log: dict, rep: dict, symptom: str) -> dict:
    s, f, b = rep["_outputs"]
    return L.analyze(_with_outputs(log, s, f, b), symptom)


def public(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


# ============================================================================================ reconstruction
def estimate_latency(log: dict, track: dict | None) -> float | None:
    """Sensor latency from how late the measured closing speed follows the measured ego speed while braking hard
    behind a stationary object (no latency on wheel speeds, the radar's is what is estimated)."""
    if not track or not track["stationary"] or track["vanished_ahead"]:
        return None
    c = log["cols"]
    a = int(round((track["t_start"] - c["t_s"][0]) / DT))
    b = int(round((track["t_end"] - c["t_s"][0]) / DT))
    k0 = next((k for k in range(a, b) if c["decel_mps2"][k] > 2.0), None)
    if k0 is None or b - k0 < 30:
        return None
    best, best_err = None, None
    for lat in range(0, 61):
        err, n = 0.0, 0
        for k in range(max(k0, a + lat), b + 1):
            truth = c["ego_kmh"][k - lat] / 3.6
            err += (c["obj_closing_mps"][k] - truth) ** 2
            n += 1
        if n >= 20:
            e = err / n
            if best_err is None or e < best_err:
                best, best_err = lat, e
    return None if best is None else round(best * DT, 2)


def _target_braking(log: dict, tr: dict) -> tuple[float, float] | None:
    """(brake start in s, deceleration) of a lead vehicle that slows down during the track."""
    c = log["cols"]
    a = int(round((tr["t_start"] - c["t_s"][0]) / DT))
    b = int(round((tr["t_end"] - c["t_s"][0]) / DT))
    tgt = [(c["ego_kmh"][k] / 3.6 - c["obj_closing_mps"][k]) for k in range(a, b + 1)]
    v0 = sorted(tgt[:20])[len(tgt[:20]) // 2]
    k1 = next((k for k in range(len(tgt)) if tgt[k] < v0 - 0.6), None)
    if k1 is None:
        return None
    k_end = next((k for k in range(k1, len(tgt)) if tgt[k] < 0.3), min(len(tgt) - 1, k1 + 150))
    if k_end - k1 < 10:
        return None
    dec = (tgt[k1] - tgt[k_end]) / ((k_end - k1) * DT)
    start = (a + k1) * DT - 0.6 / max(dec, 0.5)            # back to where the speed left v0
    return max(0.0, round(start, 2)), round(min(max(dec, 0.5), 10.0), 2)


def scenario_from_log(log: dict, an: dict, sid: str, latency: float = 0.05, seed: int = 7) -> tuple[dict, str]:
    c = log["cols"]
    t0 = c["t_s"][0]
    ego0 = c["ego_kmh"][0]
    ev = an["event"]
    trk = an["tracks"]
    tr = trk[ev["track"]] if ev.get("track") is not None else (trk[-1] if trk else None)
    s = {"id": sid, "ego_kmh": round(min(max(ego0, 1.0), 130.0), 2), "latency_s": latency, "noise": True, "seed": seed,
         "duration_s": round(min(max(an["duration_s"] + 0.5, 2.0), 60.0), 2)}
    if an["driver_brake"]:
        s["driver_brake_at_s"] = round(an["driver_brake"]["t"] - t0, 2)
        s["driver_brake_mps2"] = round(min(an["driver_brake"]["max_mps2"], 10.0), 2)
    if tr is None:
        s.update(target="none")
        basis = "no object reported by the radar"
    elif tr["phantom_like"]:
        s.update(target="none", ghost_at_s=round(max(0.0, tr["t_start"] - t0 - latency), 2), ghost_duration_s=round(tr["lifetime_s"], 2),
                 ghost_distance_m=round(min(max(tr["d_first_m"], 2.0), 100.0), 2))
        basis = (f"phantom object: a stationary object appeared at {tr['d_first_m']:.0f} m and vanished {tr['lifetime_s'] * 1000:.0f} ms later, "
                 f"still {tr['d_last_m']:.0f} m ahead")
    else:
        v_t = max(0.0, tr["target_kmh_first"]) if not tr["stationary"] else 0.0
        closing = (tr["ego_kmh_at_start"] - v_t) / 3.6
        t_meas = tr["t_start"] - t0 - latency
        gap0 = tr["d_first_m"] + closing * max(t_meas, 0.0)
        brk = None if tr["stationary"] else _target_braking(log, tr)
        if tr["stationary"]:
            s.update(target="stationary")
            basis = "stationary vehicle ahead"
        elif brk:
            s.update(target="braking", target_kmh=round(v_t, 2), target_decel_mps2=brk[1], target_brake_at_s=round(max(brk[0] - latency, 0.0), 2))
            basis = f"lead vehicle at {v_t:.0f} km/h braking at about {brk[1]:.1f} m/s²"
        else:
            s.update(target="moving", target_kmh=round(min(v_t, 130.0), 2))
            basis = f"slower vehicle ahead at {v_t:.0f} km/h"
        s["gap_m"] = round(min(max(gap0, 2.0), 300.0), 2)
        if tr["d_first_m"] < 140.0 and t_meas > 0.05:
            s["sensor_range_m"] = round(min(max(tr["d_first_m"] + max(closing, 0.0) * DT + 0.05, 10.0), 250.0), 2)
            basis += f", first seen only {tr['d_first_m']:.0f} m ahead (late detection or cut-in)"
        if an["friction_limited"]:
            full = [a for a in an["activations"] if a["max_state"] == 3]
            mu = (max(a["max_decel"] for a in full) + 0.02) / 9.81
            s["mu"] = round(min(max(mu, 0.1), 1.0), 2)
            basis += f", braking limited to {max(a['max_decel'] for a in full):.1f} m/s² (road friction about {s['mu']:.2f})"
    s["family"] = family_for(s)
    s["title"] = (basis[:1].upper() + basis[1:])[:160]
    s["source"] = "Field recording, reconstructed by the field-issue agent"
    return validate_scenario(s), basis


def fidelity(log_an: dict, sim_an: dict) -> dict:
    """Does the re-simulated scenario show what the recording shows?"""
    la, sa = log_an["activations"], sim_an["activations"]
    checks = {"activation": bool(la) == bool(sa), "warning": bool(log_an["warnings"]) == bool(sim_an["warnings"]),
              "crash": bool(log_an["crash"]) == bool(sim_an["crash"]),
              "moved_after_stop": bool(log_an["stop"] and log_an["stop"]["moved_after_m"] > 0.05) ==
                                  bool(sim_an["stop"] and sim_an["stop"]["moved_after_m"] > 0.05)}
    if la and sa:
        checks["activation_time"] = abs(la[0]["t"] - sa[0]["t"]) <= 0.1
        checks["max_state"] = la[0]["max_state"] == sa[0]["max_state"]
    if log_an["crash"] and sim_an["crash"]:
        checks["impact_speed"] = abs(log_an["crash"]["kmh"] - sim_an["crash"]["kmh"]) <= 5.0
    checks["final_speed"] = abs(log_an["speed_end_kmh"] - sim_an["speed_end_kmh"]) <= 5.0
    return {"matched": all(checks.values()), "checks": checks}


def reconstruct(log: dict, an: dict, build: dict, sid: str, symptom: str, prefer: str = "sil", max_attempts: int = 8) -> dict:
    """Closed-loop reconstruction with the field build, retrying variations until the simulation matches."""
    ev_tr = an["tracks"][an["event"]["track"]] if an["event"].get("track") is not None else None
    est = estimate_latency(log, ev_tr)
    lats = []
    for x in ([est] if est is not None else []) + [0.05, 0.10, 0.15, 0.20, 0.30]:
        if x is not None and x not in lats:
            lats.append(x)
    variants = [(lat, 7) for lat in lats] + [(lats[0], 11), (lats[0], 23)]
    attempts, best = [], None
    for lat, seed in variants[:max_attempts]:
        try:
            scn, basis = scenario_from_log(log, an, sid, lat, seed)
        except ValueError as e:
            return {"matched": False, "scenario": None, "basis": f"could not build a scenario: {e}", "attempts": attempts}
        ctrl, kind = B.controller(build, prefer)
        slog, metrics = sim_log(scn, ctrl)
        fid = fidelity(an, L.analyze(slog, symptom))
        attempts.append({"latency_s": lat, "seed": seed, "matched": fid["matched"],
                         "failed_checks": [k for k, v in fid["checks"].items() if not v]})
        cand = {"scenario": scn, "basis": basis, "fidelity": fid, "target": kind, "metrics": _brief_metrics(metrics)}
        if best is None or (fid["matched"] and not best["fidelity"]["matched"]):
            best = cand
        if fid["matched"]:
            break
    out = {"matched": best["fidelity"]["matched"], **best, "attempts": attempts, "latency_estimate_s": est}
    return out


def _brief_metrics(m: dict) -> dict:
    return {k: m.get(k) for k in ("collided", "impact_kmh", "fcw_t", "brake_t", "full_t", "max_aeb_req", "max_decel", "stopped", "stop_gap_m",
                                  "min_gap_m", "moved_after_stop_m")}


def counterfactual(scn: dict) -> dict:
    """The same situation without AEB (the driver's own braking as recorded)."""
    m = simulate(scn, B.NoAeb())["metrics"]
    has_target = scn.get("target", "none") != "none"
    return {"collision": bool(m["collided"]), "impact_kmh": m["impact_kmh"] if m["collided"] else None,
            "closest_gap_m": m["min_gap_m"] if has_target else None, "real_object": has_target}


# ============================================================================================ diagnosis
def cutoff(log: dict, symptom: str) -> int:
    """Open loop is exact only until the recorded intervention changes the vehicle's motion: reactions to the same
    recorded inputs are compared up to 200 ms after the recorded intervention (or the warning, for warnings)."""
    c, n = log["cols"], log["rows"]
    if symptom == "false_warning":
        k = next((k for k in range(n) if c["fcw"][k] or c["aeb_state"][k] in (2, 3)), None)
    elif symptom in ("unexpected_braking", "other"):
        k = next((k for k in range(n) if c["aeb_state"][k] in (2, 3)), None)
    else:
        k = None
    return n - 1 if k is None else min(n - 1, k + 20)


def reaction(log: dict, outputs: tuple, symptom: str, upto: int | None = None) -> tuple:
    """What the function did in the respect the customer complains about, from (open-loop) outputs."""
    states, fcws, brakes = outputs
    n = log["rows"]
    end = n - 1 if upto is None else upto
    act = next((k for k in range(end + 1) if states[k] in (2, 3)), None)
    if symptom == "unexpected_braking":
        return (act is not None,)
    if symptom == "false_warning":
        return (any(fcws[:end + 1]) or act is not None,)
    if symptom == "no_braking":
        full = next((k for k in range(n) if states[k] == 3), None)
        late = max(brakes[max(0, n - 31):n]) if n else 0.0
        return (None if act is None else round(act * DT, 1), None if full is None else round(full * DT, 1), late >= 1.0)
    if symptom == "moved_after_stop":
        sp = log["cols"]["ego_kmh"]
        stop = next((k for k in range(n) if sp[k] < 0.36 and sp[0] > 1.0), None)
        return (None if stop is None else min(brakes[stop:min(n, stop + 100)]) >= 0.3,)
    return (act is not None, any(fcws[:end + 1]))


def _same(a: tuple, b: tuple) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if isinstance(x, float) and isinstance(y, float):
            if abs(x - y) > 0.2:
                return False
        elif x != y:
            return False
    return True


def _reaction_with(log: dict, ctrl, symptom: str) -> tuple:
    return reaction(log, replay(log, ctrl)["_outputs"], symptom, cutoff(log, symptom))


def recorded_reaction(log: dict, symptom: str) -> tuple:
    c = log["cols"]
    return reaction(log, (c["aeb_state"], c["fcw"], c["brake_req_mps2"]), symptom, cutoff(log, symptom))


def diagnose(log: dict | None, symptom: str, build: dict, baseline: dict | None, registry: list[dict], sid: str,
             prefer: str = "sil") -> dict:
    """Root cause of one complaint. -> {class, title, confidence, evidence[], ...}"""
    ev: list[str] = []
    out = {"class": "needs_engineer", "evidence": ev, "confidence": "medium", "culprits": [], "first_difference": None,
           "counterfactual": None, "reproduced": None, "reconstruction": None, "matching_build": None, "notes": []}
    if log is None:
        out.update({"class": "insufficient_data", "confidence": "high"})
        ev.append("No vehicle recording is attached, so the event cannot be reproduced. Request the event data recorder export.")
        return _final(out)
    an = L.analyze(log, symptom)
    out["event"] = an["event"]
    # 1. is the complained event in the recording at all?
    if not L.event_present(an, symptom) and an["event"]["kind"] == "none":
        out.update({"class": "insufficient_data", "confidence": "high"})
        what = {"unexpected_braking": "no AEB braking or warning", "false_warning": "no AEB warning", "no_braking": "no collision",
                "moved_after_stop": "no movement after a stop"}.get(symptom, "no AEB event")
        ev.append(f"The recording ({an['duration_s']:.1f} s) shows {what}"
                  + (f"; the driver braked at {an['driver_brake']['t']:.2f} s" if an["driver_brake"] else "")
                  + ". It does not contain the complained event: request the recording of the actual event.")
        return _final(out)
    # 2. does the recording match the software it claims to come from?
    ctrl, kind = B.controller(build, prefer)
    rep = replay(log, ctrl)
    out["reproduced"] = public(rep) | {"target": kind}
    if kind == "model":
        out["notes"].append("No C compiler / prebuilt library: replayed on the Python model; coding defects cannot be seen.")
    if not rep["reproduced"]:
        match = None
        for other in registry:
            if other["version"] == build["version"]:
                continue
            c2, _ = B.controller(other, prefer)
            if replay(log, c2)["reproduced"]:
                match = other["version"]
                break
        out.update({"class": "insufficient_data", "confidence": "high", "matching_build": match})
        ev.append(f"Replaying the recorded inputs on AEB {build['version']} does not reproduce the recorded outputs "
                  f"({rep['state_agreement']:.1%} of cycles agree; first difference at {rep['first_difference']['t']} s)."
                  + (f" The recording matches AEB {match} instead: correct the software version of this complaint and analyse again."
                     if match else " No registered build matches: the recording or the reported software version is wrong."))
        return _final(out)
    ev.append(f"Reproduced: the recorded inputs replayed on AEB {build['version']} ({'compiled C code' if kind == 'sil' else 'model'}) "
              f"give the recorded outputs in {rep['state_agreement']:.1%} of {rep['steps']} cycles.")
    if symptom == "unexpected_braking" and not an["activations"] and an["warnings"]:
        ev.append("The customer describes braking, but the recording shows a warning only (no AEB braking).")
    # 3. implementation: the model with the same calibration (the specification) vs the build's code
    cal = B.calibration(build)
    mrep = replay(log, B.model(cal))
    if kind == "sil" and not mrep["reproduced"]:
        out.update({"class": "implementation_defect", "confidence": "high", "first_difference": mrep["first_difference"]})
        fd = mrep["first_difference"]
        ev.append(f"The reference model with the same calibration behaves differently from the code: first difference at {fd['t']} s, "
                  f"model {fd['simulated']['state']} (brake {fd['simulated']['brake_req']} m/s²) vs code {fd['recorded']['state']} "
                  f"(brake {fd['recorded']['brake_req']} m/s²) at {fd['inputs']['ego_kmh']} km/h.")
        ev.append("The calibration is not the cause; the C implementation deviates from its specification.")
        _reconstruct_into(out, log, an, build, sid, symptom, prefer)
        return _final(out)
    # 4. calibration: would the last validated calibration have shown the same behaviour?
    if baseline and baseline["version"] != build["version"]:
        bcal = B.calibration(baseline)
        diff = [k for k in CAL_INFO if getattr(cal, k) != getattr(bcal, k)]
        if diff:
            field_sig = recorded_reaction(log, symptom)
            base_sig = _reaction_with(log, B.model(bcal), symptom)
            if not _same(base_sig, field_sig):
                culprits = []
                for k in diff:
                    probe = make_calibration({**(build.get("calibration") or {}), k: getattr(bcal, k)})
                    if not _same(_reaction_with(log, B.model(probe), symptom), field_sig):
                        culprits.append(k)
                out.update({"class": "calibration_regression", "confidence": "high" if culprits else "medium",
                            "culprits": [{"parameter": k, "field": getattr(cal, k), "validated": getattr(bcal, k),
                                          "meaning": CAL_INFO[k][3], "unit": CAL_INFO[k][2]} for k in culprits]})
                ev.append(f"With the last validated calibration (AEB {baseline['version']}) the same recorded inputs give a different "
                          f"reaction: the calibration change in AEB {build['version']} is responsible.")
                if culprits:
                    ev.append("Reverting one parameter at a time isolates " + ", ".join(
                        f"{k} ({getattr(cal, k):g} instead of the validated {getattr(bcal, k):g} {CAL_INFO[k][2]}: {CAL_INFO[k][3]})" for k in culprits)
                        + f"; the other change(s) ({', '.join(k for k in diff if k not in culprits) or 'none'}) are not involved.")
                else:
                    ev.append(f"No single parameter explains it; the combination of {', '.join(diff)} does.")
                _reconstruct_into(out, log, an, build, sid, symptom, prefer)
                if out["counterfactual"] and out["counterfactual"]["collision"] and symptom in ("unexpected_braking", "other"):
                    ev.append(f"Note: in this case the intervention prevented a collision at {out['counterfactual']['impact_kmh']:.0f} km/h, "
                              f"but it happened outside the validated behaviour.")
                return _final(out)
    # 5. behaviour as validated: what was it?
    _reconstruct_into(out, log, an, build, sid, symptom, prefer)
    tr = an["tracks"][an["event"]["track"]] if an["event"].get("track") is not None else None
    if symptom in ("unexpected_braking", "false_warning", "other") and an["event"]["kind"] in ("activation", "warning"):
        if tr and tr["phantom_like"]:
            out.update({"class": "sensor_false_target", "confidence": "high" if out["reconstruction"]["matched"] else "medium"})
            ev.append(f"The radar reported a stationary object at {tr['d_first_m']:.0f} m that disappeared after {tr['lifetime_s'] * 1000:.0f} ms "
                      f"while still {tr['d_last_m']:.0f} m ahead and the vehicle never reached it: a phantom (e.g. a bridge, gantry or metal "
                      f"plate reflection). The validated calibration reacts the same way, so the function behaved as specified.")
            return _final(out)
        cf = out["counterfactual"]
        if cf and cf["real_object"] and (cf["collision"] or (cf["closest_gap_m"] is not None and cf["closest_gap_m"] < 1.0)):
            out.update({"class": "correct_intervention", "confidence": "high" if out["reconstruction"]["matched"] else "medium"})
            ev.append("A real object was ahead for the whole approach. Simulated without AEB (with the driver's own braking as recorded) the "
                      "vehicle would have " + (f"hit it at {cf['impact_kmh']:.0f} km/h." if cf["collision"] else f"come within {cf['closest_gap_m']:.1f} m."))
            return _final(out)
        ev.append("The intervention followed a real object but the simulation without AEB shows no collision: the reaction may be earlier "
                  "than needed. Review the warning and braking thresholds against this situation.")
        return _final(out)
    if symptom == "no_braking" or an["event"]["kind"] == "crash":
        reasons = []
        kmh = an["event"]["kmh"] or 0.0
        vmax, vmin = cal.v_max_mps * 3.6, cal.v_min_mps * 3.6
        acts, warns = an["activations"], an["warnings"]
        v_ref = warns[0]["kmh"] if warns else (tr["ego_kmh_at_start"] if tr else 0.0)
        if tr and v_ref > vmax + 0.5 and (not acts or acts[0]["kmh"] <= vmax + 0.5):
            reasons.append(f"the vehicle was at {v_ref:.0f} km/h when the object became critical, above the {vmax:.0f} km/h limit for "
                           f"autonomous braking; a warning {'was' if warns else 'was not'} given"
                           + (f" and braking started at {acts[0]['kmh']:.0f} km/h, once the speed was inside the window" if acts else ""))
        if tr and tr["ego_kmh_at_start"] < vmin:
            reasons.append(f"the vehicle was below the {vmin:.0f} km/h activation speed")
        if an["friction_limited"]:
            full = [a for a in acts if a["max_state"] == 3]
            reasons.append(f"full braking was requested but only {max(a['max_decel'] for a in full):.1f} m/s² was achieved (low road friction)")
        if tr and tr["appeared_in_range"] and not tr["phantom_like"]:
            v = tr["ego_kmh_at_start"] / 3.6
            need = v * v / (2 * 9.0) + v * 0.35
            if tr["d_first_m"] < need:
                reasons.append(f"the object was first detected only {tr['d_first_m']:.0f} m ahead; at {tr['ego_kmh_at_start']:.0f} km/h about "
                               f"{need:.0f} m are needed to stop")
        if reasons:
            out.update({"class": "system_limitation", "confidence": "high" if out["reconstruction"] and out["reconstruction"]["matched"] else "medium"})
            ev.append("The function behaved as specified; " + "; ".join(reasons) + ".")
            return _final(out)
        ev.append(f"Collision at {kmh:.0f} km/h without a specified limitation that explains it.")
        return _final(out)
    ev.append("The automatic analysis found no rule that explains this event.")
    return _final(out)


def _reconstruct_into(out: dict, log: dict, an: dict, build: dict, sid: str, symptom: str, prefer: str) -> None:
    rc = reconstruct(log, an, build, sid, symptom, prefer)
    out["reconstruction"] = {"matched": rc["matched"], "basis": rc["basis"], "scenario": rc["scenario"], "attempts": rc["attempts"],
                             "latency_estimate_s": rc.get("latency_estimate_s"), "metrics": rc.get("metrics")}
    if rc["scenario"]:
        out["counterfactual"] = counterfactual(rc["scenario"])
    if rc["matched"]:
        out["evidence"].append(f"Rebuilt as a closed-loop scenario ({rc['basis']}); the simulation with the field build matches the recording"
                               + (f" after {len(rc['attempts'])} attempts" if len(rc["attempts"]) > 1 else "") + ".")
    else:
        out["evidence"].append(f"The closed-loop reconstruction ({rc['basis']}) did not fully match the recording after {len(rc['attempts'])} "
                               f"attempts; conclusions drawn from it are less certain.")
        out["confidence"] = "medium" if out["confidence"] == "high" else out["confidence"]


def _final(out: dict) -> dict:
    out["title"] = CLASSES[out["class"]]
    return out


def aggregate(rows: list[dict]) -> dict:
    """Issue-level root cause from the per-complaint diagnoses."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    if not counts:
        return {"class": "insufficient_data", "counts": {}, "consistent": True}
    top = max(counts.values())
    cls = next(c for c in PRIORITY if counts.get(c) == top)
    culprits = sorted({c["parameter"] for r in rows if r["class"] == "calibration_regression" for c in r["culprits"]})
    return {"class": cls, "title": CLASSES[cls], "counts": counts, "consistent": len(counts) == 1, "culprits": culprits}


# ============================================================================================ library checks
def library_run(scns: list[dict], reqs: list[dict], ctrl) -> dict:
    """Every scenario, every requirement. -> {failures: [{scenario, requirement, blocking, detail}], metrics}"""
    fails, metrics = [], {}
    for s in scns:
        m = simulate(s, ctrl)["metrics"]
        metrics[s["id"]] = m
        for r in reqs:
            if rq.applies(r, s):
                j = rq.judge(r, m)
                if j["verdict"] == "fail":
                    fails.append({"scenario": s["id"], "requirement": r["id"], "blocking": r["release_blocking"], "detail": j["detail"]})
    return {"failures": fails, "metrics": metrics, "scenarios": len(scns)}


def escape(field: dict, baseline: dict | None, verdict: str, scns: list[dict], reqs: list[dict], released: list[dict],
           prefer: str = "sil") -> dict:
    """Why the problem reached the field."""
    findings, kind = [], None
    cal = B.calibration(field)
    validated = field.get("validated")
    released_same = [c for c in released if c.get("calibration_changes") == calibration_changes(cal)]
    findings.append(f"AEB {field['version']} " + ("was released on validation evidence: " + (field.get("validation_evidence") or "(not named)")
                                                  if validated else "has no validation evidence in the build registry"
                                                  + (f" ({field['validation_evidence']})" if field.get("validation_evidence") else ""))
                    + ".")
    if released_same:
        findings.append(f"Released ADAS campaign(s) with this calibration: {', '.join(c['ref'] for c in released_same)}.")
    ctrl, target = B.controller(field, prefer)
    lib = library_run(scns, reqs, ctrl)
    base_fail = set()
    if baseline:
        bctrl, _ = B.controller(baseline, prefer)
        base_fail = {(f["scenario"], f["requirement"]) for f in library_run(scns, reqs, bctrl)["failures"]}
    new_fail = [f for f in lib["failures"] if (f["scenario"], f["requirement"]) not in base_fail and f["blocking"]]
    b2b = None
    if verdict == "implementation_defect" and target == "sil":
        from app.modules.adas import campaign as cmp
        recorded = cmp.record(scns, cal)
        b2b = cmp.back_to_back(recorded, ctrl, None if not field.get("calibration") else cal)
    if new_fail:
        kind = "process"
        f = new_fail[0]
        findings.append(f"The existing regression would have caught it: with this build {len(new_fail)} release-blocking check(s) fail, "
                        f"e.g. {f['requirement']} on {f['scenario']} ({f['detail']}). The build reached the field without that regression run.")
    elif b2b is not None and not b2b["passed"]:
        kind = "process"
        findings.append(f"No requirement fails with this build, but back-to-back against the model differs in {len(b2b['mismatching_scenarios'])} "
                        f"scenario(s) (e.g. {b2b['mismatching_scenarios'][0]}): a back-to-back run before release would have caught it.")
    elif verdict in MALFUNCTION:
        kind = "test_gap"
        findings.append(f"All {lib['scenarios']} regression scenarios pass with this build: no scenario covers the field situation. "
                        f"Add the reconstructed field scenarios to the regression library.")
    return {"kind": kind, "findings": findings, "regression_failures": len(lib["failures"]), "new_failures": new_fail[:5],
            "scenarios_checked": lib["scenarios"], "target": target,
            "back_to_back": None if b2b is None else {"passed": b2b["passed"], "mismatching": b2b["mismatching_scenarios"][:5]}}


# ============================================================================================ fix search
SENSOR_LEVERS = ("confirm_cycles", "areq_full_mps2", "ttc_partial_s")


def _values(param: str, field_v: float, base_v: float | None, need: float | None) -> list:
    lo, hi = CAL_INFO[param][0], CAL_INFO[param][1]
    vals = {field_v}
    if param == "confirm_cycles":
        b = int(base_v) if base_v is not None else int(field_v)
        cand = {int(field_v), b, b + 3, b + 6, (int(field_v) + b) // 2}
        if need is not None:
            cand |= {int(need), int(need) + 1, int(need) + 2}
        if base_v is None:
            cand = {int(field_v), int(field_v) + 5, int(field_v) + 10, int(field_v) + 20, int(field_v) + 30}
        vals = {v for v in cand if lo <= v <= hi}
    else:
        if base_v is not None:
            vals |= {round(field_v + (base_v - field_v) * f, 3) for f in (0.25, 0.5, 0.75, 1.0)}
        else:
            step = {"areq_full_mps2": (6.5, 7.0, 8.0, 9.0), "ttc_partial_s": (1.4, 1.25, 1.1)}.get(param, ())
            vals |= set(step)
        vals = {v for v in vals if lo <= v <= hi}
    return sorted(vals)


def _complained(scn_log: dict, symptom: str) -> bool:
    return L.event_present(L.analyze(scn_log, symptom), symptom)


def fix_search(cls: str, field: dict, baseline: dict | None, cases: list[dict], scns: list[dict], reqs: list[dict],
               registry: list[dict], culprits: list[str], prefer: str = "sil") -> dict:
    """cases: [{ref, symptom, scenario, log}] for the complaints of the issue."""
    if cls == "implementation_defect":
        return _rollback(field, cases, registry, prefer)
    if cls not in ("calibration_regression", "sensor_false_target"):
        return {"kind": "none", "summary": "No technical fix: the function behaved as specified.", "candidates": [], "recommended": None}
    cases = [c for c in cases if c.get("scenario")]
    if cls == "calibration_regression" and cases and all(c.get("justified") for c in cases):
        return {"kind": "validate", "candidates": [], "recommended": None,
                "summary": (f"The changed calibration ({', '.join(culprits) or 'see diagnosis'}) caused the reaction, but in all {len(cases)} "
                            f"complaint(s) it prevented a collision. Do not revert it blindly: validate the new calibration (campaign with "
                            f"scenarios for the extended behaviour) and update the requirements before a wider rollout.")}
    if not cases:
        return {"kind": "none", "summary": "No reconstructed scenario to verify a fix against.", "candidates": [], "recommended": None}
    margin = []
    for c in cases:
        s = c["scenario"]
        if s.get("ghost_at_s") is not None:
            margin.append({**s, "id": s["id"] + "-M", "ghost_duration_s": round(min(s["ghost_duration_s"] + 0.02, 2.0), 2)})
    fcal = B.calibration(field)
    bcal = B.calibration(baseline) if baseline else None
    levers = list(culprits) if cls == "calibration_regression" and culprits else list(SENSOR_LEVERS)
    need = None
    ghost_cycles = [int(round(c["scenario"]["ghost_duration_s"] / DT)) for c in cases if c["scenario"].get("ghost_at_s") is not None]
    if ghost_cycles:
        need = max(ghost_cycles) + 1 + 2                    # one more than the longest phantom, plus the 20 ms margin case
    base_lib = library_run(scns, reqs, B.controller(baseline or field, prefer, bcal or fcal)[0])
    base_fail = {(f["scenario"], f["requirement"]) for f in base_lib["failures"]}
    rows = []
    for p in levers:
        fv, bv = getattr(fcal, p), (getattr(bcal, p) if bcal and cls == "calibration_regression" else None)
        for v in _values(p, fv, bv, need if p == "confirm_cycles" else None):
            over = {**(field.get("calibration") or {}), p: v}
            try:
                cal = make_calibration(over)
            except ValueError as e:
                rows.append({"parameter": p, "value": v, "valid": False, "note": str(e)[:120]})
                continue
            ctrl, target = B.controller(field, prefer, cal)
            fixed = sum(not _complained(sim_log(c["scenario"], ctrl)[0], c["symptom"]) for c in cases)
            mfixed = sum(not _complained(sim_log(m, ctrl)[0], "unexpected_braking") for m in margin)
            lib = library_run(scns, reqs, ctrl)
            new = sorted([f for f in lib["failures"] if (f["scenario"], f["requirement"]) not in base_fail], key=lambda f: not f["blocking"])
            delays = [lib["metrics"][s]["brake_t"] - base_lib["metrics"][s]["brake_t"] for s in lib["metrics"]
                      if lib["metrics"][s]["brake_t"] is not None and base_lib["metrics"][s]["brake_t"] is not None
                      and s.split("-")[0] in ("CCRs", "CCRm", "CCRb")]
            rows.append({"parameter": p, "value": v, "is_field_value": v == fv, "is_validated_value": bv is not None and v == bv,
                         "fixes_complaints": f"{fixed}/{len(cases)}", "fixes_margin_cases": f"{mfixed}/{len(margin)}" if margin else "n/a",
                         "regression": "pass" if not new else f"{'fail' if new[0]['blocking'] else 'degrades'}: {new[0]['requirement']}"
                                                               f"{'' if new[0]['blocking'] else ' (exploratory)'} on {new[0]['scenario']}",
                         "new_failures": len(new), "brake_onset_change_ms": round(1000 * max(delays), 0) if delays else 0,
                         "target": target, "_ok": fixed == len(cases) and mfixed == len(margin) and not new, "_dist": abs(v - fv), "valid": True})
    ok = [r for r in rows if r.get("_ok")]
    rec = None
    if ok:
        best: dict[str, dict] = {}
        for r in ok:
            cur = best.get(r["parameter"])
            key = (r["_dist"], r["brake_onset_change_ms"])
            if cur is None or key < (cur["_dist"], cur["brake_onset_change_ms"]):
                best[r["parameter"]] = r
        if cls == "calibration_regression":
            chosen = {p: best[p]["value"] for p in levers if p in best}
        else:
            r0 = min(best.values(), key=lambda r: (r["brake_onset_change_ms"], r["_dist"]))
            chosen = {r0["parameter"]: r0["value"]}
        cal_over = {**(field.get("calibration") or {}), **chosen}
        try:
            cal = make_calibration(cal_over)
            ctrl, target = B.controller(field, prefer, cal)
            lib = library_run(scns, reqs, ctrl)
            fixed = sum(not _complained(sim_log(c["scenario"], ctrl)[0], c["symptom"]) for c in cases)
            ok_all = fixed == len(cases) and not [f for f in lib["failures"] if (f["scenario"], f["requirement"]) not in base_fail]
        except ValueError:
            ok_all = False
        if ok_all:
            rec = {"calibration": {k: cal_over[k] for k in sorted(cal_over)},
                   "changes": {p: {"from": getattr(fcal, p), "to": v} for p, v in chosen.items()},
                   "why": ("smallest change from the field calibration that removes the behaviour in every complaint"
                           + (" and in the +20 ms margin cases" if margin else "") + ", with no new failure in the regression library")
                          if cls == "calibration_regression" else
                          ("interim mitigation only: it removes the reaction to these phantom objects without a new regression "
                           "failure, but delays the reaction to real objects; the sensor supplier must remove the false target"),
                   "verified": {"complaints": f"{fixed}/{len(cases)}", "regression_scenarios": lib["scenarios"], "target": target}}
    for r in rows:
        r.pop("_dist", None)
        r["passes"] = bool(r.pop("_ok", False))
    summary = (f"{len(rows)} candidate(s) checked against {len(cases)} complaint scenario(s), {len(margin)} margin case(s) and "
               f"{base_lib['scenarios']} regression scenarios; " + (f"recommended: " + ", ".join(f"{p} {c['from']:g} → {c['to']:g}" for p, c in rec["changes"].items())
                                                                     if rec else "no calibration candidate passes everything"))
    return {"kind": "calibration" if cls == "calibration_regression" else "mitigation", "summary": summary, "candidates": rows,
            "recommended": rec, "baseline_failures": len(base_lib["failures"])}


def _rollback(field: dict, cases: list[dict], registry: list[dict], prefer: str) -> dict:
    by_v = {b["version"]: b for b in registry}
    chain, seen, v = [], set(), field.get("previous")
    while v and v in by_v and v not in seen:
        seen.add(v)
        chain.append(by_v[v])
        v = by_v[v].get("previous")
    chain += [b for b in registry if b.get("validated") and b["version"] not in seen and b["version"] != field["version"]]
    rows, pick = [], None
    for b in chain:
        ctrl, target = B.controller(b, prefer)
        clean = 0
        for c in cases:
            if c.get("log") is not None:
                clean += not _same(_reaction_with(c["log"], ctrl, c["symptom"]), recorded_reaction(c["log"], c["symptom"]))
        n = sum(1 for c in cases if c.get("log") is not None)
        rows.append({"version": b["version"], "validated": b.get("validated", False), "free_of_problem": f"{clean}/{n}", "target": target})
        if pick is None and n and clean == n and b.get("validated"):
            pick = b["version"]
    return {"kind": "rollback", "candidates": rows, "recommended": {"rollback_to": pick} if pick else None,
            "summary": (f"Containment: roll back to AEB {pick} (validated; none of the {len(cases)} recording(s) show the problem with it). "
                        f"The coding defect itself must be fixed in the C code and verified with back-to-back.") if pick else
                       "No validated earlier build is free of the problem; the code must be fixed before any rollout."}


# ============================================================================================ safety proposal
HAZARDS = {"unexpected_braking": "Unintended deceleration by AEB: rear-end collision by following traffic, loss of vehicle stability",
           "false_warning": "Unnecessary collision warning: driver startled, reacts inappropriately",
           "no_braking": "Missing or insufficient emergency braking: collision with the object ahead",
           "moved_after_stop": "Vehicle not held after an automatic stop: rolls into the object ahead",
           "other": "AEB behaving differently than specified"}


def safety_proposal(symptom: str, cls: str, analyses: list[dict], complaints: list[dict], fleet: int) -> dict:
    """S/E/C proposal with rationale; a person confirms it. The ASIL comes from the HARA rule table."""
    acts = [a for an in analyses for a in an["activations"]]
    kmh = max((a["kmh"] for a in acts), default=max((an["event"]["kmh"] or 0.0 for an in analyses), default=0.0))
    req = max((a["max_brake_req"] for a in acts), default=0.0)
    dec = max((a["max_decel"] for a in acts), default=0.0)
    crash = any(an["crash"] for an in analyses)
    reported_crash = any(c.get("crash") for c in complaints)
    injury = any(c.get("injury") for c in complaints)
    why = []
    if symptom in ("unexpected_braking", "other"):
        s = 2 if (req >= 6.0 and kmh >= 50) else 1
        why.append(f"S{s}: unintended {'full' if req >= 6 else 'partial'} braking requested at up to {kmh:.0f} km/h; a following vehicle may hit "
                   f"the car ({'severe injuries possible at this speed' if s == 2 else 'light to moderate injuries'}).")
        c = 3 if dec >= 6.0 else 2 if dec >= 3.0 else 1
        why.append(f"C{c}: achieved deceleration up to {dec:.1f} m/s² without warning to the following driver.")
    elif symptom == "false_warning":
        s, c = 1, 1
        why.append("S1: a warning alone does not decelerate the vehicle; only an inappropriate driver reaction can cause harm.")
        why.append("C1: most drivers keep control after an unexpected warning.")
    elif symptom == "no_braking":
        imp = max((an["crash"]["kmh"] for an in analyses if an["crash"]), default=kmh)
        s = 3 if imp >= 60 else 2 if imp >= 30 else 1
        why.append(f"S{s}: collision at up to {imp:.0f} km/h when the expected emergency braking did not happen.")
        c = 2
        why.append("C2: the driver can still brake, but AEB exists for the cases where they do not.")
    else:
        s, c = 1, 1
        why.append("S1: low-speed contact after a stop.")
        why.append("C1: the driver can hold the vehicle with the brake pedal.")
    e = 4 if kmh >= 30 else 3
    why.insert(1, f"E{e}: the triggering situation ({'normal driving at road speed' if e == 4 else 'low-speed manoeuvres'}) occurs "
                  f"{'very often' if e == 4 else 'regularly'}; {len(complaints)} complaint(s) from {fleet:,} vehicles on this build.")
    if cls not in MALFUNCTION:
        return {"applicable": False, "reason": "No malfunction: the function behaved as specified, so no new hazard assessment is needed.",
                "severity": None, "exposure": None, "controllability": None, "asil": None, "rationale": [], "escalate": False, "hazard": None}
    escalate = injury or crash or reported_crash or hara_rules.ASIL_RANK[hara_rules.asil(s, e, c)] >= 2
    return {"applicable": True, "hazard": HAZARDS.get(symptom, HAZARDS["other"]), "severity": s, "exposure": e, "controllability": c,
            "asil": hara_rules.asil(s, e, c), "rationale": why, "ruleset": hara_rules.RULESET_VERSION,
            "escalate": bool(escalate),
            "escalation_reason": ("injury reported" if injury else "collision recorded or reported" if (crash or reported_crash) else
                                  "ASIL B or higher" if escalate else "")}
