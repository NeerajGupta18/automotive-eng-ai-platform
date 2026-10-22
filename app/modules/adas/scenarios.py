"""AEB scenario catalog: logical scenarios (families with parameter ranges) expanded into concrete
scenarios, plus export to ASAM OpenSCENARIO 1.1 for replay in other tools (e.g. esmini).

Families
  CCRs / CCRm / CCRb  car-to-car rear: stationary, moving (20 km/h) and braking target. Parameter
                      grids follow the Euro NCAP AEB Car-to-Car test protocol v3.0.2 (2019): CCRs 10-80 km/h
                      (AEB 10-50, FCW 30-80), CCRm 30-80 km/h, CCRb 50/50 km/h with 12/40 m gap and
                      2/6 m/s^2 target deceleration, in 5 km/h steps; 100 % overlap only (1-D simulation).
                      Check the current protocol version before quoting results as NCAP-like.
  SOTIF edge cases    added by this project: wet road (mu 0.6), fog-limited radar range (30 m), phantom
                      radar object (100 ms), lead vehicle pulling away, high sensor latency (300 ms),
                      driver already braking."""
from __future__ import annotations

import xml.etree.ElementTree as ET

NCAP = "Euro NCAP AEB C2C protocol v3.0.2 (2019) parameter grid"
SOTIF = "SOTIF edge case (this project)"


def _gap(ego_kmh: float, target_kmh: float = 0.0) -> float:
    closing = (ego_kmh - target_kmh) / 3.6
    return round(max(4.5 * closing, 20.0), 1)                 # start at TTC >= 4.5 s, at least 20 m


def _s(sid: str, family: str, title: str, **p) -> dict:
    base = {"id": sid, "family": family, "title": title, "target": "stationary", "target_kmh": 0.0, "mu": 1.0,
            "sensor_range_m": 150.0, "latency_s": 0.05, "noise": True, "seed": 7, "duration_s": 30.0,
            "source": NCAP if family in ("CCRs", "CCRm", "CCRb") else SOTIF}
    base.update(p)
    return base


def catalog() -> list[dict]:
    out = []
    for v in range(10, 85, 5):
        out.append(_s(f"CCRs-{v:02d}", "CCRs", f"Stationary target, ego {v} km/h", ego_kmh=float(v), gap_m=_gap(v)))
    for v in range(30, 85, 5):
        out.append(_s(f"CCRm-{v:02d}", "CCRm", f"Target at 20 km/h, ego {v} km/h", ego_kmh=float(v), target="moving",
                      target_kmh=20.0, gap_m=_gap(v, 20)))
    for gap in (12, 40):
        for dec in (2, 6):
            out.append(_s(f"CCRb-{gap}m-{dec}", "CCRb", f"Both 50 km/h, gap {gap} m, target brakes {dec} m/s²",
                          ego_kmh=50.0, target="braking", target_kmh=50.0, gap_m=float(gap), target_decel_mps2=float(dec),
                          target_brake_at_s=1.0))
    for v in range(10, 90, 10):
        out.append(_s(f"WET-{v:02d}", "Wet road", f"Stationary target on wet road (mu 0.6), ego {v} km/h",
                      ego_kmh=float(v), gap_m=_gap(v), mu=0.6))
    for v in range(30, 80, 10):
        out.append(_s(f"FOG-{v:02d}", "Fog", f"Stationary target, radar range limited to 30 m, ego {v} km/h",
                      ego_kmh=float(v), gap_m=_gap(v), sensor_range_m=30.0))
    for v in (50, 70, 85):
        out.append(_s(f"GHOST-{v:02d}", "Phantom object", f"100 ms phantom radar object at 20 m, ego {v} km/h, no real target",
                      ego_kmh=float(v), target="none", ghost_at_s=2.0, ghost_duration_s=0.1, ghost_distance_m=20.0,
                      duration_s=8.0))
    for v in (30, 50, 70):
        out.append(_s(f"PULLAWAY-{v:02d}", "Lead pulling away", f"Lead vehicle 25 m ahead, 20 km/h faster than ego {v} km/h",
                      ego_kmh=float(v), target="moving", target_kmh=float(v + 20), gap_m=25.0, duration_s=8.0))
    for v in (30, 50):
        out.append(_s(f"LATENCY-{v:02d}", "Sensor latency", f"Stationary target, 300 ms sensor latency, ego {v} km/h",
                      ego_kmh=float(v), gap_m=_gap(v), latency_s=0.3))
    out.append(_s("DRIVER-50", "Driver braking", "Driver brakes 3 m/s² late, stationary target, ego 50 km/h",
                  ego_kmh=50.0, gap_m=_gap(50), driver_brake_mps2=3.0, driver_brake_at_s=round(_gap(50) / (50 / 3.6) - 2.2, 2)))
    return out


