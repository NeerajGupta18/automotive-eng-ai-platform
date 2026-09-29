"""uds_harness.py - runs generated UDS test cases against an ECU adapter. Standard library only.

Adapter interface (implement for your test bench):
    send(data: bytes, timeout_ms: int) -> bytes | None   # one UDS request, returns the (ISO-TP reassembled) response or None
    reset() -> None                                       # bring the ECU to the default session (power cycle / 10 01 / 11 01)
    wait(ms: int) -> None                                 # let time pass (time.sleep for real ECUs)
    compute_key(level: int, seed: bytes) -> bytes         # your OEM seed-and-key algorithm
"""
from __future__ import annotations

import time


def to_bytes(text: str) -> bytes:
    return bytes(int(t, 16) for t in text.replace(",", " ").split())


def match(expected: list[str], actual: bytes | None) -> bool:
    """Tokens: 2-digit hex, '??' any byte, trailing '*' any tail. An empty expectation means 'no response'."""
    if not expected:
        return actual is None
    if actual is None:
        return False
    tail = expected[-1] == "*"
    exp = expected[:-1] if tail else expected
    if len(actual) < len(exp) or (not tail and len(actual) != len(exp)):
        return False
    return all(e == "??" or int(e, 16) == a for e, a in zip(exp, actual))


def _resolve(ecu, text: str, seed_level: int | None, seed: bytes | None) -> bytes:
    toks = text.split()
    if toks and toks[-1] in ("<KEY>", "<BADKEY>"):
        if seed is None or seed_level is None:
            raise RuntimeError("no seed available for key marker")
        key = ecu.compute_key(seed_level, seed)
        if toks[-1] == "<BADKEY>":
            key = bytes([key[0] ^ 0xFF]) + key[1:]
        return to_bytes(" ".join(toks[:-1])) + key
    return to_bytes(text)


def _send(ecu, data: bytes, timing: dict, margin_ms: int):
    """Sends one request; handles NRC 0x78 (response pending); returns (response, elapsed_ms_until_first_response)."""
    t0 = time.monotonic()
    rsp = ecu.send(data, timing["p2_ms"] + margin_ms)
    first = (time.monotonic() - t0) * 1000
    waited = 0
    while rsp and len(rsp) == 3 and rsp[0] == 0x7F and rsp[2] == 0x78 and waited < 20:
        rsp, waited = ecu.send(b"", timing["p2star_ms"]), waited + 1
    return rsp, first


def prepare(ecu, pre: dict, timing: dict, spec_levels: dict) -> list[str]:
    """Drive the ECU into the precondition: session first, then security access."""
    ecu.reset()
    errs = []
    if pre["session"] != pre.get("default_session", 1):
        rsp, _ = _send(ecu, bytes([0x10, pre["session"]]), timing, 50)
        if not rsp or rsp[0] != 0x50:
            errs.append(f"precondition failed: session 0x{pre['session']:02X} not entered (got {rsp.hex(' ') if rsp else 'no response'})")
    if pre.get("unlock") and not errs:
        lv = pre["unlock"]
        rsp, _ = _send(ecu, bytes([0x27, 2 * lv - 1]), timing, 50)
        if not rsp or rsp[0] != 0x67:
            return errs + ["precondition failed: seed request rejected"]
        rsp2, _ = _send(ecu, bytes([0x27, 2 * lv]) + ecu.compute_key(lv, rsp[2:]), timing, 50)
        if not rsp2 or rsp2[0] != 0x67:
            errs.append("precondition failed: security access not granted")
    return errs


def run_case(ecu, case: dict, timing: dict, p2_margin_ms: int = 20, check_p2: bool = True) -> list[str]:
    """Returns a list of failure messages (empty = pass)."""
    fails = prepare(ecu, case["pre"], timing, {})
    if fails:
        return fails
    seed_level = seed = None
    for i, step in enumerate(case["steps"], start=1):
        if "wait_ms" in step:
            ecu.wait(step["wait_ms"])
            continue
        try:
            data = _resolve(ecu, step["req"], seed_level, seed)
        except RuntimeError as e:
            fails.append(f"step {i} '{step['req']}': {e}")
            break
        rsp, first_ms = _send(ecu, data, timing, p2_margin_ms)
        if len(data) >= 2 and data[0] == 0x27 and data[1] % 2 == 1 and rsp and rsp[0] == 0x67 and any(rsp[2:]):
            seed_level, seed = (data[1] + 1) // 2, rsp[2:]
        if not match(step["expect"], rsp):
            got = rsp.hex(" ").upper() if rsp else "no response"
            fails.append(f"step {i} '{step['req']}': expected {' '.join(step['expect']) or 'no response'}, got {got}  [{step['rule']}]")
        elif check_p2 and rsp and first_ms > timing["p2_ms"] + p2_margin_ms:
            fails.append(f"step {i} '{step['req']}': response after {first_ms:.0f} ms exceeds P2 {timing['p2_ms']} ms")
    return fails


class SocketCanEcu:
    """UNTESTED skeleton for Linux SocketCAN + kernel ISO-TP (modprobe can-isotp). Adapt IDs and the key algorithm."""

    def __init__(self, iface="can0", tx_id=0x7E0, rx_id=0x7E8, key_fn=None):
        import socket
        self.sock = socket.socket(socket.AF_CAN, socket.SOCK_DGRAM, socket.CAN_ISOTP)
        self.sock.bind((iface, rx_id, tx_id))
        self.key_fn = key_fn

    def send(self, data: bytes, timeout_ms: int):
        if data:
            self.sock.send(data)
        self.sock.settimeout(timeout_ms / 1000)
        try:
            return self.sock.recv(4095)
        except OSError:
            return None

    def reset(self):
        self.send(bytes([0x10, 0x01]), 1000)

    def wait(self, ms):
        time.sleep(ms / 1000)

    def compute_key(self, level, seed):
        if not self.key_fn:
            raise NotImplementedError("provide your OEM seed-and-key algorithm")
        return self.key_fn(level, seed)
