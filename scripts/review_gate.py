"""Offline code-review gate for CI: analyses C sources without the server and exits non-zero on blocking findings.

  python scripts/review_gate.py src/ --fail-on high --format text
  python scripts/review_gate.py src/ --format sarif > review.sarif        (upload to GitHub code scanning)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.codereview.analyzer import SEV_RANK, analyze  # noqa: E402
from app.modules.codereview.sarif import to_sarif  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--fail-on", default="high", choices=list(SEV_RANK))
    ap.add_argument("--format", default="text", choices=["text", "json", "sarif"])
    ap.add_argument("--ignore", default="", help="comma separated rule ids to ignore, e.g. CR008,CR025")
    a = ap.parse_args()
    ignore = {x.strip() for x in a.ignore.split(",") if x.strip()}
    files = []
    for p in map(Path, a.paths):
        files += sorted(x for x in p.rglob("*") if x.suffix in (".c", ".h")) if p.is_dir() else [p]
    items = []
    for f in files:
        try:
            text = f.read_bytes().decode("utf-8", errors="replace")
        except OSError as e:
            print(f"cannot read {f}: {e}", file=sys.stderr)
            return 2
        for x in analyze(f.name, text):
            if x["rule"] not in ignore:
                items.append({**{k: x[k] for k in ("rule", "title", "severity", "message", "line", "cwe", "cert", "category", "fingerprint", "function")}, "file": str(f)})
    blockers = [i for i in items if SEV_RANK[i["severity"]] >= SEV_RANK[a.fail_on]]
    if a.format == "sarif":
        print(json.dumps(to_sarif(items), indent=2))
    elif a.format == "json":
        print(json.dumps({"passed": not blockers, "findings": items}, indent=2))
    else:
        for i in sorted(items, key=lambda i: (i["file"], i["line"])):
            print(f"{i['file']}:{i['line']}: {i['severity']}: {i['message']} [{i['rule']}, {i['cwe'] or '-'}]")
        print(f"\n{len(files)} files, {len(items)} findings, {len(blockers)} at or above '{a.fail_on}': {'FAILED' if blockers else 'PASSED'}", file=sys.stderr)
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
