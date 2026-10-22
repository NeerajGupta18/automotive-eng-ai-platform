"""Machine-checkable AEB requirements: which scenarios verify a requirement and how one simulation
result is judged against it.

A requirement has a selector (scenario families and an ego speed range) and a check with parameters.
Per scenario the verdict is pass, fail or n/a (the check does not apply to what happened, e.g. a hold
requirement when the vehicle never stopped). Per requirement:
  pass          at least one applicable scenario passed and none failed
  fail          at least one applicable scenario failed
  not_verified  scenarios exist but none of them exercised the requirement
  not_covered   no scenario matches the selector (a test gap)"""
from __future__ import annotations

import json
import re
from pathlib import Path

LIBRARY = Path(__file__).resolve().parents[3] / "data" / "samples" / "adas" / "aeb_requirements.json"
ID_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-[A-Z0-9]{1,6}$")


def _no_collision(m: dict, p: dict) -> tuple[str, str]:
    if m["collided"]:
        return "fail", f"collision at {m['impact_kmh']:.1f} km/h"
    gap = m.get("stop_gap_m") if m.get("stopped") else m.get("min_gap_m")
    return "pass", (f"stopped {gap:.2f} m before the object" if m.get("stopped") and gap is not None
                    else f"no collision, closest gap {gap:.2f} m" if gap is not None else "no collision")


def _avoid_or_reduce(m: dict, p: dict) -> tuple[str, str]:
    if not m["collided"]:
        return _no_collision(m, p)
    if m["initial_kmh"] <= p.get("avoid_up_to_kmh", -1):
        return "fail", f"collision at {m['impact_kmh']:.1f} km/h (must be avoided up to {p['avoid_up_to_kmh']} km/h)"
    closing = m["initial_closing_kmh"] or 0.0
    red = (closing - m["impact_kmh"]) / closing if closing > 0 else 0.0
    need = p.get("min_reduction", 0.5)
    return ("pass" if red >= need else "fail"), f"impact {m['impact_kmh']:.1f} km/h, speed reduced by {red:.0%} (need {need:.0%})"


def _fcw_lead(m: dict, p: dict) -> tuple[str, str]:
    if m["brake_t"] is None:
        return "n/a", "no autonomous braking"
    if m["fcw_t"] is None:
        return "fail", "braking without a prior warning"
    lead = round(m["brake_t"] - m["fcw_t"], 2)
    return ("pass" if lead >= p.get("min_s", 0.8) - 1e-9 else "fail"), f"warning {lead:.2f} s before braking (need {p.get('min_s', 0.8)} s)"


def _driver(m: dict, p: dict) -> tuple[str, str]:
    return ("pass", "brake request ≥ driver request throughout") if m["driver_respected"] else \
        ("fail", "brake request fell below the driver's request")


def _hold(m: dict, p: dict) -> tuple[str, str]:
    if not m.get("stopped") or not m.get("activations"):
        return "n/a", "no autonomous stop"
    moved = m.get("moved_after_stop_m") or 0.0
    return ("pass" if moved <= p.get("max_m", 0.05) else "fail"), f"moved {moved:.2f} m after stopping"


def _max_stop_gap(m: dict, p: dict) -> tuple[str, str]:
    if not m.get("stopped") or m["collided"] or m.get("stop_gap_m") is None:
        return "n/a", "no autonomous stop"
    return ("pass" if m["stop_gap_m"] <= p.get("max_m", 12.0) else "fail"), f"stopped {m['stop_gap_m']:.2f} m before the object"


def _activation_window(m: dict, p: dict) -> tuple[str, str]:
    v, warned, braked = m["initial_kmh"], m["fcw_t"] is not None, m["brake_t"] is not None
    if v < p.get("v_min_kmh", 10):
        ok = not warned and not braked
        return ("pass" if ok else "fail"), f"{v:g} km/h: warning {'yes' if warned else 'no'}, braking {'yes' if braked else 'no'} (expected none)"
    if v > p.get("v_max_kmh", 85):
        early = braked and (m.get("brake_kmh") or 0.0) > p.get("v_max_kmh", 85) + 0.05
        ok = warned and not early
        how = "no braking" if not braked else f"braking started at {m['brake_kmh']:.1f} km/h"
        return ("pass" if ok else "fail"), f"{v:g} km/h: warning {'yes' if warned else 'no'}, {how} (expected a warning and no braking above {p.get('v_max_kmh', 85)} km/h)"
    return "n/a", "inside the activation window"


