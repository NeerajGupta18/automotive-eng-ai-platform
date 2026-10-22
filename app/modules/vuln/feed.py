"""Vulnerability advisory feed in OSV format (https://ossf.github.io/osv-schema/).

Accepted inputs: one OSV JSON object, a JSON list of them, {"vulnerabilities": [...]} with optional
feed metadata, or a .zip of OSV JSON files (the format of the osv.dev per-ecosystem exports).
Severity is computed from the CVSS v3 vector when present (cvss.py); otherwise a textual severity
from database_specific is used; otherwise it is 'unknown' and shown as such, never guessed."""
from __future__ import annotations

import io
import json
import zipfile

from app.core.errors import Invalid
from app.modules.vuln import cvss
from app.modules.vuln.sbom import purl_key

MAX_BYTES = 50 * 1024 * 1024
MAX_ADVISORIES = 50000
TEXT_SEVERITY = {"critical": "critical", "high": "high", "moderate": "medium", "medium": "medium", "low": "low"}


def _severity(entry: dict) -> dict:
    for s in entry.get("severity") or []:
        if isinstance(s, dict) and str(s.get("type", "")).upper() in ("CVSS_V3", "CVSS_V31", "CVSS_V30"):
            vector = str(s.get("score", ""))
            try:
                score = cvss.base_score(vector)
                return {"score": score, "band": cvss.band(score), "vector": vector, "source": "cvss_v3"}
            except cvss.CVSSError:
                continue
    text = str((entry.get("database_specific") or {}).get("severity", "")).lower()
    if text in TEXT_SEVERITY:
        return {"score": None, "band": TEXT_SEVERITY[text], "vector": "", "source": "database_specific"}
    return {"score": None, "band": "unknown", "vector": "", "source": "none"}


def normalize(entry: dict) -> dict:
    if not isinstance(entry, dict) or not entry.get("id"):
        raise Invalid("Every advisory needs an 'id'")
    affected = []
    for a in entry.get("affected") or []:
        pkg = a.get("package") or {}
        name = str(pkg.get("name", "")).strip().lower()
        pk = purl_key(pkg.get("purl", "")) if pkg.get("purl") else None
        if not name and not pk:
            continue
        affected.append({"name": name, "purl_key": pk, "ecosystem": pkg.get("ecosystem", ""),
                         "ranges": a.get("ranges") or [], "versions": [str(v) for v in a.get("versions") or []]})
    if not affected:
        raise Invalid(f"Advisory {entry['id']} lists no affected package")
    refs = [r.get("url") for r in entry.get("references") or [] if isinstance(r, dict) and r.get("url")][:5]
    return {"id": str(entry["id"])[:64], "aliases": [str(x) for x in entry.get("aliases") or []][:10],
            "summary": str(entry.get("summary") or entry.get("details") or "")[:300],
            "details": str(entry.get("details") or "")[:2000], "severity": _severity(entry), "affected": affected,
            "published": entry.get("published", ""), "modified": entry.get("modified", ""), "references": refs,
            "synthetic": bool((entry.get("database_specific") or {}).get("synthetic"))}


def parse_feed(content: bytes, filename: str = "feed.json") -> tuple[dict, list[dict]]:
    """-> (feed metadata, normalized advisories)"""
    if not content:
        raise Invalid("Empty feed")
    if len(content) > MAX_BYTES:
        raise Invalid(f"Feed exceeds {MAX_BYTES // (1024 * 1024)} MB")
    raw: list = []
    meta = {"name": filename, "version": "", "note": ""}
    if filename.lower().endswith(".zip") or content[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as e:
            raise Invalid(f"Not a valid zip: {e}") from e
        total = 0
        for info in zf.infolist():
            if info.is_dir() or not info.filename.endswith(".json"):
                continue
            total += info.file_size
            if total > 4 * MAX_BYTES:
                raise Invalid("Feed archive expands too much")
            raw.append(json.loads(zf.read(info)))
    else:
        try:
            doc = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise Invalid(f"Feed is not valid JSON: {e}") from e
        if isinstance(doc, dict) and "vulnerabilities" in doc:
            meta.update({k: str(doc.get(k, "")) for k in ("name", "version", "note") if doc.get(k)})
            raw = doc["vulnerabilities"]
        elif isinstance(doc, list):
            raw = doc
        else:
            raw = [doc]
    if len(raw) > MAX_ADVISORIES:
        raise Invalid(f"Feed has more than {MAX_ADVISORIES} advisories")
    return meta, [normalize(e) for e in raw]
