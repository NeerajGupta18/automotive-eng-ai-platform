"""Phase 4: ADAS virtual validation of the AEB function (model, C code in the loop, scenarios, requirements,
back-to-back, mutation testing, OpenSCENARIO export, workflow and API)."""
import dataclasses
import io
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import get_settings
from app.container import Container
from app.modules.adas import authoring, campaign, sil
from app.modules.adas import requirements as rq
from app.modules.adas.model import CAL, FULL, HOLD, OFF, PARTIAL, WARN, AebModel, make_calibration, required_decel
from app.modules.adas.scenarios import catalog, road_xodr, to_openscenario
from app.modules.adas.sim import simulate

ROOT = Path(__file__).resolve().parent.parent
needs_cc = pytest.mark.skipif(not sil.status()["available"], reason="needs a C compiler or a prebuilt SIL library")
KMH = 1 / 3.6


def step(m, v_kmh, d, closing_kmh, valid=True, driver=0.0):
    return m.step(v_kmh * KMH, valid, d, closing_kmh * KMH, driver)


@pytest.fixture(scope="module")
def lib():
    return rq.load_library()


@pytest.fixture(scope="module")
def full_set(lib):
    scns = [authoring.validate_scenario(s) for s in catalog()]
    return scns + [p["scenario"] for p in authoring.propose_boundary(lib["requirements"], scns)]


# ------------------------------------------------------------------ reference model
def test_object_must_be_confirmed_for_150_ms_before_any_reaction():
    m = AebModel()
    outs = [step(m, 50, 20.0, 50) for _ in range(15)]                  # TTC 1.44 s from the first cycle
    assert all(o["state"] == OFF for o in outs[:14])
    assert outs[14]["state"] == PARTIAL and outs[14]["fcw"] and outs[14]["aeb_req"] == CAL.a_partial_mps2


def test_staged_response_warning_partial_full():
    def first_state(d, closing_kmh=50):
        m = AebModel()
        return [step(m, 50, d, closing_kmh) for _ in range(15)][-1]["state"]
    assert first_state(33.0) == WARN                                   # TTC 2.4 s
    assert first_state(20.0) == PARTIAL                                # TTC 1.44 s
    assert first_state(13.0) == FULL                                   # TTC 0.94 s
    assert first_state(80.0) == OFF


def test_activation_window_and_braking_below_it():
    m = AebModel()
    assert [step(m, 8, 3.0, 8) for _ in range(20)][-1]["state"] == OFF       # below 10 km/h: neither warning nor braking
    m = AebModel()
    o = [step(m, 90, 30.0, 90) for _ in range(20)][-1]                       # above 85 km/h: warning only
    assert o["state"] == WARN and o["aeb_req"] == 0.0
    m = AebModel()
    [step(m, 50, 13.0, 50) for _ in range(15)]
    assert m.state == FULL
    assert step(m, 8, 2.0, 8)["state"] == FULL                               # braking that started continues below 10 km/h


def test_release_hold_and_driver_arbitration():
    m = AebModel()
    [step(m, 50, 13.0, 50) for _ in range(15)]
    assert step(m, 30, 6.0, -1.0)["state"] == OFF                            # object moving away above 10 km/h -> release
    m = AebModel()
    [step(m, 50, 13.0, 50) for _ in range(15)]
    assert step(m, 5, 2.0, -1.0)["state"] == FULL                            # below 10 km/h braking is completed
    o = step(m, 0.05, 2.0, 0.05)
    assert o["state"] == HOLD and o["aeb_req"] == CAL.a_hold_mps2 and not o["fcw"]
    assert step(m, 0.0, 2.0, 0.0, valid=False)["state"] == OFF               # hold ends when the object is gone
    m = AebModel()
    assert step(m, 50, 100.0, 0, driver=5.5)["brake_req"] == 5.5             # never below the driver's request
    [step(m, 50, 20.0, 50) for _ in range(15)]
    assert step(m, 50, 19.0, 50, driver=2.0)["brake_req"] == CAL.a_partial_mps2


