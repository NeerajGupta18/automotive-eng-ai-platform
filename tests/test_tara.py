import json
from datetime import date
from pathlib import Path

import pytest

from app.core.errors import Invalid
from app.modules.tara import architecture as arch, rules

S = Path(__file__).resolve().parent.parent / "data" / "samples"
A, B = {"X-User": "alice"}, {"X-User": "bob"}
T = "/projects/tara1/tara"
AP_EASY = dict(elapsed_time="<=1w", expertise="proficient", knowledge="restricted", window="moderate", equipment="standard")
IMP = dict(safety="major", financial="moderate", operational="moderate", privacy="negligible")


@pytest.fixture
def tara(client):
    client.post("/projects", json={"name": "tara1"})
    assert client.post(T + "/library/load", json={}).status_code == 201
    sysref = client.post(T + "/systems", headers=A, json={
        "name": "BCM system", "architecture": json.loads((S / "tara_system_bcm.json").read_text())}).json()["ref"]
    r = client.post(f"{T}/systems/{sysref}/analyze", headers=A)
    assert r.status_code == 200, r.text
    return client, sysref


def find(c, asset_name, pattern):
    return next(t for t in c.get(T + "/threats").json()
                if t["payload"]["asset_name"] == asset_name and t["payload"]["pattern"] == pattern)


def assess(c, ref, who=A, impact=IMP, ap=AP_EASY, **kw):
    return c.put(f"{T}/threats/{ref}/assessment", headers=who, json={"impact": impact, "ap": ap, "rationale": "Reasoned by the security team", **kw})


# ------------------------------------------------------------------ rules
def test_attack_potential_thresholds():
    assert rules.feasibility(13) == "high" and rules.feasibility(14) == "medium"
    assert rules.feasibility(19) == "medium" and rules.feasibility(20) == "low"
    assert rules.feasibility(24) == "low" and rules.feasibility(25) == "very_low"
    assert rules.attack_potential(AP_EASY) == (11, "high")
    hard = dict(elapsed_time=">6m", expertise="multiple_experts", knowledge="strictly_confidential", window="difficult", equipment="multiple_bespoke")
    assert rules.attack_potential(hard) == (57, "very_low")
    with pytest.raises(Invalid):
        rules.attack_potential({**AP_EASY, "expertise": "wizard"})


def test_risk_matrix_and_impact():
    assert rules.risk_value("severe", "high") == 5 and rules.risk_value("negligible", "high") == 1
    assert rules.risk_value("major", "high") == 4 and rules.risk_value("severe", "very_low") == 2
    for lvl in rules.IMPACT_LEVELS:            # risk never decreases as feasibility grows
        vals = [rules.risk_value(lvl, f) for f in ("very_low", "low", "medium", "high")]
        assert vals == sorted(vals)
    assert rules.impact_rating(IMP) == "major"
    with pytest.raises(Invalid):
        rules.impact_rating({**IMP, "safety": "huge"})


# ------------------------------------------------------------------ architecture model
def test_architecture_validation():
    with pytest.raises(Invalid):
        arch.normalize({"components": [{"name": "A"}, {"name": "A"}]})
    with pytest.raises(Invalid):
        arch.normalize({"components": [{"name": "A", "interfaces": ["Telepathy"]}]})
    with pytest.raises(Invalid):
        arch.normalize({"components": [{"name": "A"}], "data_flows": [{"name": "m", "from": "A", "to": "Z", "channel": "CAN"}]})
    with pytest.raises(Invalid):
        arch.normalize({"components": [{"name": "A", "type": "toaster"}]})


def test_comm_matrix_parsing_and_merge():
    frag = arch.parse_comm_matrix((S / "comm_matrix.csv").read_text())
    assert {c["name"] for c in frag["components"]} == {"PT_ECU", "BCM", "Gateway", "Airbag_ECU"}
    assert len(frag["data_flows"]) == 4 and all(f["channel"] == "CAN" for f in frag["data_flows"])
    assert next(f for f in frag["data_flows"] if f["name"] == "CrashState")["safety_relevant"]
    for bad in ("a,b\n1,2\n", "message,sender,receivers,bus\nm,,B,CAN\n", "message,sender,receivers,bus\nm,A,B,carrier-pigeon\n"):
        with pytest.raises(Invalid):
            arch.parse_comm_matrix(bad)
    base = arch.normalize(json.loads((S / "tara_system_bcm.json").read_text()))
    merged = arch.merge(base, frag)
    assert "Airbag_ECU" in {c["name"] for c in merged["components"]}
    assert next(c for c in merged["components"] if c["name"] == "BCM")["zone"] == "body"    # curated data kept


