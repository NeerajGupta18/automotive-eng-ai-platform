"""Vehicle field logs: the recording a dealer or the vehicle's event data recorder delivers with a complaint, and
what can be read from it before any simulation.

Format (CSV, one row per 10 ms ECU cycle; signals as the AEB ECU saw and sent them on the vehicle bus):

    # AEAI field log v1
    # vin=MAT612345K1A00001
    # ecu=AEB
    # sw_version=2.4.0                  (read from the ECU over UDS, DID F189)
    # trigger=aeb                       (aeb | crash | manual)
    # recorded_at=2026-09-14T08:12:44+05:30
    t_s,ego_kmh,obj_valid,obj_dist_m,obj_closing_mps,driver_brake_mps2,aeb_state,fcw,brake_req_mps2,decel_mps2
    0.00,80.000,0,0.000,0.0000,0.00,0,0,0.000,0.000

The first six signals are the AEB function's inputs (ego speed, radar object, driver brake request); aeb_state, fcw
and brake_req are its outputs; decel is the measured vehicle deceleration (wheel speeds / IMU).

Untrusted input: size and row limits, a fixed column set, numeric ranges, a monotonic 10 ms time base. Nothing in a
log is ever executed or interpreted beyond these numbers."""
from __future__ import annotations

import csv
import io
import math
import re

COLUMNS = ("t_s", "ego_kmh", "obj_valid", "obj_dist_m", "obj_closing_mps", "driver_brake_mps2", "aeb_state", "fcw",
           "brake_req_mps2", "decel_mps2")
RANGES = {"t_s": (0.0, 600.0), "ego_kmh": (0.0, 250.0), "obj_valid": (0, 1), "obj_dist_m": (0.0, 400.0),
          "obj_closing_mps": (-80.0, 80.0), "driver_brake_mps2": (0.0, 15.0), "aeb_state": (0, 4), "fcw": (0, 1),
          "brake_req_mps2": (0.0, 15.0), "decel_mps2": (-15.0, 15.0)}
HEADER_KEYS = ("vin", "ecu", "sw_version", "trigger", "recorded_at", "source", "note")
TRIGGERS = ("aeb", "crash", "manual")
MAX_BYTES = 3 * 1024 * 1024
MAX_ROWS = 30_000
MIN_ROWS = 100
DT = 0.01
STATE_NAMES = {0: "off", 1: "warning", 2: "partial braking", 3: "full braking", 4: "hold"}
_SAFE = re.compile(r"^[A-Za-z0-9 _.:+\-/()]{0,80}$")


class LogError(ValueError):
    pass