def test_required_deceleration_accounts_for_a_braking_lead_vehicle():
    assert required_decel(21.5, 10.0) == pytest.approx(100 / 40)              # v^2 / 2(d - 1.5)
    assert required_decel(21.5, -1.0) == 0.0
    # lead vehicle stops first: ego 13.9 m/s, both same speed, lead braking 6 m/s^2, 12 m gap
    a = required_decel(12.0, 0.0, v_ego=13.9, a_tgt=6.0)
    assert a == pytest.approx(13.9 ** 2 / (2 * (10.5 + 13.9 ** 2 / 12)))
    # speeds match before the lead stops (11.4 s > 10 s): relative motion plus the lead's deceleration
    assert required_decel(21.5, 10.0, v_ego=30.0, a_tgt=1.5) == pytest.approx(1.5 + 100 / 40)
    # here the lead stops first (after 10 s; matching would take 11.4 s): stop behind its stopping point
    assert required_decel(30.0, 5.0, v_ego=25.0, a_tgt=2.0) == pytest.approx(625 / (2 * (28.5 + 100)))


def test_target_deceleration_estimate_needs_200_ms_of_history():
    m = AebModel()
    outs = []
    for k in range(25):
        outs.append(m.step(15.0, True, 40.0, 0.06 * k, 0.0))                  # lead loses 6 m/s^2 * 10 ms per cycle
    assert all(o["a_tgt"] == 0.0 for o in outs[:20]) and outs[20]["a_tgt"] == pytest.approx(6.0, rel=1e-6)


def test_calibration_changes_are_validated():
    assert make_calibration({"ttc_partial_s": 1.2}).ttc_partial_s == 1.2
    for bad in ({"ttc_partial_s": 9}, {"nope": 1}, {"ttc_full_s": 2.0}, {"areq_warn_mps2": 7.0}, {"confirm_cycles": True}):
        with pytest.raises(ValueError):
            make_calibration(bad)


# ------------------------------------------------------------------ simulation and scenarios
def test_simulation_is_deterministic_and_physical():
    s = next(x for x in catalog() if x["id"] == "CCRs-50")
    a, b = simulate(s, AebModel())["metrics"], simulate(s, AebModel())["metrics"]
    assert a == b and not a["collided"] and a["stopped"] and 0.5 < a["stop_gap_m"] < 12 and a["moved_after_stop_m"] <= 0.05
    off = dataclasses.replace(CAL, v_max_mps=5.0, v_min_mps=4.9)             # function effectively disabled at 50 km/h
    m = simulate(s, AebModel(off))["metrics"]
    assert m["collided"] and m["impact_kmh"] == pytest.approx(50.0, abs=0.1)
    none = simulate({**s, "target": "none"}, AebModel())["metrics"]
    assert none["min_gap_m"] is None and not none["collided"] and none["fcw_t"] is None


def test_catalog_follows_the_euro_ncap_grids():
    cat = catalog()
    ids = [s["id"] for s in cat]
    assert len(ids) == len(set(ids)) == 52
    fam = lambda f: [s for s in cat if s["family"] == f]  # noqa: E731
    assert [s["ego_kmh"] for s in fam("CCRs")] == list(range(10, 85, 5))
    assert [s["ego_kmh"] for s in fam("CCRm")] == list(range(30, 85, 5)) and {s["target_kmh"] for s in fam("CCRm")} == {20.0}
    assert {(s["gap_m"], s["target_decel_mps2"]) for s in fam("CCRb")} == {(12, 2), (12, 6), (40, 2), (40, 6)}
    assert all(authoring.validate_scenario(s) for s in cat)


def test_reference_model_meets_every_release_blocking_requirement(lib, full_set):
    res = campaign.run_campaign(full_set, lib["requirements"], target="model", do_b2b=False)
    assert res["verdict"]["passed"], res["verdict"]
    byid = {r["id"]: r for r in res["requirements"]}
    assert byid["AEB-S04"]["verdict"] == "fail"                              # known SOTIF finding: fog shortens the warning
    assert {x["scenario"] for x in byid["AEB-S04"]["scenarios"] if x["verdict"] == "fail"} == {"FOG-50", "FOG-60", "FOG-70"}
    assert byid["AEB-004"]["counts"]["pass"] == 4 and byid["AEB-009"]["counts"]["pass"] == 2


