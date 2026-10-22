"""Reference model of the AEB (Autonomous Emergency Braking) function: the 'model' in
model-in-the-loop (MIL). The production-style C implementation (firmware/aeb/aeb_ctrl.c) must behave
the same way; back-to-back tests compare the two on every scenario.

Function, every 10 ms:
  1. An in-path object must be reported and closing for 150 ms before the function reacts
     (robustness against short false detections, a SOTIF concern: unintended braking).
  2. Threat measures:
       TTC   = distance / closing speed (constant-velocity assumption)
       a_req = deceleration the ego vehicle needs to stay 1.5 m behind the object. The object's own
               deceleration is estimated from its speed over the last 200 ms (ego speed - closing
               speed); if it is braking, the formula accounts for it (lead vehicle braking hard).
  3. Staged response: warning (TTC < 2.6 s or a_req > 3.5 m/s^2) -> partial braking 4 m/s^2
     (TTC < 1.6 s) -> full braking 9 m/s^2 (TTC < 1.0 s or a_req > 6 m/s^2). Braking is never stepped
     down while the object is being closed in on; it is released when the object is lost or, above
     10 km/h, when it is clearly moving away (closing speed below -0.2 m/s, e.g. a slower lead
     vehicle after the speeds have matched). Below 10 km/h braking is completed to standstill.
  4. Warnings from 10 km/h; new braking only between 10 and 85 km/h.
  5. At standstill after autonomous braking, hold the vehicle (3 m/s^2) while the object is present.
  6. The brake request sent to the brake system is the larger of the AEB and the driver request.

Calibration values are example values for a demonstration, not a validated production calibration."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace

OFF, WARN, PARTIAL, FULL, HOLD = 0, 1, 2, 3, 4
STATE_NAMES = {OFF: "off", WARN: "warning", PARTIAL: "partial", FULL: "full", HOLD: "hold"}
HIST_LEN = 20                         # target-speed history: 20 x 10 ms = 200 ms
CYCLE_S = 0.01


@dataclass(frozen=True)
class Calibration:
    v_min_mps: float = 2.77           # 10 km/h (2.778 m/s) with margin for rounding
    v_max_mps: float = 23.62          # 85 km/h
    confirm_cycles: int = 15          # 150 ms at 10 ms
    closing_min_mps: float = 0.5
    release_closing_mps: float = -0.2
    ttc_warn_s: float = 2.6
    ttc_partial_s: float = 1.6
    ttc_full_s: float = 1.0
    areq_warn_mps2: float = 3.5
    areq_full_mps2: float = 6.0
    a_tgt_min_mps2: float = 1.0       # below this the object is treated as not braking (estimate noise)
    a_partial_mps2: float = 4.0
    a_full_mps2: float = 9.0
    a_hold_mps2: float = 3.0
    stop_gap_m: float = 1.5
    standstill_mps: float = 0.1
    ttc_max_s: float = 99.0


CAL = Calibration()

# what-if limits for calibration changes made through the API/UI: (min, max, unit, meaning)
CAL_INFO = {
    "v_min_mps": (0.0, 10.0, "m/s", "lowest speed for warnings and new braking"),
    "v_max_mps": (5.0, 45.0, "m/s", "highest speed for new braking"),
    "confirm_cycles": (1, 100, "cycles of 10 ms", "object confirmation time"),
    "closing_min_mps": (0.1, 3.0, "m/s", "closing speed counted as approaching"),
    "release_closing_mps": (-3.0, 0.0, "m/s", "braking released below this closing speed"),
    "ttc_warn_s": (0.5, 5.0, "s", "TTC for the forward collision warning"),
    "ttc_partial_s": (0.3, 4.0, "s", "TTC for partial braking"),
    "ttc_full_s": (0.2, 3.0, "s", "TTC for full braking"),
    "areq_warn_mps2": (1.0, 10.0, "m/s²", "required deceleration for the warning"),
    "areq_full_mps2": (2.0, 12.0, "m/s²", "required deceleration for full braking"),
    "a_tgt_min_mps2": (0.2, 5.0, "m/s²", "object deceleration treated as braking"),
    "a_partial_mps2": (1.0, 10.0, "m/s²", "partial braking request"),
    "a_full_mps2": (2.0, 12.0, "m/s²", "full braking request"),
    "a_hold_mps2": (0.5, 6.0, "m/s²", "hold request at standstill"),
    "stop_gap_m": (0.5, 5.0, "m", "intended gap after stopping"),
    "standstill_mps": (0.01, 1.0, "m/s", "speed treated as standstill"),
    "ttc_max_s": (10.0, 999.0, "s", "TTC reported when not closing"),
}


def make_calibration(overrides: dict | None) -> Calibration:
    """Default calibration with validated changes; raises ValueError."""
    overrides = overrides or {}
    unknown = set(overrides) - set(CAL_INFO)
    if unknown:
        raise ValueError(f"unknown calibration parameters: {sorted(unknown)}")
    vals = {}
    for k, v in overrides.items():
        lo, hi = CAL_INFO[k][:2]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"{k} must be between {lo} and {hi} {CAL_INFO[k][2]}")
        vals[k] = int(v) if k == "confirm_cycles" else float(v)
    cal = replace(CAL, **vals)
    if not cal.ttc_full_s < cal.ttc_partial_s < cal.ttc_warn_s:
        raise ValueError("TTC thresholds must satisfy full < partial < warning")
    if not cal.areq_warn_mps2 < cal.areq_full_mps2:
        raise ValueError("areq_warn_mps2 must be below areq_full_mps2")
    if not cal.v_min_mps < cal.v_max_mps:
        raise ValueError("v_min_mps must be below v_max_mps")
    if not cal.a_partial_mps2 < cal.a_full_mps2:
        raise ValueError("a_partial_mps2 must be below a_full_mps2")
    return cal


def calibration_changes(cal: Calibration) -> dict:
    return {f.name: {"default": getattr(CAL, f.name), "value": getattr(cal, f.name)}
            for f in fields(Calibration) if getattr(cal, f.name) != getattr(CAL, f.name)}


def ttc(distance: float, closing: float, cal: Calibration = CAL) -> float:
    return distance / closing if closing > 0.1 else cal.ttc_max_s


def required_decel(distance: float, closing: float, v_ego: float = 0.0, a_tgt: float = 0.0,
                   cal: Calibration = CAL) -> float:
    """Deceleration the ego vehicle needs so that it ends up stop_gap behind the object.
    a_tgt is the object's deceleration (positive = braking)."""
    d_eff = max(distance - cal.stop_gap_m, 0.1)
    if a_tgt < cal.a_tgt_min_mps2:
        return closing * closing / (2.0 * d_eff) if closing > 0.0 else 0.0
    v_tgt = max(v_ego - closing, 0.0)
    if closing > 0.0 and 2.0 * d_eff * a_tgt < v_tgt * closing:
        # the speeds match before the object has stopped: relative motion with both braking
        return a_tgt + closing * closing / (2.0 * d_eff)
    # the object stops first: stop behind its stopping point
    return v_ego * v_ego / (2.0 * (d_eff + v_tgt * v_tgt / (2.0 * a_tgt)))


