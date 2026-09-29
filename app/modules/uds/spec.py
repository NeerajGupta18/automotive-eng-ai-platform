"""Diagnostic specification model: normalisation, ingestion from spec documents, consistency checks.

The model is deliberately small and explicit (sessions, security levels, services, DIDs, routines, DTCs,
timing). Everything the oracle and the test generator do is derived from it."""
from __future__ import annotations

import re

from app.core.errors import Invalid
from app.core.parsing import Page, split_sections
from app.modules.hld.extractor import _col, _get, parse_tables

# modelled services: sid -> (name, has_subfunction)
MODELLED = {0x10: ("DiagnosticSessionControl", True), 0x11: ("ECUReset", True), 0x14: ("ClearDiagnosticInformation", False),
            0x19: ("ReadDTCInformation", True), 0x22: ("ReadDataByIdentifier", False), 0x27: ("SecurityAccess", True),
            0x2E: ("WriteDataByIdentifier", False), 0x31: ("RoutineControl", True), 0x3E: ("TesterPresent", True),
            0x85: ("ControlDTCSetting", True)}
UNMODELLED = {0x23: "ReadMemoryByAddress", 0x28: "CommunicationControl", 0x2F: "InputOutputControlByIdentifier",
              0x34: "RequestDownload", 0x35: "RequestUpload", 0x36: "TransferData", 0x37: "RequestTransferExit"}
SUBS = {0x11: [(1, "hardReset"), (3, "softReset")], 0x19: [(1, "reportNumberOfDTCByStatusMask"), (2, "reportDTCByStatusMask")],
        0x3E: [(0, "zeroSubFunction")], 0x85: [(1, "on"), (2, "off")],
        0x31: [(1, "startRoutine"), (2, "stopRoutine"), (3, "requestRoutineResults")]}
CONTROLS = {"start": 1, "startroutine": 1, "stop": 2, "stoproutine": 2, "results": 3, "result": 3, "requestroutineresults": 3}
DEFAULT_SESSIONS = [{"id": 1, "name": "default"}, {"id": 2, "name": "programming"}, {"id": 3, "name": "extended"}]

NRC = {0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
       0x13: "incorrectMessageLengthOrInvalidFormat", 0x22: "conditionsNotCorrect", 0x24: "requestSequenceError",
       0x31: "requestOutOfRange", 0x33: "securityAccessDenied", 0x35: "invalidKey", 0x36: "exceededNumberOfAttempts",
       0x37: "requiredTimeDelayNotExpired", 0x78: "requestCorrectlyReceivedResponsePending",
       0x7E: "subFunctionNotSupportedInActiveSession", 0x7F: "serviceNotSupportedInActiveSession"}


def to_int(v, what: str = "value") -> int:
    if isinstance(v, bool):
        raise Invalid(f"{what}: not a number")
    if isinstance(v, int):
        return v
    s = str(v).strip().replace("_", "")
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except ValueError:
        raise Invalid(f"{what}: '{v}' is not a number (use decimal or 0x hex)")


