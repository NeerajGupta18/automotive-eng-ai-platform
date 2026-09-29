"""Architecture model for TARA: components, interfaces, data flows, zones.
Inputs: JSON architecture, communication-matrix CSV, or a Stage-1 HLD extraction."""
from __future__ import annotations

import csv
import io

from app.core.errors import Invalid

INTERFACE_EXPOSURE = {"CAN": "internal", "LIN": "internal", "Ethernet": "internal", "FlexRay": "internal",
                      "internal": "internal", "OBD": "physical", "USB": "physical", "Bluetooth": "adjacent",
                      "WiFi": "adjacent", "Cellular": "remote", "OTA": "remote", "Backend": "remote"}
COMPONENT_TYPES = {"ecu", "gateway", "telematics", "infotainment", "hsm", "sensor", "actuator", "swc", "backend",
                   "external_tool"}
_BUS_ALIASES = {"can": "CAN", "can-fd": "CAN", "canfd": "CAN", "lin": "LIN", "eth": "Ethernet", "ethernet": "Ethernet",
                "flexray": "FlexRay", "obd": "OBD", "usb": "USB", "bluetooth": "Bluetooth", "ble": "Bluetooth",
                "wifi": "WiFi", "wi-fi": "WiFi", "cellular": "Cellular", "lte": "Cellular", "5g": "Cellular",
                "ota": "OTA", "backend": "Backend"}
MAX_ROWS = 5000
_TRUE = {"1", "true", "yes", "y", "x"}


def _bool(v) -> bool:
    return str(v).strip().lower() in _TRUE if not isinstance(v, bool) else v


def norm_interface(name: str) -> str:
    key = (name or "").strip()
    out = _BUS_ALIASES.get(key.lower())
    if out:
        return out
    if key in INTERFACE_EXPOSURE:
        return key
    raise Invalid(f"Unknown interface/bus type '{name}'. Known: {sorted(INTERFACE_EXPOSURE)}")


def normalize(arch: dict) -> dict:
    if not isinstance(arch, dict):
        raise Invalid("Architecture must be an object")
    comps, seen = [], set()
    for c in arch.get("components", []):
        name = (c.get("name") or "").strip()
        if not name:
            raise Invalid("Every component needs a name")
        if name in seen:
            raise Invalid(f"Duplicate component '{name}'")
        seen.add(name)
        ctype = (c.get("type") or "ecu").lower()
        if ctype not in COMPONENT_TYPES:
            raise Invalid(f"{name}: unknown type '{ctype}'. Known: {sorted(COMPONENT_TYPES)}")
        comps.append({"name": name, "type": ctype, "zone": (c.get("zone") or "internal").strip(),
                      "interfaces": sorted({norm_interface(i) for i in c.get("interfaces", [])}),
                      "security_relevant": _bool(c.get("security_relevant", False)),
                      "safety_relevant": _bool(c.get("safety_relevant", False)),
                      "handles_personal_data": _bool(c.get("handles_personal_data", False))})
    flows = []
    for f in arch.get("data_flows", []):
        for k in ("name", "from", "to", "channel"):
            if not str(f.get(k, "")).strip():
                raise Invalid(f"Data flow needs '{k}': {f}")
        for end in (f["from"], f["to"]):
            if end not in seen:
                raise Invalid(f"Data flow '{f['name']}' references unknown component '{end}'")
        flows.append({"name": f["name"].strip(), "from": f["from"], "to": f["to"], "channel": norm_interface(f["channel"]),
                      "safety_relevant": _bool(f.get("safety_relevant", False)),
                      "confidential": _bool(f.get("confidential", False)), "data": f.get("data", "")})
    return {"components": comps, "data_flows": flows}


def merge(base: dict, extra: dict) -> dict:
    """Merge without overwriting what the engineer already curated."""
    comps = {c["name"]: dict(c) for c in base.get("components", [])}
    for c in extra.get("components", []):
        if c["name"] in comps:
            comps[c["name"]]["interfaces"] = sorted(set(comps[c["name"]]["interfaces"]) | set(c["interfaces"]))
        else:
            comps[c["name"]] = dict(c)
    flows = {(f["name"], f["from"], f["to"], f["channel"]): f for f in base.get("data_flows", [])}
    for f in extra.get("data_flows", []):
        flows.setdefault((f["name"], f["from"], f["to"], f["channel"]), f)
    return normalize({"components": list(comps.values()), "data_flows": list(flows.values())})


def parse_comm_matrix(text: str) -> dict:
    """CSV columns: message, sender, receivers (';' separated), bus [, id, safety_relevant, confidential]."""
    rdr = csv.DictReader(io.StringIO(text))
    cols = {c.strip().lower() for c in (rdr.fieldnames or [])}
    if not {"message", "sender", "receivers", "bus"} <= cols:
        raise Invalid("Communication matrix needs columns: message, sender, receivers, bus")
    comps: dict[str, set] = {}
    flows = []
    for n, raw in enumerate(rdr, start=2):
        if n > MAX_ROWS:
            raise Invalid(f"Communication matrix exceeds {MAX_ROWS} rows")
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if not row.get("message") or not row.get("sender") or not row.get("receivers"):
            raise Invalid(f"Line {n}: message, sender and receivers are required")
        bus = norm_interface(row["bus"])
        comps.setdefault(row["sender"], set()).add(bus)
        for rcv in [r.strip() for r in row["receivers"].split(";") if r.strip()]:
            comps.setdefault(rcv, set()).add(bus)
            flows.append({"name": row["message"], "from": row["sender"], "to": rcv, "channel": bus,
                          "safety_relevant": _bool(row.get("safety_relevant", "")),
                          "confidential": _bool(row.get("confidential", "")), "data": row.get("id", "")})
    return normalize({"components": [{"name": n, "interfaces": sorted(i)} for n, i in comps.items()], "data_flows": flows})


def from_hld(extraction: dict) -> dict:
    comps = [{"name": c["name"], "type": "swc", "zone": "software"} for c in extraction["components"]]
    flows = [{"name": e["interface"], "from": e["provider"], "to": e["consumer"], "channel": "internal"}
             for e in extraction["dependencies"]]
    return normalize({"components": comps, "data_flows": flows})


def attack_surface(arch: dict) -> list[dict]:
    return [{"component": c["name"], "type": c["type"], "zone": c["zone"], "interface": i,
             "exposure": INTERFACE_EXPOSURE[i]} for c in arch["components"] for i in c["interfaces"]]


def boundary_crossings(arch: dict) -> list[dict]:
    zone = {c["name"]: c["zone"] for c in arch["components"]}
    return [{"flow": f["name"], "from": f["from"], "to": f["to"], "from_zone": zone[f["from"]],
             "to_zone": zone[f["to"]], "channel": f["channel"]}
            for f in arch["data_flows"] if zone[f["from"]] != zone[f["to"]]]
