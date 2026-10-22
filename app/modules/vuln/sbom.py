"""CycloneDX SBOM ingestion (JSON, spec 1.2 - 1.6).

An SBOM here describes ONE ECU software release: metadata.component is the ECU software (name =
the ECU as named in the TARA architecture, version = the installed software version) and
components[] are the third-party and in-house packages inside it. Two optional properties on
metadata.component carry type-approval data used by the OTA checks:
    aeai:hardware_id   hardware variant the software is built for
    aeai:rxswin        RX Software Identification Number (UN R156) of the regulated system"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import unquote

from app.core.errors import Invalid

MAX_BYTES = 5 * 1024 * 1024
MAX_COMPONENTS = 5000
SPEC_VERSIONS = ("1.2", "1.3", "1.4", "1.5", "1.6")


def parse_purl(purl: str) -> dict | None:
    """pkg:type/namespace/name@version?qualifiers#subpath -> {type, namespace, name, version}"""
    if not isinstance(purl, str) or not purl.startswith("pkg:"):
        return None
    body = purl[4:].split("#", 1)[0].split("?", 1)[0]
    body, _, version = body.partition("@")
    parts = [unquote(p) for p in body.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    return {"type": parts[0].lower(), "namespace": "/".join(parts[1:-1]).lower(), "name": parts[-1].lower(),
            "version": unquote(version)}


def purl_key(purl: str) -> str | None:
    p = parse_purl(purl)
    return f"{p['type']}/{p['namespace']}/{p['name']}" if p else None


def _props(obj: dict) -> dict:
    return {p.get("name"): p.get("value") for p in obj.get("properties") or [] if isinstance(p, dict)}


def _flatten(components: list, out: list, depth: int = 0) -> None:
    if depth > 10:
        raise Invalid("SBOM component nesting is too deep")
    for c in components or []:
        if not isinstance(c, dict):
            raise Invalid("SBOM components must be objects")
        out.append(c)
        if len(out) > MAX_COMPONENTS:
            raise Invalid(f"SBOM has more than {MAX_COMPONENTS} components")
        _flatten(c.get("components"), out, depth + 1)


def parse_cyclonedx(content: bytes) -> dict:
    if not content:
        raise Invalid("Empty SBOM")
    if len(content) > MAX_BYTES:
        raise Invalid(f"SBOM exceeds {MAX_BYTES // (1024 * 1024)} MB")
    try:
        doc = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise Invalid(f"SBOM is not valid JSON: {e}") from e
    if not isinstance(doc, dict) or doc.get("bomFormat") != "CycloneDX":
        raise Invalid("Not a CycloneDX SBOM (bomFormat must be 'CycloneDX')")
    spec = str(doc.get("specVersion", ""))
    if spec not in SPEC_VERSIONS:
        raise Invalid(f"Unsupported CycloneDX specVersion '{spec}' (supported: {', '.join(SPEC_VERSIONS)})")
    meta = (doc.get("metadata") or {}).get("component") or {}
    ecu, ecu_version = str(meta.get("name", "")).strip(), str(meta.get("version", "")).strip()
    if not ecu or not ecu_version:
        raise Invalid("metadata.component must name the ECU software and its version")
    props = _props(meta)
    flat: list[dict] = []
    _flatten(doc.get("components"), flat)
    comps, warnings = [], []
    for i, c in enumerate(flat):
        name = str(c.get("name", "")).strip()
        if not name:
            raise Invalid(f"Component #{i + 1} has no name")
        version = str(c.get("version", "")).strip()
        purl = str(c.get("purl", "")).strip()
        if purl and parse_purl(purl) is None:
            warnings.append(f"{name}: unreadable purl '{purl[:80]}'")
            purl = ""
        if not version:
            warnings.append(f"{name}: no version, cannot be matched against advisories")
        supplier = (c.get("supplier") or {}).get("name") if isinstance(c.get("supplier"), dict) else None
        licenses = []
        for lic in c.get("licenses") or []:
            if isinstance(lic, dict):
                l = lic.get("license") or {}
                licenses.append(l.get("id") or l.get("name") or lic.get("expression") or "")
        comps.append({"name": name, "version": version, "purl": purl, "bom_ref": str(c.get("bom-ref") or f"c{i + 1}"),
                      "type": c.get("type", "library"), "supplier": supplier or (c.get("author") or ""),
                      "licenses": [x for x in licenses if x]})
    serial = str(doc.get("serialNumber", ""))
    return {
        "ecu": ecu, "ecu_version": ecu_version, "hardware_id": props.get("aeai:hardware_id", ""),
        "rxswin": props.get("aeai:rxswin", ""), "spec_version": spec, "serial": serial,
        "timestamp": (doc.get("metadata") or {}).get("timestamp", ""), "components": comps,
        "component_count": len(comps), "sha256": hashlib.sha256(content).hexdigest(), "warnings": warnings,
    }
