"""OBD-II readings from an ELM327 adapter (Phase C), decoded on the server.

The app is a pipe: it sends a fixed list of commands to the adapter over Bluetooth and posts the raw text answers here.
Decoding in one place (and in Python, where it is tested with recorded answers) keeps the phone code small and lets the
same decoder serve the Android app, Chrome's Web Bluetooth and the demo adapter.

  ATI     adapter version          ATRV  voltage at the OBD port (the battery, even with the engine off)
  ATDPN   protocol in use          0101  engine light (MIL) on, number of stored codes
  03      stored fault codes       07    pending codes (seen once, not confirmed)      0A  permanent codes
  0105    coolant temperature      010C  engine speed       0142  control module voltage
  0121    km driven with the engine light on                0131  km since the codes were cleared

Answers vary by car and adapter: echo on or off, spaces or none, "SEARCHING...", several control units answering, CAN
multi-frame answers ("00A / 0: 43 04 ... / 1: ...") and the older protocols' fixed three-code frames padded with zeros.
Every answer is decoded on its own; an answer that is missing or unreadable leaves that value unknown."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

COMMANDS = ("ATI", "ATRV", "ATDPN", "0101", "03", "07", "0A", "0105", "010C", "0142", "0121", "0131")
MAX_ANSWER = 1200
ERRORS = ("NO DATA", "UNABLE TO CONNECT", "CAN ERROR", "BUS ERROR", "BUS BUSY", "STOPPED", "FB ERROR", "DATA ERROR",
          "BUFFER FULL", "ERROR", "?", "ACT ALERT", "LV RESET", "<RX ERROR")
PROTOCOLS = {"1": "SAE J1850 PWM", "2": "SAE J1850 VPW", "3": "ISO 9141-2", "4": "ISO 14230-4 KWP (5 baud init)",
             "5": "ISO 14230-4 KWP (fast init)", "6": "ISO 15765-4 CAN (11 bit, 500 kbit/s)", "7": "ISO 15765-4 CAN (29 bit, 500 kbit/s)",
             "8": "ISO 15765-4 CAN (11 bit, 250 kbit/s)", "9": "ISO 15765-4 CAN (29 bit, 250 kbit/s)", "A": "SAE J1939 CAN",
             "B": "User CAN 1", "C": "User CAN 2"}
CAN = set("6789ABC")


class ObdError(ValueError):
    pass


def lines(raw: str, cmd: str) -> list[str]:
    """The answer's lines without the prompt, the echo of the command and the adapter's progress messages."""
    text = (raw or "").replace("\r", "\n").replace(">", "\n")
    out = []
    for ln in (x.strip() for x in text.split("\n")):
        if not ln:
            continue
        up = ln.upper()
        if up.replace(" ", "") == cmd.upper() or up.startswith("SEARCHING") or up.startswith("BUS INIT"):
            continue
        out.append(ln)
    return out


def error_of(ls: list[str]) -> str | None:
    for ln in ls:
        up = ln.upper().rstrip(".")
        for e in ERRORS:
            if up == e or (len(e) > 1 and up.startswith(e)):
                return e
    return None


def messages(ls: list[str]) -> list[list[int]]:
    """Byte messages, one per control unit; CAN multi-frame answers are put back together."""
    msgs: list[list[int]] = []
    frame: list[int] | None = None
    size = 0
    for ln in ls:
        s = ln.replace(" ", "").upper()
        if re.fullmatch(r"[0-9A-F]{3}", s):                                     # multi-frame: total byte count first
            if frame is not None:
                msgs.append(frame[:size])
            frame, size = [], int(s, 16)
            continue
        m = re.fullmatch(r"([0-9A-F]):([0-9A-F]+)", s)
        if m and frame is not None:
            frame += _hex(m.group(2))
            continue
        if frame is not None:
            msgs.append(frame[:size])
            frame = None
        if re.fullmatch(r"([0-9A-F]{2})+", s):
            msgs.append(_hex(s))
    if frame is not None:
        msgs.append(frame[:size])
    return msgs


def _hex(s: str) -> list[int]:
    return [int(s[i:i + 2], 16) for i in range(0, len(s) - 1, 2)]


def pid(raw: str, cmd: str) -> list[int] | None:
    """Data bytes of a mode 01 answer (e.g. 0105 -> [A]); with several units answering, the first one."""
    want = [0x41, int(cmd[2:], 16)]
    for m in messages(lines(raw, cmd)):
        if m[:2] == want and len(m) > 2:
            return m[2:]
    return None


def dtc(b1: int, b2: int) -> str:
    return f"{'PCBU'[b1 >> 6]}{(b1 >> 4) & 3}{b1 & 0xF:X}{b2 >> 4:X}{b2 & 0xF:X}"


def codes(raw: str, cmd: str, can: bool | None) -> list[str]:
    """Fault codes from a mode 03 / 07 / 0A answer. CAN answers carry a count byte; older protocols send frames of three
    code slots padded with zeros."""
    mode = 0x40 + int(cmd, 16)
    out: list[str] = []
    for m in messages(lines(raw, cmd)):
        if not m or m[0] != mode:
            continue
        body = m[1:]
        has_count = can if can is not None else len(body) % 2 == 1
        if has_count and body:
            n, body = body[0], body[1:]
            body = body[: 2 * n]
        for i in range(0, len(body) - 1, 2):
            if body[i] or body[i + 1]:
                c = dtc(body[i], body[i + 1])
                if c not in out:
                    out.append(c)
    return out


def decode(answers: dict) -> dict:
    """Raw answers {command: text} -> what they mean. Raises ObdError for answers that can't be taken at all."""
    if not isinstance(answers, dict) or not answers:
        raise ObdError("No answers from the adapter")
    extra = set(answers) - set(COMMANDS)
    if extra:
        raise ObdError(f"Unexpected commands: {', '.join(sorted(extra))[:80]}")
    for k, v in answers.items():
        if not isinstance(v, str) or len(v) > MAX_ANSWER:
            raise ObdError(f"The answer to {k} is too long")
    a = {k: answers.get(k, "") for k in COMMANDS}
    errors = {k: error_of(lines(v, k)) for k, v in a.items() if v and error_of(lines(v, k))}
    dpn = "".join(lines(a["ATDPN"], "ATDPN")).upper().replace(" ", "")
    proto = dpn[-1:] if dpn and dpn[-1:] in PROTOCOLS else ""
    can = (proto in CAN) if proto else None
    adapter = " ".join(lines(a["ATI"], "ATI"))[:60]
    rv = re.search(r"(\d{1,2}(?:\.\d{1,2})?)\s*V", " ".join(lines(a["ATRV"], "ATRV")).upper())
    out = {"adapter": adapter, "protocol": PROTOCOLS.get(proto, ""), "can": can, "errors": errors,
           "mil_on": None, "stored_count": None, "codes": [], "battery_mv": None, "battery_source": None,
           "coolant_c": None, "rpm": None, "engine_running": None, "km_with_mil": None, "km_since_clear": None}
    b = pid(a["0101"], "0101")
    if b:
        out["mil_on"], out["stored_count"] = bool(b[0] & 0x80), b[0] & 0x7F
    for cmd, kind in (("03", "stored"), ("07", "pending"), ("0A", "permanent")):
        for c in codes(a[cmd], cmd, can):
            if not any(x["code"] == c for x in out["codes"]):
                out["codes"].append({"code": c, "kind": kind})
    b = pid(a["0105"], "0105")
    if b:
        out["coolant_c"] = b[0] - 40
    b = pid(a["010C"], "010C")
    if b and len(b) >= 2:
        out["rpm"] = round((256 * b[0] + b[1]) / 4)
        out["engine_running"] = out["rpm"] > 300
    b = pid(a["0142"], "0142")
    if b and len(b) >= 2 and 5000 <= 256 * b[0] + b[1] <= 20000:
        out["battery_mv"], out["battery_source"] = 256 * b[0] + b[1], "car"
    elif rv and 5 <= float(rv.group(1)) <= 20:
        out["battery_mv"], out["battery_source"] = round(float(rv.group(1)) * 1000), "adapter"
    for cmd, key in (("0121", "km_with_mil"), ("0131", "km_since_clear")):
        b = pid(a[cmd], cmd)
        if b and len(b) >= 2:
            out[key] = 256 * b[0] + b[1]
    out["connected"] = any(m and m[0] == 0x40 + int(c[:2], 16) for c in ("0101", "03", "07", "0A", "0105", "010C", "0142")
                           for m in messages(lines(a[c], c)))                    # the car itself answered
    return out


