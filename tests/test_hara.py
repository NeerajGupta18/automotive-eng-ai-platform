import pytest

from app.core.reqquality import check_requirement
from app.modules.hara import rules

A, B = {"X-User": "alice"}, {"X-User": "bob"}
GOOD = {"rationale_S": "Door opening at speed can eject occupants", "rationale_E": "Highway driving is common",
        "rationale_C": "Driver can rarely react in time"}


def P(path=""):
    return f"/projects/hara1/hara{path}"


@pytest.fixture
def proj(client):
    client.post("/projects", json={"name": "hara1"})
    assert client.post(P("/library/load"), json={}).status_code == 201
    item = client.post(P("/items"), headers=A, json={
        "name": "BCM central locking", "description": "door locking",
        "functions": [{"name": "Central door locking", "description": "locks and unlocks all doors",
                       "guidewords": ["unintended", "loss"]}]}).json()
    return client, item["ref"]


def test_asil_rule_corners():
    exp = {(3, 4, 3): "D", (3, 4, 2): "C", (3, 3, 3): "C", (2, 4, 3): "C", (3, 4, 1): "B", (1, 4, 3): "B",
           (3, 2, 3): "B", (3, 1, 3): "A", (2, 3, 2): "A", (1, 1, 1): "QM", (1, 3, 3): "A", (2, 2, 2): "QM",
           (3, 0, 3): "QM", (0, 4, 3): "QM", (3, 4, 0): "QM"}
    for sec, a in exp.items():
        assert rules.asil(*sec) == a, sec
    assert rules.asil(1, 4, 3) == "B" and rules.max_asil([r["asil"] for r in rules.asil_table() if r["S"] == 1]) == "B"
    with pytest.raises(Exception):
        rules.asil(4, 1, 1)
    assert rules.max_asil(["A", "C", "QM"]) == "C"


def test_requirement_quality_rules():
    ids = lambda t, **k: {f["rule"] for f in check_requirement(t, **k)}
    assert "Q001" in ids("The door should lock quickly when driving above the threshold")
    assert {"Q004"} <= ids("The BCM shall transition to safe state within [FTTI_ms] ms of detection.")
    assert "Q003" in ids("The BCM shall respond quickly to a detected fault in the door lock.")
    assert "Q002" in ids("The BCM shall lock doors and the BCM shall report the lock state to the gateway.")
    assert "Q008" in ids("The BCM shall lock all doors above the speed threshold without delay.", require_quantity=True)
    assert ids("The BCM shall lock all doors within 200 ms after speed exceeds 10 km/h.", require_quantity=True) == set()
    assert ids("") == {"Q000"}


def test_library_validation(client):
    client.post("/projects", json={"name": "hara1"})
    assert client.post(P("/library/load"), json={"library": {"entries": []}}).status_code == 422
    bad = {"entries": [{"id": "X", "function": "f", "malfunction": "nope", "hazard": "h", "situation": "s",
                        "S": 1, "E": 1, "C": 1, "rationale": {}}]}
    assert client.post(P("/library/load"), json={"library": bad}).status_code == 422


def test_item_validation(client):
    client.post("/projects", json={"name": "hara1"})
    assert client.post(P("/items"), json={"name": "x", "functions": []}).status_code == 422
    assert client.post(P("/items"), json={"name": "x", "functions": [{"name": "a"}, {"name": "A"}]}).status_code == 422
    assert client.post(P("/items"), json={"name": "x", "functions": [{"name": "a", "guidewords": ["bogus"]}]}).status_code == 422
    assert client.post(P("/items"), json={"name": "x", "functions": [{"name": "a"}], "hld_doc_id": "nope"}).status_code == 422


def test_hazard_suggestion_is_idempotent_and_uses_library(proj):
    c, item = proj
    r = c.post(P(f"/items/{item}/suggest-hazards"), headers=A).json()
    assert r["created"] == 2
    assert c.post(P(f"/items/{item}/suggest-hazards"), headers=A).json()["created"] == 0
    unint = next(h for h in r["hazards"] if h["payload"]["guideword"] == "unintended")
    assert unint["status"] == "proposed" and unint["payload"]["origin"] == "rules"
    assert any(s["id"] == "LIB-H-001" for s in unint["payload"]["similar"])