def test_surface_and_boundaries():
    a = arch.normalize(json.loads((S / "tara_system_bcm.json").read_text()))
    exp = {(r["component"], r["interface"]): r["exposure"] for r in arch.attack_surface(a)}
    assert exp[("Telematics", "Cellular")] == "remote" and exp[("Gateway", "OBD")] == "physical"
    assert exp[("BCM", "CAN")] == "internal"
    crossing = {c["flow"] for c in arch.boundary_crossings(a)}
    assert {"OTAPackage", "DiagRequest", "VehicleSpeed"} <= crossing and "DoorLockCmd" not in crossing


# ------------------------------------------------------------------ library
def test_library_validation(client):
    client.post("/projects", json={"name": "tara1"})
    assert client.post(T + "/library/load", json={"library": {"patterns": [], "controls": []}}).status_code == 422
    lib = json.loads((Path(__file__).resolve().parent.parent / "data/library/tara_library.json").read_text())
    lib["patterns"][0]["controls"] = ["C-NOPE"]
    assert client.post(T + "/library/load", json={"library": lib}).status_code == 422
    lib["patterns"][0]["controls"] = ["C-IDS"]; lib["patterns"][0]["stride"] = "hacking"
    assert client.post(T + "/library/load", json={"library": lib}).status_code == 422
    assert client.post(f"{T}/systems", json={"name": "x"}).status_code == 201
    assert client.post(f"{T}/systems/SYS-001/analyze").status_code == 409        # no library / no components


# ------------------------------------------------------------------ analysis
def test_asset_and_threat_generation(tara):
    c, s = tara
    names = {a["payload"]["name"] for a in c.get(T + "/assets").json()}
    assert {"VehicleSpeed", "DoorLockCmd", "Gateway diagnostic access", "Telematics update package",
            "Telematics personal data", "Gateway cryptographic keys", "BCM configuration data"} <= names
    pats = lambda asset: {t["payload"]["pattern"] for t in c.get(T + "/threats").json() if t["payload"]["asset_name"] == asset}
    assert pats("VehicleSpeed") == {"TP-001", "TP-002", "TP-003", "TP-004"}
    assert pats("DoorLockCmd") == {"TP-008"}
    assert pats("Gateway diagnostic access") == {"TP-009", "TP-010", "TP-011"}
    assert {"TP-013", "TP-014", "TP-015"} <= pats("Telematics update package")
    assert pats("DiagRequest") == set()                 # nothing in the library covers this: reported, not invented
    t = find(c, "VehicleSpeed", "TP-001")
    assert t["status"] == "proposed" and t["payload"]["origin"] == "library" and t["payload"]["source"]["library"] == "TP-001"
    assert t["payload"]["risk"] is None and t["payload"]["exposure"] == "internal"


def test_analysis_is_idempotent_and_reports_unmatched(tara):
    c, s = tara
    n = len(c.get(T + "/threats").json())
    r = c.post(f"{T}/systems/{s}/analyze").json()
    assert r["assets_created"] == 0 and r["threats_created"] == 0 and len(c.get(T + "/threats").json()) == n
    assert r["assets_without_threats"]


def test_safety_suggestion_downgraded_for_non_safety_assets(tara):
    c, _ = tara
    unsafe = find(c, "UpdateImage", "TP-005")["payload"]["suggested"]
    assert unsafe["impact"]["safety"] == "negligible" and "not marked safety relevant" in unsafe["note"]
    assert find(c, "VehicleSpeed", "TP-001")["payload"]["suggested"]["impact"]["safety"] == "major"


def test_stale_library_entries_are_flagged(container, tara):
    c, s = tara
    assert not any(t["payload"]["source"]["stale"] for t in c.get(T + "/threats").json())
    container.tara.today = lambda: date(2028, 6, 1)
    r = c.post(f"{T}/systems/{s}/analyze").json()
    assert any("past its review date" in w for w in r["warnings"])
    assert any(p["stale"] for p in c.get(T + "/library").json()["patterns"])


