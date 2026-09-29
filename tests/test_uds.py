import copy
import json
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

from app.core.errors import Invalid
from app.core.parsing import parse_document
from app.modules.uds import harness_template as H
from app.modules.uds.engine import State, parse_request, respond, tick
from app.modules.uds.simulator import MUTATIONS, SimEcu
from app.modules.uds.spec import check_spec, from_document, normalize_spec
from app.modules.uds.testgen import generate

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"
A, B = {"X-User": "alice"}, {"X-User": "bob"}
U = "/projects/uds1/uds"


@pytest.fixture(scope="module")
def spec():
    return json.loads((SAMPLES / "uds_bcm_spec.json").read_text())


def R(spec, text, session=1, unlock=None, seed_level=None):
    out, st = respond(spec, State(session=session, unlocked=unlock, seed_level=seed_level), parse_request(text))
    return out


def seq(spec, texts, session=1, unlock=None):
    st, outs = State(session=session, unlocked=unlock), []
    for t in texts:
        o, st = respond(spec, st, parse_request(t))
        outs.append(o)
    return outs


# ------------------------------------------------------------------ specification model
def test_normalization_accepts_names_hex_and_ints():
    s = normalize_spec({"services": [{"sid": "0x10", "sessions": "default, extended"}, {"sid": 34, "sessions": ["extended"]}],
                        "dids": [{"did": "0xF190", "length": "17", "access": "R", "sessions": "all"}],
                        "security_levels": [{"level": 1}]})
    assert [x["id"] for x in s["sessions"]] == [1, 2, 3]
    assert s["services"][0]["sessions"] == [1, 3] and s["services"][1]["sid"] == 34
    assert s["dids"][0]["read"] and not s["dids"][0]["write"] and s["dids"][0]["sessions"] == [1, 2, 3]
    assert s["security_levels"][0]["sessions"] == [2, 3]                       # default: every non-default session
    assert [x["id"] for x in s["services"][0]["subfunctions"]] == [1, 2, 3]   # 0x10 sub-functions come from the sessions


@pytest.mark.parametrize("bad", [
    {"services": [{"sid": "0xZZ"}]}, {"services": [{"sid": 0x7F}]}, {"services": [{"sid": 0x10}, {"sid": 0x10}]},
    {"services": [{"sid": 0x22, "sessions": "warp"}]}, {"dids": [{"did": 1, "length": 0}]},
    {"dids": [{"did": 1, "length": 1, "range": [9, 3]}]}, {"security_levels": [{"level": 0}]},
    {"sessions": [{"id": 1, "name": "a"}, {"id": 1, "name": "b"}]}, {"timing": {"p2_ms": 0}}, [],
])
def test_normalization_rejects_bad_input(bad):
    with pytest.raises(Invalid):
        normalize_spec(bad)


def test_document_extraction_matches_golden(spec):
    got, warnings = from_document(parse_document(SAMPLES / "uds_bcm_spec.md"))
    assert got == spec and warnings == []
    assert got["timing"] == {"p2_ms": 50, "p2star_ms": 5000, "s3_ms": 5000} and got["ecu"] == "BCM"
    d = next(d for d in got["dids"] if d["did"] == 0x0100)
    assert d["write"] and d["security"] == 1 and d["range"] == [5, 30] and d["sessions"] == [3]
    with pytest.raises(Invalid):
        from_document(parse_document(SAMPLES / "hld_bcm_v1.md"))               # not a diagnostic spec