def normalize_spec(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise Invalid("Specification must be an object")
    sessions = [{"id": to_int(s["id"], "session id"), "name": str(s["name"]).strip().lower()}
                for s in raw.get("sessions") or DEFAULT_SESSIONS]
    if len({s["id"] for s in sessions}) != len(sessions) or len({s["name"] for s in sessions}) != len(sessions):
        raise Invalid("Duplicate session id or name")
    by_name = {s["name"]: s["id"] for s in sessions}
    all_ids = [s["id"] for s in sessions]

    def sess(v, what) -> list[int]:
        if v in (None, "", "all", ["all"]):
            return list(all_ids)
        items = [x.strip() for x in v.split(",")] if isinstance(v, str) else list(v)
        out = []
        for x in items:
            if str(x).lower() == "all":
                return list(all_ids)
            key = str(x).strip().lower()
            sid = by_name.get(key, None)
            if sid is None:
                sid = to_int(x, what)
            if sid not in all_ids:
                raise Invalid(f"{what}: unknown session '{x}'")
            out.append(sid)
        return sorted(set(out))

    def sec(v) -> int | None:
        if v in (None, "", "-", "none", "None"):
            return None
        m = re.search(r"\d+|0x[0-9a-fA-F]+", str(v))
        return to_int(m.group(0), "security level") if m else None

    levels = []
    for l in raw.get("security_levels", []):
        levels.append({"level": to_int(l["level"], "security level"), "seed_length": to_int(l.get("seed_length", 2)),
                       "max_attempts": to_int(l.get("max_attempts", 3)),
                       "sessions": sess(l.get("sessions") or [n for n in by_name if n != "default"], "security level sessions")})
        if not 1 <= levels[-1]["seed_length"] <= 16 or levels[-1]["max_attempts"] < 1 or not 1 <= levels[-1]["level"] <= 0x3F:
            raise Invalid("security level: level must be 1-63, seed length 1-16, attempts >= 1")
    level_ids = {l["level"] for l in levels}

    services = []
    for s in raw.get("services", []):
        sid = to_int(s["sid"], "sid")
        if not 0 < sid <= 0xFF or sid == 0x7F:
            raise Invalid(f"Invalid SID 0x{sid:02X}")
        name = s.get("name") or MODELLED.get(sid, (UNMODELLED.get(sid, f"Service_{sid:02X}"),))[0]
        ss = sess(s.get("sessions"), f"service 0x{sid:02X} sessions")
        subs = s.get("subfunctions")
        if subs:
            subs = [{"id": to_int(x["id"]), "name": x.get("name", ""), "sessions": sess(x.get("sessions", ss), "sub sessions")} for x in subs]
        elif sid == 0x10:
            subs = [{"id": x["id"], "name": f"{x['name']}Session", "sessions": ss} for x in sessions]
        elif sid == 0x27:
            subs = []
            for l in levels:
                subs += [{"id": 2 * l["level"] - 1, "name": f"requestSeed_L{l['level']}", "sessions": l["sessions"]},
                         {"id": 2 * l["level"], "name": f"sendKey_L{l['level']}", "sessions": l["sessions"]}]
        elif sid in SUBS:
            subs = [{"id": i, "name": n, "sessions": ss} for i, n in SUBS[sid]]
        else:
            subs = []
        services.append({"sid": sid, "name": name, "sessions": ss, "security": sec(s.get("security")), "subfunctions": subs,
                         "modelled": sid in MODELLED})
    if len({s["sid"] for s in services}) != len(services):
        raise Invalid("Duplicate service SID")

    dids = []
    for d in raw.get("dids", []):
        acc = str(d.get("access", "")).upper().replace("/", "").replace(" ", "").replace("READ", "R").replace("WRITE", "W")
        read = bool(d["read"]) if "read" in d else "R" in acc
        write = bool(d["write"]) if "write" in d else "W" in acc
        rng = d.get("range")
        if isinstance(rng, str):
            m = re.match(r"^\s*(-?\d+)\s*(?:-|\.\.|to)\s*(-?\d+)\s*$", rng)
            rng = [int(m.group(1)), int(m.group(2))] if m else None
        if rng is not None and (len(rng) != 2 or rng[0] > rng[1]):
            raise Invalid(f"DID range must be [min, max]: {rng}")
        length = to_int(d.get("length", 0), "DID length")
        if not 0 < length <= 255:
            raise Invalid(f"DID 0x{to_int(d['did']):04X}: length must be 1-255 bytes")
        dids.append({"did": to_int(d["did"], "DID"), "name": d.get("name", ""), "length": length, "read": read, "write": write,
                     "sessions": sess(d.get("sessions"), "DID sessions"), "security": sec(d.get("security")), "range": rng})
    routines = []
    for r in raw.get("routines", []):
        ctrl = r.get("controls", ["start"])
        ctrl = [x.strip() for x in ctrl.split(",")] if isinstance(ctrl, str) else ctrl
        ids = sorted({CONTROLS[str(c).lower()] if str(c).lower() in CONTROLS else to_int(c, "routine control") for c in ctrl})
        routines.append({"rid": to_int(r["rid"], "RID"), "name": r.get("name", ""), "controls": ids,
                         "sessions": sess(r.get("sessions"), "routine sessions"), "security": sec(r.get("security")),
                         "option_length": to_int(r.get("option_length", 0))})
    dtcs = [{"code": to_int(d["code"], "DTC"), "name": d.get("name", "")} for d in raw.get("dtcs", [])]
    t = raw.get("timing") or {}
    timing = {"p2_ms": to_int(t.get("p2_ms", 50)), "p2star_ms": to_int(t.get("p2star_ms", 5000)), "s3_ms": to_int(t.get("s3_ms", 5000))}
    if not (0 < timing["p2_ms"] < 65536 and 0 < timing["p2star_ms"] < 655360 and timing["s3_ms"] > 0):
        raise Invalid("Timing values out of range")
    return {"name": str(raw.get("name", "")), "ecu": str(raw.get("ecu", "")), "version": str(raw.get("version", "")),
            "sessions": sessions, "security_levels": levels, "services": services, "dids": dids, "routines": routines,
            "dtcs": dtcs, "timing": timing}


# ------------------------------------------------------------------ ingestion from a spec document
def from_document(pages: list[Page]) -> tuple[dict, list[str]]:
    """Rule-based extraction from Markdown/PDF tables (same table parser as the HLD module)."""
    raw: dict = {"sessions": [], "security_levels": [], "services": [], "dids": [], "routines": [], "dtcs": []}
    warnings: list[str] = []
    text_lines: list[str] = []
    for sec_ in split_sections(pages):
        tables, rest = parse_tables(sec_.lines)
        text_lines += [t for _, t in rest]
        for tb in tables:
            h = tb["header"]
            rows = [r for _, r in tb["rows"]]
            if _col(h, "sid") is not None:
                for r in rows:
                    raw["services"].append({"sid": _get(r, _col(h, "sid")), "name": _get(r, _col(h, "service", "name")),
                                            "sessions": _get(r, _col(h, "sessions", "session")), "security": _get(r, _col(h, "security"))})
            elif _col(h, "did") is not None:
                for r in rows:
                    raw["dids"].append({"did": _get(r, _col(h, "did")), "name": _get(r, _col(h, "name")), "length": _get(r, _col(h, "length")),
                                        "access": _get(r, _col(h, "access")), "sessions": _get(r, _col(h, "sessions", "session")),
                                        "security": _get(r, _col(h, "security")), "range": _get(r, _col(h, "range")) or None})
            elif _col(h, "rid") is not None:
                for r in rows:
                    raw["routines"].append({"rid": _get(r, _col(h, "rid")), "name": _get(r, _col(h, "name")),
                                            "controls": _get(r, _col(h, "controls")) or "start", "sessions": _get(r, _col(h, "sessions", "session")),
                                            "security": _get(r, _col(h, "security")), "option_length": _get(r, _col(h, "option")) or 0})
            elif _col(h, "dtc") is not None:
                raw["dtcs"] += [{"code": _get(r, _col(h, "dtc")), "name": _get(r, _col(h, "name"))} for r in rows]
            elif _col(h, "level") is not None and _col(h, "seed") is not None:
                for r in rows:
                    raw["security_levels"].append({"level": _get(r, _col(h, "level")), "seed_length": _get(r, _col(h, "seed")) or 2,
                                                   "max_attempts": _get(r, _col(h, "max", "attempts")) or 3,
                                                   "sessions": _get(r, _col(h, "sessions", "session")) or None})
            elif _col(h, "session") is not None and _col(h, "id") is not None:
                raw["sessions"] += [{"id": _get(r, _col(h, "id")), "name": _get(r, _col(h, "session"))} for r in rows]
            else:
                warnings.append(f"Table on page {tb['page']} with columns {h} was not recognised as a diagnostic table")
    body = "\n".join(text_lines)
    timing = {}
    for key, pat in (("p2star_ms", r"P2\*[^0-9\n]*(\d+)\s*ms"), ("p2_ms", r"P2(?!\*)[^0-9\n*]*(\d+)\s*ms"), ("s3_ms", r"S3[^0-9\n]*(\d+)\s*ms")):
        m = re.search(pat, body)
        if m:
            timing[key] = int(m.group(1))
    if timing:
        raw["timing"] = timing
    m = re.search(r"^ECU\s*:\s*(.+)$", body, re.M)
    raw["ecu"] = m.group(1).strip() if m else ""
    m = re.search(r"^Version\s*:\s*(.+)$", body, re.M)
    raw["version"] = m.group(1).strip() if m else ""
    if not raw["sessions"]:
        raw.pop("sessions")
    if not raw["services"]:
        raise Invalid("No service table (column 'SID') found in the document")
    return normalize_spec(raw), warnings


# ------------------------------------------------------------------ consistency checks on the specification itself
def check_spec(spec: dict) -> list[dict]:
    out: list[dict] = []

    def add(rule, sev, msg, el=""):
        out.append({"rule": rule, "severity": sev, "message": msg, "element": el})

    sids = {s["sid"]: s for s in spec["services"]}
    sess_ids = {s["id"] for s in spec["sessions"]}
    levels = {l["level"] for l in spec["security_levels"]}
    seen: set[int] = set()
    for d in spec["dids"]:
        el = f"DID:{d['did']:04X}"
        if d["did"] in seen:
            add("U001", "error", f"Duplicate DID 0x{d['did']:04X}", el)
        seen.add(d["did"])
        if not d["read"] and not d["write"]:
            add("U002", "error", f"DID 0x{d['did']:04X} is neither readable nor writable", el)
        if d["write"] and d["security"] is None:
            add("U003", "warning", f"Writable DID 0x{d['did']:04X} has no security requirement", el)
        if d["security"] is not None and d["security"] not in levels:
            add("U004", "error", f"DID 0x{d['did']:04X} requires undefined security level {d['security']}", el)
        if d["range"] and not d["write"]:
            add("U005", "warning", f"DID 0x{d['did']:04X} has a value range but is not writable", el)
    for r in spec["routines"]:
        el = f"RID:{r['rid']:04X}"
        if r["security"] is not None and r["security"] not in levels:
            add("U004", "error", f"Routine 0x{r['rid']:04X} requires undefined security level {r['security']}", el)
    for s in spec["services"]:
        if not s["modelled"]:
            add("U006", "warning", f"Service 0x{s['sid']:02X} ({s['name']}) is not modelled: no tests will be generated for it", f"SVC:{s['sid']:02X}")
        if s["security"] is not None and s["security"] not in levels:
            add("U004", "error", f"Service 0x{s['sid']:02X} requires undefined security level {s['security']}", f"SVC:{s['sid']:02X}")
        if not set(s["sessions"]) <= sess_ids:
            add("U007", "error", f"Service 0x{s['sid']:02X} references an undefined session", f"SVC:{s['sid']:02X}")
    for need, what, present in ((0x22, "DIDs", any(d["read"] for d in spec["dids"])), (0x2E, "writable DIDs", any(d["write"] for d in spec["dids"])),
                                (0x31, "routines", bool(spec["routines"])), (0x19, "DTCs", bool(spec["dtcs"]))):
        if present and need not in sids:
            add("U008", "error", f"The specification defines {what} but service 0x{need:02X} is not declared", f"SVC:{need:02X}")
    if 0x27 in sids and not levels:
        add("U009", "error", "SecurityAccess (0x27) is declared but no security level is defined", "SVC:27")
    if levels and 0x27 not in sids:
        add("U009", "error", "Security levels are defined but SecurityAccess (0x27) is not declared", "SVC:27")
    if 0x10 not in sids:
        add("U010", "error", "DiagnosticSessionControl (0x10) is not declared", "SVC:10")
    if 0x3E not in sids:
        add("U011", "warning", "TesterPresent (0x3E) is not declared; sessions cannot be kept alive", "SVC:3E")
    if len({r["rid"] for r in spec["routines"]}) != len(spec["routines"]):
        add("U001", "error", "Duplicate routine identifier", "RID")
    return out