def test_catalog_alone_leaves_the_activation_window_uncovered(lib):
    res = campaign.run_campaign(catalog(), lib["requirements"], target="model", do_b2b=False)
    assert not res["verdict"]["passed"] and res["verdict"]["blocking_not_passed"] == ["AEB-009"]
    assert res["verdict"]["requirements"]["not_covered"] == 1


# ------------------------------------------------------------------ requirements
def test_requirement_library_and_checks(lib):
    reqs = lib["requirements"]
    assert len(reqs) == 15 and sum(r["release_blocking"] for r in reqs) == 11
    ccrb = next(r for r in reqs if r["id"] == "AEB-004")
    s = next(x for x in catalog() if x["id"] == "CCRb-12m-6")
    assert rq.applies(ccrb, s) and not rq.applies(ccrb, {**s, "gap_m": 20.0})          # exact-match selector
    base = {"collided": False, "impact_kmh": 0.0, "stopped": True, "stop_gap_m": 3.0, "min_gap_m": 3.0, "fcw_t": 1.0,
            "brake_t": 2.0, "brake_kmh": 50.0, "max_aeb_req": 9.0, "driver_respected": True, "activations": 1,
            "moved_after_stop_m": 0.0, "initial_kmh": 50.0, "initial_closing_kmh": 50.0}
    j = lambda rid, **kw: rq.judge(next(r for r in reqs if r["id"] == rid), {**base, **kw})["verdict"]  # noqa: E731
    assert j("AEB-001") == "pass" and j("AEB-001", collided=True, impact_kmh=5.0) == "fail"
    assert j("AEB-002", collided=True, impact_kmh=20.0) == "pass" and j("AEB-002", collided=True, impact_kmh=30.0) == "fail"
    assert j("AEB-005") == "pass" and j("AEB-005", fcw_t=1.5) == "fail" and j("AEB-005", brake_t=None) == "n/a"
    assert j("AEB-007", moved_after_stop_m=0.3) == "fail" and j("AEB-007", stopped=False) == "n/a"
    assert j("AEB-008", stop_gap_m=15.0) == "fail"
    assert j("AEB-009", initial_kmh=8.0, fcw_t=None, brake_t=None) == "pass" and j("AEB-009", initial_kmh=8.0, brake_t=None) == "fail"
    assert j("AEB-009", initial_kmh=90.0, brake_kmh=84.0) == "pass" and j("AEB-009", initial_kmh=90.0, brake_kmh=89.0) == "fail"
    assert j("AEB-S01", fcw_t=None, max_aeb_req=0.0) == "pass" and j("AEB-S01") == "fail"
    assert rq.summarize([], False) == "not_covered" and rq.summarize(["n/a"], True) == "not_verified"
    with pytest.raises(ValueError):
        rq.validate({"id": "bad id", "title": "x", "text": "long enough text", "check": "no_collision", "applies": {"families": ["*"]}})
    with pytest.raises(ValueError):
        rq.validate({"id": "AEB-100", "title": "x", "text": "long enough text", "check": "made_up", "applies": {"families": ["*"]}})


# ------------------------------------------------------------------ authoring
@pytest.mark.parametrize("text,expect", [
    ("Truck stopped in fog at 60 km/h on a wet road at night", {"ego_kmh": 60, "target": "stationary", "mu": 0.6, "sensor_range_m": 30, "family": "Wet road"}),
    ("Car pulling away from us at 70 km/h while we drive 50 km/h", {"ego_kmh": 50, "target": "moving", "target_kmh": 70, "family": "Lead pulling away"}),
    ("Slow tractor at 25 km/h, ego 60 km/h", {"ego_kmh": 60, "target": "moving", "target_kmh": 25, "family": "CCRm"}),
    ("Auto-rickshaw ahead brakes hard at 6 m/s2, both at 40 km/h, 15 m gap", {"ego_kmh": 40, "target": "braking", "target_kmh": 40, "gap_m": 15, "target_decel_mps2": 6, "family": "CCRb"}),
    ("Phantom braking under a metal bridge at 80 km/h", {"ego_kmh": 80, "target": "none", "family": "Phantom object"}),
    ("Cow standing on the road, we are doing 70 km/h, 300 ms sensor latency", {"ego_kmh": 70, "latency_s": 0.3, "family": "Sensor latency"}),
])
def test_plain_language_scenarios(text, expect):
    r = authoring.from_text(text)
    for k, v in expect.items():
        assert r["scenario"][k] == (pytest.approx(v) if isinstance(v, (int, float)) else v), (k, r)


