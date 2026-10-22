"""Phase 2: SBOM vulnerability monitoring, OTA package verification, R155/R156 evidence."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.errors import Conflict, Invalid
from app.modules.compliance.r155 import categories_for_threat
from app.modules.ota import package as pkg
from app.modules.ota import selftest
from app.modules.ota.checks import Context, verify
from app.modules.vuln import cvss
from app.modules.vuln.feed import parse_feed
from app.modules.vuln.sbom import parse_cyclonedx, parse_purl
from app.modules.vuln.service import match_components
from app.modules.vuln.versions import compare, in_ranges

ROOT = Path(__file__).resolve().parent.parent
SB = ROOT / "data" / "samples" / "sbom"
OTA = ROOT / "data" / "samples" / "ota"
FEED = ROOT / "data" / "library" / "vuln_feed_demo.json"


# ------------------------------------------------------------------ versions and CVSS
@pytest.mark.parametrize("a,b,want", [("1.0.1f", "1.0.1g", -1), ("1.0.1", "1.0.1a", -1), ("1.0.1g", "1.0.2", -1),
                                      ("3.0.0-alpha1", "3.0.0", -1), ("v2.4", "2.4.0", 0), ("1.2.13", "1.2.12", 1),
                                      ("10.0", "9.9.9", 1), ("2.4.1", "2.4", 1)])
def test_version_ordering(a, b, want):
    assert compare(a, b) == want


def test_osv_range_semantics():
    heart = [{"type": "ECOSYSTEM", "events": [{"introduced": "1.0.1"}, {"fixed": "1.0.1g"}]}]
    assert [in_ranges(v, heart) for v in ("1.0.0", "1.0.1", "1.0.1f", "1.0.1g")] == [False, True, True, False]
    assert in_ranges("5.0", [{"events": [{"introduced": "0"}]}])                         # open ended
    assert in_ranges("1.4", [{"events": [{"introduced": "1.0"}, {"last_affected": "1.4"}]}])
    assert not in_ranges("1.5", [{"events": [{"introduced": "1.0"}, {"last_affected": "1.4"}]}])
    assert in_ranges("2.2", [], versions=["2.1", "2.2"])
    assert not in_ranges("1.0", [{"type": "GIT", "events": [{"introduced": "0"}]}])       # commit ranges are not guessed
    two = [{"events": [{"introduced": "1.0"}, {"fixed": "1.2"}, {"introduced": "2.0"}, {"fixed": "2.3"}]}]
    assert [in_ranges(v, two) for v in ("1.1", "1.5", "2.1", "2.3")] == [True, False, True, False]


@pytest.mark.parametrize("vector,score", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8), ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H", 7.5),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1), ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H", 7.8),
    ("CVSS:3.1/AV:N/AC:L/PR:H/UI:N/S:C/C:H/I:H/A:H", 9.1), ("CVSS:3.1/AV:P/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0)])
def test_cvss_base_scores(vector, score):
    assert cvss.base_score(vector) == score


def test_cvss_rejects_bad_vectors_and_bands():
    for bad in ("", "CVSS:2.0/AV:N", "CVSS:3.1/AV:N/AC:L", "CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"):
        with pytest.raises(cvss.CVSSError):
            cvss.base_score(bad)
    assert [cvss.band(x) for x in (None, 0, 3.9, 4.0, 6.9, 7.0, 8.9, 9.0)] == \
        ["unknown", "none", "low", "medium", "medium", "high", "high", "critical"]


# ------------------------------------------------------------------ SBOM and feed parsing
def test_parse_cyclonedx_demo_and_purl():
    s = parse_cyclonedx((SB / "sbom_gateway_2.3.0.cdx.json").read_bytes())
    assert (s["ecu"], s["ecu_version"], s["hardware_id"], s["rxswin"]) == ("Gateway", "2.3.0", "PGW-HW-B", "RXSWIN-GW-0023")
    assert s["component_count"] == 7 and not s["warnings"]
    assert parse_purl("pkg:generic/acme/acme-secoc@1.2.0?arch=arm#sub") == \
        {"type": "generic", "namespace": "acme", "name": "acme-secoc", "version": "1.2.0"}
    assert parse_purl("not-a-purl") is None


@pytest.mark.parametrize("doc,msg", [
    (b"{not json", "not valid JSON"), (b'{"bomFormat": "SPDX"}', "Not a CycloneDX"),
    (b'{"bomFormat": "CycloneDX", "specVersion": "0.9"}', "specVersion"),
    (b'{"bomFormat": "CycloneDX", "specVersion": "1.5", "metadata": {}}', "metadata.component"),
    (b'{"bomFormat": "CycloneDX", "specVersion": "1.5", "metadata": {"component": {"name": "X", "version": "1"}}, "components": [{"version": "1"}]}', "no name"),
    (b"", "Empty")])
def test_sbom_validation(doc, msg):
    with pytest.raises(Invalid, match=msg):
        parse_cyclonedx(doc)


def test_sbom_nested_components_and_limits(monkeypatch):
    doc = {"bomFormat": "CycloneDX", "specVersion": "1.6", "metadata": {"component": {"name": "ECU", "version": "1"}},
           "components": [{"name": "a", "version": "1", "components": [{"name": "b", "version": "2"}]}, {"name": "c"}]}
    s = parse_cyclonedx(json.dumps(doc).encode())
    assert [c["name"] for c in s["components"]] == ["a", "b", "c"] and any("no version" in w for w in s["warnings"])
    import app.modules.vuln.sbom as sbom_mod
    monkeypatch.setattr(sbom_mod, "MAX_COMPONENTS", 2)
    with pytest.raises(Invalid, match="more than 2"):
        parse_cyclonedx(json.dumps(doc).encode())


def test_feed_formats_and_severity_sources():
    meta, advs = parse_feed(FEED.read_bytes(), FEED.name)
    assert len(advs) == 7 and meta["version"] == "2026.10"
    bands = {a["id"]: (a["severity"]["band"], a["severity"]["score"]) for a in advs}
    assert bands["CVE-2022-37434"] == ("critical", 9.8) and bands["CVE-2014-0160"] == ("high", 7.5)
    one = {"id": "X-1", "affected": [{"package": {"name": "foo"}, "ranges": []}], "database_specific": {"severity": "MODERATE"}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("X-1.json", json.dumps(one))
    _, z = parse_feed(buf.getvalue(), "osv.zip")
    assert z[0]["severity"] == {"score": None, "band": "medium", "vector": "", "source": "database_specific"}
    _, u = parse_feed(json.dumps({**one, "database_specific": {}}).encode())
    assert u[0]["severity"]["band"] == "unknown"
    with pytest.raises(Invalid, match="needs an 'id'"):
        parse_feed(b'[{"affected": []}]')
    with pytest.raises(Invalid, match="no affected package"):
        parse_feed(b'[{"id": "X-2", "affected": []}]')


def test_matching_ground_truth_and_false_positive_guards():
    _, advs = parse_feed(FEED.read_bytes(), FEED.name)
    got = set()
    for f in sorted(SB.glob("*.json")):
        s = parse_cyclonedx(f.read_bytes())
        got |= {(s["ecu"], s["ecu_version"], c["name"], a["id"]) for c, a in match_components(s["components"], advs)}
    assert got == {("Gateway", "2.3.0", "zlib", "CVE-2022-37434"), ("Gateway", "2.3.0", "openssl", "CVE-2022-3602"),
                   ("Gateway", "2.3.0", "curl", "CVE-2023-38545"), ("Gateway", "2.3.0", "acme-secoc", "DEMO-2026-0001"),
                   ("Telematics", "5.1.0", "openssl", "CVE-2014-0160"), ("BCM", "1.0.0", "acme-autosar-com", "DEMO-2026-0002"),
                   ("BCM", "1.0.0", "acme-bootloader", "DEMO-2026-0003")}       # curl 7.68.0 and zlib 1.2.13 correctly NOT matched
    npm_zlib = [{"name": "zlib", "version": "1.0.5", "purl": "pkg:npm/zlib@1.0.5", "bom_ref": "x"}]
    assert match_components(npm_zlib, advs) == []                                  # same name, different product


# ------------------------------------------------------------------ helpers for service tests
def _tara(c, project):
    c.tara.load_library(project, "seed")
    arch = json.loads((ROOT / "data" / "samples" / "tara_system_bcm.json").read_text())
    s = c.tara.create_system(project, "BCM system", "", arch, "seed")
    c.tara.analyze(project, s["ref"], "seed")
    return s["ref"]


def _baseline(c, project):
    c.vuln.load_feed(project, "asha")
    for f in ("sbom_gateway_2.3.0.cdx.json", "sbom_telematics_5.1.0.cdx.json", "sbom_bcm_1.0.0.cdx.json"):
        c.vuln.add_sbom(project, f, (SB / f).read_bytes(), "ravi")


def _finding(c, project, advisory):
    return next(f for f in c.vuln.findings(project) if f["payload"]["advisory"] == advisory)


# ------------------------------------------------------------------ vulnerability workflow
def test_scan_triage_and_tara_link(container):
    c, P = container, "p"
    c.db.create_project(P)
    _tara(c, P)
    _baseline(c, P)
    assert len(c.vuln.findings(P)) == 7
    again = c.vuln.add_sbom(P, "dup.json", (SB / "sbom_gateway_2.3.0.cdx.json").read_bytes(), "ravi")
    assert again["created"] is False and again["scan"]["new"] == 0                  # idempotent
    gw_threats = [t for t in c.db.rec_list(P, "tara", "threat") if t["payload"]["component"] == "Gateway"]
    t = gw_threats[0]
    c.tara.assess_threat(P, t["ref"], t["payload"]["suggested"]["impact"], t["payload"]["suggested"]["ap"],
                         "Assessed for the test case", None, "ravi")
    c.tara.decide_threat(P, t["ref"], "approved", "ok", "asha")
    z = _finding(c, P, "CVE-2022-37434")
    out = c.vuln.triage(P, z["ref"], "exploitable", "ravi", response=["update"], detail="update client inflates gzip headers")
    assert out["status"] == "approved" and t["ref"] in out["tara_suspect"]
    assert c.db.rec_get(P, t["ref"])["status"] == "suspect"
    untouched = gw_threats[1]
    assert c.db.rec_get(P, untouched["ref"])["status"] == "proposed"                 # unassessed threats are linked, not flagged
    assert untouched["ref"] in c.vuln.trace.children(P, z["ref"], "raises_feasibility_of")
    with pytest.raises(Conflict):
        c.vuln.triage(P, z["ref"], "false_positive", "ravi", detail="changed my mind")


def test_dismissing_high_findings_needs_two_people(container):
    c, P = container, "p"
    c.db.create_project(P)
    _baseline(c, P)
    hb = _finding(c, P, "CVE-2014-0160")
    with pytest.raises(Invalid, match="justification"):
        c.vuln.triage(P, hb["ref"], "not_affected", "ravi", detail="heartbeat extension compiled out")
    with pytest.raises(Invalid, match="20 characters"):
        c.vuln.triage(P, hb["ref"], "not_affected", "ravi", justification="code_not_present", detail="short")
    with pytest.raises(Invalid, match="Unknown response"):
        c.vuln.triage(P, hb["ref"], "exploitable", "ravi", response=["panic"])
    r = c.vuln.triage(P, hb["ref"], "not_affected", "ravi", justification="code_not_present",
                      detail="Built with OPENSSL_NO_HEARTBEATS; heartbeat code is not in the binary")
    assert r["status"] == "assessed" and r["payload"]["pending"] == "not_affected"
    with pytest.raises(Conflict, match="Four-eyes"):
        c.vuln.approve_pending(P, hb["ref"], "approved", "", "ravi")
    assert c.vuln.approve_pending(P, hb["ref"], "approved", "checked build flags", "asha")["status"] == "rejected"
    med = _finding(c, P, "DEMO-2026-0001")                                            # medium: one person is enough
    assert c.vuln.triage(P, med["ref"], "false_positive", "ravi", detail="SecOC is disabled on this ECU variant")["status"] == "rejected"
    with pytest.raises(Invalid):
        c.vuln.reopen(P, med["ref"], "short", "ravi")
    assert c.vuln.reopen(P, med["ref"], "SecOC is enabled after all in variant B", "asha")["status"] == "proposed"
    vex = c.vuln.vex(P)
    states = {v["id"]: v["analysis"] for v in vex["vulnerabilities"]}
    assert states["CVE-2014-0160"] == {"state": "not_affected", "justification": "code_not_present",
                                       "detail": "Built with OPENSSL_NO_HEARTBEATS; heartbeat code is not in the binary"}
    assert states["DEMO-2026-0001"]["state"] == "in_triage"
    ref = next(v for v in vex["vulnerabilities"] if v["id"] == "CVE-2014-0160")["affects"][0]["ref"]
    assert ref.startswith("urn:cdx:3f1a2b4c-0000-4000-8000-000000000510/1#openssl@")    # BOM-Link into the ECU SBOM
    m = c.vuln.metrics(P)
    assert m["findings"] == 7 and m["not_affected_or_fp"] == 1 and m["mean_hours_to_triage"] is not None


def test_newer_sbom_resolves_and_vulnerable_version_reintroduces(container):
    c, P = container, "p"
    c.db.create_project(P)
    _baseline(c, P)
    r = c.vuln.add_sbom(P, "gw24.json", (SB / "sbom_gateway_2.4.0.cdx.json").read_bytes(), "ravi")
    assert r["scan"]["resolved"] == 4
    assert {e["ecu"]: e["open_findings"] for e in c.vuln.ecus(P)}["Gateway"] == 0
    doc = json.loads((SB / "sbom_gateway_2.4.0.cdx.json").read_text())
    doc["metadata"]["component"]["version"] = "2.5.0"
    doc["components"][0]["version"] = "1.2.11"                                        # zlib regression
    doc["components"][0]["purl"] = "pkg:generic/zlib@1.2.11"
    r = c.vuln.add_sbom(P, "gw25.json", json.dumps(doc).encode(), "ravi")
    z = _finding(c, P, "CVE-2022-37434")
    assert z["status"] == "proposed" and r["scan"]["updated"] == 1


# ------------------------------------------------------------------ OTA packages
def _key_and_ctx(now=None, **key_over):
    now = now or datetime.now(timezone.utc)
    k = pkg.generate_key()
    entry = {"public_key_pem": pkg.public_pem(k), "status": "approved", "valid_from": "", "valid_until": ""}
    entry.update(key_over)
    ctx = Context(keys={"k1": entry}, baseline={"Gateway": {"version": "2.3.0", "hardware_id": "PGW-HW-B", "rxswin": "RX-1"}},
                  now=now)
    return k, ctx


def _manifest(**over):
    m = {"schema": pkg.SCHEMA, "package_id": "T-1", "target": {"ecu": "Gateway", "hardware_id": "PGW-HW-B"},
         "from_versions": ["2.3.0"], "to_version": "2.4.0", "type_approval_relevant": True, "rxswin": "RX-2",
         "created": "2026-01-01T00:00:00Z", "expires": "2099-01-01T00:00:00Z",
         "preconditions": {"vehicle_state": "parked", "min_battery_soc_pct": 30}}
    m.update(over)
    return m


def test_selftest_detects_every_attack():
    r = selftest.run()
    assert r["genuine_passes"] and r["attacks"] == 17 and r["detected"] == 17 and r["all_ok"]


@pytest.mark.parametrize("entries,msg", [
    ({"../evil": b"x"}, "Unsafe path"), ({"/abs": b"x"}, "Unsafe path"), ({"a\\b": b"x"}, "Unsafe path"),
    ({f"f{i}": b"x" for i in range(70)}, "Too many files"), ({"bomb.bin": b"\0" * 2_000_000}, "compression ratio")])
def test_unsafe_archives_fail_structure_check(entries, msg):
    k, ctx = _key_and_ctx()
    good = pkg.read_package(pkg.build(_manifest(), {"fw.bin": os.urandom(1000)}, k, "k1"))
    r = verify(pkg.zip_entries({**good, **entries}), ctx)
    assert r["verdict"] == "fail" and r["checks"][0]["status"] == "fail" and msg in r["checks"][0]["detail"]
    assert all(c["status"] == "skip" for c in r["checks"][1:])


def test_symlink_entry_rejected():
    k, ctx = _key_and_ctx()
    files = pkg.read_package(pkg.build(_manifest(), {"fw.bin": os.urandom(100)}, k, "k1"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, d in files.items():
            zf.writestr(n, d)
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")
    assert "Symbolic link" in verify(buf.getvalue(), ctx)["checks"][0]["detail"]


def test_manifest_schema_errors_and_key_types():
    errs = pkg.validate_manifest({**_manifest(), "type_approval_relevant": True, "rxswin": "", "payload": [{"path": "x"}]})
    assert any("rxswin" in e for e in errs) and any("payload[0]" in e for e in errs)
    with pytest.raises(pkg.PackageError, match="P-256"):
        pkg.load_public(pkg.public_pem(ec.generate_private_key(ec.SECP384R1())))
    with pytest.raises(pkg.PackageError):
        pkg.load_public("-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----")


def test_key_validity_window_and_missing_preconditions_warn():
    now = datetime.now(timezone.utc)
    k, ctx = _key_and_ctx(now, valid_from=(now + timedelta(days=1)).isoformat())
    r = verify(pkg.build(_manifest(), {"fw.bin": os.urandom(500)}, k, "k1"), ctx)
    assert "not valid before" in next(c for c in r["checks"] if c["id"] == "P03")["detail"]
    k, ctx = _key_and_ctx(now)
    m = _manifest()
    del m["preconditions"]
    r = verify(pkg.build(m, {"fw.bin": os.urandom(500)}, k, "k1"), ctx)
    assert r["verdict"] == "warn" and next(c for c in r["checks"] if c["id"] == "P10")["status"] == "warn"


def _ota_setup(c, P):
    c.db.create_project(P)
    _baseline(c, P)
    key = pkg.generate_key()
    k = c.ota.add_key(P, "oem-ota-2026", "OEM release HSM", pkg.public_pem(key), "ravi", valid_until="2099-01-01T00:00:00Z")
    c.ota.decide_key(P, k["ref"], "approved", "fingerprint checked", "asha")
    return key, k


def _gw_package(key, **over):
    m = {**_manifest(package_id="PGW-2.4.0-R1", rxswin="RXSWIN-GW-0024"), "affects": ["Gateway"], **over}
    return pkg.build(m, {"firmware/pgw.bin": os.urandom(4000)}, key, "oem-ota-2026",
                     sbom=(SB / "sbom_gateway_2.4.0.cdx.json").read_bytes())


def test_key_registry_rules(container):
    c, P = container, "p"
    c.db.create_project(P)
    key = pkg.generate_key()
    with pytest.raises(Invalid):
        c.ota.add_key(P, "k", "OEM", pkg.public_pem(key), "ravi")                     # key id too short
    with pytest.raises(Invalid):
        c.ota.add_key(P, "oem-1", "OEM", "garbage", "ravi")
    k = c.ota.add_key(P, "oem-1", "OEM HSM", pkg.public_pem(key), "ravi")
    with pytest.raises(Conflict):
        c.ota.add_key(P, "oem-2", "OEM HSM", pkg.public_pem(key), "ravi")               # same public key twice
    with pytest.raises(Conflict, match="Four-eyes"):
        c.ota.decide_key(P, k["ref"], "approved", "", "ravi")
    assert c.ota.decide_key(P, k["ref"], "approved", "ok", "asha")["status"] == "approved"
    with pytest.raises(Invalid):
        c.ota.revoke_key(P, k["ref"], "short", "asha")


def test_demo_packages_and_release_flow(container):
    c, P = container, "p"
    c.db.create_project(P)
    _baseline(c, P)
    k = c.ota.add_key(P, "oem-ota-2026", "OEM HSM", (OTA / "ota_demo_key.public.pem").read_text(), "ravi")
    c.ota.decide_key(P, k["ref"], "approved", "ok", "asha")
    res = {}
    for f in ("pkg_gateway_2.4.0_signed.zip", "pkg_gateway_2.4.0_tampered.zip", "pkg_gateway_2.2.0_rollback.zip",
              "pkg_bcm_1.1.0_untrusted_key.zip"):
        r = c.ota.add_package(P, f, (OTA / f).read_bytes(), "ravi")
        res[f] = (r["ref"], r["payload"]["verdict"], r["payload"]["failed"])
    assert res["pkg_gateway_2.4.0_signed.zip"][1:] == ("pass", [])
    assert res["pkg_gateway_2.4.0_tampered.zip"][2] == ["P04"]
    assert res["pkg_gateway_2.2.0_rollback.zip"][2] == ["P07"]
    assert res["pkg_bcm_1.1.0_untrusted_key.zip"][2] == ["P03"]
    good, bad = res["pkg_gateway_2.4.0_signed.zip"][0], res["pkg_gateway_2.4.0_tampered.zip"][0]
    with pytest.raises(Conflict, match="cannot be released"):
        c.ota.decide_package(P, bad, "approved", "trying anyway", "asha")
    with pytest.raises(Conflict, match="Four-eyes"):
        c.ota.decide_package(P, good, "approved", "my own upload", "ravi")
    rel = c.ota.decide_package(P, good, "approved", "security release", "asha")
    assert rel["status"] == "approved" and rel["payload"]["vulnerabilities_resolved"] == 4
    assert {e["ecu"]: e["ecu_version"] for e in c.vuln.ecus(P)}["Gateway"] == "2.4.0"
    md, _ = c.ota.report(P, good, "md")
    assert "Verdict: PASS" in md and "released" in md and "uploaded by ravi" in md
    again = c.ota.add_package(P, "copy.zip", _gw_package(pkg.generate_key()), "ravi")    # other key, same id, after release
    assert {"P03", "P08"} <= set(again["payload"]["failed"])


def test_revocation_reverifies_unreleased_packages(container):
    c, P = container, "p"
    key, k = _ota_setup(c, P)
    r = c.ota.add_package(P, "gw.zip", _gw_package(key), "ravi")
    assert r["payload"]["verdict"] == "pass"
    out = c.ota.revoke_key(P, k["ref"], "Private key exposed in a CI log", "asha")
    assert r["ref"] in out["reverified"]
    after = c.ota.package(P, r["ref"])
    assert after["payload"]["failed"] == ["P03"] and "revoked" in after["payload"]["checks"][2]["detail"]


def test_safety_impact_requires_comment_and_flags_tests(container):
    c, P = container, "p"
    key, _ = _ota_setup(c, P)
    goal, _ = c.db.rec_create(P, "hara", "goal", "SG", {"title": "No unintended unlock", "asil": "C"}, "approved", "x")
    fsr, _ = c.db.rec_create(P, "hara", "fsr", "FSR", {"text": "Lock state shall ...", "asil": "C"}, "approved", "x")
    test, _ = c.db.rec_create(P, "uds", "test", "TC", {"title": "Read lock DID"}, "approved", "x")
    c.db.link_add(P, goal["ref"], fsr["ref"], "refined_by", "x")
    c.db.link_add(P, fsr["ref"], "COMP:Gateway", "allocated_to", "x")
    c.db.link_add(P, fsr["ref"], f"TEST:{test['ref']}", "verified_by", "x")
    r = c.ota.add_package(P, "gw.zip", _gw_package(key), "ravi")
    p12 = next(x for x in r["payload"]["checks"] if x["id"] == "P12")
    assert p12["status"] == "warn" and "ASIL C" in p12["detail"]
    assert r["payload"]["impact"]["safety_and_cyber_goals"] == [goal["ref"]] and r["payload"]["impact"]["tests"] == [f"TEST:{test['ref']}"]
    with pytest.raises(Invalid, match="warnings"):
        c.ota.decide_package(P, r["ref"], "approved", "ok", "asha")
    rel = c.ota.decide_package(P, r["ref"], "approved", "Safety team re-validated the lock function on HIL", "asha")
    assert rel["payload"]["reverify_needed"] == [test["ref"]]
    assert c.db.rec_get(P, test["ref"])["status"] == "suspect"


# ------------------------------------------------------------------ compliance
def test_r155_mapping_rules():
    assert categories_for_threat({"interface": "OTA", "asset_kind": "ota_update_package", "name": "Malicious update"}) == ["R155-3"]
    assert set(categories_for_threat({"interface": "OBD", "asset_kind": "firmware", "name": "Reflash via diagnostics"})) == {"R155-5", "R155-6"}
    assert categories_for_threat({"interface": "CAN", "asset_kind": "message", "name": "Spoofed message", "stride": "spoofing"}) == ["R155-2"]
    assert "R155-1" in categories_for_threat({"interface": "Backend", "asset_kind": "message", "name": "x"})


def test_evidence_pack(container):
    c, P = container, "p"
    c.db.create_project(P)
    cov = c.compliance.r155(P)
    assert {x["id"]: x["status"] for x in cov["categories"]}["R155-7"] == "gap"           # nothing monitored yet
    _tara(c, P)
    _ota_setup_after_project(c, P)
    cov = c.compliance.r155(P)
    st = {x["id"]: x["status"] for x in cov["categories"]}
    assert st["R155-7"] == "monitored" and st["R155-4"] == "organisational" and st["R155-3"] in ("identified", "in_progress")
    ev = c.compliance.evidence(P, "asha")
    md, mime = c.compliance.render(ev, "md")
    assert mime == "text/markdown" and "## 1. Coverage by UN R155 Annex 5 threat group" in md and "17/17 attacks detected" in md
    assert "not a compliance statement" in md and any(a.startswith("Triage VF-") for a in ev["actions"])
    assert json.loads(c.compliance.render(ev, "json")[0])["software_updates"]["self_test"]["all_ok"]


def _ota_setup_after_project(c, P):
    _baseline(c, P)
    key = pkg.generate_key()
    k = c.ota.add_key(P, "oem-ota-2026", "OEM release HSM", pkg.public_pem(key), "ravi")
    c.ota.decide_key(P, k["ref"], "approved", "ok", "asha")


# ------------------------------------------------------------------ API and CLI
def test_api_end_to_end(client):
    H = lambda u: {"X-User": u}  # noqa: E731
    client.post("/projects", json={"name": "api"})
    assert client.post("/projects/api/vuln/library/load", headers=H("asha")).status_code == 201
    r = client.post("/projects/api/vuln/sboms", headers=H("ravi"),
                    files={"file": ("gw.json", (SB / "sbom_gateway_2.3.0.cdx.json").read_bytes())})
    assert r.status_code == 201 and r.json()["scan"]["new"] == 4
    assert client.post("/projects/api/vuln/sboms", files={"file": ("x.json", b"{}")}).status_code == 422
    v = client.get("/projects/api/vuln/vex?ecu=Gateway")
    assert v.headers["content-type"].startswith("application/vnd.cyclonedx+json") and len(v.json()["vulnerabilities"]) == 4
    k = client.post("/projects/api/ota/keys", headers=H("ravi"), json={"key_id": "oem-ota-2026", "owner": "OEM",
                                                                       "public_key_pem": (OTA / "ota_demo_key.public.pem").read_text()}).json()
    assert client.post(f"/projects/api/ota/keys/{k['ref']}/decision", headers=H("ravi"), json={"decision": "approved"}).status_code == 409
    assert client.post(f"/projects/api/ota/keys/{k['ref']}/decision", headers=H("asha"), json={"decision": "approved"}).status_code == 200
    p = client.post("/projects/api/ota/packages", headers=H("ravi"),
                    files={"file": ("t.zip", (OTA / "pkg_gateway_2.4.0_tampered.zip").read_bytes())}).json()
    assert p["payload"]["failed"] == ["P04"]
    assert client.post(f"/projects/api/ota/packages/{p['ref']}/decision", headers=H("asha"),
                       json={"decision": "approved", "comment": "x"}).status_code == 409
    assert client.get(f"/projects/api/ota/packages/{p['ref']}/report?format=md").text.startswith("# Update readiness report")
    assert client.get("/projects/api/ota/self-test").json()["all_ok"]
    assert client.get("/projects/api/compliance/evidence?format=md").status_code == 200
    assert client.get("/projects/api/compliance/evidence?format=pdf").status_code == 422


def test_ota_tool_cli_roundtrip(tmp_path):
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts" / "ota_tool.py"), *a], capture_output=True, text=True)  # noqa: E731
    assert run("keygen", "--key-id", "cli-key", "--out", str(tmp_path)).returncode == 0
    assert oct((tmp_path / "cli-key.private.pem").stat().st_mode & 0o777) == "0o600"
    man = tmp_path / "m.json"
    man.write_text(json.dumps(_manifest(package_id="CLI-1")))
    fw = tmp_path / "app.bin"
    fw.write_bytes(os.urandom(2000))
    out = tmp_path / "pkg.zip"
    r = run("build", "--manifest", str(man), "--key", str(tmp_path / "cli-key.private.pem"), "--key-id", "cli-key",
            "--payload", str(fw), "--out", str(out))
    assert r.returncode == 0, r.stderr
    ok = run("inspect", str(out), "--pubkey", str(tmp_path / "cli-key.public.pem"))
    assert ok.returncode == 0 and "signature: VALID" in ok.stdout
    other = pkg.generate_key()
    (tmp_path / "other.pem").write_text(pkg.public_pem(other))
    assert run("inspect", str(out), "--pubkey", str(tmp_path / "other.pem")).returncode == 1