def test_events_require_accepted_hazard(proj):
    c, item = proj
    c.post(P(f"/items/{item}/suggest-hazards"))
    assert c.post(P("/hazards/H-001/propose-events")).status_code == 409
    assert c.post(P("/hazards/H-001/decision"), json={"decision": "accepted"}).status_code == 200
    assert c.post(P("/hazards/H-001/decision"), json={"decision": "rejected"}).status_code == 409
    assert c.post(P("/hazards/H-001/decision"), json={"decision": "maybe"}).status_code == 422
    ev = c.post(P("/hazards/H-001/propose-events")).json()
    assert ev["created"] >= 4
    hw = next(e for e in ev["events"] if e["payload"]["situation"].startswith("Highway"))
    assert hw["payload"]["suggested"]["source"] == "LIB-H-001" and hw["payload"]["asil"] is None


def test_assessment_validation_and_four_eyes(proj):
    c, item = proj
    c.post(P(f"/items/{item}/suggest-hazards")); c.post(P("/hazards/H-001/decision"), json={"decision": "accepted"})
    ev = c.post(P("/hazards/H-001/propose-events")).json()["events"]
    hw = next(e for e in ev if e["payload"]["situation"].startswith("Highway"))["ref"]
    assert c.put(P(f"/events/{hw}/assessment"), json={"S": 3, "E": 2, "C": 2}, headers=A).status_code == 422   # no rationale
    assert c.put(P(f"/events/{hw}/assessment"), json={"S": 4, "E": 2, "C": 2, **GOOD}, headers=A).status_code == 422
    r = c.put(P(f"/events/{hw}/assessment"), json={"S": 3, "E": 2, "C": 2, **GOOD}, headers=A).json()
    assert r["payload"]["asil"] == "A" and r["payload"]["suggestion_adopted"] and r["status"] == "assessed"
    assert "example-additive-v1" in r["payload"]["asil_rationale"]
    # not assessed -> cannot approve; author cannot approve; other reviewer can
    other = next(e["ref"] for e in ev if e["ref"] != hw)
    assert c.post(P(f"/events/{other}/decision"), json={"decision": "approved"}, headers=B).status_code == 422
    assert c.post(P(f"/events/{hw}/decision"), json={"decision": "approved"}, headers=A).status_code == 409
    ok = c.post(P(f"/events/{hw}/decision"), json={"decision": "approved", "comment": "agree"}, headers=B).json()
    assert ok["status"] == "approved" and ok["approved_by"] == "bob"
    assert c.post(P(f"/events/{hw}/decision"), json={"decision": "approved"}, headers=B).status_code == 409


def full_flow(c, item, asil_sec=(3, 4, 3)):
    """Runs a complete, compliant pass through one goal. Returns refs."""
    c.post(P(f"/items/{item}/suggest-hazards"))
    c.post(P("/hazards/H-001/decision"), json={"decision": "accepted"})
    c.post(P("/hazards/H-002/decision"), json={"decision": "rejected"})
    ev = c.post(P("/hazards/H-001/propose-events")).json()["events"]
    hw = next(e["ref"] for e in ev if e["payload"]["situation"].startswith("Highway"))
    for e in ev:
        if e["ref"] != hw:
            c.post(P(f"/events/{e['ref']}/decision"), json={"decision": "rejected"}, headers=B)
    S, E, C = asil_sec
    c.put(P(f"/events/{hw}/assessment"), json={"S": S, "E": E, "C": C, **GOOD}, headers=A)
    c.post(P(f"/events/{hw}/decision"), json={"decision": "approved"}, headers=B)
    goals = c.post(P(f"/items/{item}/derive-goals"), headers=A).json()
    return hw, goals


