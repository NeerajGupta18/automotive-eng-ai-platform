"""A simulated ECU driven by the oracle, with injectable defects. It lets the whole pipeline run without hardware
and proves that the generated tests actually detect deviations from the specification."""
from __future__ import annotations

import copy

from app.modules.uds.engine import HAS_SUB, State, respond, tick
from app.modules.uds.spec import NRC

MUTATIONS = {
    "none": "Correct implementation",
    "no_session_restrictions": "Ignores session restrictions on services, DIDs and routines",
    "no_security": "Ignores security access requirements",
    "accept_any_key": "Accepts any key in SecurityAccess",
    "wrong_length_nrc": "Answers length errors with NRC 0x31 instead of 0x13",
    "ignore_spr": "Ignores the suppressPositiveResponse bit",
    "no_range_check": "Accepts out-of-range values in WriteDataByIdentifier",
    "no_attempt_limit": "Never locks out after failed key attempts",
}


def mutate_spec(spec: dict, mutation: str) -> dict:
    s = copy.deepcopy(spec)
    ids = [x["id"] for x in s["sessions"]]
    if mutation == "no_session_restrictions":
        for x in s["services"]:
            x["sessions"] = list(ids)
            for sd in x["subfunctions"]:
                sd["sessions"] = list(ids)
        for x in s["dids"] + s["routines"]:
            x["sessions"] = list(ids)
    elif mutation == "no_security":
        for x in s["services"] + s["dids"] + s["routines"]:
            x["security"] = None
    elif mutation == "no_range_check":
        for d in s["dids"]:
            d["range"] = None
    elif mutation == "no_attempt_limit":
        for l in s["security_levels"]:
            l["max_attempts"] = 10 ** 6
    return s


class SimEcu:
    def __init__(self, spec: dict, mutation: str = "none"):
        if mutation not in MUTATIONS:
            raise ValueError(f"unknown mutation '{mutation}'")
        self.mutation, self.spec = mutation, mutate_spec(spec, mutation)
        self.state, self._n, self.pending_seed = State(), 0, None

    def reset(self):
        self.state, self.pending_seed = State(), None

    def wait(self, ms: int):
        self.state = tick(self.spec, self.state, ms)

    @staticmethod
    def compute_key(level: int, seed: bytes) -> bytes:
        return bytes(b ^ 0xA5 for b in seed)

    def send(self, data: bytes, timeout_ms: int = 50):
        if not data:
            return None
        req: list = list(data)
        if self.mutation == "ignore_spr" and len(req) >= 2 and req[0] in HAS_SUB and req[0] != 0x27:
            req[1] &= 0x7F
        if req[0] == 0x27 and len(req) > 2 and req[1] % 2 == 0:
            lv = next((l for l in self.spec["security_levels"] if l["level"] == req[1] // 2), None)
            if lv and len(req) - 2 == lv["seed_length"]:            # only a key-sized payload is judged as a key
                good = self.pending_seed is not None and bytes(req[2:]) == self.compute_key(req[1] // 2, self.pending_seed)
                req = req[:2] + (["<KEY>"] if good or self.mutation == "accept_any_key" else ["<BADKEY>"])
        out, self.state = respond(self.spec, self.state, req)
        if self.mutation == "wrong_length_nrc" and out.nrc == 0x13:
            return bytes([0x7F, req[0], 0x31])
        if out.kind == "suppressed":
            return None
        rsp = bytearray()
        for i, t in enumerate(out.tokens):
            if t == "*":
                break
            rsp.append(0 if t == "??" else int(t, 16))
        if out.rule == "U-POS-27-SEED":
            self._n += 1
            seed = bytes(((0x3C * self._n + 0x11 * k + 7) % 251) + 1 for k in range(len(rsp) - 2))
            self.pending_seed, rsp[2:] = seed, seed
        return bytes(rsp)
