"""SARIF 2.1.0 export (usable by GitHub code scanning and most IDEs)."""
from __future__ import annotations

from app.modules.codereview.analyzer import RULES

LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}


def to_sarif(findings: list[dict], tool_version: str = "1.0") -> dict:
    """findings: dicts with rule, title, severity, message, file, line, cwe, fingerprint [, suppression]."""
    rules, seen = [], set()
    for f in findings:
        if f["rule"] not in seen:
            seen.add(f["rule"])
            meta = RULES.get(f["rule"], {})
            rules.append({"id": f["rule"], "name": f["title"].replace(" ", ""), "shortDescription": {"text": f["title"]},
                          "fullDescription": {"text": meta.get("why", f["title"])},
                          "help": {"text": meta.get("fix", "")},
                          "properties": {"tags": [t for t in (f.get("cwe"), f.get("cert"), f.get("category")) if t], "security-severity": {"critical": "9.5", "high": "8.0", "medium": "5.0", "low": "2.0", "info": "0.5"}[f["severity"]]}})
    results = []
    for f in findings:
        r = {"ruleId": f["rule"], "level": LEVEL[f["severity"]], "message": {"text": f["message"]},
             "locations": [{"physicalLocation": {"artifactLocation": {"uri": f["file"]}, "region": {"startLine": max(1, f["line"])}}}],
             "partialFingerprints": {"primaryLocationLineHash": f["fingerprint"]}}
        if f.get("suppression"):
            r["suppressions"] = [{"kind": "external", "justification": f["suppression"]}]
        results.append(r)
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "Automotive Engineering AI code review", "version": tool_version,
                                          "informationUri": "https://example.invalid/automotive-eng-ai", "rules": rules}},
                      "results": results}]}
