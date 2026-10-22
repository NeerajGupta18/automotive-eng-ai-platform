"""Load the sample BCM HLDs plus HARA and TARA starting points, the Phase 2 vulnerability/OTA data, the Phase 4 ADAS
validation set and the Phase 5 integration/agent data into a demo project (API must be running)."""
import json
import os
import sys
from pathlib import Path

import requests

API = os.getenv("API_URL", "http://localhost:8000")

S = requests.Session()
if os.getenv("API_TOKEN"):                      # AUTH_MODE=enforced: export API_TOKEN=<an engineer/admin token>
    S.headers["Authorization"] = f"Bearer {os.environ['API_TOKEN']}"
SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


def main() -> None:
    existing = S.get(f"{API}/projects", timeout=30)
    existing.raise_for_status()
    if any(p.get("name") == "bcm-demo" for p in existing.json()):
        P, added = f"{API}/projects/bcm-demo", []
        st = S.get(f"{P}/adas/status", timeout=30)              # demo data from an earlier phase: add what is missing
        if st.ok and not st.json().get("library_loaded"):
            seed_phase4(P)
            added.append("Phase 4 ADAS")
        imp = S.get(f"{P}/integrations/reqif/imports", timeout=30)
        if imp.ok and not imp.json():
            seed_phase5(P)
            added.append("Phase 5 integration and agent")
        fo = S.get(f"{P}/field/overview", timeout=30)
        if fo.ok and not fo.json().get("complaints"):
            seed_phase6(P)
            added.append("Phase 6 field issues")
        if added:
            print(f"Added the {' and '.join(added)} data to the existing project 'bcm-demo'.")
            return
        print("Project 'bcm-demo' already exists, so the demo data is already loaded. Nothing to do.\n"
              "To start fresh: stop the API, delete the storage folder, start the API and run this again.")
        return
    S.post(f"{API}/projects", json={"name": "bcm-demo"}, timeout=30).raise_for_status()
    for fname, version in (("hld_bcm_v1.pdf", "1.0"), ("hld_bcm_v2.pdf", "1.1")):
        r = S.post(f"{API}/projects/bcm-demo/documents", timeout=120,
                          files={"file": (fname, (SAMPLES / fname).read_bytes())},
                          data={"version": version, "status": "approved"})
        print(fname, r.status_code, r.json().get("id") or r.json())
    P = f"{API}/projects/bcm-demo"
    hdr = {"X-User": "seed-script"}
    S.post(f"{P}/hara/library/load", json={}, headers=({} if os.getenv('API_TOKEN') else hdr), timeout=60).raise_for_status()
    item = S.post(f"{P}/hara/items", headers=({} if os.getenv('API_TOKEN') else hdr), timeout=60, json={
        "name": "BCM central locking", "description": "Locks and unlocks all doors",
        "functions": [{"name": "Central door locking", "description": "locks and unlocks all doors",
                       "guidewords": ["unintended", "loss"]}]}).json()["ref"]
    n = S.post(f"{P}/hara/items/{item}/suggest-hazards", headers=({} if os.getenv('API_TOKEN') else hdr), timeout=60).json()["created"]
    print(f"HARA: item {item}, {n} hazards suggested (triage them in the UI)")
    S.post(f"{P}/tara/library/load", json={}, headers=({} if os.getenv('API_TOKEN') else hdr), timeout=60).raise_for_status()
    arch = json.loads((SAMPLES / "tara_system_bcm.json").read_text())
    sysref = S.post(f"{P}/tara/systems", json={"name": "BCM system", "architecture": arch}, headers=({} if os.getenv('API_TOKEN') else hdr), timeout=60).json()["ref"]
    r = S.post(f"{P}/tara/systems/{sysref}/analyze", headers=({} if os.getenv('API_TOKEN') else hdr), timeout=60).json()
    print(f"TARA: system {sysref}, {r['assets']} assets, {r['threats']} threat scenarios proposed")
    doc = S.post(f"{P}/documents", timeout=60, headers=({} if os.getenv('API_TOKEN') else hdr), data={"version": "1.0", "status": "approved"},
                        files={"file": ("uds_bcm_spec.md", (SAMPLES / "uds_bcm_spec.md").read_bytes())}).json()
    spec = S.post(f"{P}/uds/specs/from-document", json={"doc_id": doc["id"]}, headers=({} if os.getenv('API_TOKEN') else hdr), timeout=60).json()["ref"]
    g = S.post(f"{P}/uds/specs/{spec}/generate", headers=({} if os.getenv('API_TOKEN') else hdr), timeout=60).json()
    print(f"UDS: spec {spec} extracted, {g['created']} tests generated (approve the spec, then review the tests)")
    code = SAMPLES / "code"
    for src, name in (("door_lock_ctrl_v1.c", "door_lock_ctrl.c"), ("diag_handler.c", "diag_handler.c")):
        S.post(f"{P}/codereview/sources", headers=({} if os.getenv('API_TOKEN') else hdr), timeout=60, files={"file": (name, (code / src).read_bytes())}).raise_for_status()
    for log in ("cppcheck_report.xml", "vendor_scan.sarif"):
        S.post(f"{P}/codereview/logs", headers=({} if os.getenv('API_TOKEN') else hdr), timeout=60, files={"file": (log, (code / log).read_bytes())}).raise_for_status()
    hld = S.get(f"{P}/documents", timeout=30).json()[0]["id"]
    S.put(f"{P}/codereview/context", json={"hld_doc_id": hld}, headers=({} if os.getenv('API_TOKEN') else hdr), timeout=30)
    a = S.post(f"{P}/codereview/analyze", json={}, headers=({} if os.getenv('API_TOKEN') else hdr), timeout=120).json()
    print(f"CODE: {a['new']} findings ({a['tool_findings']} from tool logs, {a['corroborated']} corroborated); "
          f"to see fixed findings, upload data/samples/code/door_lock_ctrl_v2.c RENAMED to door_lock_ctrl.c "
          f"(revisions are matched by file name)")
    seed_phase2(P)
    seed_phase4(P)
    seed_phase5(P)
    seed_phase6(P)
    print("Done. Open the UI and select project 'bcm-demo'.")