def test_spec_consistency_rules(spec):
    assert check_spec(spec) == []
    def rules(mut):
        s = copy.deepcopy(spec); mut(s)
        return {i["rule"]: i["severity"] for i in check_spec(s)}
    assert rules(lambda s: s["dids"].append(dict(s["dids"][0])))["U001"] == "error"
    assert rules(lambda s: s["dids"][0].update(read=False, write=False))["U002"] == "error"
    assert rules(lambda s: s["dids"][0].update(write=True, security=None))["U003"] == "warning"
    assert rules(lambda s: s["dids"][2].update(security=9))["U004"] == "error"
    assert rules(lambda s: s["dids"][0].update(range=[1, 2]))["U005"] == "warning"
    assert rules(lambda s: s["services"].append({"sid": 0x28, "name": "CommunicationControl", "sessions": [1], "security": None, "subfunctions": [], "modelled": False}))["U006"] == "warning"
    assert rules(lambda s: s.update(services=[x for x in s["services"] if x["sid"] != 0x22]))["U008"] == "error"
    assert rules(lambda s: s.update(services=[x for x in s["services"] if x["sid"] != 0x27]))["U009"] == "error"
    assert rules(lambda s: s.update(services=[x for x in s["services"] if x["sid"] != 0x10]))["U010"] == "error"
    assert rules(lambda s: s.update(services=[x for x in s["services"] if x["sid"] != 0x3E]))["U011"] == "warning"


# ------------------------------------------------------------------ oracle
def test_oracle_check_order_and_nrcs(spec):
    o = R(spec, "5A 01");                       assert (o.nrc, o.rule) == (0x11, "U-11-SERVICE")
    o = R(spec, "2E 01 00 0A", session=1);      assert o.nrc == 0x7F                      # service not in default session
    o = R(spec, "10");                          assert o.nrc == 0x13                      # missing sub-function
    o = R(spec, "10 09");                       assert o.nrc == 0x12
    o = R(spec, "10 03 00");                    assert o.nrc == 0x13
    o = R(spec, "22 F1");                       assert o.nrc == 0x13
    o = R(spec, "22 F1 FF");                    assert o.nrc == 0x31
    o = R(spec, "22 01 00", session=1);         assert (o.nrc, o.rule) == (0x31, "U-31-DID-SESSION")
    o = R(spec, "22 01 00", session=3);         assert o.nrc == 0x33
    o = R(spec, "22 01 00", session=3, unlock=1); assert o.kind == "positive" and o.tokens == ["62", "01", "00", "??"]
    o = R(spec, "22 F1 90");                    assert o.tokens[:3] == ["62", "F1", "90"] and len(o.tokens) == 3 + 17
    o = R(spec, "19 01 FF");                    assert o.kind == "positive"
    o = R(spec, "14 12 34 56", session=3);      assert o.nrc == 0x31
    o = R(spec, "14 FF FF FF", session=1);      assert o.nrc == 0x7F
    o = R(spec, "3E 00");                       assert o.tokens == ["7E", "00"]


def test_oracle_suppress_positive_response(spec):
    assert R(spec, "3E 80").kind == "suppressed" and R(spec, "10 83", session=3).kind == "suppressed"
    assert R(spec, "3E 81").nrc == 0x12                                                    # suppress bit set on an unknown sub-function
    assert R(spec, "27 81", session=3).nrc == 0x12                                         # SecurityAccess does not support the bit
    assert R(spec, "3E 80 00").kind == "negative"                                          # errors are never suppressed


def test_oracle_write_rules(spec):
    ok = lambda t: R(spec, t, session=3, unlock=1)
    assert ok("2E 01 00 0A").kind == "positive" and ok("2E 01 00 05").kind == "positive" and ok("2E 01 00 1E").kind == "positive"
    assert ok("2E 01 00 04").nrc == 0x31 and ok("2E 01 00 1F").nrc == 0x31               # range
    assert ok("2E 01 00").nrc == 0x13 and ok("2E 01 00 0A 0B").nrc == 0x13                # length
    assert ok("2E F1 90 00").nrc == 0x31                                                   # read-only DID
    assert R(spec, "2E 01 00 0A", session=3).nrc == 0x33
    assert R(spec, "2E 01 01 03 E8", session=3, unlock=1).kind == "positive"              # 1000 = max of a 2-byte range
    assert R(spec, "2E 01 01 03 E9", session=3, unlock=1).nrc == 0x31