def test_goal_derivation_and_approval_gates(proj):
    c, item = proj
    hw, res = full_flow(c, item)
    g = res["goals"][0]
    assert g["payload"]["asil"] == "D" and g["payload"]["events"] == [hw] and res["qm_events"] == []
    assert c.post(P(f"/goals/{g['ref']}/decision"), json={"decision": "approved"}, headers=B).status_code == 422  # no safe state
    assert c.put(P(f"/goals/{g['ref']}"), json={"ftti_ms": -5}, headers=A).status_code == 422
    c.put(P(f"/goals/{g['ref']}"), json={"safe_state": "doors unlocked and lock output disabled", "ftti_ms": 200}, headers=A)
    assert c.post(P(f"/goals/{g['ref']}/decision"), json={"decision": "approved"}, headers=A).status_code == 409
    assert c.post(P(f"/goals/{g['ref']}/decision"), json={"decision": "approved"}, headers=B).json()["status"] == "approved"
    # idempotent
    again = c.post(P(f"/items/{item}/derive-goals")).json()
    assert again["created"] == [] and again["updated"] == []


def test_requirements_are_gated_until_complete(proj):
    c, item = proj
    hw, res = full_flow(c, item)
    g = res["goals"][0]["ref"]
    c.put(P(f"/goals/{g}"), json={"safe_state": "lock output disabled", "ftti_ms": 200}, headers=A)
    reqs = c.post(P(f"/goals/{g}/draft-requirements"), headers=A).json()["requirements"]
    fsr, tsr = reqs
    assert fsr["payload"]["level"] == "FSR" and "within 200 ms" in fsr["payload"]["text"] and "lock output disabled" in fsr["payload"]["text"]
    assert any(q["rule"] == "Q004" for q in tsr["payload"]["quality"])            # placeholders remain
    dec = lambda r, who: c.post(P(f"/requirements/{r}/decision"), json={"decision": "approved"}, headers=who)
    assert dec(tsr["ref"], B).status_code == 422 and "placeholder" in dec(tsr["ref"], B).json()["detail"].lower()
    assert dec(fsr["ref"], B).status_code == 422                                  # not allocated
    comp = c.get(P(f"/items/{item}/completeness")).json()
    assert not comp["complete"] and comp["traceability_completeness"] < 1
    # engineer completes both
    c.put(P(f"/requirements/{fsr['ref']}"), headers=A, json={"allocated_to": "SWC_DoorLockCtrl", "verification": ["TC-101"]})
    c.put(P(f"/requirements/{tsr['ref']}"), headers=A, json={
        "allocated_to": "SWC_DoorLockCtrl", "verification": ["TC-102", "REV-7"],
        "text": "The software component allocated to 'Central door locking' shall disable the lock output within 50 ms after the plausibility monitor detects unintended activation."})
    assert dec(fsr["ref"], A).status_code == 409                                  # four-eyes
    assert dec(fsr["ref"], B).status_code == 200 and dec(tsr["ref"], B).status_code == 200
    c.post(P(f"/goals/{g}/decision"), json={"decision": "approved"}, headers=B)
    comp = c.get(P(f"/items/{item}/completeness")).json()
    assert comp["complete"], [x for x in comp["checks"] if not x["passed"]]
    assert comp["traceability_completeness"] == 1.0


def test_change_impact_marks_downstream_suspect(proj):
    c, item = proj
    hw, res = full_flow(c, item)
    g = res["goals"][0]["ref"]
    c.put(P(f"/goals/{g}"), json={"safe_state": "lock output disabled", "ftti_ms": 200}, headers=A)
    c.post(P(f"/goals/{g}/decision"), json={"decision": "approved"}, headers=B)
    fsr, tsr = c.post(P(f"/goals/{g}/draft-requirements")).json()["requirements"]
    # re-assess the approved event with a different ASIL
    r = c.put(P(f"/events/{hw}/assessment"), headers=A, json={"S": 2, "E": 2, "C": 2, **GOOD}).json()
    assert r["payload"]["asil"] == "QM" and r["status"] == "assessed"
    st = {x["ref"]: x["status"] for x in c.get(P("/requirements")).json() + c.get(P("/goals")).json()}
    assert st[g] == "suspect" and st[fsr["ref"]] == "suspect" and st[tsr["ref"]] == "suspect"
    assert "changed D -> QM" in c.get(P("/goals")).json()[0]["payload"]["suspect_reason"]
    # once the event is approved again the goal loses its basis
    c.post(P(f"/events/{hw}/decision"), json={"decision": "approved"}, headers=B)
    res = c.post(P(f"/items/{item}/derive-goals")).json()
    assert res["orphaned"] == [g] and res["qm_events"] == [hw]
    assert c.post(P(f"/goals/{g}/decision"), json={"decision": "approved"}, headers=B).status_code in (409, 422)