def seed_phase2(P: str) -> None:
    """Vulnerability monitoring + OTA: feed, ECU SBOMs, one approved Gateway threat assessment, an active
    signing key and one verified (not yet released) update package. Engineer 'ravi' proposes,
    reviewer 'asha' approves, so the four-eyes rules are exercised. With API_TOKEN set (enforced auth)
    the token identity is used for everything and the second-person steps are skipped."""
    token = bool(os.getenv("API_TOKEN"))
    as_ = (lambda user: {}) if token else (lambda user: {"X-User": user})
    S.post(f"{P}/vuln/library/load", headers=as_("asha"), timeout=60).raise_for_status()
    for name in ("sbom_gateway_2.3.0.cdx.json", "sbom_telematics_5.1.0.cdx.json", "sbom_bcm_1.0.0.cdx.json"):
        S.post(f"{P}/vuln/sboms", headers=as_("ravi"), timeout=60,
               files={"file": (name, (SAMPLES / "sbom" / name).read_bytes())}).raise_for_status()
    m = S.get(f"{P}/vuln/metrics", timeout=30).json()
    print(f"VULN: {m['ecus_with_sbom']} ECU SBOMs, {m['findings']} findings "
          f"({m['open_by_severity']['critical']} critical, {m['open_by_severity']['high']} high)")
    threats = [t for t in S.get(f"{P}/tara/threats", timeout=30).json()
               if t["payload"]["component"] == "Gateway" and t["payload"].get("suggested")]
    if threats and not token:
        t = next((x for x in threats if "firmware" in x["payload"].get("asset_name", "").lower()
                  or "software" in x["payload"].get("asset_name", "").lower()), threats[0])
        sug = t["payload"]["suggested"]
        S.put(f"{P}/tara/threats/{t['ref']}/assessment", headers=as_("ravi"), timeout=30, json={
            "impact": sug["impact"], "ap": sug["ap"],
            "rationale": "Demo assessment using the library suggestion; Gateway firmware reachable via Ethernet/OBD"}).raise_for_status()
        S.post(f"{P}/tara/threats/{t['ref']}/decision", headers=as_("asha"), timeout=30,
               json={"decision": "approved", "comment": "Reviewed for the demo"}).raise_for_status()
        print(f"TARA: {t['ref']} ({t['payload']['name']}) assessed and approved, so a confirmed Gateway vulnerability will flag it")
    key = S.post(f"{P}/ota/keys", headers=as_("ravi"), timeout=30, json={
        "key_id": "oem-ota-2026", "owner": "OEM release HSM (demo)",
        "public_key_pem": (SAMPLES / "ota" / "ota_demo_key.public.pem").read_text(),
        "valid_until": "2028-12-31T00:00:00Z"})
    key.raise_for_status()
    if not token:
        S.post(f"{P}/ota/keys/{key.json()['ref']}/decision", headers=as_("asha"), timeout=30,
               json={"decision": "approved", "comment": "Fingerprint checked against the HSM export"}).raise_for_status()
    pk = S.post(f"{P}/ota/packages", headers=as_("ravi"), timeout=60, files={"file": (
        "pkg_gateway_2.4.0_signed.zip", (SAMPLES / "ota" / "pkg_gateway_2.4.0_signed.zip").read_bytes())}).json()
    print(f"OTA: key oem-ota-2026 {'active' if not token else 'proposed'}; {pk['ref']} {pk['payload']['title']} "
          f"verdict {pk['payload']['verdict']} (release it as a second person in the UI); try the tampered/rollback "
          f"packages in data/samples/ota")