def test_oracle_security_flow(spec):
    o = seq(spec, ["27 01", "27 02 <KEY>", "27 01"], session=3)
    assert [x.rule for x in o] == ["U-POS-27-SEED", "U-POS-27-KEY", "U-POS-27-UNLOCKED"] and o[2].tokens == ["67", "01", "00", "00"]
    assert seq(spec, ["27 02 00 00"], session=3)[0].nrc == 0x24                            # key without seed
    assert seq(spec, ["27 01", "27 02 00"], session=3)[1].nrc == 0x13                      # wrong key length
    assert seq(spec, ["27 01", "27 02 <BADKEY>"], session=3)[1].nrc == 0x35
    lock = seq(spec, ["27 01", "27 02 <BADKEY>"] * 3 + ["27 01"], session=3)
    assert [x.nrc for x in lock if x.kind == "negative"] == [0x35, 0x35, 0x36, 0x37]       # limit reached, then delay
    assert R(spec, "27 01", session=1).nrc == 0x7F
    # unlocking survives nothing that changes the session
    o = seq(spec, ["27 01", "27 02 <KEY>", "10 01", "10 03", "22 01 00"], session=3)
    assert o[-1].nrc == 0x33
    o = seq(spec, ["27 01", "27 02 <KEY>", "11 01", "10 03", "22 01 00"], session=3)
    assert o[-1].nrc == 0x33


def test_oracle_session_timing_and_reset(spec):
    st = State(session=3, unlocked=1)
    assert tick(spec, st, 4000).session == 3 and tick(spec, st, 5001).session == 1 and tick(spec, st, 5001).unlocked is None
    st2 = tick(spec, State(session=3), 3000)
    assert respond(spec, st2, parse_request("3E 00"))[1].idle_ms == 0                      # any request restarts S3
    assert seq(spec, ["11 01", "14 FF FF FF"], session=3)[1].nrc == 0x7F                   # reset returns to default
    assert seq(spec, ["10 02", "14 FF FF FF"], session=3)[1].nrc == 0x7F
    assert R(spec, "10 02", session=1).tokens == ["50", "02", "00", "32", "01", "F4"]      # P2 = 50 ms, P2* = 5000 ms / 10


def test_oracle_routines(spec):
    ok = lambda t, **k: R(spec, t, session=3, unlock=1, **k)
    assert ok("31 01 02 03 00").kind == "positive" and ok("31 03 02 03 00").kind == "positive"
    assert ok("31 01 02 03").nrc == 0x13 and ok("31 01 02 03 00 00").nrc == 0x13
    assert ok("31 02 03 01").nrc == 0x12 and ok("31 01 BE EF").nrc == 0x31 and ok("31 01 03 01").kind == "positive"
    assert R(spec, "31 01 03 01", session=3).nrc == 0x33 and R(spec, "31 01 03 01", session=1).nrc == 0x7F


# ------------------------------------------------------------------ test generation
def test_generation_is_deterministic_and_covers_all_categories(spec):
    a, b = generate(spec), generate(spec)
    assert a == b and len(a) > 100 and len({c["key"] for c in a}) == len(a)
    assert {c["category"] for c in a} == {"positive", "negative", "boundary", "session", "security", "timing"}
    for c in a:                                     # every stored expectation is exactly what the oracle says
        st = State(session=c["pre"]["session"], unlocked=c["pre"]["unlock"])
        for s in c["steps"]:
            if "wait_ms" in s:
                st = tick(spec, st, s["wait_ms"]); continue
            o, st = respond(spec, st, parse_request(s["req"]))
            assert (o.kind, o.tokens) == (s["kind"], s["expect"])


