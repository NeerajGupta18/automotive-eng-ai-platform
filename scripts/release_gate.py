"""Release gate for external pipelines (Jenkins, GitHub Actions, GitLab CI, Azure DevOps): asks a running platform
for the project's release readiness and fails the pipeline on NO-GO.

  python scripts/release_gate.py --project bcm-demo [--api https://aeai.example.com] [--allow-conditions] [--report out.md]

Uses only the Python standard library. The token comes from AEAI_API_TOKEN (needed when AUTH_MODE=enforced).
Exit codes: 0 GO (or GO WITH CONDITIONS with --allow-conditions), 1 not releasable, 2 the platform could not be reached."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def get(api: str, path: str, token: str, accept: str = "application/json") -> bytes:
    req = urllib.request.Request(api.rstrip("/") + urllib.parse.quote(path), headers={"Accept": accept})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--api", default=os.getenv("AEAI_API_URL", "http://localhost:8000"))
    ap.add_argument("--allow-conditions", action="store_true", help="accept GO WITH CONDITIONS (warnings only)")
    ap.add_argument("--report", help="also write the Markdown readiness report to this file")
    a = ap.parse_args()
    token = os.getenv("AEAI_API_TOKEN", "")
    try:
        r = json.loads(get(a.api, f"/projects/{a.project}/readiness", token))
        if a.report:
            with open(a.report, "wb") as f:
                f.write(get(a.api, f"/projects/{a.project}/readiness/report", token, "text/markdown"))
    except urllib.error.HTTPError as e:
        print(f"release gate: the platform answered HTTP {e.code}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"release gate: cannot reach {a.api}: {e}", file=sys.stderr)
        return 2
    print(f"Release readiness of '{a.project}': {r['verdict']}")
    for c in r["checks"]:
        if c["status"] in ("block", "warn"):
            print(f"  {c['status'].upper():5s} {c['id']} {c['title']}: {c['summary']}  [owner: {c['owner']}]")
    ok = r["verdict"] == "GO" or (a.allow_conditions and r["verdict"] == "GO WITH CONDITIONS")
    print("Gate PASSED" if ok else "Gate FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