class AebModel:
    """Same interface as the SIL wrapper: reset() and step(inputs) -> outputs."""
    name = "model"

    def __init__(self, cal: Calibration = CAL):
        self.cal = cal
        self.reset()

    def reset(self) -> None:
        self.confirm = 0
        self.state = OFF
        self.hist = [0.0] * HIST_LEN
        self.hist_i = 0
        self.hist_n = 0

    def _target_decel(self, v: float, valid: bool, closing: float) -> float:
        """Object deceleration from its speed now and 200 ms ago (0 until the history is full)."""
        if not valid:
            self.hist_n = 0
            return 0.0
        v_tgt = v - closing
        old = self.hist[self.hist_i]
        full = self.hist_n >= HIST_LEN
        self.hist[self.hist_i] = v_tgt
        self.hist_i = (self.hist_i + 1) % HIST_LEN
        if not full:
            self.hist_n += 1
            return 0.0
        return max((old - v_tgt) / (HIST_LEN * CYCLE_S), 0.0)

    def _decide(self, v: float, valid: bool, closing: float, confirmed: bool, t: float, areq: float) -> int:
        c, s = self.cal, self.state
        if s == HOLD:
            return HOLD if valid else OFF
        if s in (PARTIAL, FULL):
            return self._continue_braking(v, valid, closing, confirmed, t, areq)
        if not confirmed or v < c.v_min_mps:
            return OFF
        if v <= c.v_max_mps:
            if t < c.ttc_full_s or areq > c.areq_full_mps2:
                return FULL
            if t < c.ttc_partial_s:
                return PARTIAL
        return WARN if (t < c.ttc_warn_s or areq > c.areq_warn_mps2) else OFF

    def _continue_braking(self, v: float, valid: bool, closing: float, confirmed: bool, t: float, areq: float) -> int:
        c = self.cal
        if v < c.standstill_mps:
            return HOLD
        if not valid or (closing < c.release_closing_mps and v >= c.v_min_mps):
            return OFF
        if confirmed and (t < c.ttc_full_s or areq > c.areq_full_mps2):
            return FULL
        return self.state

    def step(self, ego_speed: float, obj_valid: bool, distance: float, closing: float, driver_brake: float) -> dict:
        c = self.cal
        if obj_valid and closing > c.closing_min_mps:
            self.confirm = min(self.confirm + 1, 65535)
        else:
            self.confirm = 0
        a_tgt = self._target_decel(ego_speed, obj_valid, closing)
        confirmed = self.confirm >= c.confirm_cycles
        t = ttc(distance, closing, c) if confirmed else c.ttc_max_s
        areq = required_decel(distance, closing, ego_speed, a_tgt, c) if confirmed else 0.0
        self.state = self._decide(ego_speed, obj_valid, closing, confirmed, t, areq)
        aeb = {PARTIAL: c.a_partial_mps2, FULL: c.a_full_mps2, HOLD: c.a_hold_mps2}.get(self.state, 0.0)
        return {"brake_req": max(aeb, driver_brake), "aeb_req": aeb, "ttc": t, "a_req": areq, "a_tgt": a_tgt,
                "fcw": self.state in (WARN, PARTIAL, FULL), "state": self.state}
