"""Load the sample BCM HLDs into a demo project (API must be running)."""
import os
import sys
from pathlib import Path

import requests

API = os.getenv("API_URL", "http://localhost:8000")
SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


def main() -> None:
    requests.post(f"{API}/projects", json={"name": "bcm-demo"}, timeout=30).raise_for_status()
    for fname, version in (("hld_bcm_v1.pdf", "1.0"), ("hld_bcm_v2.pdf", "1.1")):
        r = requests.post(f"{API}/projects/bcm-demo/documents", timeout=120,
                          files={"file": (fname, (SAMPLES / fname).read_bytes())},
                          data={"version": version, "status": "approved"})
        print(fname, r.status_code, r.json().get("id") or r.json())
    print("Done. Open the UI and select project 'bcm-demo'.")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        sys.exit(f"Could not reach the API at {API}: {e}")