def test_generated_scenarios_include_the_important_cases(spec):
    by = {c["key"]: c for c in generate(spec)}
    assert by["DID:0100:write-below"]["expected_nrc"] == 0x31 and by["DID:0100:write-max"]["expected_kind"] == "positive"
    assert by["SEC:L1:lockout"]["steps"][-1]["nrc"] == 0x37 and by["SEC:L1:wrong-key"]["expected_nrc"] == 0x35
    assert by["SEC:L1:relock"]["steps"][-1]["nrc"] == 0x33 and by["SEC:L1:key-no-seed"]["expected_nrc"] == 0x24
    assert by["TIMING:S3:timeout"]["steps"][-1]["kind"] == "negative" and by["TIMING:S3:tester-present"]["steps"][-1]["kind"] == "positive"
    assert by["SVC:3E:spr"]["expected_kind"] == "suppressed" and by["SVC:UNSUPPORTED:sid"]["expected_nrc"] == 0x11
    assert by["RID:0203:option-long"]["expected_nrc"] == 0x13 and by["DID:F190:write-readonly"]["expected_nrc"] == 0x31


def test_simulated_correct_ecu_passes_and_every_defect_is_detected(spec):
    cases = generate(spec)
    for m in MUTATIONS:
        failed = [c["key"] for c in cases if H.run_case(SimEcu(spec, m), c, spec["timing"])]
        assert (not failed) if m == "none" else failed, m


def test_harness_matching_and_preconditions(spec):
    assert H.match(["7F", "22", "31"], bytes([0x7F, 0x22, 0x31])) and not H.match(["7F", "22", "31"], bytes([0x7F, 0x22, 0x33]))
    assert H.match(["62", "F1", "??"], bytes([0x62, 0xF1, 0x00])) and not H.match(["62", "F1", "??"], bytes([0x62, 0xF1]))
    assert H.match(["71", "01", "*"], bytes([0x71, 0x01, 1, 2, 3])) and H.match([], None) and not H.match([], b"\x7f")
    bad = {"pre": {"session": 3, "unlock": 1, "default_session": 1}, "steps": []}
    assert H.run_case(SimEcu(spec), bad, spec["timing"]) == []                            # preconditions succeed on a correct ECU
    silent = SimEcu(spec); silent.send = lambda *a, **k: None
    assert "precondition failed" in H.run_case(silent, bad, spec["timing"])[0]


# ------------------------------------------------------------------ API workflow
@pytest.fixture
def uds(client, sample):
    client.post("/projects", json={"name": "uds1"})
    doc = client.post("/projects/uds1/documents", files={"file": ("uds_bcm_spec.md", sample("uds_bcm_spec.md"))}).json()
    ref = client.post(U + "/specs/from-document", headers=A, json={"doc_id": doc["id"]}).json()["ref"]
    return client, ref, doc["id"]


def test_spec_from_document_and_approval_gates(uds, spec):
    c, ref, doc = uds
    r = c.get(U + "/specs").json()[0]
    assert r["status"] == "proposed" and r["payload"]["origin"] == "rules" and r["payload"]["source_doc"] == doc and r["payload"]["issues"] == []
    assert c.post(U + "/specs/from-document", json={"doc_id": "nope"}).status_code == 422
    bad = copy.deepcopy(spec); bad["dids"].append(dict(bad["dids"][0]))
    ref2 = c.post(U + "/specs", headers=A, json={"name": "bad", "spec": bad}).json()["ref"]
    r = c.post(f"{U}/specs/{ref2}/decision", headers=B, json={"decision": "approved"})
    assert r.status_code == 422 and "Duplicate DID" in r.json()["detail"]
    assert c.post(U + "/specs", json={"spec": {"services": [{"sid": "zz"}]}}).status_code == 422
    assert c.post(f"{U}/specs/{ref2}/decision", headers=A, json={"decision": "approved"}).status_code in (409, 422)