def _no_intervention(m: dict, p: dict) -> tuple[str, str]:
    ok = m["fcw_t"] is None and not m["max_aeb_req"]
    return ("pass", "no warning, no braking") if ok else \
        ("fail", f"unintended {'braking' if m['max_aeb_req'] else 'warning'} at t={m['brake_t'] if m['brake_t'] is not None else m['fcw_t']} s")


CHECKS = {"no_collision": _no_collision, "avoid_or_reduce": _avoid_or_reduce, "fcw_lead": _fcw_lead,
          "driver_respected": _driver, "hold_at_standstill": _hold, "max_stop_gap": _max_stop_gap,
          "activation_window": _activation_window, "no_intervention": _no_intervention}
CHECK_TEXT = {"no_collision": "no collision", "avoid_or_reduce": "avoid, or reduce impact speed",
              "fcw_lead": "warning time before braking", "driver_respected": "brake request ≥ driver request",
              "hold_at_standstill": "vehicle held after an autonomous stop", "max_stop_gap": "stop not excessively early",
              "activation_window": "no reaction outside the speed window", "no_intervention": "no warning and no braking"}


def validate(req: dict) -> dict:
    """Normalise one requirement definition; raises ValueError with a readable message."""
    rid = str(req.get("id", ""))
    if not ID_RE.match(rid):
        raise ValueError(f"requirement id '{rid}' must look like AEB-001")
    if req.get("check") not in CHECKS:
        raise ValueError(f"{rid}: check must be one of {sorted(CHECKS)}")
    title, text = " ".join(str(req.get("title", "")).split()), " ".join(str(req.get("text", "")).split())
    if not title or len(text) < 10:
        raise ValueError(f"{rid}: title and text are required")
    ap = req.get("applies") or {}
    fams = ap.get("families")
    if not isinstance(fams, list) or not fams or not all(isinstance(f, str) and f for f in fams):
        raise ValueError(f"{rid}: applies.families must be a non-empty list ('*' = every scenario)")
    for k in ("ego_min", "ego_max"):
        if k in ap and not isinstance(ap[k], (int, float)):
            raise ValueError(f"{rid}: applies.{k} must be a number (km/h)")
    match = ap.get("match") or {}
    if not isinstance(match, dict) or not all(isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v)
                                             for v in match.values()):
        raise ValueError(f"{rid}: applies.match must map scenario parameters to lists of allowed numbers")
    params = req.get("params") or {}
    if not isinstance(params, dict) or not all(isinstance(v, (int, float)) for v in params.values()):
        raise ValueError(f"{rid}: params must map names to numbers")
    return {"id": rid, "title": title[:160], "text": text[:1000], "category": str(req.get("category", ""))[:80],
            "release_blocking": bool(req.get("release_blocking", True)), "check": req["check"], "params": params,
            "applies": {"families": fams, **{k: ap[k] for k in ("ego_min", "ego_max") if k in ap},
                        **({"match": match} if match else {})}}


def load_library(path: Path = LIBRARY) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    reqs = [validate(r) for r in data.get("requirements", [])]
    ids = [r["id"] for r in reqs]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate requirement ids")
    return {"name": data.get("name", ""), "version": data.get("version", ""), "note": data.get("note", ""), "requirements": reqs}


def applies(req: dict, scn: dict) -> bool:
    ap = req["applies"]
    if "*" not in ap["families"] and scn.get("family") not in ap["families"]:
        return False
    v = scn.get("ego_kmh", 0.0)
    if not ap.get("ego_min", -1e9) <= v <= ap.get("ego_max", 1e9):
        return False
    return all(any(abs(float(scn.get(k, -1e9)) - x) < 1e-6 for x in allowed) for k, allowed in ap.get("match", {}).items())


def judge(req: dict, metrics: dict) -> dict:
    verdict, detail = CHECKS[req["check"]](metrics, req.get("params") or {})
    return {"verdict": verdict, "detail": detail}


def summarize(verdicts: list[str], covered: bool) -> str:
    if not covered:
        return "not_covered"
    if "fail" in verdicts:
        return "fail"
    return "pass" if "pass" in verdicts else "not_verified"