# ------------------------------------------------------------------ assessment workflow
def test_assessment_computes_risk_and_enforces_four_eyes(tara):
    c, _ = tara
    ref = find(c, "VehicleSpeed", "TP-001")["ref"]
    assert c.put(f"{T}/threats/{ref}/assessment", headers=A, json={"impact": IMP, "ap": AP_EASY}).status_code == 422
    assert assess(c, ref, impact={**IMP, "safety": "huge"}).status_code == 422
    r = assess(c, ref).json()
    p = r["payload"]
    assert (p["impact_rating"], p["ap_sum"], p["feasibility"], p["risk"]) == ("major", 11, "high", 4)
    assert p["suggestion_adopted"] and p["mitigation_required"] and r["status"] == "assessed"
    assert c.post(f"{T}/threats/{ref}/decision", headers=A, json={"decision": "approved"}).status_code == 409
    assert c.post(f"{T}/threats/{ref}/decision", headers=B, json={"decision": "approved"}).json()["approved_by"] == "bob"
    other = find(c, "VehicleSpeed", "TP-002")["ref"]
    assert c.post(f"{T}/threats/{other}/decision", headers=B, json={"decision": "approved"}).status_code == 422   # unassessed


def test_treatment_and_risk_acceptance(tara):
    c, _ = tara
    ref = find(c, "VehicleSpeed", "TP-001")["ref"]
    assert c.put(f"{T}/threats/{ref}/treatment", headers=A, json={"decision": "reduce"}).status_code == 409   # not assessed
    assess(c, ref)
    assert c.put(f"{T}/threats/{ref}/treatment", headers=A, json={"decision": "ignore"}).status_code == 422
    assert c.put(f"{T}/threats/{ref}/treatment", headers=A, json={"decision": "retain", "justification": "meh"}).status_code == 422
    r = c.put(f"{T}/threats/{ref}/treatment", headers=A, json={"decision": "retain", "justification": "Attack needs physical bus access that the vehicle design prevents."}).json()
    assert r["payload"]["risk_acceptance"] == "pending"
    assert c.post(f"{T}/threats/{ref}/accept-risk", headers=A, json={}).status_code == 409
    assert c.post(f"{T}/threats/{ref}/accept-risk", headers=B, json={"comment": "agreed"}).json()["payload"]["risk_acceptance"] == "accepted"
    assert c.post(f"{T}/threats/{ref}/accept-risk", headers=B, json={}).status_code == 409


def test_safety_link_to_hara(tara):
    c, _ = tara
    H = "/projects/tara1/hara"
    c.post(H + "/library/load", json={})
    item = c.post(H + "/items", json={"name": "BCM", "functions": [{"name": "Central door locking", "guidewords": ["unintended"]}]}).json()["ref"]
    c.post(f"{H}/items/{item}/suggest-hazards"); c.post(H + "/hazards/H-001/decision", json={"decision": "accepted"})
    ev = c.post(H + "/hazards/H-001/propose-events").json()["events"][0]["ref"]
    ref = find(c, "VehicleSpeed", "TP-001")["ref"]
    assert assess(c, ref, safety_link=ev).status_code == 422                     # event not approved yet
    good = {"rationale_S": "Occupant ejection risk at speed", "rationale_E": "Highway driving is frequent", "rationale_C": "Driver cannot react in time"}
    c.put(f"{H}/events/{ev}/assessment", headers=A, json={"S": 3, "E": 4, "C": 3, **good})
    c.post(f"{H}/events/{ev}/decision", headers=B, json={"decision": "approved"})
    low = assess(c, ref, impact={**IMP, "safety": "moderate"}, safety_link=ev).json()
    assert "lower than the HARA severity S3" in low["payload"]["safety_warning"]
    ok = assess(c, ref, impact={**IMP, "safety": "severe"}, safety_link=ev).json()
    assert ok["payload"]["safety_warning"] is None and ok["payload"]["risk"] == 5
    assert assess(c, ref, safety_link="TS-001").status_code == 422               # not a HARA event


def workflow(c, s, refs=("TP-001", "TP-002")):
    """assess+treat+approve the given VehicleSpeed threats, derive goal, recommend controls."""
    for p in refs:
        ref = find(c, "VehicleSpeed", p)["ref"]
        assess(c, ref); c.put(f"{T}/threats/{ref}/treatment", headers=A, json={"decision": "reduce"})
        assert c.post(f"{T}/threats/{ref}/decision", headers=B, json={"decision": "approved"}).status_code == 200
    goals = c.post(f"{T}/systems/{s}/derive-goals", headers=A).json()
    g = goals["goals"][0]["ref"]
    reqs = c.post(f"{T}/goals/{g}/recommend-controls", headers=A).json()["requirements"]
    return g, reqs, goals