def test_plain_language_reports_assumptions_and_limits():
    r = authoring.from_text("Pedestrian crossing at a junction")
    assert any("ego speed 50" in a for a in r["assumptions"])
    assert any("vulnerable road users" in n for n in r["not_modelled"]) and any("lateral" in n for n in r["not_modelled"])
    with pytest.raises(ValueError):
        authoring.from_text("hi")


def test_scenario_validation_rejects_bad_input():
    good = {"id": "X-01", "family": "CCRs", "ego_kmh": 50, "gap_m": 40}
    assert authoring.validate_scenario(good)["target"] == "stationary"
    for bad in ({**good, "ego_kmh": 500}, {**good, "family": "Moon"}, {**good, "rm -rf": 1}, {**good, "id": "../x"},
                {**good, "target": "braking"}, {**good, "duration_s": 3600}, {**good, "gap_m": "40"}):
        with pytest.raises(ValueError):
            authoring.validate_scenario(bad)


def test_boundary_value_analysis_finds_only_real_gaps(lib):
    props = authoring.propose_boundary(lib["requirements"], catalog())
    assert [(p["requirement"], p["scenario"]["ego_kmh"]) for p in props] == [("AEB-009", 8.0), ("AEB-009", 90.0)]
    assert authoring.propose_boundary(lib["requirements"], catalog() + [p["scenario"] for p in props]) == []


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    def generate(self, system, prompt, temperature=0.1):
        assert "untrusted data" in prompt
        return self.reply


def test_llm_values_are_accepted_only_within_bounds_and_when_stated():
    text = "Lorry braking ahead of us, both at 40 km/h, gap 18 m"
    r = authoring.from_text_llm(text, FakeLLM(json.dumps({"gap_m": 18, "target_decel_mps2": 7, "ego_kmh": 400, "colour": "red"})))
    assert r["scenario"]["gap_m"] == 18 and r["scenario"]["target_decel_mps2"] == 6.0     # 7 is not in the text: rule value kept
    assert "number not in the description" in r["llm"] and "out of range" in r["llm"] and "unknown field" in r["llm"]
    r = authoring.from_text_llm(text, FakeLLM("no json here"))
    assert r["origin"] == "rules" and r["llm"].startswith("not used")


# ------------------------------------------------------------------ OpenSCENARIO
def test_openscenario_and_opendrive_are_well_formed(full_set):
    for s in full_set:
        root = ET.fromstring(to_openscenario(s))
        assert root.find("FileHeader").get("revMinor") == "1"
        assert (root.find(".//ScenarioObject[@name='Target']") is None) == (s["target"] == "none")
    wet = to_openscenario(next(s for s in full_set if s["id"] == "WET-60"))
    assert 'frictionScaleFactor="0.6"' in wet and "Precipitation" in wet
    assert ET.fromstring(road_xodr()).find("road").get("length") == "3000.0"


def test_openscenario_is_valid_against_the_asam_schemas(full_set):
    sg = pytest.importorskip("scenariogeneration")
    xmlschema = pytest.importorskip("xmlschema")
    d = Path(sg.__file__).resolve().parent.parent / "schemas"
    if not (d / "OpenSCENARIO_1_1.xsd").exists():
        pytest.skip("schemas not shipped with this scenariogeneration version")
    xosc, xodr = xmlschema.XMLSchema(str(d / "OpenSCENARIO_1_1.xsd")), xmlschema.XMLSchema(str(d / "opendrive_17_core.xsd"))
    xodr.validate(road_xodr())
    for s in full_set:
        xosc.validate(to_openscenario(s))