def test_history_report_metrics_and_trace(proj):
    c, item = proj
    hw, res = full_flow(c, item)
    g = res["goals"][0]["ref"]
    c.put(P(f"/goals/{g}"), json={"safe_state": "lock output disabled", "ftti_ms": 200}, headers=A)
    c.post(P(f"/goals/{g}/draft-requirements"))
    hist = c.get(P(f"/records/{hw}/history")).json()
    assert [h["event"] for h in hist][:3] == ["create", "assess", "decision:approved"]
    assert {h["actor"] for h in hist} >= {"rules", "alice", "bob"}
    assert hist[1]["snapshot"]["S"] == 3
    rows = c.get(P(f"/items/{item}/trace")).json()
    assert rows[0]["goal"] == g and rows[0]["fsr"] == "FSR-001" and rows[0]["tsr"] == "TSR-001"
    md = c.get(P(f"/items/{item}/report"), params={"format": "md"}).text
    assert "## Hazardous events" in md and "ASIL" in md and "approved by bob" in md
    assert c.get(P(f"/items/{item}/report"), params={"format": "csv"}).text.startswith("goal,")
    assert c.get(P(f"/items/{item}/report"), params={"format": "json"}).json()["ruleset"] == rules.RULESET_VERSION
    m = c.get(P(f"/items/{item}/metrics")).json()
    assert m["asil_distribution"] == {"D": 1} and m["counts"]["goal"] == 1
    assert m["recommendation_acceptance"]["accepted"] >= 1 and m["recommendation_acceptance"]["rejected"] >= 1
    assert c.get(P("/graph")).text.startswith("digraph")


def test_allocation_is_checked_against_linked_hld(client, sample):
    client.post("/projects", json={"name": "hara1"})
    doc = client.post("/projects/hara1/documents", files={"file": ("hld_bcm_v1.md", sample("hld_bcm_v1.md"))}).json()
    client.post(P("/library/load"), json={})
    item = client.post(P("/items"), headers=A, json={"name": "BCM", "hld_doc_id": doc["id"],
        "functions": [{"name": "Central door locking", "guidewords": ["unintended"]}]}).json()["ref"]
    hw, res = full_flow(client, item)
    g = res["goals"][0]["ref"]
    client.put(P(f"/goals/{g}"), json={"safe_state": "lock output disabled", "ftti_ms": 200}, headers=A)
    fsr = client.post(P(f"/goals/{g}/draft-requirements")).json()["requirements"][0]
    bad = client.put(P(f"/requirements/{fsr['ref']}"), headers=A, json={"allocated_to": "SWC_Imaginary", "verification": ["T1"]}).json()
    assert bad["payload"]["allocation_check"] == "not_in_architecture"
    r = client.post(P(f"/requirements/{fsr['ref']}/decision"), json={"decision": "approved"}, headers=B)
    assert r.status_code == 422 and "not a component" in r.json()["detail"]
    ok = client.put(P(f"/requirements/{fsr['ref']}"), headers=A, json={"allocated_to": "SWC_DoorLockCtrl"}).json()
    assert ok["payload"]["allocation_check"] == "ok"
    assert client.post(P(f"/requirements/{fsr['ref']}/decision"), json={"decision": "approved"}, headers=B).status_code == 200


def test_isolation_between_projects(client):
    client.post("/projects", json={"name": "hara1"}); client.post("/projects", json={"name": "other1"})
    client.post(P("/library/load"), json={})
    item = client.post(P("/items"), json={"name": "x", "functions": [{"name": "f"}]}).json()["ref"]
    assert client.get(f"/projects/other1/hara/items/{item}/completeness").status_code == 404
    assert client.get("/projects/other1/hara/records/" + item + "/history").status_code == 404
