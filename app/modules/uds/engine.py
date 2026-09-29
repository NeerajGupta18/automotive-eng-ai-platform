"""The response oracle: given a spec, the ECU state and a request, predict the ECU's response.

This is the single source of truth for (a) request validation, (b) expected results of generated tests and
(c) the behaviour of the simulator. It is deterministic; the LLM has no role in it.

Check order (documented so that it can be aligned with an OEM specification):
  1. service supported                      -> NRC 0x11
  2. service allowed in active session      -> NRC 0x7F
  3. sub-function present (length >= 2)     -> NRC 0x13
  4. sub-function supported                 -> NRC 0x12
  5. sub-function allowed in active session -> NRC 0x7E
  6. minimum/exact message length           -> NRC 0x13
  7. parameter validity (DID / RID / range) -> NRC 0x31
  8. security access                        -> NRC 0x33
  9. sequence / key checks                  -> NRC 0x24 / 0x35 / 0x36 / 0x37
Requests are lists of ints; the markers '<KEY>' and '<BADKEY>' stand for a correct / incorrect key.
Responses are token lists: 2-digit hex, '??' (any byte) and a trailing '*' (any tail)."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.modules.uds.spec import NRC

SPR_OK = {0x10: True, 0x11: True, 0x19: True, 0x27: False, 0x31: True, 0x3E: True, 0x85: True}
HAS_SUB = set(SPR_OK)


@dataclass
class State:
    session: int = 1
    unlocked: int | None = None
    seed_level: int | None = None
    attempts: dict = field(default_factory=dict)
    idle_ms: int = 0

    def copy(self) -> "State":
        return State(self.session, self.unlocked, self.seed_level, dict(self.attempts), self.idle_ms)


@dataclass
class Outcome:
    kind: str                 # positive | negative | suppressed
    tokens: list[str]
    nrc: int | None
    rule: str
    why: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "response": " ".join(self.tokens), "nrc": self.nrc,
                "nrc_name": NRC.get(self.nrc) if self.nrc else None, "rule": self.rule, "why": self.why}


def h(v: int, w: int = 2) -> str:
    return f"{v:0{w}X}"


def _neg(sid: int, nrc: int, rule: str, why: str) -> Outcome:
    return Outcome("negative", ["7F", h(sid), h(nrc)], nrc, rule, f"{why} -> NRC 0x{nrc:02X} {NRC[nrc]}")


def _pos(tokens: list[str], rule: str, why: str) -> Outcome:
    return Outcome("positive", tokens, None, rule, why)


def parse_request(text: str) -> list:
    out: list = []
    for tok in text.replace(",", " ").split():
        t = tok.strip()
        if t.upper() in ("<KEY>", "<BADKEY>"):
            out.append(t.upper())
        else:
            try:
                v = int(t, 16)
            except ValueError:
                raise ValueError(f"'{tok}' is not a hex byte")
            if not 0 <= v <= 255:
                raise ValueError(f"'{tok}' is not a byte")
            out.append(v)
    return out


def tick(spec: dict, st: State, ms: int) -> State:
    """Time passes without requests: S3 timeout returns the ECU to the default session and locks security."""
    s = st.copy()
    s.idle_ms += ms
    if s.session != 1 and s.idle_ms > spec["timing"]["s3_ms"]:
        s.session, s.unlocked, s.seed_level, s.idle_ms = 1, None, None, 0
    return s


def _svc(spec: dict, sid: int):
    return next((x for x in spec["services"] if x["sid"] == sid), None)


def _sess_name(spec: dict, sid: int) -> str:
    return next((s["name"] for s in spec["sessions"] if s["id"] == sid), f"0x{sid:02X}")


def respond(spec: dict, state: State, req: list) -> tuple[Outcome, State]:
    st = state.copy()
    st.idle_ms = 0
    if not req or not isinstance(req[0], int):
        return _neg(0, 0x13, "U-EMPTY", "Empty request"), st
    sid = req[0]
    svc = _svc(spec, sid)
    if svc is None or not svc["modelled"]:
        return _neg(sid, 0x11, "U-11-SERVICE", f"Service 0x{sid:02X} is not supported by this ECU"), st
    if st.session not in svc["sessions"]:
        return _neg(sid, 0x7F, "U-7F-SESSION", f"Service 0x{sid:02X} is not available in the {_sess_name(spec, st.session)} session"), st
    sub, spr = None, False
    if sid in HAS_SUB:
        if len(req) < 2:
            return _neg(sid, 0x13, "U-13-NOSUB", "Sub-function byte missing"), st
        raw = req[1]
        spr = bool(raw & 0x80) and SPR_OK[sid]
        sub = raw & 0x7F if SPR_OK[sid] else raw
        sd = next((x for x in svc["subfunctions"] if x["id"] == sub), None)
        if sd is None:
            return _neg(sid, 0x12, "U-12-SUB", f"Sub-function 0x{raw:02X} is not supported by service 0x{sid:02X}"), st
        if st.session not in sd["sessions"]:
            return _neg(sid, 0x7E, "U-7E-SUBSESSION", f"Sub-function 0x{sub:02X} is not available in the {_sess_name(spec, st.session)} session"), st
    if svc["security"] is not None and st.unlocked != svc["security"]:
        return _neg(sid, 0x33, "U-33-SERVICE", f"Service 0x{sid:02X} requires security level {svc['security']}"), st
    out = HANDLERS[sid](spec, st, req, sub)
    if out.kind == "positive" and spr:
        out = Outcome("suppressed", [], None, "U-SPR", "Positive response suppressed (bit 7 of the sub-function is set)")
    return out, st


# ------------------------------------------------------------------ service handlers
def _h10(spec, st, req, sub):
    if len(req) != 2:
        return _neg(0x10, 0x13, "U-13-LEN", "DiagnosticSessionControl needs exactly 2 bytes")
    t = spec["timing"]
    if sub != st.session:
        st.unlocked, st.seed_level = None, None      # a session change locks security access
    st.session = sub
    p2, p2s = t["p2_ms"], t["p2star_ms"] // 10
    return _pos(["50", h(sub), h(p2 >> 8), h(p2 & 255), h(p2s >> 8), h(p2s & 255)], "U-POS-10",
                f"Session changes to {_sess_name(spec, sub)}")


def _h11(spec, st, req, sub):
    if len(req) != 2:
        return _neg(0x11, 0x13, "U-13-LEN", "ECUReset needs exactly 2 bytes")
    st.session, st.unlocked, st.seed_level = 1, None, None
    return _pos(["51", h(sub)], "U-POS-11", "ECU resets; session returns to default and security locks")


def _h3e(spec, st, req, sub):
    if len(req) != 2:
        return _neg(0x3E, 0x13, "U-13-LEN", "TesterPresent needs exactly 2 bytes")
    return _pos(["7E", "00"], "U-POS-3E", "TesterPresent acknowledged")


def _h85(spec, st, req, sub):
    if len(req) != 2:
        return _neg(0x85, 0x13, "U-13-LEN", "ControlDTCSetting needs exactly 2 bytes")
    return _pos(["C5", h(sub)], "U-POS-85", "DTC setting changed")


def _h19(spec, st, req, sub):
    if len(req) != 3:
        return _neg(0x19, 0x13, "U-13-LEN", "ReadDTCInformation (this sub-function) needs exactly 3 bytes")
    return _pos(["59", h(sub), "??", "??", "??", "??"] if sub == 1 else ["59", h(sub), "??", "*"], "U-POS-19", "DTC information returned")


def _h14(spec, st, req, sub):
    if len(req) != 4:
        return _neg(0x14, 0x13, "U-13-LEN", "ClearDiagnosticInformation needs exactly 4 bytes")
    group = (req[1] << 16) | (req[2] << 8) | req[3]
    if group != 0xFFFFFF and group not in {d["code"] for d in spec["dtcs"]}:
        return _neg(0x14, 0x31, "U-31-DTC", f"DTC group 0x{group:06X} is not supported")
    return _pos(["54"], "U-POS-14", "Diagnostic information cleared")


def _did(spec, did):
    return next((d for d in spec["dids"] if d["did"] == did), None)


def _sec_fail(st, need, sid, what):
    if need is not None and st.unlocked != need:
        return _neg(sid, 0x33, "U-33-SECURITY", f"{what} requires security level {need}, currently "
                    + ("locked" if st.unlocked is None else f"level {st.unlocked}"))
    return None


def _h22(spec, st, req, sub):
    if len(req) < 3 or (len(req) - 1) % 2:
        return _neg(0x22, 0x13, "U-13-LEN", "ReadDataByIdentifier needs 1 or more 2-byte DIDs")
    tokens = ["62"]
    for i in range(1, len(req), 2):
        did = (req[i] << 8) | req[i + 1]
        d = _did(spec, did)
        if d is None or not d["read"]:
            return _neg(0x22, 0x31, "U-31-DID", f"DID 0x{did:04X} is " + ("unknown" if d is None else "not readable"))
        if st.session not in d["sessions"]:
            return _neg(0x22, 0x31, "U-31-DID-SESSION", f"DID 0x{did:04X} is not available in the {_sess_name(spec, st.session)} session")
        fail = _sec_fail(st, d["security"], 0x22, f"DID 0x{did:04X}")
        if fail:
            return fail
        tokens += [h(did >> 8), h(did & 255)] + ["??"] * d["length"]
    return _pos(tokens, "U-POS-22", "Data returned")


def _h2e(spec, st, req, sub):
    if len(req) < 4:
        return _neg(0x2E, 0x13, "U-13-LEN", "WriteDataByIdentifier needs a DID and at least 1 data byte")
    did = (req[1] << 8) | req[2]
    d = _did(spec, did)
    if d is None or not d["write"]:
        return _neg(0x2E, 0x31, "U-31-DID", f"DID 0x{did:04X} is " + ("unknown" if d is None else "not writable"))
    if st.session not in d["sessions"]:
        return _neg(0x2E, 0x31, "U-31-DID-SESSION", f"DID 0x{did:04X} is not available in the {_sess_name(spec, st.session)} session")
    if len(req) - 3 != d["length"]:
        return _neg(0x2E, 0x13, "U-13-DID-LEN", f"DID 0x{did:04X} has {d['length']} data byte(s), request carries {len(req) - 3}")
    fail = _sec_fail(st, d["security"], 0x2E, f"DID 0x{did:04X}")
    if fail:
        return fail
    if d["range"]:
        val = int.from_bytes(bytes(req[3:]), "big")
        if not d["range"][0] <= val <= d["range"][1]:
            return _neg(0x2E, 0x31, "U-31-RANGE", f"Value {val} is outside the allowed range {d['range'][0]}-{d['range'][1]}")
    return _pos(["6E", h(did >> 8), h(did & 255)], "U-POS-2E", "Data written")


def _h31(spec, st, req, sub):
    if len(req) < 4:
        return _neg(0x31, 0x13, "U-13-LEN", "RoutineControl needs a 2-byte routine identifier")
    rid = (req[2] << 8) | req[3]
    r = next((x for x in spec["routines"] if x["rid"] == rid), None)
    if r is None:
        return _neg(0x31, 0x31, "U-31-RID", f"Routine 0x{rid:04X} is unknown")
    if sub not in r["controls"]:
        return _neg(0x31, 0x12, "U-12-RID-CONTROL", f"Routine 0x{rid:04X} does not support control type 0x{sub:02X}")
    if st.session not in r["sessions"]:
        return _neg(0x31, 0x31, "U-31-RID-SESSION", f"Routine 0x{rid:04X} is not available in the {_sess_name(spec, st.session)} session")
    if len(req) - 4 != r["option_length"]:
        return _neg(0x31, 0x13, "U-13-RID-LEN", f"Routine 0x{rid:04X} expects {r['option_length']} option byte(s), request carries {len(req) - 4}")
    fail = _sec_fail(st, r["security"], 0x31, f"Routine 0x{rid:04X}")
    if fail:
        return fail
    return _pos(["71", h(sub), h(rid >> 8), h(rid & 255), "*"], "U-POS-31", "Routine executed")


def _h27(spec, st, req, sub):
    level = (sub + 1) // 2
    lv = next(l for l in spec["security_levels"] if l["level"] == level)
    if sub % 2:                                                       # requestSeed
        if len(req) != 2:
            return _neg(0x27, 0x13, "U-13-LEN", "requestSeed needs exactly 2 bytes")
        if st.attempts.get(level, 0) >= lv["max_attempts"]:
            return _neg(0x27, 0x37, "U-37-DELAY", "Too many failed attempts; required time delay has not expired")
        if st.unlocked == level:
            return _pos(["67", h(sub)] + ["00"] * lv["seed_length"], "U-POS-27-UNLOCKED", "Already unlocked: the seed is all zeros")
        st.seed_level = level
        return _pos(["67", h(sub)] + ["??"] * lv["seed_length"], "U-POS-27-SEED", f"Seed issued for level {level}")
    key = req[2:]                                                    # sendKey
    klen = lv["seed_length"] if len(key) == 1 and isinstance(key[0], str) else len(key)
    if klen != lv["seed_length"]:
        return _neg(0x27, 0x13, "U-13-KEYLEN", f"Key must be {lv['seed_length']} byte(s)")
    if st.seed_level != level:
        return _neg(0x27, 0x24, "U-24-SEQUENCE", "sendKey without a preceding requestSeed for this level")
    st.seed_level = None
    if key == ["<KEY>"]:
        st.unlocked, st.attempts[level] = level, 0
        return _pos(["67", h(sub)], "U-POS-27-KEY", f"Correct key: level {level} unlocked")
    st.attempts[level] = st.attempts.get(level, 0) + 1
    if st.attempts[level] >= lv["max_attempts"]:
        return _neg(0x27, 0x36, "U-36-ATTEMPTS", f"Wrong key; attempt limit ({lv['max_attempts']}) reached")
    return _neg(0x27, 0x35, "U-35-KEY", "Wrong key")


HANDLERS = {0x10: _h10, 0x11: _h11, 0x14: _h14, 0x19: _h19, 0x22: _h22, 0x27: _h27, 0x2E: _h2e, 0x31: _h31,
            0x3E: _h3e, 0x85: _h85}
