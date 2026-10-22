"""Closed-loop longitudinal simulation for AEB scenarios (10 ms step).

  ego vehicle     constant speed until braking; brake actuator modelled as a first-order lag
                  (tau = 0.15 s); achievable deceleration limited by road friction (mu * g);
                  creep (0.3 m/s^2) below 2 m/s when the brakes are released, like an automatic/EV
  target vehicle  none, stationary, constant speed, or braking at a given time
  sensor          radar-like: max range, latency (delay line), seeded Gaussian noise on distance and
                  closing speed, optional phantom ("ghost") object for a short time
  driver          optional brake request from a given time, or a given time after the warning
  end             collision, 5 s after standstill, 3 s after an intervention ended behind a moving
                  vehicle, or the scenario duration

Deliberately 1-D (longitudinal, 100 % overlap): lateral offsets, curves, pedestrians and sensor
fusion are out of scope. It is a regression and comparison harness for the control logic, not a
replacement for vehicle-dynamics simulation or proving-ground tests."""
from __future__ import annotations

import random
from collections import deque

G = 9.81
DT = 0.01
TAU_BRAKE = 0.15
CREEP_MPS2 = 0.3
POST_STOP_S = 5.0
SETTLE_S = 3.0


def simulate(scn: dict, controller, trace_every: int = 5, keep_series: bool = False) -> dict:
    """Run one concrete scenario with a controller exposing reset() and
    step(ego_speed, obj_valid, distance, closing, driver_brake) -> {brake_req, aeb_req, ttc, fcw, state}.
    keep_series=True also returns every controller input and output (for back-to-back replay)."""
    rnd = random.Random(scn.get("seed", 1))
    controller.reset()
    v_e = scn["ego_kmh"] / 3.6
    v0 = v_e
    kind = scn.get("target", "none")
    has_target = kind != "none"
    v_t = scn.get("target_kmh", 0.0) / 3.6 if kind in ("moving", "braking") else 0.0
    x_e, x_t = 0.0, scn.get("gap_m", 100.0)
    a_act = 0.0
    mu = scn.get("mu", 1.0)
    rng_max = scn.get("sensor_range_m", 150.0)
    lat_steps = max(0, round(scn.get("latency_s", 0.05) / DT))
    noise = scn.get("noise", True)
    delay: deque = deque()
    ghost_at, ghost_len, ghost_d = scn.get("ghost_at_s"), scn.get("ghost_duration_s", 0.0), scn.get("ghost_distance_m", 20.0)
    drv_at, drv_a = scn.get("driver_brake_at_s"), scn.get("driver_brake_mps2", 0.0)
    drv_react = scn.get("driver_fcw_reaction_s")                    # driver brakes this long after the warning
    duration = scn.get("duration_s", 30.0)

    m = {"collided": False, "impact_kmh": 0.0, "min_gap_m": x_t if has_target else None, "min_ttc_s": None,
         "fcw_t": None, "brake_t": None, "full_t": None, "max_brake_req": 0.0, "max_aeb_req": 0.0, "max_decel": 0.0,
         "driver_respected": True, "stopped": False, "stop_gap_m": None, "end_t": 0.0, "activations": 0,
         "brake_kmh": None, "driver_brake_t": None}
    trace, series = [], []
    stop_t, stop_x, prev_brake, settle_t = None, 0.0, False, None
    steps = int(duration / DT)
    for k in range(steps):
        t = k * DT
        if kind == "braking" and t >= scn.get("target_brake_at_s", 1.0):
            v_t = max(0.0, v_t - scn.get("target_decel_mps2", 0.0) * DT)
        if has_target:
            x_t += v_t * DT
        gap, closing = x_t - x_e, v_e - v_t

        valid = has_target and 0.0 < gap <= rng_max
        d_meas, c_meas = gap, closing
        if ghost_at is not None and ghost_at <= t < ghost_at + ghost_len:
            valid, d_meas, c_meas = True, ghost_d - v_e * (t - ghost_at), v_e     # phantom stationary object
        if noise and valid:
            d_meas += rnd.gauss(0.0, 0.05)
            c_meas += rnd.gauss(0.0, 0.05)
        delay.append((valid, d_meas, c_meas))
        meas = delay.popleft() if len(delay) > lat_steps else (False, 0.0, 0.0)

        if drv_react is not None and drv_at is None and m["fcw_t"] is not None:
            drv_at = m["fcw_t"] + drv_react
        driver = drv_a if (drv_at is not None and t >= drv_at - 1e-9) else 0.0
        inp = (v_e, bool(meas[0]), float(meas[1]), float(meas[2]), driver)
        out = controller.step(*inp)
        if keep_series:
            series.append((inp, (out["brake_req"], int(out["state"]), bool(out["fcw"]), out["ttc"])))
        a_cmd = min(max(out["brake_req"], 0.0), mu * G)
        a_act += (a_cmd - a_act) * DT / TAU_BRAKE
        accel = CREEP_MPS2 if (a_act < 0.05 and v_e < 2.0) else -a_act
        v_e = max(0.0, v_e + accel * DT)
        x_e += v_e * DT

        braking = out["state"] in (2, 3)
        if braking and not prev_brake:
            m["activations"] += 1
        prev_brake = braking
        if out["fcw"] and m["fcw_t"] is None:
            m["fcw_t"] = round(t, 2)
        if braking and m["brake_t"] is None:
            m["brake_t"], m["brake_kmh"] = round(t, 2), round(inp[0] * 3.6, 2)
        if driver > 0 and m["driver_brake_t"] is None:
            m["driver_brake_t"] = round(t, 2)
        if out["state"] == 3 and m["full_t"] is None:
            m["full_t"] = round(t, 2)
        m["max_brake_req"] = max(m["max_brake_req"], out["brake_req"])
        m["max_aeb_req"] = max(m["max_aeb_req"], out["aeb_req"])
        m["max_decel"] = max(m["max_decel"], a_act)
        if out["brake_req"] < driver - 1e-6:
            m["driver_respected"] = False
        if has_target:
            gap = x_t - x_e
            m["min_gap_m"] = min(m["min_gap_m"], gap)
            if closing > 0.1:
                tt = gap / closing
                m["min_ttc_s"] = tt if m["min_ttc_s"] is None else min(m["min_ttc_s"], tt)
            if gap <= 0.0:
                m["collided"], m["impact_kmh"] = True, round(max(0.0, v_e - v_t) * 3.6, 2)
                m["end_t"] = round(t, 2)
                break
        if k % trace_every == 0:
            trace.append({"t": round(t, 2), "gap": round(x_t - x_e, 2) if has_target else None, "v_ego_kmh": round(v_e * 3.6, 2),
                          "v_target_kmh": round(v_t * 3.6, 2) if has_target else None, "brake_req": round(out["brake_req"], 2),
                          "decel": round(a_act, 2), "fcw": bool(out["fcw"]), "state": int(out["state"])})
        if m["brake_t"] is not None and out["state"] == 0 and closing <= 0.0 and not driver:
            settle_t = t if settle_t is None else settle_t                 # intervention over, speeds matched
            if t - settle_t >= SETTLE_S:
                m["end_t"] = round(t, 2)
                break
        else:
            settle_t = None
        if v_e < 0.01 and v0 > 0:
            if stop_t is None:
                stop_t, stop_x = t, x_e
                m["stopped"] = True
                m["stop_gap_m"] = round(x_t - x_e, 2) if has_target else None
            elif t - stop_t >= POST_STOP_S:
                break
        m["end_t"] = round(t, 2)
    m["moved_after_stop_m"] = round(x_e - stop_x, 3) if stop_t is not None else None
    for key in ("min_gap_m", "min_ttc_s", "max_brake_req", "max_aeb_req", "max_decel"):
        if m[key] is not None:
            m[key] = round(m[key], 3)
    m["initial_kmh"] = scn["ego_kmh"]
    m["initial_closing_kmh"] = round(scn["ego_kmh"] - (scn.get("target_kmh", 0.0) if has_target else scn["ego_kmh"]), 2)
    m["fcw_lead_s"] = round(m["brake_t"] - m["fcw_t"], 2) if m["fcw_t"] is not None and m["brake_t"] is not None else None
    return {"metrics": m, "trace": trace, "series": series if keep_series else None}

