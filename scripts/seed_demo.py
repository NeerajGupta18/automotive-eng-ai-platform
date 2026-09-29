"""Load the sample BCM HLDs plus HARA and TARA starting points into a demo project (API must be running)."""
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
          f"upload data/samples/code/door_lock_ctrl_v2.c as a new revision to see fixed findings")
    print("Done. Open the UI and select project 'bcm-demo'.")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        sys.exit(f"Could not reach the API at {API}: {e}")