# ------------------------------------------------------------------ firmware, SIL, back-to-back, mutation
def test_firmware_passes_the_platforms_own_code_review():
    out = subprocess.run([sys.executable, "scripts/review_gate.py", "firmware/aeb", "--fail-on", "info"], cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.skipif(not shutil.which("cc"), reason="needs cc")
def test_firmware_compiles_cleanly_with_strict_warnings(tmp_path):
    r = subprocess.run(["cc", "-std=c99", "-Wall", "-Wextra", "-Wpedantic", "-Wconversion", "-Wshadow", "-Werror", "-c",
                        str(ROOT / "firmware/aeb/aeb_ctrl.c"), "-o", str(tmp_path / "a.o")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@needs_cc
def test_sil_build_is_cached_and_layout_checked(tmp_path):
    a = sil.build(None, tmp_path)
    b = sil.build(None, tmp_path)
    assert b["cached"] and a["hash"] == b["hash"] == sil.status(tmp_path)["firmware_hash"]
    ctrl = sil.SilController(a["path"])
    assert ctrl.version == "1.0.0"
    assert campaign.calibration_check(ctrl, None) == []                      # C defaults == model defaults
    other = sil.build(campaign.mutate(sil.sources()[0], campaign.MUTANTS[5]), tmp_path)
    assert other["hash"] != a["hash"]
    assert campaign.calibration_check(sil.SilController(other["path"]), None)[0]["parameter"] == "confirm_cycles"


@needs_cc
def test_back_to_back_c_code_matches_the_model_on_every_step(full_set):
    ctrl = sil.SilController(sil.build()["path"])
    b = campaign.back_to_back(campaign.record(full_set), ctrl, None)
    assert b["passed"] and b["steps"] > 40000 and b["max_brake_diff"] == 0.0 and not b["mismatching_scenarios"]


@needs_cc
def test_sil_campaign_with_mutation_testing(lib, full_set):
    res = campaign.run_campaign(full_set, lib["requirements"], target="sil", do_b2b=True, do_mutation=True)
    assert res["target"] == "sil" and res["verdict"]["passed"] and res["back_to_back"]["passed"]
    m = res["mutation"]
    killed = {r["id"] for r in m["rows"] if r["status"] == "killed"}
    assert killed == {"M03", "M04", "M05", "M06", "M09", "M10", "M11"}
    assert m["survivors_caught_by_b2b"] == m["survived"] == 4                # back-to-back catches what requirements miss
    assert next(r for r in m["rows"] if r["id"] == "M06")["killed_by"] == ["AEB-S01"]


@needs_cc
def test_calibration_what_if_is_tested_on_the_c_code(lib, full_set):
    weak = make_calibration({"a_full_mps2": 6.0, "areq_full_mps2": 7.0})
    res = campaign.run_campaign(full_set, lib["requirements"], target="sil", cal=weak)
    assert not res["verdict"]["passed"] and {"AEB-003", "AEB-004"} <= set(res["verdict"]["blocking_not_passed"])
    assert res["back_to_back"]["passed"]                                     # same calibration on both sides


def test_every_mutant_applies_exactly_once():
    c_text = sil.sources()[0]
    for m in campaign.MUTANTS:
        assert campaign.mutate(c_text, m) != c_text
    with pytest.raises(ValueError):
        campaign.mutate(c_text, {"id": "X", "find": "ctx->", "replace": "y"})


def test_cli_campaign_is_a_ci_gate(tmp_path):
    out = subprocess.run([sys.executable, "scripts/sil_run.py", "campaign", "--target", "auto", "--out", str(tmp_path)],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "PASSED" in out.stdout and (tmp_path / "validation_report.md").read_text().startswith("# AEB virtual validation report")
    bad = subprocess.run([sys.executable, "scripts/sil_run.py", "campaign", "--target", "model", "--no-bva"], cwd=ROOT,
                         capture_output=True, text=True)
    assert bad.returncode == 1 and "AEB-009" in bad.stdout


# ------------------------------------------------------------------ service and API
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("VECTOR_BACKEND", "numpy")
    c = TestClient(create_app(Container(get_settings())))
    assert c.post("/projects", json={"name": "aeb"}).status_code == 201
    return c


A = "/projects/aeb/adas"
U = lambda u: {"X-User": u}  # noqa: E731


@needs_cc
def test_validation_workflow_end_to_end(api):
    c = api
    assert c.post(f"{A}/campaigns", json={}, headers=U("ravi")).status_code == 409             # nothing loaded yet
    r = c.post(f"{A}/library/load", headers=U("asha")).json()
    assert r["requirements_added"] == 15 and r["scenarios_added"] == 52
    assert c.post(f"{A}/library/load", headers=U("asha")).json()["scenarios_added"] == 0       # idempotent
    assert c.get(f"{A}/coverage").json()["gaps"] == ["AEB-009"]

    first = c.post(f"{A}/campaigns", json={"target": "sil"}, headers=U("ravi")).json()
    assert first["payload"]["verdict"]["blocking_not_passed"] == ["AEB-009"]
    rel = c.post(f"{A}/campaigns/{first['ref']}/decision", json={"decision": "approved", "comment": "ok for me"}, headers=U("asha"))
    assert rel.status_code == 409 and "AEB-009" in rel.json()["detail"]

    props = c.post(f"{A}/scenarios/propose", headers=U("ravi")).json()
    assert [p["id"] for p in props["proposals"]] == ["BVA-ACTWIN-08", "BVA-ACTWIN-90"]
    for p in props["proposals"]:                                                            # machine proposals: any reviewer
        assert c.post(f"{A}/scenarios/{p['ref']}/decision", json={"decision": "approved"}, headers=U("ravi")).status_code == 200
    txt = c.post(f"{A}/scenarios/from-text", json={"text": "Truck stopped in fog at 60 km/h on a wet road"}, headers=U("ravi")).json()
    sref = txt["scenario"]["ref"]
    assert txt["scenario"]["status"] == "proposed" and txt["understood"]
    assert c.post(f"{A}/scenarios/{sref}/decision", json={"decision": "approved"}, headers=U("ravi")).status_code == 409   # four-eyes
    assert c.post(f"{A}/scenarios/{sref}/decision", json={"decision": "approved"}, headers=U("asha")).status_code == 200
    one = c.post(f"{A}/scenarios/{sref}/run", json={"target": "sil"}).json()
    assert one["metrics"]["collided"] and one["trace"] and {v["requirement"] for v in one["verdicts"]} >= {"AEB-S03"}

    model_only = c.post(f"{A}/campaigns", json={"target": "model"}, headers=U("ravi")).json()
    good = c.post(f"{A}/campaigns", json={"target": "sil", "mutation": True, "note": "after closing the gap"}, headers=U("ravi")).json()
    assert good["payload"]["verdict"]["passed"] and good["stale"] == [] and good["payload"]["mutation"]["score"] >= 0.6
    assert c.get(f"{A}/campaigns").json()[0]["ref"] == good["ref"]
    assert c.get(f"{A}/campaigns/{first['ref']}").json()["stale"] == ["the approved scenarios changed"]
    for who, ref, why in (("asha", model_only["ref"], "model only"), ("ravi", good["ref"], "Four-eyes")):
        r = c.post(f"{A}/campaigns/{ref}/decision", json={"decision": "approved", "comment": "reviewed it"}, headers=U(who))
        assert r.status_code == 409 and why in r.json()["detail"]
    assert c.post(f"{A}/campaigns/{good['ref']}/decision", json={"decision": "approved", "comment": ""}, headers=U("asha")).status_code == 422
    ok = c.post(f"{A}/campaigns/{good['ref']}/decision", json={"decision": "approved", "comment": "Requirement table, b2b and mutants reviewed"},
                headers=U("asha")).json()
    assert ok["status"] == "approved" and ok["approved_by"] == "asha"
    assert c.get(f"{A}/status").json()["current_release"]["ref"] == good["ref"]

    md = c.get(f"{A}/campaigns/{good['ref']}/report").text
    assert "RELEASED by asha" in md and "M02 | New full braking needs TTC and a_req together (\\|\\| became &&)" in md
    assert json.loads(c.get(f"{A}/campaigns/{good['ref']}/report", params={"format": "json"}).text)["ref"] == good["ref"]

    # change impact: editing an approved requirement invalidates the release
    req = next(r for r in c.get(f"{A}/requirements").json() if r["payload"]["id"] == "AEB-005")
    body = {k: req["payload"][k] for k in ("title", "text", "category", "release_blocking", "check", "params", "applies")}
    body["params"] = {"min_s": 1.0}
    assert c.put(f"{A}/requirements/{req['ref']}", json={"requirement": {**body, "id": "HACK-1"}}, headers=U("ravi")).json()["payload"]["id"] == "AEB-005"
    rel = c.get(f"{A}/campaigns/{good['ref']}").json()
    assert rel["status"] == "suspect" and "AEB-005" in rel["payload"]["suspect_reason"]
    assert any(n["ref"] == good["ref"] for n in c.get("/projects/aeb/trace/suspect").json())
    assert good["ref"] in {d["ref"] for d in c.get(f"/projects/aeb/trace/nodes/{req['ref']}").json()["descendants"]}


def test_api_validation_errors_and_exports(api):
    c = api
    c.post(f"{A}/library/load", headers=U("asha"))
    scn = c.get(f"{A}/scenarios", params={"family": "CCRs"}).json()[0]
    assert c.post(f"{A}/scenarios/{scn['ref']}/run", json={"target": "model", "calibration": {"ttc_partial_s": 9}}).status_code == 422
    assert c.post(f"{A}/scenarios/{scn['ref']}/run", json={"target": "hil"}).status_code == 422
    assert c.post(f"{A}/scenarios/SC-999/run", json={}).status_code == 404
    assert c.post(f"{A}/scenarios", json={"scenario": {"id": "MY-01", "family": "CCRs", "ego_kmh": 999, "gap_m": 30}}, headers=U("ravi")).status_code == 422
    assert c.post(f"{A}/scenarios", json={"scenario": {**scn["payload"]["scenario"]}}, headers=U("ravi")).status_code == 409
    new = c.post(f"{A}/scenarios", json={"scenario": {"id": "MY-01", "family": "CCRs", "ego_kmh": 42, "gap_m": 30}}, headers=U("ravi"))
    assert new.status_code == 201 and new.json()["status"] == "proposed"
    assert c.post(f"{A}/scenarios/from-text", json={"text": "x" * 600}, headers=U("ravi")).status_code == 422
    assert c.post(f"{A}/campaigns", json={"target": "quantum"}, headers=U("ravi")).status_code == 422
    r = c.post(f"{A}/requirements", json={"requirement": {"id": "AEB-020", "title": "Warn early on wet roads",
                                                          "text": "On wet roads the warning should come 1 s before braking.",
                                                          "check": "fcw_lead", "params": {"min_s": 1.0}, "release_blocking": False,
                                                          "applies": {"families": ["Wet road"]}}}, headers=U("ravi"))
    assert r.status_code == 201 and r.json()["status"] == "proposed"
    assert c.post(f"{A}/requirements/{r.json()['ref']}/decision", json={"decision": "approved"}, headers=U("ravi")).status_code == 409
    x = c.get(f"{A}/scenarios/{scn['ref']}/openscenario")
    assert x.status_code == 200 and ET.fromstring(x.content).tag == "OpenSCENARIO"
    z = zipfile.ZipFile(io.BytesIO(c.get(f"{A}/openscenario.zip").content))
    assert "straight_road.xodr" in z.namelist() and "CCRb-12m-6.xosc" in z.namelist() and "MY-01.xosc" not in z.namelist()
    fw = c.get(f"{A}/firmware").json()
    assert [f["name"] for f in fw["files"]] == ["aeb_ctrl.c", "aeb_ctrl.h"] and fw["review_findings"] == []
    ref = c.get(f"{A}/reference").json()
    assert {x["name"] for x in ref["calibration"]} >= {"ttc_partial_s", "a_full_mps2"} and len(ref["mutants"]) == 11
    assert c.post(f"{A}/campaigns", json={"target": "model", "back_to_back": True}, headers=U("ravi")).json()["payload"]["notes"]