def test_goals_and_controls_with_provenance(tara):
    c, s = tara
    g, reqs, goals = workflow(c, s)
    assert len(goals["goals"]) == 1                                              # same asset + property grouped
    gp = goals["goals"][0]["payload"]
    assert gp["risk"] == 4 and gp["property"] == "authenticity" and len(gp["threats"]) == 2
    assert {r["payload"]["control_id"] for r in reqs} == {"C-AUTH-MSG", "C-GW-FILTER", "C-IDS"}
    auth = next(r for r in reqs if r["payload"]["control_id"] == "C-AUTH-MSG")["payload"]
    assert auth["source"]["library"] == "C-AUTH-MSG" and auth["assumptions"] and auth["alternatives"]
    assert auth["suggested_allocation"] == "BCM" and "VehicleSpeed" in auth["text"]
    assert any(q["rule"] == "Q004" for q in auth["quality"])                     # placeholder [MAC_LENGTH_BITS]
    assert c.post(f"{T}/systems/{s}/derive-goals").json()["created"] == []       # idempotent


def test_untreated_or_unapproved_threats_produce_no_goals(tara):
    c, s = tara
    ref = find(c, "VehicleSpeed", "TP-001")["ref"]
    assess(c, ref)
    assert c.post(f"{T}/systems/{s}/derive-goals").json()["goals"] == []         # not approved / no treatment


def test_csr_gates_and_completion(tara):
    c, s = tara
    g, reqs, _ = workflow(c, s)
    auth = next(r for r in reqs if r["payload"]["control_id"] == "C-AUTH-MSG")["ref"]
    dec = lambda r, who=B: c.post(f"{T}/requirements/{r}/decision", headers=who, json={"decision": "approved"})
    assert dec(auth).status_code == 422 and "placeholder" in dec(auth).json()["detail"].lower()
    c.put(f"{T}/requirements/{auth}", headers=A, json={
        "text": "The BCM shall verify the authenticity and freshness of 'VehicleSpeed' using a message authentication code of at least 64 bits before processing the message.",
        "allocated_to": "NoSuchEcu", "verification": ["SEC-T-1"]})
    r = dec(auth)
    assert r.status_code == 422 and "not a component" in r.json()["detail"]
    c.put(f"{T}/requirements/{auth}", headers=A, json={"allocated_to": "BCM"})
    assert dec(auth, A).status_code == 409                                       # four-eyes
    assert dec(auth).status_code == 200
    comp = c.get(f"{T}/systems/{s}/completeness").json()
    checks = {x["id"]: x for x in comp["checks"]}
    assert not comp["complete"] and auth not in checks["csr_approved"]["failing"]
    assert set(checks["csr_allocated"]["failing"]) == {r["ref"] for r in reqs} - {auth}
    assert 0 < comp["traceability_completeness"] < 1


def test_change_impact_on_reassessment(tara):
    c, s = tara
    g, reqs, _ = workflow(c, s)
    ref = find(c, "VehicleSpeed", "TP-001")["ref"]
    hard = dict(elapsed_time=">6m", expertise="multiple_experts", knowledge="strictly_confidential", window="difficult", equipment="multiple_bespoke")
    r = assess(c, ref, ap=hard).json()
    assert r["payload"]["risk"] == 1 and r["status"] == "assessed"
    st = {x["ref"]: x["status"] for x in c.get(T + "/goals").json() + c.get(T + "/requirements").json()}
    assert st[g] == "suspect" and all(st[x["ref"]] == "suspect" for x in reqs)
    assert "changed 4 -> 1" in c.get(T + "/goals").json()[0]["payload"]["suspect_reason"]


def test_coverage_checklist_and_not_applicable_flow(tara):
    c, s = tara
    cov = c.get(f"{T}/systems/{s}/coverage").json()
    assert cov["expert_review_required"] and cov["gaps"] > 0 and 0 < cov["coverage"] < 1
    gap = next(r for r in cov["rows"] if r["status"] == "missing")
    body = {"component": gap["component"], "interface": gap["interface"], "stride": gap["stride"]}
    assert c.post(f"{T}/systems/{s}/coverage/na", headers=A, json={**body, "justification": "too short"}).status_code == 422
    na = c.post(f"{T}/systems/{s}/coverage/na", headers=A, json={**body, "justification": "Interface is disabled in production software builds."}).json()
    pend = c.get(f"{T}/systems/{s}/coverage").json()
    assert next(r for r in pend["rows"] if r["na_ref"] == na["ref"])["status"] == "na_pending" and pend["gaps"] == cov["gaps"]
    assert c.post(f"{T}/na/{na['ref']}/decision", headers=A, json={"decision": "approved"}).status_code == 409
    c.post(f"{T}/na/{na['ref']}/decision", headers=B, json={"decision": "approved"})
    done = c.get(f"{T}/systems/{s}/coverage").json()
    assert done["gaps"] == cov["gaps"] - 1 and next(r for r in done["rows"] if r["na_ref"] == na["ref"])["status"] == "not_applicable"