def test_validate_endpoint(uds):
    c, ref, _ = uds
    r = c.post(f"{U}/specs/{ref}/validate", json={"requests": ["22 F1 90", "22 01 00"], "session": "default"}).json()["results"]
    assert r[0]["kind"] == "positive" and r[1]["nrc"] == 0x31 and "not available in the default session" in r[1]["why"]
    s = c.post(f"{U}/specs/{ref}/validate", json={"requests": ["27 01", "27 02 <KEY>", "22 01 00"], "session": "extended"}).json()["results"]
    assert s[1]["state_after"] == {"session": 3, "unlocked": 1} and s[2]["kind"] == "positive"
    assert c.post(f"{U}/specs/{ref}/validate", json={"requests": ["ZZ"]}).status_code == 422
    assert c.post(f"{U}/specs/{ref}/validate", json={"requests": ["10 01"], "session": "warp"}).status_code == 422
    assert c.post(f"{U}/specs/{ref}/validate", json={"requests": ["10 01"], "unlock": 7}).status_code == 422
    assert c.post(f"{U}/specs/{ref}/validate", json={"requests": []}).status_code == 422
    assert c.get("/uds/reference").json()["nrc"]["0x33"] == "securityAccessDenied"


def test_test_review_workflow(uds):
    c, ref, _ = uds
    g = c.post(f"{U}/specs/{ref}/generate", headers=A).json()
    assert g["created"] == g["total"] > 100 and not g["spec_approved"]
    assert c.post(f"{U}/specs/{ref}/generate", headers=A).json()["unchanged"] == g["total"]     # idempotent
    tests = c.get(U + "/tests", params={"category": "security"}).json()
    assert tests and all(t["payload"]["category"] == "security" for t in tests)
    tc = tests[0]["ref"]
    assert c.post(f"{U}/tests/{tc}/decision", headers=B, json={"decision": "approved"}).status_code == 409   # spec not approved
    assert c.get(f"{U}/specs/{ref}/export", params={"format": "python"}).status_code == 409                # nothing approved
    assert c.post(f"{U}/specs/{ref}/decision", headers=B, json={"decision": "approved"}).status_code == 200
    assert c.post(f"{U}/tests/{tc}/decision", headers=B, json={"decision": "approved", "comment": "ok"}).json()["approved_by"] == "bob"
    rej = tests[1]["ref"]
    assert c.post(f"{U}/tests/{rej}/decision", headers=B, json={"decision": "rejected"}).json()["status"] == "rejected"
    assert c.get(f"{U}/records/{tc}/history").json()[-1]["event"] == "decision:approved"
    csv = c.get(f"{U}/specs/{ref}/export", params={"format": "csv"}).text
    assert tc in csv and rej not in csv                                          # only approved tests are exported by default
    assert len(c.get(f"{U}/specs/{ref}/export", params={"format": "csv", "include_unapproved": True}).text.splitlines()) > 100


def approve_all(c, ref):
    c.post(f"{U}/specs/{ref}/decision", headers=B, json={"decision": "approved"})
    for t in c.get(U + "/tests").json():
        c.post(f"{U}/tests/{t['ref']}/decision", headers=B, json={"decision": "approved"})


def test_coverage_completeness_and_metrics(uds):
    c, ref, _ = uds
    c.post(f"{U}/specs/{ref}/generate", headers=A)
    cov = c.get(f"{U}/specs/{ref}/coverage").json()
    assert cov["coverage"] == 1.0 and cov["nrc_missing"] == [] and cov["approved_only"] == 0.0
    assert {"0x33 securityAccessDenied", "0x37 requiredTimeDelayNotExpired"} <= set(cov["nrc_exercised"])
    comp = c.get(f"{U}/specs/{ref}/completeness").json()
    assert not comp["complete"] and {x["id"] for x in comp["checks"] if not x["passed"]} == {"spec_approved", "tests_decided"}
    approve_all(c, ref)
    comp = c.get(f"{U}/specs/{ref}/completeness").json()
    assert comp["complete"], [x for x in comp["checks"] if not x["passed"]]
    m = c.get(f"{U}/specs/{ref}/metrics").json()
    assert m["approved_only_coverage"] == 1.0 and m["tests"]["approved"] > 100 and m["recommendation_acceptance"]["rate"] == 1.0
    assert "| DID:0100 |" in c.get(f"{U}/specs/{ref}/report").text