def parse(content: bytes | str) -> dict:
    """-> {"header": {...}, "rows": n, "cols": {name: [values]}}; raises LogError with a readable message."""
    if isinstance(content, bytes):
        if len(content) > MAX_BYTES:
            raise LogError(f"log larger than {MAX_BYTES // (1024 * 1024)} MB")
        try:
            content = content.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise LogError("log is not UTF-8 text (expected the CSV export of the event data recorder)") from e
    elif len(content) > MAX_BYTES:
        raise LogError(f"log larger than {MAX_BYTES // (1024 * 1024)} MB")
    header, body = {}, []
    for n, line in enumerate(content.splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            kv = s[1:].strip()
            if "=" in kv:
                k, v = (x.strip() for x in kv.split("=", 1))
                if k in HEADER_KEYS:
                    if not _SAFE.match(v):
                        raise LogError(f"line {n}: header value for '{k}' has unexpected characters")
                    header[k] = v
            continue
        body.append(s)
    if not body:
        raise LogError("no data rows")
    reader = csv.reader(io.StringIO("\n".join(body)))
    names = [c.strip() for c in next(reader)]
    if sorted(names) != sorted(COLUMNS):
        missing, extra = sorted(set(COLUMNS) - set(names)), sorted(set(names) - set(COLUMNS))
        raise LogError(f"columns must be exactly {', '.join(COLUMNS)}" + (f"; missing {missing}" if missing else "")
                       + (f"; unexpected {extra}" if extra else ""))
    idx = {c: names.index(c) for c in COLUMNS}
    cols: dict[str, list] = {c: [] for c in COLUMNS}
    for i, row in enumerate(reader, 2):
        if len(cols["t_s"]) >= MAX_ROWS:
            raise LogError(f"more than {MAX_ROWS} rows")
        if len(row) != len(names):
            raise LogError(f"data row {i}: {len(row)} values, expected {len(names)}")
        for c in COLUMNS:
            try:
                v = float(row[idx[c]])
            except ValueError as e:
                raise LogError(f"data row {i}: {c} is not a number") from e
            lo, hi = RANGES[c]
            if not math.isfinite(v) or not lo <= v <= hi:
                raise LogError(f"data row {i}: {c}={v:g} outside {lo:g}..{hi:g}")
            cols[c].append(int(round(v)) if c in ("obj_valid", "aeb_state", "fcw") else v)
    n = len(cols["t_s"])
    if n < MIN_ROWS:
        raise LogError(f"only {n} rows; at least {MIN_ROWS} (1 s) are needed")
    t = cols["t_s"]
    for k in range(1, n):
        step = t[k] - t[k - 1]
        if not 0.008 <= step <= 0.012:
            raise LogError(f"time base: step {step:.3f} s at t={t[k]:.2f} s (expected 0.01 s, no gaps)")
    trig = header.get("trigger", "manual")
    if trig not in TRIGGERS:
        raise LogError(f"trigger must be one of {list(TRIGGERS)}")
    header["trigger"] = trig
    return {"header": header, "rows": n, "cols": cols}


def to_csv(header: dict, rows: list[tuple]) -> str:
    """rows: (t, ego_kmh, valid, dist, closing, driver, state, fcw, brake_req, decel)."""
    out = ["# AEAI field log v1"] + [f"# {k}={header[k]}" for k in HEADER_KEYS if header.get(k) not in (None, "")]
    out.append(",".join(COLUMNS))
    for r in rows:
        out.append(f"{r[0]:.2f},{r[1]:.3f},{int(r[2])},{r[3]:.3f},{r[4]:.4f},{r[5]:.2f},{int(r[6])},{int(r[7])},{r[8]:.3f},{r[9]:.3f}")
    return "\n".join(out) + "\n"


def inputs(log: dict) -> list[tuple]:
    """Controller inputs per cycle, as (ego m/s, valid, distance, closing, driver brake)."""
    c = log["cols"]
    return [(c["ego_kmh"][k] / 3.6, bool(c["obj_valid"][k]), c["obj_dist_m"][k], c["obj_closing_mps"][k], c["driver_brake_mps2"][k])
            for k in range(log["rows"])]


def recorded_outputs(log: dict) -> list[tuple]:
    c = log["cols"]
    return [(c["aeb_state"][k], bool(c["fcw"][k]), c["brake_req_mps2"][k]) for k in range(log["rows"])]


# ------------------------------------------------------------------------------------------------ analysis
def _segments(flags: list[bool]) -> list[tuple[int, int]]:
    out, start = [], None
    for k, f in enumerate(flags):
        if f and start is None:
            start = k
        elif not f and start is not None:
            out.append((start, k - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def tracks(log: dict) -> list[dict]:
    """Radar object tracks: when an object was reported, how it moved and whether it behaved like a real object."""
    c, n = log["cols"], log["rows"]
    out = []
    crash_end = log["header"].get("trigger") == "crash"
    for a, b in _segments([bool(v) for v in c["obj_valid"]]):
        ego = [c["ego_kmh"][k] for k in range(a, b + 1)]
        tgt = [c["ego_kmh"][k] - c["obj_closing_mps"][k] * 3.6 for k in range(a, b + 1)]
        head = tgt[: min(len(tgt), 50)]
        tgt_first = sorted(head)[len(head) // 2]
        tail = tgt[-min(len(tgt), 20):]
        tgt_last = sorted(tail)[len(tail) // 2]
        d_first, d_last = c["obj_dist_m"][a], c["obj_dist_m"][b]
        ended_in_log = b < n - 1
        vanished_ahead = ended_in_log and d_last > 3.0 and (n - 1 - b) >= 20
        moving_ego = max(ego) > 5.0
        stationary = moving_ego and abs(tgt_first) < 4.0
        out.append({"t_start": _r(c["t_s"][a]), "t_end": _r(c["t_s"][b]), "cycles": b - a + 1, "lifetime_s": _r((b - a + 1) * DT),
                    "d_first_m": _r(d_first), "d_last_m": _r(d_last), "target_kmh_first": _r(tgt_first, 1), "target_kmh_last": _r(tgt_last, 1),
                    "ego_kmh_at_start": _r(ego[0], 1), "stationary": stationary, "vanished_ahead": vanished_ahead,
                    "appeared_in_range": d_first < 140.0, "until_end": not ended_in_log, "ended_with_crash": crash_end and not ended_in_log,
                    "phantom_like": bool(vanished_ahead and stationary and d_last < 100.0 and (b - a + 1) * DT < 2.5),
                    "_a": a, "_b": b})
    return out


def analyze(log: dict, symptom: str = "other") -> dict:
    """Events and features of one recording; `event` is the moment the complaint is most likely about."""
    c, n, h = log["cols"], log["rows"], log["header"]
    t = c["t_s"]
    trk = tracks(log)
    acts = []
    for a, b in _segments([s in (2, 3) for s in c["aeb_state"]]):
        acts.append({"t": _r(t[a]), "t_end": _r(t[b]), "kmh": _r(c["ego_kmh"][a], 1), "max_state": max(c["aeb_state"][a:b + 1]),
                     "max_brake_req": _r(max(c["brake_req_mps2"][a:b + 1])), "max_decel": _r(max(c["decel_mps2"][a:min(n, b + 30)])),
                     "duration_s": _r((b - a + 1) * DT), "_a": a})
    warns = [{"t": _r(t[a]), "t_end": _r(t[b]), "kmh": _r(c["ego_kmh"][a], 1), "_a": a}
             for a, b in _segments([bool(f) for f in c["fcw"]])]
    drv = next((k for k in range(n) if c["driver_brake_mps2"][k] > 0.05), None)
    driver = None if drv is None else {"t": _r(t[drv]), "max_mps2": _r(max(c["driver_brake_mps2"])), "kmh": _r(c["ego_kmh"][drv], 1)}
    stop = next((k for k in range(n) if c["ego_kmh"][k] < 0.36 and c["ego_kmh"][0] > 1.0), None)
    stopped = None
    if stop is not None:
        moved = sum(c["ego_kmh"][k] / 3.6 * DT for k in range(stop, n))
        held = any(c["aeb_state"][k] == 4 for k in range(stop, n))
        stopped = {"t": _r(t[stop]), "moved_after_m": _r(moved, 3), "aeb_hold": held,
                   "aeb_stopped": any(c["aeb_state"][k] in (2, 3) for k in range(max(0, stop - 300), stop + 1)),
                   "obj_dist_m": _r(c["obj_dist_m"][stop]) if c["obj_valid"][stop] else None}
    crash = None
    if h.get("trigger") == "crash" or (c["obj_valid"][-1] and c["obj_dist_m"][-1] < 0.6 and c["obj_closing_mps"][-1] > 0.3):
        crash = {"t": _r(t[-1]), "kmh": _r(c["ego_kmh"][-1], 1), "closing_kmh": _r(c["obj_closing_mps"][-1] * 3.6, 1)}
    full = [a for a in acts if a["max_state"] == 3]
    friction_limited = bool(full and max(a["max_decel"] for a in full) < 0.85 * max(a["max_brake_req"] for a in full)
                            and max(a["duration_s"] for a in full) > 0.6)

    def track_at(k: int):
        return next((x for x in trk if x["_a"] <= k <= x["_b"]), None)

    event = {"kind": "none", "t": None, "kmh": None}
    pick = None
    if symptom in ("unexpected_braking", "other"):
        pick = ("activation", acts[0]) if acts else ("warning", warns[0]) if warns else None
    elif symptom == "false_warning":
        pick = ("warning", warns[0]) if warns else ("activation", acts[0]) if acts else None
    elif symptom == "no_braking":
        if crash:
            pick = ("crash", {"t": crash["t"], "kmh": crash["kmh"], "_a": n - 1})
        elif acts:
            pick = ("activation", acts[0])
    elif symptom == "moved_after_stop" and stopped and stopped["moved_after_m"] > 0.05:
        pick = ("moved_after_stop", {"t": stopped["t"], "kmh": 0.0, "_a": int(round(stopped["t"] / DT))})
    if pick is None and crash:
        pick = ("crash", {"t": crash["t"], "kmh": crash["kmh"], "_a": n - 1})
    if pick:
        kind, e = pick
        tr = track_at(e["_a"]) or (track_at(e["_a"] - 1) if e["_a"] else None)
        event = {"kind": kind, "t": e["t"], "kmh": e["kmh"], "track": trk.index(tr) if tr else None}
        if kind == "activation":
            event.update(max_state=e["max_state"], max_brake_req=e["max_brake_req"], max_decel=e["max_decel"], duration_s=e["duration_s"])
    clean = lambda rows: [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]  # noqa: E731
    return {"rows": n, "duration_s": _r(t[-1] - t[0] + DT), "sw_version": h.get("sw_version"), "trigger": h.get("trigger"),
            "vin": h.get("vin"), "speed_start_kmh": _r(c["ego_kmh"][0], 1), "speed_end_kmh": _r(c["ego_kmh"][-1], 1),
            "speed_max_kmh": _r(max(c["ego_kmh"]), 1), "activations": clean(acts), "warnings": clean(warns), "tracks": clean(trk),
            "driver_brake": driver, "stop": stopped, "crash": crash, "friction_limited": friction_limited, "event": event}


def event_present(an: dict, symptom: str) -> bool:
    """Is the behaviour the customer complains about in this (recorded or re-simulated) analysis?"""
    if symptom == "unexpected_braking":
        return bool(an["activations"])
    if symptom == "false_warning":
        return bool(an["warnings"]) or bool(an["activations"])
    if symptom == "no_braking":
        return bool(an["crash"])
    if symptom == "moved_after_stop":
        return bool(an["stop"] and an["stop"]["moved_after_m"] > 0.05)
    return bool(an["activations"] or an["warnings"] or an["crash"])


def series(log: dict, every: int = 2) -> list[dict]:
    """Down-sampled rows for plots."""
    c = log["cols"]
    return [{"t": round(c["t_s"][k], 2), "ego_kmh": round(c["ego_kmh"][k], 2), "obj_dist_m": round(c["obj_dist_m"][k], 2) if c["obj_valid"][k] else None,
             "aeb_state": c["aeb_state"][k], "fcw": c["fcw"][k], "brake_req": round(c["brake_req_mps2"][k], 2), "decel": round(c["decel_mps2"][k], 2),
             "driver_brake": round(c["driver_brake_mps2"][k], 2)} for k in range(0, log["rows"], every)]