def test_manual_threat_addition(tara):
    c, s = tara
    asset = next(a for a in c.get(T + "/assets").json() if a["payload"]["name"] == "DiagRequest")["ref"]
    body = {"name": "Malformed diagnostic frame", "stride": "denial_of_service", "property": "availability", "interface": "OBD",
            "description": "Crafted diagnostic frames could make the gateway stop responding.", "damage": "Diagnostics unavailable"}
    r = c.post(f"{T}/assets/{asset}/threats", headers=A, json=body)
    assert r.status_code == 201 and r.json()["payload"]["origin"] == "manual"
    assert c.post(f"{T}/assets/{asset}/threats", headers=A, json=body).status_code == 409
    assert c.post(f"{T}/assets/{asset}/threats", headers=A, json={**body, "stride": "hacking"}).status_code == 422


def test_imports_via_api(tara, sample):
    c, s = tara
    r = c.post(f"{T}/systems/{s}/import/comm-matrix", headers=A, files={"file": ("cm.csv", (S / "comm_matrix.csv").read_bytes())})
    assert r.status_code == 200 and "Airbag_ECU" in {x["name"] for x in r.json()["payload"]["architecture"]["components"]}
    assert c.post(f"{T}/systems/{s}/import/comm-matrix", files={"file": ("bad.csv", b"x,y\n1,2")}).status_code == 422
    assert c.post(f"{T}/systems/{s}/import/comm-matrix", files={"file": ("bin.csv", b"\xff\xfe\x00")}).status_code == 422
    doc = c.post("/projects/tara1/documents", files={"file": ("hld_bcm_v1.md", sample("hld_bcm_v1.md"))}).json()
    r = c.post(f"{T}/systems/{s}/import/hld", json={"doc_id": doc["id"]})
    comps = {x["name"] for x in r.json()["payload"]["architecture"]["components"]}
    assert "SWC_DoorLockCtrl" in comps and "BCM" in comps and r.json()["payload"]["hld_doc_id"] == doc["id"]
    assert c.post(f"{T}/systems/{s}/import/hld", json={"doc_id": "nope"}).status_code == 422


def test_reports_metrics_history_and_isolation(tara):
    c, s = tara
    g, reqs, _ = workflow(c, s)
    ref = find(c, "VehicleSpeed", "TP-001")["ref"]
    hist = c.get(f"{T}/records/{ref}/history").json()
    assert [h["event"] for h in hist][:4] == ["create", "assess", "treat", "decision:approved"]
    assert {h["actor"] for h in hist} >= {"rules", "alice", "bob"}
    md = c.get(f"{T}/systems/{s}/report", params={"format": "md"}).text
    assert "## Attack surface" in md and "## Coverage checklist" in md and "approved by bob" in md and "C-AUTH-MSG" not in md.split("## Threat")[0]
    assert c.get(f"{T}/systems/{s}/report", params={"format": "csv"}).text.startswith("asset,")
    assert json.loads(c.get(f"{T}/systems/{s}/report", params={"format": "json"}).text)["ruleset"] == rules.RULESET_VERSION
    m = c.get(f"{T}/systems/{s}/metrics").json()
    assert m["knowledge_reuse"] == 1.0 and m["counts"]["goals"] == 1 and m["risk_distribution"] == {"4": 2}
    assert m["recommendation_acceptance"]["accepted"] >= 2
    assert c.get(f"{T}/graph").text.startswith("digraph")
    c.post("/projects", json={"name": "other1"})
    assert c.get(f"/projects/other1/tara/systems/{s}/coverage").status_code == 404
    assert c.get(f"/projects/other1/tara/records/{ref}/history").status_code == 404
    assert c.get("/tara/rules").json()["ruleset"] == rules.RULESET_VERSION