def seed_phase4(P: str) -> None:
    """ADAS virtual validation: AEB requirements + scenario catalog (reviewer 'asha'), a plain-language scenario
    proposal and a first SIL campaign by engineer 'ravi'. The first campaign fails on purpose: requirement AEB-009
    (activation speed window) has no scenario yet. The demo closes the gap in the UI and releases."""
    token = bool(os.getenv("API_TOKEN"))
    as_ = (lambda user: {}) if token else (lambda user: {"X-User": user})
    A = f"{P}/adas"
    S.post(f"{A}/library/load", headers=as_("asha"), timeout=60).raise_for_status()
    S.post(f"{A}/scenarios/from-text", headers=as_("ravi"), timeout=60,
           json={"text": "Truck stopped in fog at 60 km/h on a wet road"}).raise_for_status()
    c = S.post(f"{A}/campaigns", headers=as_("ravi"), timeout=600,
               json={"target": "auto", "back_to_back": True, "mutation": False, "note": "first run after loading the catalog"})
    c.raise_for_status()
    p = c.json()["payload"]
    print(f"ADAS: {len(p['requirement_set'])} AEB requirements, {len(p['scenario_set'])} scenarios; {c.json()['ref']} on "
          f"{p['target'].upper()}: {'PASSED' if p['verdict']['passed'] else 'FAILED'}"
          + (f" (not passed: {', '.join(p['verdict']['blocking_not_passed'])}; propose boundary scenarios in the UI)"
             if p["verdict"]["blocking_not_passed"] else ""))


OEM_LINKS = {"CR-AEB-01": "AEB-009", "CR-AEB-02": "AEB-001", "CR-AEB-03": "AEB-005", "CR-AEB-04": "AEB-S01",
             "CR-AEB-05": "AEB-006", "CR-AEB-06": "AEB-007"}


def seed_phase5(P: str) -> None:
    """Enterprise integration and the engineering agent: the OEM's AEB requirements arrive as ReqIF (v1), a reviewer
    accepts them and they are linked to the platform's ADAS requirements; a webhook (dry run unless
    INTEGRATIONS_LIVE=1) and an agent run that waits at its first checkpoint. Version 2 of the OEM file is left for
    the demo: importing it shows the customer's changes and what they invalidate."""
    token = bool(os.getenv("API_TOKEN"))
    as_ = (lambda user: {}) if token else (lambda user: {"X-User": user})
    I = f"{P}/integrations"
    f = SAMPLES / "integration" / "oem_aeb_requirements_v1.reqif"
    imp = S.post(f"{I}/reqif/import", headers=as_("ravi"), timeout=60, files={"file": (f.name, f.read_bytes())})
    imp.raise_for_status()
    xr = S.get(f"{I}/reqif/requirements", timeout=30).json()
    ar = {r["payload"]["id"]: r["ref"] for r in S.get(f"{P}/adas/requirements", timeout=30).json()}
    for x in xr:
        S.post(f"{I}/reqif/requirements/{x['ref']}/decision", headers=as_("asha"), timeout=30,
               json={"decision": "approved", "comment": "Accepted for the AEB development (demo)"}).raise_for_status()
        target = ar.get(OEM_LINKS.get(x["payload"]["key"], ""))
        if target:
            S.post(f"{I}/reqif/links", headers=as_("ravi"), timeout=30, json={"xr_ref": x["ref"], "target_ref": target}).raise_for_status()
    hook = S.post(f"{I}/webhooks", headers=as_("asha"), timeout=30, json={
        "url": "https://hooks.example.com/automotive-eng-ai", "description": "Demo receiver (dry run unless INTEGRATIONS_LIVE=1)"})
    run = S.post(f"{P}/agent/runs", headers=as_("ravi"), timeout=300,
                 json={"goal": "Are we ready to release? Prepare the release readiness review.", "playbook": "release_readiness"})
    run.raise_for_status()
    r = run.json()
    print(f"INTEGRATION: OEM ReqIF v1 imported ({len(xr)} requirements accepted and linked to the ADAS requirements); "
          f"webhook {'added' if hook.ok else 'skipped (needs a project administrator)'}; agent run {r['ref']} {r['status']} at step "
          f"{r['payload']['steps'][-1]['n']} ({r['payload']['steps'][-1]['tool']}). Import "
          f"data/samples/integration/oem_aeb_requirements_v2.reqif in the UI to see the customer's changes.")


def seed_phase6(P: str) -> None:
    """Field issues: the AEB build registry and six dealer complaints with event recordings (synthetic). Complaints with the
    same symptom on the same build become one issue; the agent investigates every new issue automatically and stops at its
    first human checkpoint (the hazard rating, or the corrective actions when there is no malfunction)."""
    token = bool(os.getenv("API_TOKEN"))
    as_ = (lambda user: {}) if token else (lambda user: {"X-User": user})
    r = S.post(f"{P}/field/samples/load", headers=as_("ravi"), timeout=900)
    r.raise_for_status()
    d = r.json()
    iss = S.get(f"{P}/field/issues", timeout=30).json()
    desc = "; ".join(f"{i['ref']} {i['title']} -> {(i['class'] or 'not investigated').replace('_', ' ')}"
                     + (f" ({i['run']} waiting at {i['waiting_for']['tool']})" if i["waiting_for"] else "") for i in reversed(iss))
    print(f"FIELD: {len(d['complaints'])} complaints, {len(d['issues'])} issues investigated by the agent: {desc}")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        sys.exit(f"Could not reach the API at {API}: {e}")