def by_id(scenarios: list[dict]) -> dict[str, dict]:
    return {s["id"]: s for s in scenarios}


# ---------------------------------------------------------------------------- OpenSCENARIO 1.1 export
def _vehicle(parent: ET.Element, name: str) -> None:
    obj = ET.SubElement(parent, "ScenarioObject", name=name)
    veh = ET.SubElement(obj, "Vehicle", name="car_white" if name == "Ego" else "car_red", vehicleCategory="car")
    ET.SubElement(veh, "ParameterDeclarations")
    bb = ET.SubElement(veh, "BoundingBox")
    ET.SubElement(bb, "Center", x="1.4", y="0.0", z="0.75")
    ET.SubElement(bb, "Dimensions", width="1.8", length="4.5", height="1.5")
    ET.SubElement(veh, "Performance", maxSpeed="70", maxAcceleration="10", maxDeceleration="10")
    ax = ET.SubElement(veh, "Axles")
    ET.SubElement(ax, "FrontAxle", maxSteering="0.5", wheelDiameter="0.6", trackWidth="1.8", positionX="3.1", positionZ="0.3")
    ET.SubElement(ax, "RearAxle", maxSteering="0", wheelDiameter="0.6", trackWidth="1.8", positionX="0", positionZ="0.3")
    ET.SubElement(veh, "Properties")


def _init(actions: ET.Element, entity: str, s: float, speed_param: str) -> None:
    p = ET.SubElement(actions, "Private", entityRef=entity)
    a1 = ET.SubElement(ET.SubElement(p, "PrivateAction"), "TeleportAction")
    ET.SubElement(ET.SubElement(a1, "Position"), "LanePosition", roadId="0", laneId="-1", offset="0", s=f"{s:.2f}")
    sp = ET.SubElement(ET.SubElement(ET.SubElement(p, "PrivateAction"), "LongitudinalAction"), "SpeedAction")
    ET.SubElement(sp, "SpeedActionDynamics", dynamicsShape="step", value="0", dynamicsDimension="time")
    ET.SubElement(ET.SubElement(sp, "SpeedActionTarget"), "AbsoluteTargetSpeed", value=f"${speed_param}")


def _sim_time_trigger(parent: ET.Element, tag: str, seconds: float, name: str) -> None:
    trig = ET.SubElement(parent, tag)
    cond = ET.SubElement(ET.SubElement(trig, "ConditionGroup"), "Condition", name=name, delay="0", conditionEdge="rising")
    ET.SubElement(ET.SubElement(cond, "ByValueCondition"), "SimulationTimeCondition", value=f"{seconds:g}", rule="greaterThan")


ROAD_FILE = "straight_road.xodr"


def road_xodr(length_m: float = 3000.0) -> str:
    """A straight two-lane road in ASAM OpenDRIVE 1.7 for the exported scenarios."""
    root = ET.Element("OpenDRIVE")
    ET.SubElement(root, "header", revMajor="1", revMinor="7", name="straight_road", version="1.0",
                  date="2026-01-01T00:00:00", north="0", south="0", east="0", west="0", vendor="automotive-eng-ai")
    road = ET.SubElement(root, "road", name="straight", length=f"{length_m:.1f}", id="0", junction="-1")
    ET.SubElement(road, "link")
    geo = ET.SubElement(ET.SubElement(road, "planView"), "geometry", s="0.0", x="0.0", y="0.0", hdg="0.0", length=f"{length_m:.1f}")
    ET.SubElement(geo, "line")
    sec = ET.SubElement(ET.SubElement(road, "lanes"), "laneSection", s="0.0")
    for side, lane_id in (("left", "1"), ("center", "0"), ("right", "-1")):
        lane = ET.SubElement(ET.SubElement(sec, side), "lane", id=lane_id, type="none" if lane_id == "0" else "driving", level="false")
        if lane_id != "0":
            ET.SubElement(lane, "link")
            ET.SubElement(lane, "width", sOffset="0.0", a="3.5", b="0.0", c="0.0", d="0.0")
        ET.SubElement(lane, "roadMark", sOffset="0.0", type="broken" if lane_id == "0" else "solid", weight="standard",
                      color="standard", width="0.12")
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _environment(actions: ET.Element, s: dict) -> None:
    """Fog (radar range) and road friction as an OpenSCENARIO environment, when they differ from clear and dry."""
    mu, rng = float(s.get("mu", 1.0)), float(s.get("sensor_range_m", 150.0))
    if mu >= 1.0 and rng >= 150.0:
        return
    env = ET.SubElement(ET.SubElement(ET.SubElement(actions, "GlobalAction"), "EnvironmentAction"), "Environment", name="Conditions")
    ET.SubElement(env, "TimeOfDay", animation="false", dateTime="2026-07-01T12:00:00")
    weather = ET.SubElement(env, "Weather")
    if rng < 150.0:
        ET.SubElement(weather, "Fog", visualRange=f"{rng:g}")
    if mu < 1.0:
        ET.SubElement(weather, "Precipitation", precipitationType="snow" if mu < 0.45 else "rain", precipitationIntensity="5")
    ET.SubElement(env, "RoadCondition", frictionScaleFactor=f"{mu:g}")


