from pathlib import Path

import pytest

from app.core.parsing import parse_document
from app.modules.hld import analysis
from app.modules.hld.extractor import extract_entities, llm_fallback
from app.core.parsing import split_sections

S = Path(__file__).resolve().parent.parent / "data" / "samples"


@pytest.fixture(scope="module", params=["md", "pdf"])
def v1(request):
    return extract_entities(parse_document(S / f"hld_bcm_v1.{request.param}"))


def test_entity_counts(v1):
    assert len(v1["components"]) == 6
    assert len(v1["interfaces"]) == 9
    assert sum(len(c["ports"]) for c in v1["components"]) == 14


def test_port_details(v1):
    lock = next(c for c in v1["components"] if c["name"] == "SWC_DoorLockCtrl")
    assert {(p["name"], p["direction"], p["interface"]) for p in lock["ports"]} == {
        ("pDoorLockCmd", "P", "If_DoorLockCmd"), ("rVehicleSpeed", "R", "If_VehicleSpeed"),
        ("rCrashState", "R", "If_CrashState")}
    assert lock["runnables"] == ["DoorLockCtrl_Run10ms", "DoorLockCtrl_Init"]
    assert lock["source"]["section"].endswith("3.1 SWC_DoorLockCtrl")


def test_interface_elements(v1):
    spd = next(i for i in v1["interfaces"] if i["name"] == "If_VehicleSpeed")
    assert spd["type"] == "SenderReceiver"
    assert spd["elements"] == [{"name": "VehicleSpeed", "type": "uint16", "unit": "km/h"}]


def test_dependencies(v1):
    edges = {(e["consumer"], e["provider"], e["interface"]) for e in analysis.dependencies(v1)}
    assert ("SWC_DoorLockCtrl", "SWC_VehicleSpeedMgr", "If_VehicleSpeed") in edges
    assert ("SWC_VehicleSpeedMgr", "SWC_WheelSensorIf", "If_WheelPulse") in edges


def test_planted_defects_are_found(v1):
    ids = {i["id"] for i in analysis.check_consistency(v1)}
    assert "R003:SWC_LightCtrl.rIgnitionState" in ids           # undefined interface
    assert "R001:SWC_DoorLockCtrl.rCrashState" in ids           # missing provider
    assert "R008:SWC_LightCtrl.rHeadlampFeedback" in ids        # r-name but P-port
    assert "R010:If_WheelPulse" in ids                          # interface without type
    assert "R004:If_UnusedDiag" in ids                          # unused interface


def test_clean_parts_have_no_false_positives(v1):
    ids = {i["id"] for i in analysis.check_consistency(v1)}
    assert not any(i.startswith(("R001:SWC_VehicleSpeedMgr", "R001:SWC_DoorStateMgr", "R005", "R006")) for i in ids)


def test_duplicate_and_empty_component_detection():
    pages = [__import__("app.core.parsing", fromlist=["Page"]).Page(
        1, "### SWC_A\n\nType: x\n\n### SWC_A\n\n| Port | Direction | Interface |\n|---|---|---|\n| pX | P-Port | If_Z |")]
    ids = {i["rule"] for i in analysis.check_consistency(extract_entities(pages))}
    assert {"R005", "R006", "R003"} <= ids


def test_compare_v1_v2():
    a = extract_entities(parse_document(S / "hld_bcm_v1.md"))
    b = extract_entities(parse_document(S / "hld_bcm_v2.md"))
    d = analysis.compare(a, b)
    assert d["components_added"] == ["SWC_CrashEventMgr"]
    assert "If_UnusedDiag" in d["interfaces_removed"]
    changed = {c["name"]: c for c in d["interfaces_changed"]}
    assert changed["If_VehicleSpeed"]["elements_added"] == [("VehicleSpeed", "float32", "km/h")]
    assert any(c["name"] == "SWC_DoorLockCtrl" and c["ports_added"] for c in d["components_changed"])
    assert "R001:SWC_DoorLockCtrl.rCrashState" in d["issues_resolved"]


def test_impact_analysis():
    ex = extract_entities(parse_document(S / "hld_bcm_v1.md"))
    r = analysis.impact(ex, "SWC_WheelSensorIf")
    # wheel pulses -> speed -> auto-lock -> lock command -> door state -> lights: change ripples 4 levels
    assert r["downstream_impacted"] == {"SWC_VehicleSpeedMgr": 1, "SWC_DoorLockCtrl": 2,
                                        "SWC_DoorStateMgr": 3, "SWC_LightCtrl": 4}
    assert analysis.impact(ex, "SWC_DoorLockCtrl")["upstream_dependencies"]["SWC_WheelSensorIf"] == 2
    with pytest.raises(KeyError):
        analysis.impact(ex, "SWC_Nope")


def test_graph_and_csv_exports():
    ex = extract_entities(parse_document(S / "hld_bcm_v1.md"))
    dot = analysis.to_dot(ex)
    assert dot.startswith("digraph") and '"SWC_VehicleSpeedMgr" -> "SWC_DoorLockCtrl"' in dot
    rows = analysis.ports_csv(ex).strip().splitlines()
    assert rows[0].startswith("component,port") and len(rows) == 15


def test_llm_fallback_tags_origin_and_ignores_garbage():
    class FakeLLM:
        def __init__(self): self.n = 0
        def generate(self, system, prompt, temperature=0.1):
            self.n += 1
            return ('{"components":[{"name":"EngineCtrl","ports":[{"name":"pRpm","direction":"P","interface":"IfRpm"}]}],'
                    '"interfaces":[{"name":"IfRpm","type":"SenderReceiver"}]}') if self.n == 1 else "not json at all"
    secs = split_sections([__import__("app.core.parsing", fromlist=["Page"]).Page(1, "## A\n\n" + "text " * 60 + "\n\n## B\n\n" + "more " * 60)])
    out = llm_fallback(FakeLLM(), secs)
    assert [c["name"] for c in out["components"]] == ["EngineCtrl"]
    assert out["components"][0]["origin"] == "llm" and out["components"][0]["ports"][0]["direction"] == "P"
