"""Ingestion of third-party static-analysis output: cppcheck XML, SARIF 2.1.0 JSON, compiler-style text."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath, PureWindowsPath

from app.core.errors import Invalid

MAX_ENTRIES = 20000
SEV_CPPCHECK = {"error": "high", "warning": "medium", "style": "low", "performance": "low", "portability": "low", "information": "info"}
SEV_SARIF = {"error": "high", "warning": "medium", "note": "low", "none": "info"}
SEV_TEXT = {"error": "high", "warning": "medium", "note": "low", "style": "low", "performance": "low", "portability": "low", "information": "info"}


def basename(p: str) -> str:
    p = p.replace("file://", "")
    return (PureWindowsPath(p) if "\\" in p else PurePosixPath(p)).name


def _entry(tool, file, line, rule, sev, msg, cwe=None) -> dict:
    return {"tool": tool, "file": basename(file), "path": file, "line": int(line or 0), "rule_id": rule or "", "severity": sev,
            "message": " ".join(str(msg).split())[:400], "cwe": f"CWE-{cwe}" if cwe and str(cwe).isdigit() else None}


def parse_cppcheck(raw: bytes) -> list[dict]:
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", raw, re.I):
        raise Invalid("XML with DOCTYPE/ENTITY declarations is rejected (entity expansion risk)")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise Invalid(f"Malformed XML: {e}")
    out = []
    for e in root.iter("error"):
        loc = next(iter(e.findall("location")), None)
        if loc is None:
            continue
        out.append(_entry("cppcheck", loc.get("file", ""), loc.get("line"), e.get("id"), SEV_CPPCHECK.get(e.get("severity", ""), "low"),
                          e.get("msg") or e.get("verbose", ""), e.get("cwe")))
    return out


def parse_sarif(raw: bytes) -> list[dict]:
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise Invalid(f"Malformed JSON: {e}")
    if not isinstance(doc, dict) or "runs" not in doc:
        raise Invalid("Not a SARIF document (no 'runs')")
    out = []
    for run in doc["runs"]:
        tool = ((run.get("tool") or {}).get("driver") or {}).get("name", "sarif")
        rules = {r.get("id"): r for r in ((run.get("tool") or {}).get("driver") or {}).get("rules", [])}
        for res in run.get("results", []):
            for loc in res.get("locations", [])[:1]:
                p = loc.get("physicalLocation", {})
                uri = (p.get("artifactLocation") or {}).get("uri", "")
                line = (p.get("region") or {}).get("startLine", 0)
                tags = " ".join((rules.get(res.get("ruleId"), {}).get("properties") or {}).get("tags", []))
                m = re.search(r"cwe[-/]?(\d+)", tags, re.I) or re.search(r"cwe[-/]?(\d+)", res.get("ruleId") or "", re.I)
                out.append(_entry(tool, uri, line, res.get("ruleId"), SEV_SARIF.get(res.get("level", "warning"), "medium"),
                                  (res.get("message") or {}).get("text", ""), m.group(1) if m else None))
    return out


_TEXT = re.compile(r"^(?P<file>(?:[A-Za-z]:[\\/])?[^:\n]+?):(?P<line>\d+)(?::\d+)?:\s*(?P<sev>error|warning|note|style|performance|portability|information)\s*:\s*(?P<msg>.*?)(?:\s*\[(?P<id>[^\]]+)\])?\s*$", re.M)


def parse_text(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8", errors="replace")
    return [_entry("compiler", m["file"], m["line"], m["id"], SEV_TEXT[m["sev"]], m["msg"]) for m in _TEXT.finditer(text)]


def parse_log(raw: bytes) -> tuple[str, list[dict]]:
    head = raw.lstrip()[:64]
    if head.startswith(b"<"):
        kind, entries = "cppcheck-xml", parse_cppcheck(raw)
    elif head.startswith(b"{"):
        kind, entries = "sarif", parse_sarif(raw)
    else:
        kind, entries = "text", parse_text(raw)
    if not entries:
        raise Invalid("No findings could be parsed from this log (supported: cppcheck XML, SARIF 2.1.0, 'file:line: warning: msg' text)")
    if len(entries) > MAX_ENTRIES:
        raise Invalid(f"Log has more than {MAX_ENTRIES} entries")
    return kind, entries