# ---------------------------------------------------------------------------------------------------------------- fault codes
CODE_RE = re.compile(r"^[PCBU][0-3][0-9A-F]{3}$")
FAMILIES = (                                                                 # (pattern, title, short name, drive)
    (r"^P0[0-2]", "Fuel and air mixture fault", "fuel and air mixture", "carefully"),
    (r"^P03", "Ignition or misfire fault", "ignition", "carefully"),
    (r"^P04", "Emission control fault", "emission control", "yes"),
    (r"^P05", "Speed, idle or engine input fault", "engine input", "carefully"),
    (r"^P06", "Engine computer fault", "engine computer", "carefully"),
    (r"^P0[789]", "Gearbox fault", "gearbox", "carefully"),
    (r"^P0[A-C]", "Hybrid or electric drive fault", "hybrid or electric drive", "carefully"),
    (r"^P[13]", "Manufacturer-specific engine or gearbox code", "manufacturer-specific engine or gearbox", "carefully"),
    (r"^P2", "Engine or emissions fault", "engine or emissions", "carefully"),
    (r"^B00", "Airbag or seat belt fault", "airbag", "carefully"),
    (r"^B", "Body fault (lights, locks, seats or comfort)", "body", "yes"),
    (r"^C", "Chassis fault (brakes, ABS, steering or suspension)", "chassis", "carefully"),
    (r"^U", "Network fault (control units not talking to each other)", "network", "carefully"),
)
SEVERITY = {"no": "stop", "carefully": "soon", "yes": "info"}
DRIVE_TEXT = {"no": "Don't drive: get it towed or call roadside assistance.", "carefully": "Drive gently and get it checked soon.",
              "yes": "Safe to drive; get it checked at your next service."}


