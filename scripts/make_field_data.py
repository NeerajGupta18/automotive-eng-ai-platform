"""Generate the synthetic field data for the field-issue investigator (Phase 6).

    python scripts/make_field_data.py            # writes data/samples/field/ and eval/field/

Every recording is produced by simulating a situation with the software build the vehicle runs (its calibration
and, for the pilot builds that stand for coding defects, the corresponding predefined C-code variant compiled and run
in the loop), recorded the way an event data recorder would. Because the cause of each case is known, the cases
can be used to measure the agent. They are synthetic: real recordings are noisier and messier."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.modules.field import builds as B  # noqa: E402
from app.modules.field import logs as L  # noqa: E402
from app.modules.field.investigate import log_rows, sim_log  # noqa: E402

SAMPLES = ROOT / "data" / "samples" / "field"
EVAL = ROOT / "eval" / "field"

BUILDS = [
    {"version": "2.3.0", "ecu": "AEB", "firmware": "reference", "calibration": {}, "vehicles": 41200, "released_at": "2025-11-03",
     "validated": True, "validation_evidence": "SIL release campaign before start of production (V&V report AEB-VR-230)",
     "previous": None, "notes": "Start-of-production software"},
    {"version": "2.4.0", "ecu": "AEB", "firmware": "reference", "calibration": {"confirm_cycles": 5, "ttc_warn_s": 2.8}, "vehicles": 18500,
     "released_at": "2026-07-15", "validated": False,
     "validation_evidence": "calibration-only update over the air; the SIL regression campaign was not re-run",
     "previous": "2.3.0", "notes": "Faster object confirmation for cut-ins and an earlier forward collision warning"},
    {"version": "2.4.1-pilot", "ecu": "AEB", "firmware": "M04", "calibration": {}, "vehicles": 950, "released_at": "2026-08-20",
     "validated": False, "validation_evidence": "pilot fleet; unit tests only", "previous": "2.3.0",
     "notes": "Refactored standstill hold logic (pilot fleet). Stands for a coding defect: predefined variant M04"},
    {"version": "2.4.2-pilot", "ecu": "AEB", "firmware": "M11", "calibration": {}, "vehicles": 620, "released_at": "2026-08-27",
     "validated": False, "validation_evidence": "pilot fleet; unit tests only", "previous": "2.3.0",
     "notes": "Refactored speed-window checks (pilot fleet). Stands for a coding defect: predefined variant M11"},
    {"version": "2.4.3-pilot", "ecu": "AEB", "firmware": "M03", "calibration": {}, "vehicles": 480, "released_at": "2026-09-02",
     "validated": False, "validation_evidence": "pilot fleet; unit tests only", "previous": "2.3.0",
     "notes": "Simplified required-deceleration computation (pilot fleet). Stands for a coding defect: predefined variant M03"},
    {"version": "2.5.0-pilot", "ecu": "AEB", "firmware": "reference", "calibration": {"v_max_mps": 30.56}, "vehicles": 400,
     "released_at": "2026-09-10", "validated": False, "validation_evidence": "pilot: activation window extended to 110 km/h, not yet validated",
     "previous": "2.3.0", "notes": "AEB braking up to 110 km/h (pilot fleet)"},
]


def ghost(v, d, dur, at=3.0, seed=5, **kw):
    return {"ego_kmh": float(v), "target": "none", "ghost_at_s": at, "ghost_duration_s": dur, "ghost_distance_m": float(d),
            "duration_s": at + 5.0, "seed": seed, **kw}


def stationary(v, gap, seed=5, **kw):
    return {"ego_kmh": float(v), "target": "stationary", "gap_m": float(gap), "duration_s": 25.0, "seed": seed, **kw}


DEMO = [
    {"file": "fc_chakan_flyover.csv", "build": "2.4.0", "vin": "MA1DEMO0AEB000117", "reported_at": "2026-09-14T08:12:44+05:30",
     "dealer": "Pune Central Service", "scenario": ghost(80, 24, 0.12, seed=11),
     "text": "Car braked hard on its own under the flyover near Chakan on the Pune-Nashik highway at around 80 km/h. Nothing was in "
             "front of us. Very scary, the truck behind had to brake and honked."},
    {"file": "fc_bridge_joint.csv", "build": "2.4.0", "vin": "MA1DEMO0AEB000342", "reported_at": "2026-09-18T19:40:02+05:30",
     "dealer": "Nashik Road Motors", "scenario": ghost(70, 22, 0.09, seed=12),
     "text": "Sudden jerk and automatic braking while crossing a bridge expansion joint, approx 70 kmph. No vehicle ahead."},
    {"file": "fc_metal_plate.csv", "build": "2.4.0", "vin": "MA1DEMO0AEB000561", "reported_at": "2026-09-22T07:05:51+05:30",
     "dealer": "Pune Central Service", "scenario": ghost(65, 20, 0.07, seed=13),
     "text": "Emergency brake activated for no reason on the expressway near a metal plate on the road. Speed was 65 km/h."},
    {"file": "fc_toll_gantry.csv", "build": "2.4.0", "vin": "MA1DEMO0AEB000809", "reported_at": "2026-09-27T22:31:10+05:30",
     "dealer": "Lonavala Auto Care", "scenario": ghost(75, 26, 0.11, seed=14),
     "text": "Phantom braking twice this week at night on the Mumbai-Pune Expressway near the toll gantry, about 75 km/h. "
             "The car behind nearly hit us."},
    {"file": "fc_toll_queue.csv", "build": "2.3.0", "vin": "MA1DEMO0AEB001204", "reported_at": "2026-10-02T17:48:33+05:30",
     "dealer": "Pune Central Service",
     "scenario": stationary(45, 40, seed=15, driver_brake_at_s=2.95, driver_brake_mps2=5.0),
     "text": "The car slammed the brakes by itself near the toll plaza queue at about 45 km/h. I was about to brake anyway. "
             "The system is too aggressive."},
    {"file": "fc_rolled_forward.csv", "build": "2.4.1-pilot", "vin": "MA1DEMO0AEB002077", "reported_at": "2026-10-05T10:22:09+05:30",
     "dealer": "Hinjewadi Fleet Service", "scenario": stationary(30, 30, seed=16),
     "text": "After the automatic emergency stop in slow traffic the car rolled forward on its own and touched the car in front. "
             "Pilot fleet vehicle."},
]

EVAL_CASES = [
    # calibration regression: 2.4.0 confirms objects after 50 ms instead of 150 ms
    ("E01", "2.4.0", "Braked hard under a flyover at 80 km/h, nothing ahead.", ghost(80, 24, 0.12, seed=21), "calibration_regression", {"lever": "confirm_cycles"}),
    ("E02", "2.4.0", "Automatic braking for no reason at 60 km/h on a bridge.", ghost(60, 18, 0.08, seed=22), "calibration_regression", {"lever": "confirm_cycles"}),
    ("E03", "2.4.0", "Car jerked and braked by itself at 85 kmph, empty road.", ghost(85, 28, 0.10, seed=23), "calibration_regression", {"lever": "confirm_cycles"}),
    ("E04", "2.4.0", "Sudden brake activation over a metal plate at 55 km/h.", ghost(55, 15, 0.13, seed=24), "calibration_regression", {"lever": "confirm_cycles"}),
    ("E05", "2.4.0", "The collision warning beeps for no reason on the highway at 70 km/h.", ghost(70, 40, 0.10, seed=25), "calibration_regression",
     {"lever": "confirm_cycles", "symptom": "false_warning"}),
    ("E06", "2.4.0", "Phantom braking near the toll gantry at 75 km/h.", ghost(75, 22, 0.09, seed=26, latency_s=0.1), "calibration_regression", {"lever": "confirm_cycles"}),
    ("E07", "2.4.0", "Gaadi ne achanak brake maar diya flyover ke neeche, speed 50 thi.", ghost(50, 16, 0.11, seed=27), "calibration_regression", {"lever": "confirm_cycles"}),
    # sensor false target: long phantom objects that also make the validated build brake
    ("S01", "2.3.0", "Hard braking under an overhead sign gantry at 80 km/h, no car ahead.", ghost(80, 45, 0.6, seed=31), "sensor_false_target", {}),
    ("S02", "2.3.0", "Brakes slammed at a bridge at 70 km/h with nothing in front.", ghost(70, 40, 0.8, seed=32), "sensor_false_target", {}),
    ("S03", "2.4.0", "Emergency braking at 80 km/h under a railway overbridge, road was empty.", ghost(80, 50, 1.0, seed=33), "sensor_false_target", {}),
    ("S04", "2.3.0", "Car braked by itself at 60 km/h before a flyover, nothing there.", ghost(60, 30, 0.5, seed=34), "sensor_false_target", {}),
    # correct interventions
    ("C01", "2.3.0", "The car slammed the brakes by itself in the toll queue at 45 km/h, I was about to brake.",
     stationary(45, 40, seed=41, driver_brake_at_s=2.95, driver_brake_mps2=5.0), "correct_intervention", {}),
    ("C02", "2.4.0", "AEB braked suddenly at 60 km/h, very uncomfortable.", stationary(60, 70, seed=42), "correct_intervention", {}),
    ("C03", "2.3.0", "Car braked hard on its own behind another car at 50 km/h.",
     {"ego_kmh": 50.0, "target": "braking", "target_kmh": 50.0, "gap_m": 12.0, "target_decel_mps2": 6.0, "target_brake_at_s": 1.0,
      "duration_s": 15.0, "seed": 43}, "correct_intervention", {}),
    ("C04", "2.3.0", "Unexpected hard braking when a scooter came into my lane at 50 km/h.",
     {"ego_kmh": 50.0, "target": "moving", "target_kmh": 20.0, "gap_m": 40.0, "sensor_range_m": 15.0, "duration_s": 15.0, "seed": 44},
     "correct_intervention", {}),
    # system limitations
    ("L01", "2.3.0", "AEB did not brake at 100 km/h on the highway and we hit a stopped truck.",
     stationary(100, 160, seed=51, driver_fcw_reaction_s=2.5, driver_brake_mps2=6.0), "system_limitation", {"symptom": "no_braking"}),
    ("L02", "2.3.0", "Emergency braking did not stop the car on the wet road at 60 km/h, we crashed.",
     stationary(60, 75, seed=52, mu=0.45), "system_limitation", {"symptom": "no_braking"}),
    ("L03", "2.3.0", "In heavy fog the car hit a stopped car at 60 km/h, the AEB did not stop us.",
     stationary(60, 75, seed=53, sensor_range_m=18.0), "system_limitation", {"symptom": "no_braking"}),
    ("L04", "2.3.0", "In the parking the car did not brake automatically at 8 km/h and bumped the wall.",
     {"ego_kmh": 8.0, "target": "stationary", "gap_m": 6.0, "duration_s": 10.0, "seed": 54}, "system_limitation", {"symptom": "no_braking"}),
    # coding defects in pilot builds
    ("D01", "2.4.1-pilot", "After the automatic stop the car rolled forward and touched the car in front.", stationary(30, 30, seed=61),
     "implementation_defect", {"rollback": "2.3.0", "symptom": "moved_after_stop"}),
    ("D02", "2.4.1-pilot", "Car crept forward after the emergency brake stopped it at 40 km/h.", stationary(40, 45, seed=62),
     "implementation_defect", {"rollback": "2.3.0", "symptom": "moved_after_stop"}),
    ("D03", "2.4.2-pilot", "The car braked very hard by itself at 95 km/h on the highway.",
     stationary(95, 160, seed=63, driver_fcw_reaction_s=1.6, driver_brake_mps2=8.0), "implementation_defect", {"rollback": "2.3.0"}),
    ("D04", "2.4.3-pilot", "The car ahead braked hard, our AEB braked too late and we hit it at low speed.",
     {"ego_kmh": 50.0, "target": "braking", "target_kmh": 50.0, "gap_m": 12.0, "target_decel_mps2": 6.0, "target_brake_at_s": 1.0,
      "duration_s": 15.0, "seed": 64}, "implementation_defect", {"rollback": "2.3.0", "symptom": "no_braking"}),
    # calibration regression in the 110 km/h pilot
    ("R01", "2.5.0-pilot", "Car braked hard on its own at 100 km/h behind a slow truck.",
     {"ego_kmh": 100.0, "target": "moving", "target_kmh": 40.0, "gap_m": 150.0, "duration_s": 20.0, "seed": 71},
     "calibration_regression", {"lever": "v_max_mps"}),
    # insufficient data
    ("I01", "2.3.0", "The car braked suddenly by itself at 50 km/h.",
     {"ego_kmh": 50.0, "target": "none", "driver_brake_at_s": 3.0, "driver_brake_mps2": 7.0, "duration_s": 8.0, "seed": 81},
     "insufficient_data", {}),
    ("I02", "2.3.0", "Braked for no reason at 75 km/h on the expressway.", ghost(75, 24, 0.11, seed=82), "insufficient_data",
     {"recorded_with": "2.4.0", "matching_build": "2.4.0"}),
    ("I03", "2.4.0", "Car braked hard for no reason near the toll booth. No recording available.", None, "insufficient_data", {"no_log": True}),
]


def _record(build: dict, scn: dict, header: dict) -> str:
    ctrl, kind = B.controller(build, "sil")
    if build["firmware"] != "reference" and kind != "sil":
        raise SystemExit("A C compiler is needed to generate recordings of the defect builds")
    s = {"id": "gen", "family": "Custom", "title": "generated", "mu": 1.0, "sensor_range_m": 150.0, "latency_s": 0.05, "noise": True, **scn}
    log, _ = sim_log(s, ctrl, header)
    return L.to_csv(log["header"], log_rows(log))


def main() -> None:
    by_v = {b["version"]: B.validate(b) for b in BUILDS}
    (SAMPLES / "logs").mkdir(parents=True, exist_ok=True)
    (EVAL / "logs").mkdir(parents=True, exist_ok=True)
    (SAMPLES / "builds.json").write_text(json.dumps({"note": "Synthetic AEB software builds in the field (demo).", "builds": list(by_v.values())},
                                                    indent=2) + "\n")
    complaints = []
    for i, d in enumerate(DEMO, 1):
        hdr = {"vin": d["vin"], "ecu": "AEB", "sw_version": d["build"], "recorded_at": d["reported_at"],
               "source": "event data recorder (synthetic)"}
        (SAMPLES / "logs" / d["file"]).write_text(_record(by_v[d["build"]], d["scenario"], hdr))
        complaints.append({"text": d["text"], "vin": d["vin"], "build": d["build"], "reported_at": d["reported_at"], "dealer": d["dealer"],
                           "log": f"logs/{d['file']}"})
    (SAMPLES / "complaints.json").write_text(json.dumps({"note": "Synthetic dealer complaints with event recordings (demo).",
                                                         "complaints": complaints}, indent=2) + "\n")
    cases = []
    for cid, build, text, scn, cls, exp in EVAL_CASES:
        rec_build = exp.get("recorded_with", build)
        expected = {"class": cls, "symptom": "unexpected_braking", **{k: v for k, v in exp.items() if k != "recorded_with"}}
        entry = {"id": cid, "build": build, "text": text, "expected": expected,
                 "scenario": scn, "log": None}
        if scn is not None:
            hdr = {"vin": f"MA1EVAL0AEB{cid}0001"[:17], "ecu": "AEB", "sw_version": build, "recorded_at": "2026-09-01T12:00:00+05:30",
                   "source": "event data recorder (synthetic)"}
            (EVAL / "logs" / f"{cid}.csv").write_text(_record(by_v[rec_build], scn, hdr))
            entry["log"] = f"logs/{cid}.csv"
        if rec_build != build:
            entry["generated_with"] = rec_build
        cases.append(entry)
    (EVAL / "cases.json").write_text(json.dumps({"note": "Labelled synthetic field cases for the field-issue agent. The cause of each case "
                                                         "is known because the recording was generated from it.",
                                                 "builds": "data/samples/field/builds.json", "cases": cases}, indent=2) + "\n")
    print(f"{len(complaints)} demo complaints in {SAMPLES}, {len(cases)} evaluation cases in {EVAL}")


if __name__ == "__main__":
    main()