def test_simulator_runs_and_detection_matrix(uds):
    c, ref, _ = uds
    c.post(f"{U}/specs/{ref}/generate", headers=A)
    assert c.post(f"{U}/specs/{ref}/run", json={"only_approved": True}).status_code == 409      # nothing approved yet
    ok = c.post(f"{U}/specs/{ref}/run", json={"mutation": "none"}).json()
    assert ok["failed"] == 0 and ok["passed"] == ok["total"] and ok["defect_detected"] is None
    bad = c.post(f"{U}/specs/{ref}/run", json={"mutation": "no_security"}).json()
    assert bad["defect_detected"] and any("DID:0100" in r["element"] for r in bad["results"] if not r["passed"])
    assert "expected 7F 22 33" in next(f for r in bad["results"] for f in r["failures"])
    assert c.post(f"{U}/specs/{ref}/run", json={"mutation": "nope"}).status_code == 422
    dm = c.post(f"{U}/specs/{ref}/detection-matrix").json()
    assert dm["none"]["failed"] == 0 and all(v["detected"] for k, v in dm.items() if k != "none")


def test_spec_change_marks_tests_suspect(uds, spec):
    c, ref, _ = uds
    c.post(f"{U}/specs/{ref}/generate", headers=A)
    approve_all(c, ref)
    new = copy.deepcopy(spec)
    new["dids"][2]["range"] = [5, 40]                                   # widen a range
    new["dids"] = [d for d in new["dids"] if d["did"] != 0x0200]        # remove a DID
    r = c.put(f"{U}/specs/{ref}", headers=A, json={"spec": new}).json()
    assert r["status"] == "suspect" and r["rev"] > 1
    g = c.post(f"{U}/specs/{ref}/generate", headers=A).json()
    assert g["changed"] and g["orphaned"] and g["created"] == 0
    st = {t["ref"]: t for t in c.get(U + "/tests").json()}
    assert all(st[x]["status"] == "suspect" for x in g["changed"] + g["orphaned"])
    assert "changed with specification" in st[g["changed"][0]]["payload"]["changed_reason"]
    assert all(st[x]["payload"]["orphan"] for x in g["orphaned"])
    assert c.post(f"{U}/tests/{g['orphaned'][0]}/decision", headers=B, json={"decision": "approved"}).status_code in (409,)
    assert c.get(f"{U}/specs/{ref}/completeness").json()["complete"] is False


def test_exported_bundle_runs_under_pytest(uds, spec, tmp_path):
    c, ref, _ = uds
    c.post(f"{U}/specs/{ref}/generate", headers=A)
    approve_all(c, ref)
    z = c.get(f"{U}/specs/{ref}/export", params={"format": "bundle"})
    assert z.headers["content-type"] == "application/zip"
    (tmp_path / "b.zip").write_bytes(z.content)
    zipfile.ZipFile(tmp_path / "b.zip").extractall(tmp_path)
    names = {p.name for p in tmp_path.iterdir()}
    assert {"test_uds_generated.py", "uds_harness.py", "conftest.py", "uds_tests.can", "uds_tests.csv", "README.txt"} <= names
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    for mutation, expect_fail in (("none", False), ("no_security", True), ("accept_any_key", True)):
        (tmp_path / "conftest.py").write_text(textwrap.dedent(f'''
            import json, sys, pytest
            sys.path.insert(0, {str(ROOT)!r})
            from app.modules.uds.simulator import SimEcu
            @pytest.fixture
            def ecu():
                return SimEcu(json.load(open({str(tmp_path / "spec.json")!r})), {mutation!r})
            '''))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path / "test_uds_generated.py")],
                           capture_output=True, text=True, cwd=tmp_path)
        assert (r.returncode != 0) == expect_fail, (mutation, r.stdout[-800:])
        if not expect_fail:
            assert " passed" in r.stdout and "failed" not in r.stdout
    capl = (tmp_path / "uds_tests.can").read_text()
    assert "EXPERIMENTAL" in capl and "testcase TC_001()" in capl and "computeKey(" in capl and capl.count("testcase ") >= 100
    assert capl.count("{") == capl.count("}")
    py = c.get(f"{U}/specs/{ref}/export", params={"format": "python"}).text
    compile(py, "generated", "exec")