@lru_cache(maxsize=1)
def _dictionary() -> dict:
    return json.loads((Path(__file__).resolve().parent / "obd_codes.json").read_text(encoding="utf-8"))["codes"]


def normalise_code(code: str) -> str:
    c = re.sub(r"[\s-]", "", str(code or "")).upper()
    if not CODE_RE.match(c):
        raise ObdError(f"{str(code)[:12]!r} isn't a fault code: they look like P0171 (a letter P, C, B or U and four characters)")
    return c


def explain(code: str) -> dict:
    """Plain-language meaning of a fault code; codes not in the dictionary are explained by their family."""
    c = normalise_code(code)
    d = _dictionary().get(c)
    specific = c[1] in "13" or (c[0] in "BCU" and c[1] in "123")
    if d:
        drive = d["drive"]
        out = {"code": c, "title": d["title"], "plain": d["plain"], "causes": d.get("causes", []), "guide": d.get("guide"), "known": True}
    else:
        fam = next((f for f in FAMILIES if re.match(f[0], c)), ("", "Fault", "", "carefully"))
        drive = fam[3]
        out = {"code": c, "title": fam[1], "known": False, "causes": [], "guide": None,
               "plain": (f"{'An' if fam[2][:1] in 'aeiou' else 'A'} {fam[2]} code this app doesn't have a description for. " +
                         ("It is specific to the car's manufacturer: the service centre's diagnostic tool shows what it means."
                          if specific else "The service centre can look it up and test the part."))}
    out.update(drive=drive, severity=SEVERITY[drive], drive_text=DRIVE_TEXT[drive], manufacturer_specific=specific)
    return out
