"""UN Regulation No. 155, Annex 5 Part A: high-level threat groups, and a rule-based mapping of the
platform's TARA threats and monitoring evidence onto them.

The seven groups follow the structure of Annex 5 Part A (threats regarding back-end servers,
communication channels, update procedures, unintended human actions, external connectivity, data/code,
and potential vulnerabilities). Wording here is a paraphrase for orientation. Check the mapping against
the official regulation text (free from unece.org) and your approval authority's interpretation; India's
draft AIS-189 follows the same UN framework."""
from __future__ import annotations

CATEGORIES = [
    {"id": "R155-1", "title": "Back-end servers related to vehicles in the field",
     "examples": "Back-end used to attack vehicles or extract data; service disruption; data breach at the back-end"},
    {"id": "R155-2", "title": "Vehicle communication channels",
     "examples": "Spoofed or manipulated messages, replay, injection, eavesdropping, denial of service on buses and radio links"},
    {"id": "R155-3", "title": "Update procedures",
     "examples": "Compromised or manipulated updates, denial of legitimate updates, abuse of update keys"},
    {"id": "R155-4", "title": "Unintended human actions facilitating an attack",
     "examples": "Social engineering of owners or staff, misconfiguration, insecure handling by service personnel"},
    {"id": "R155-5", "title": "External connectivity and connections",
     "examples": "Abuse of remote functions, third-party apps, USB/OBD and other external interfaces"},
    {"id": "R155-6", "title": "Vehicle data and code",
     "examples": "Extraction or manipulation of software, keys, configuration and personal data; unauthorised erasure"},
    {"id": "R155-7", "title": "Potential vulnerabilities if not sufficiently protected or hardened",
     "examples": "Weak cryptography, unpatched components, software bugs, design flaws, debug access left open"},
]
TITLE = {c["id"]: c["title"] for c in CATEGORIES}

EXTERNAL = {"OBD", "USB", "Bluetooth", "WiFi", "Cellular"}
BUS = {"CAN", "LIN", "Ethernet", "FlexRay"}


def categories_for_threat(t: dict) -> list[str]:
    """Rule-based mapping of one TARA threat (payload) to R155 groups. A threat can support several."""
    iface, kind = t.get("interface", ""), t.get("asset_kind") or ""
    name = (t.get("name", "") + " " + t.get("asset_name", "")).lower()
    out = []
    if iface == "Backend" or "backend" in name:
        out.append("R155-1")
    radio_attack = iface in ("Cellular", "Bluetooth", "WiFi") and t.get("stride") in (
        "spoofing", "tampering", "information_disclosure", "denial_of_service")
    if iface in BUS or radio_attack:
        out.append("R155-2")
    if iface == "OTA" or "update" in name or kind == "ota_update_package":
        out.append("R155-3")
    if iface in EXTERNAL or "diagnostic" in name:
        out.append("R155-5")
    if kind in ("firmware", "configuration", "key_material", "personal_data") or any(
            w in name for w in ("software", "firmware", "key", "configuration", "personal data")):
        out.append("R155-6")
    return list(dict.fromkeys(out))


def map_threats(threats: list[dict], assets: dict[str, dict]) -> dict[str, list[dict]]:
    by_cat = {c["id"]: [] for c in CATEGORIES}
    for t in threats:
        p = dict(t["payload"])
        p.setdefault("asset_kind", (assets.get(p.get("asset", "")) or {}).get("kind", ""))
        for cid in categories_for_threat(p):
            by_cat[cid].append(t)
    return by_cat