def test_link_tests_to_safety_and_security_requirements(uds):
    c, ref, _ = uds
    H_ = "/projects/uds1/hara"
    good = {"rationale_S": "Occupant risk at speed", "rationale_E": "Driving is frequent", "rationale_C": "Driver cannot react"}
    c.post(H_ + "/library/load", json={})
    item = c.post(H_ + "/items", json={"name": "BCM", "functions": [{"name": "Central door locking", "guidewords": ["unintended"]}]}).json()["ref"]
    c.post(f"{H_}/items/{item}/suggest-hazards"); c.post(H_ + "/hazards/H-001/decision", json={"decision": "accepted"})
    events = c.post(H_ + "/hazards/H-001/propose-events").json()["events"]
    ev = events[0]["ref"]
    for e in events[1:]:
        c.post(f"{H_}/events/{e['ref']}/decision", headers=B, json={"decision": "rejected"})
    c.put(f"{H_}/events/{ev}/assessment", headers=A, json={"S": 3, "E": 4, "C": 3, **good})
    c.post(f"{H_}/events/{ev}/decision", headers=B, json={"decision": "approved"})
    goal = c.post(f"{H_}/items/{item}/derive-goals").json()["goals"][0]["ref"]
    fsr = c.post(f"{H_}/goals/{goal}/draft-requirements").json()["requirements"][0]["ref"]
    c.post(f"{U}/specs/{ref}/generate", headers=A)
    approve_all(c, ref)
    test = next(t["ref"] for t in c.get(U + "/tests").json() if t["payload"]["element"] == "DID:0100" and t["payload"]["key"].endswith("write-locked"))
    r = c.post(f"{U}/tests/{test}/link", headers=A, json={"requirement": fsr}).json()
    assert test in r["payload"]["verification"] and r["status"] == "assessed"
    assert c.post(f"{U}/tests/{test}/link", headers=A, json={"requirement": fsr}).json()["payload"]["verification"].count(test) == 1
    assert c.post(f"{U}/tests/{test}/link", json={"requirement": ev}).status_code == 422       # not a requirement
    assert c.post(f"{U}/tests/{test}/link", json={"requirement": "FSR-999"}).status_code == 422
    # traceability spans modules: hazardous event -> goal -> FSR -> test evidence
    tr = c.app.state.container.hara.trace
    assert test in tr.descendants("uds1", ev)
    # ...so re-assessing the event flags the test evidence for re-review
    c.put(f"{H_}/events/{ev}/assessment", headers=A, json={"S": 2, "E": 2, "C": 2, **good})
    assert c.get(U + "/tests", params={"status": "suspect"}).json()[0]["ref"] == test or any(t["ref"] == test for t in c.get(U + "/tests", params={"status": "suspect"}).json())


def test_ask_and_isolation(uds, spec):
    c, ref, _ = uds
    r = c.post(f"{U}/specs/{ref}/ask", json={"question": "Which sessions allow ReadDataByIdentifier?"})
    assert r.status_code == 200 and "answer" in r.json()
    manual = c.post(U + "/specs", json={"name": "m", "spec": spec}).json()["ref"]
    assert c.post(f"{U}/specs/{manual}/ask", json={"question": "anything"}).status_code == 409
    c.post("/projects", json={"name": "other1"})
    assert c.get(f"/projects/other1/uds/specs/{ref}/coverage").status_code == 404
    assert c.get(f"/projects/other1/uds/records/{ref}/history").status_code == 404