def to_openscenario(s: dict, road: str = ROAD_FILE) -> str:
    """One concrete scenario as OpenSCENARIO 1.1 XML for replay in other tools. The ego vehicle gets its initial
    speed and no controller: the AEB under test is supplied by the tool. Sensor effects (range, latency,
    phantom objects) and the driver model belong to this project's simulation; range and friction are also
    expressed as fog and road condition."""
    root = ET.Element("OpenSCENARIO")
    ET.SubElement(root, "FileHeader", revMajor="1", revMinor="1", date="2026-01-01T00:00:00",
                  description=f"{s['id']}: {s['title']} ({s['source']})", author="automotive-eng-ai")
    params = ET.SubElement(root, "ParameterDeclarations")
    ET.SubElement(params, "ParameterDeclaration", name="EgoSpeed", parameterType="double", value=f"{s['ego_kmh'] / 3.6:.4f}")
    ET.SubElement(params, "ParameterDeclaration", name="TargetSpeed", parameterType="double", value=f"{s.get('target_kmh', 0) / 3.6:.4f}")
    ET.SubElement(params, "ParameterDeclaration", name="Gap", parameterType="double", value=f"{s.get('gap_m', 0):.2f}")
    # simulation-specific settings, kept as documented parameters (not interpreted by OpenSCENARIO players)
    for name, key, default in (("TargetDeceleration", "target_decel_mps2", 0.0), ("SensorRange", "sensor_range_m", 150.0),
                               ("SensorLatency", "latency_s", 0.05), ("Friction", "mu", 1.0)):
        ET.SubElement(params, "ParameterDeclaration", name=name, parameterType="double", value=f"{float(s.get(key, default)):g}")
    ET.SubElement(root, "CatalogLocations")
    ET.SubElement(ET.SubElement(root, "RoadNetwork"), "LogicFile", filepath=road)
    ents = ET.SubElement(root, "Entities")
    with_target = s.get("target", "none") != "none"
    _vehicle(ents, "Ego")
    if with_target:
        _vehicle(ents, "Target")
    sb = ET.SubElement(root, "Storyboard")
    actions = ET.SubElement(ET.SubElement(sb, "Init"), "Actions")
    _environment(actions, s)
    ego_s = 50.0
    _init(actions, "Ego", ego_s, "EgoSpeed")
    if with_target:
        _init(actions, "Target", ego_s + 4.5 + s.get("gap_m", 0), "TargetSpeed")    # gap is bumper to bumper
    story = ET.SubElement(sb, "Story", name="AEB_story")
    act = ET.SubElement(story, "Act", name="AEB_act")
    if s.get("target") == "braking":
        mg = ET.SubElement(act, "ManeuverGroup", maximumExecutionCount="1", name="TargetBrakes")
        actors = ET.SubElement(mg, "Actors", selectTriggeringEntities="false")
        ET.SubElement(actors, "EntityRef", entityRef="Target")
        ev = ET.SubElement(ET.SubElement(mg, "Maneuver", name="Brake"), "Event", name="BrakeEvent", priority="overwrite")
        sp = ET.SubElement(ET.SubElement(ET.SubElement(ET.SubElement(ev, "Action", name="BrakeAction"), "PrivateAction"),
                                         "LongitudinalAction"), "SpeedAction")
        ET.SubElement(sp, "SpeedActionDynamics", dynamicsShape="linear", value=f"{s['target_decel_mps2']:g}", dynamicsDimension="rate")
        ET.SubElement(ET.SubElement(sp, "SpeedActionTarget"), "AbsoluteTargetSpeed", value="0")
        _sim_time_trigger(ev, "StartTrigger", s.get("target_brake_at_s", 1.0), "BrakeStart")
    else:
        mg = ET.SubElement(act, "ManeuverGroup", maximumExecutionCount="1", name="Approach")
        ET.SubElement(mg, "Actors", selectTriggeringEntities="false")
    _sim_time_trigger(act, "StartTrigger", 0, "ActStart")
    _sim_time_trigger(sb, "StopTrigger", s.get("duration_s", 30.0), "End")
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
