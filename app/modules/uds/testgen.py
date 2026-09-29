"""Deterministic test generation. Candidate stimuli are enumerated from the spec and the *oracle* computes the
expected result of every step, so expectations can never drift from the validation rules."""
from __future__ import annotations

from app.modules.uds.engine import HAS_SUB, State, parse_request, respond, tick
from app.modules.uds.spec import NRC

CATEGORIES = ("positive", "negative", "boundary", "security", "session", "timing")


def hx(v: int, n: int = 1) -> str:
    return " ".join(f"{b:02X}" for b in v.to_bytes(n, "big"))


class Gen:
    def __init__(self, spec: dict):
        self.spec, self.cases = spec, []
        self.sids = {s["sid"]: s for s in spec["services"] if s["modelled"]}
        self.sess = [s["id"] for s in spec["sessions"]]
        self.name = {s["id"]: s["name"] for s in spec["sessions"]}
        self.level = {l["level"]: l for l in spec["security_levels"]}

    # ------------------------------------------------------------ building blocks
    def case(self, element, slug, title, category, session, unlock, seq, note=""):
        st = State(session=session, unlocked=unlock)
        steps = []
        for item in seq:
            if isinstance(item, tuple):                        # ("wait", ms)
                st = tick(self.spec, st, item[1])
                steps.append({"wait_ms": item[1]})
                continue
            out, st = respond(self.spec, st, parse_request(item))
            steps.append({"req": item, "expect": out.tokens, "kind": out.kind, "nrc": out.nrc, "rule": out.rule, "why": out.why})
        last = next((s for s in reversed(steps) if "req" in s), None)
        covers = [[element, last["kind"]]] if last else []
        for s_ in steps:                       # every request also exercises its service
            if "req" in s_ and [f"SVC:{int(s_['req'].split()[0], 16):02X}", s_["kind"]] not in covers:
                covers.append([f"SVC:{int(s_['req'].split()[0], 16):02X}", s_["kind"]])
        self.cases.append({"covers": covers, "key": f"{element}:{slug}", "element": element, "title": title, "category": category,
                           "pre": {"session": session, "unlock": unlock}, "steps": steps,
                           "expected_kind": last["kind"] if last else None, "expected_nrc": last["nrc"] if last else None,
                           "note": note})

    def pick_session(self, allowed: list[int], level: int | None = None) -> int | None:
        c = [s for s in allowed if level is None or s in self.level[level]["sessions"]]
        return c[0] if c else None

    def ok_ctx(self, sid, item_sessions, need_level):
        """A session (and unlock level) in which a request is expected to be accepted."""
        s = self.pick_session([x for x in self.sids[sid]["sessions"] if x in item_sessions], need_level)
        return s

    def readable(self):
        return [d for d in self.spec["dids"] if d["read"]]

    def valid_data(self, d, which="mid") -> str:
        n = d["length"]
        if d["range"]:
            lo, hi = d["range"]
            val = {"min": lo, "max": hi, "mid": (lo + hi) // 2}[which]
        else:
            val = 0x5A if which == "mid" else 0
        return hx(val, n) if val < 256 ** n else "00 " * (n - 1) + "00"

    def minimal(self, sid: int) -> str | None:
        s = self.sids[sid]
        subs = [x["id"] for x in s["subfunctions"]]
        if sid in (0x10, 0x11, 0x3E, 0x85):
            return f"{sid:02X} {subs[0]:02X}"
        if sid == 0x14:
            return "14 FF FF FF"
        if sid == 0x19:
            return "19 01 FF"
        if sid == 0x27:
            return f"27 {subs[0]:02X}" if subs else None
        if sid == 0x22:
            r = self.readable()
            return f"22 {hx(r[0]['did'], 2)}" if r else None
        if sid == 0x2E:
            w = [d for d in self.spec["dids"] if d["write"]]
            return f"2E {hx(w[0]['did'], 2)} {self.valid_data(w[0])}" if w else None
        if sid == 0x31:
            r = self.spec["routines"]
            return f"31 {r[0]['controls'][0]:02X} {hx(r[0]['rid'], 2)} " + " ".join(["00"] * r[0]["option_length"]) if r else None
        return None

    def with_sub(self, sid: int, sub: int) -> str | None:
        m = self.minimal(sid)
        if m is None:
            return None
        parts = m.split()
        parts[1] = f"{sub:02X}"
        return " ".join(parts)

    # ------------------------------------------------------------ generators
    def run(self) -> list[dict]:
        self.unsupported_sid()
        for sid, svc in self.sids.items():
            self.generic(sid, svc)
            getattr(self, f"svc_{sid:02X}", lambda s: None)(svc)
        self.dids()
        self.routines()
        self.security()
        self.timing()
        return self.cases

    def unsupported_sid(self):
        for cand in (0x5A, 0x99, 0xB0, 0xA5):
            if cand not in self.sids:
                self.case("SVC:UNSUPPORTED", "sid", f"Unsupported service 0x{cand:02X}", "negative", self.sess[0], None, [f"{cand:02X} 01"])
                return

    def generic(self, sid, svc):
        el, req = f"SVC:{sid:02X}", self.minimal(sid)
        if req is None:
            return
        for s in self.sess:
            if s not in svc["sessions"]:
                self.case(el, f"denied-in-{self.name[s]}", f"0x{sid:02X} {svc['name']} rejected in {self.name[s]} session", "session", s, None, [req])
        if sid in HAS_SUB:
            self.case(el, "missing-sub", f"0x{sid:02X} without sub-function", "negative", svc["sessions"][0], None, [f"{sid:02X}"])
            ids = {x["id"] for x in svc["subfunctions"]}
            bad = next((i for i in (0x7E, 0x55, 0x2A, 0x0F) if i not in ids and (i | 0x80) not in ids), None)
            ctx = self.with_sub(sid, bad) if bad is not None else None
            if ctx:
                self.case(el, "unsupported-sub", f"0x{sid:02X} unsupported sub-function 0x{bad:02X}", "negative", svc["sessions"][0], None, [ctx])
        if sid in (0x10, 0x11, 0x3E, 0x85, 0x14, 0x19):
            s0 = svc["sessions"][0]
            self.case(el, "too-long", f"0x{sid:02X} with an extra byte", "boundary", s0, None, [req + " 00"])
            if sid in (0x14, 0x19):
                self.case(el, "too-short", f"0x{sid:02X} with a byte missing", "boundary", s0, None, [" ".join(req.split()[:-1])])

    def svc_10(self, svc):
        el = "SVC:10"
        for sd in svc["subfunctions"]:
            for frm in sd["sessions"]:
                if frm != sd["id"]:
                    self.case(el, f"{self.name[frm]}-to-{self.name[sd['id']]}", f"Session {self.name[frm]} -> {self.name[sd['id']]}", "session", frm, None, [f"10 {sd['id']:02X}"])
        s = svc["subfunctions"][-1]
        self.case(el, "spr", f"10 {s['id'] | 0x80:02X}: positive response suppressed", "positive", s["sessions"][0], None, [f"10 {s['id'] | 0x80:02X}"])

    def svc_11(self, svc):
        el, s0 = "SVC:11", svc["sessions"][0]
        for sd in svc["subfunctions"]:
            self.case(el, f"reset-{sd['id']:02X}", f"ECU reset 0x{sd['id']:02X}", "positive", s0, None, [f"11 {sd['id']:02X}"])
        self.case(el, "spr", "ECUReset with suppressed positive response", "positive", s0, None, [f"11 {svc['subfunctions'][0]['id'] | 0x80:02X}"])

    def svc_3E(self, svc):
        el, s0 = "SVC:3E", svc["sessions"][0]
        self.case(el, "ok", "TesterPresent", "positive", s0, None, ["3E 00"])
        self.case(el, "spr", "TesterPresent with suppressed positive response", "positive", s0, None, ["3E 80"])
        self.case(el, "too-long-2", "TesterPresent with two extra bytes", "boundary", s0, None, ["3E 00 00 00"])

    def svc_85(self, svc):
        for sd in svc["subfunctions"]:
            self.case("SVC:85", f"sub-{sd['id']:02X}", f"ControlDTCSetting {sd['name']}", "positive", svc["sessions"][0], None, [f"85 {sd['id']:02X}"])
        self.case("SVC:85", "spr", "ControlDTCSetting with suppressed positive response", "positive", svc["sessions"][0], None, [f"85 {svc['subfunctions'][0]['id'] | 0x80:02X}"])

    def svc_19(self, svc):
        s0 = svc["sessions"][0]
        for sd in svc["subfunctions"]:
            self.case("SVC:19", f"sub-{sd['id']:02X}", f"ReadDTCInformation {sd['name']}", "positive", s0, None, [f"19 {sd['id']:02X} FF"])

    def svc_14(self, svc):
        s0 = svc["sessions"][0]
        self.case("SVC:14", "all", "Clear all DTCs", "positive", s0, None, ["14 FF FF FF"])
        for d in self.spec["dtcs"]:
            el = f"DTC:{d['code']:06X}"
            self.case(el, "clear", f"Clear DTC 0x{d['code']:06X} ({d['name']})", "positive", s0, None, [f"14 {hx(d['code'], 3)}"])
            denied = next((x for x in self.sess if x not in svc["sessions"]), None)
            if denied is not None:
                self.case(el, "clear-denied", f"Clear DTC 0x{d['code']:06X} in {self.name[denied]} session", "session", denied, None, [f"14 {hx(d['code'], 3)}"])
            else:
                self.case(el, "clear-long", f"Clear DTC 0x{d['code']:06X} with an extra byte", "boundary", s0, None, [f"14 {hx(d['code'], 3)} 00"])
        used = {d["code"] for d in self.spec["dtcs"]}
        bad = next(c for c in (0x123456, 0x654321, 0x0A0B0C) if c not in used)
        self.case("SVC:14", "unknown-group", "Clear unknown DTC group", "negative", s0, None, [f"14 {hx(bad, 3)}"])

    def dids(self):
        spec = self.spec
        s22, s2e = self.sids.get(0x22), self.sids.get(0x2E)
        used = {d["did"] for d in spec["dids"]}
        if s22:
            s0 = s22["sessions"][0]
            bad = next(x for x in (0xF1FF, 0xF1FE, 0x0FFF) if x not in used)
            self.case("SVC:22", "unknown-did", "Read unknown DID", "negative", s0, None, [f"22 {hx(bad, 2)}"])
            self.case("SVC:22", "odd-length", "Read with a truncated DID", "boundary", s0, None, [f"22 {hx(bad, 2).split()[0]}"])
            r = [d for d in self.readable() if d["security"] is None and s0 in d["sessions"]]
            if len(r) >= 2:
                self.case("SVC:22", "multi-ok", "Read two DIDs in one request", "positive", s0, None, [f"22 {hx(r[0]['did'], 2)} {hx(r[1]['did'], 2)}"])
                self.case("SVC:22", "multi-one-bad", "Read one valid and one unknown DID", "negative", s0, None, [f"22 {hx(r[0]['did'], 2)} {hx(bad, 2)}"])
        for d in spec["dids"]:
            el, did = f"DID:{d['did']:04X}", hx(d["did"], 2)
            if d["read"] and s22:
                s = self.ok_ctx(0x22, d["sessions"], d["security"])
                if s is not None:
                    self.case(el, "read", f"Read DID 0x{d['did']:04X} {d['name']}", "positive", s, d["security"], [f"22 {did}"])
                    if d["security"] is not None:
                        self.case(el, "read-locked", f"Read DID 0x{d['did']:04X} without security access", "security", s, None, [f"22 {did}"])
                for x in s22["sessions"]:
                    if x not in d["sessions"]:
                        self.case(el, f"read-in-{self.name[x]}", f"Read DID 0x{d['did']:04X} in {self.name[x]} session", "session", x, None, [f"22 {did}"])
            if s2e:
                if not d["write"]:
                    if d["read"]:
                        self.case(el, "write-readonly", f"Write to read-only DID 0x{d['did']:04X}", "negative", s2e["sessions"][0], None, [f"2E {did} {self.valid_data(d)}"])
                    continue
                s = self.ok_ctx(0x2E, d["sessions"], d["security"])
                if s is None:
                    continue
                u, n = d["security"], d["length"]
                self.case(el, "write", f"Write DID 0x{d['did']:04X} {d['name']}", "positive", s, u, [f"2E {did} {self.valid_data(d)}"])
                self.case(el, "write-short", f"Write DID 0x{d['did']:04X} with too few data bytes", "boundary", s, u, [f"2E {did} " + " ".join(["00"] * (n - 1))] if n > 1 else [f"2E {did}"])
                self.case(el, "write-long", f"Write DID 0x{d['did']:04X} with too many data bytes", "boundary", s, u, [f"2E {did} " + " ".join(["00"] * (n + 1))])
                if u is not None:
                    self.case(el, "write-locked", f"Write DID 0x{d['did']:04X} without security access", "security", s, None, [f"2E {did} {self.valid_data(d)}"])
                if d["range"]:
                    lo, hi = d["range"]
                    self.case(el, "write-min", f"Write minimum value {lo}", "boundary", s, u, [f"2E {did} {self.valid_data(d, 'min')}"])
                    self.case(el, "write-max", f"Write maximum value {hi}", "boundary", s, u, [f"2E {did} {self.valid_data(d, 'max')}"])
                    if lo > 0:
                        self.case(el, "write-below", f"Write {lo - 1} (below range)", "boundary", s, u, [f"2E {did} {hx(lo - 1, n)}"])
                    if hi + 1 < 256 ** n:
                        self.case(el, "write-above", f"Write {hi + 1} (above range)", "boundary", s, u, [f"2E {did} {hx(hi + 1, n)}"])
                for x in s2e["sessions"]:
                    if x not in d["sessions"]:
                        self.case(el, f"write-in-{self.name[x]}", f"Write DID 0x{d['did']:04X} in {self.name[x]} session", "session", x, None, [f"2E {did} {self.valid_data(d)}"])

    def routines(self):
        svc = self.sids.get(0x31)
        if not svc:
            return
        used = {r["rid"] for r in self.spec["routines"]}
        s0 = svc["sessions"][0]
        bad = next(x for x in (0xFFFE, 0xABCD, 0x1234) if x not in used)
        self.case("SVC:31", "unknown-rid", "Unknown routine identifier", "negative", s0, None, [f"31 01 {hx(bad, 2)}"])
        self.case("SVC:31", "too-short", "RoutineControl without routine identifier", "boundary", s0, None, ["31 01"])
        for r in self.spec["routines"]:
            el, rid, n = f"RID:{r['rid']:04X}", hx(r["rid"], 2), r["option_length"]
            opts = lambda k: " ".join(["00"] * k)
            s = self.ok_ctx(0x31, r["sessions"], r["security"])
            if s is None:
                continue
            u = r["security"]
            for c in r["controls"]:
                self.case(el, f"control-{c:02X}", f"Routine 0x{r['rid']:04X} {r['name']} control 0x{c:02X}", "positive", s, u, [f"31 {c:02X} {rid} {opts(n)}".strip()])
            for c in (1, 2, 3):
                if c not in r["controls"]:
                    self.case(el, f"unsupported-control-{c:02X}", f"Routine 0x{r['rid']:04X} unsupported control 0x{c:02X}", "negative", s, u, [f"31 {c:02X} {rid} {opts(n)}".strip()])
                    break
            self.case(el, "option-long", f"Routine 0x{r['rid']:04X} with an extra option byte", "boundary", s, u, [f"31 {r['controls'][0]:02X} {rid} {opts(n + 1)}"])
            if n:
                self.case(el, "option-short", f"Routine 0x{r['rid']:04X} with a missing option byte", "boundary", s, u, [f"31 {r['controls'][0]:02X} {rid} {opts(n - 1)}".strip()])
            if u is not None:
                self.case(el, "locked", f"Routine 0x{r['rid']:04X} without security access", "security", s, None, [f"31 {r['controls'][0]:02X} {rid} {opts(n)}".strip()])
            for x in svc["sessions"]:
                if x not in r["sessions"]:
                    self.case(el, f"in-{self.name[x]}", f"Routine 0x{r['rid']:04X} in {self.name[x]} session", "session", x, None, [f"31 {r['controls'][0]:02X} {rid} {opts(n)}".strip()])

    def security(self):
        svc = self.sids.get(0x27)
        if not svc:
            return
        for L, lv in self.level.items():
            el, s, sd, kd = f"SEC:L{L}", lv["sessions"][0], f"27 {2 * L - 1:02X}", f"27 {2 * L:02X}"
            self.case(el, "seed", f"Level {L}: request seed", "security", s, None, [sd])
            self.case(el, "unlock", f"Level {L}: unlock with the correct key", "security", s, None, [sd, f"{kd} <KEY>"])
            self.case(el, "wrong-key", f"Level {L}: wrong key is rejected", "security", s, None, [sd, f"{kd} <BADKEY>"])
            self.case(el, "key-no-seed", f"Level {L}: key without a seed request", "security", s, None, [f"{kd} " + " ".join(["00"] * lv["seed_length"])])
            self.case(el, "key-length", f"Level {L}: key of wrong length", "boundary", s, None, [sd, f"{kd} 00"])
            self.case(el, "seed-unlocked", f"Level {L}: seed request while unlocked returns zeros", "security", s, None, [sd, f"{kd} <KEY>", sd])
            self.case(el, "seed-long", f"Level {L}: seed request with an extra byte", "boundary", s, None, [sd + " 00"])
            self.case(el, "spr-unsupported", f"Level {L}: suppress bit is not supported", "negative", s, None, [f"27 {(2 * L - 1) | 0x80:02X}"])
            seq = []
            for _ in range(lv["max_attempts"]):
                seq += [sd, f"{kd} <BADKEY>"]
            self.case(el, "lockout", f"Level {L}: attempt limit ({lv['max_attempts']}) and delay", "security", s, None, seq + [sd])
            for x in self.sess:
                if x not in lv["sessions"]:
                    self.case(el, f"seed-in-{self.name[x]}", f"Level {L}: seed request in {self.name[x]} session", "session", x, None, [sd])
            prot = next((f"22 {hx(d['did'], 2)}" for d in self.readable() if d["security"] == L and s in d["sessions"]), None)
            if prot and 0x10 in self.sids:
                back = self.sess[0]
                self.case(el, "relock", f"Level {L}: session change locks security access", "security", s, None,
                          [sd, f"{kd} <KEY>", f"10 {back:02X}", f"10 {s:02X}", prot])

    def timing(self):
        s3 = self.spec["timing"]["s3_ms"]
        if 0x10 not in self.sids:
            return
        for ext in [x for x in self.sess if x != self.sess[0]]:
            probe = None
            for sid in self.sids:                    # a request that works in this session but not in default
                req = self.minimal(sid) if sid not in (0x10, 0x27, 0x31, 0x2E, 0x22) else None
                if not req:
                    continue
                a, _ = respond(self.spec, State(session=ext), parse_request(req))
                b, _ = respond(self.spec, State(session=self.sess[0]), parse_request(req))
                if a.kind == "positive" and b.kind == "negative":
                    probe = req
                    break
            if not probe:
                continue
            self.case("TIMING:S3", "timeout", f"S3 timeout ({s3} ms) returns the ECU to the default session", "timing", ext, None, [("wait", s3 + 500), probe])
            self.case("TIMING:S3", "within", "Session persists while S3 has not expired", "timing", ext, None, [("wait", int(s3 * 0.6)), probe])
            if 0x3E in self.sids:
                self.case("TIMING:S3", "tester-present", "TesterPresent keeps the session alive", "timing", ext, None,
                          [("wait", int(s3 * 0.6)), "3E 00", ("wait", int(s3 * 0.6)), probe])
            return


def generate(spec: dict) -> list[dict]:
    seen: set[str] = set()
    out = []
    for c in Gen(spec).run():
        if c["key"] not in seen:
            seen.add(c["key"])
            out.append(c)
    return out


# ------------------------------------------------------------------ coverage
def spec_elements(spec: dict) -> list[str]:
    els = [f"SVC:{s['sid']:02X}" for s in spec["services"] if s["modelled"]]
    els += [f"DID:{d['did']:04X}" for d in spec["dids"]] + [f"RID:{r['rid']:04X}" for r in spec["routines"]]
    els += [f"SEC:L{l['level']}" for l in spec["security_levels"]] + [f"DTC:{d['code']:06X}" for d in spec["dtcs"]]
    return els


def applicable_nrcs(spec: dict) -> set[int]:
    n = {0x11, 0x13}
    all_s = {s["id"] for s in spec["sessions"]}
    if any(set(s["sessions"]) != all_s for s in spec["services"]):
        n.add(0x7F)
    if any(s["subfunctions"] for s in spec["services"]):
        n.add(0x12)
    if any(set(x["sessions"]) != set(s["sessions"]) for s in spec["services"] for x in s["subfunctions"]):
        n.add(0x7E)
    if spec["dids"] or spec["routines"] or spec["dtcs"]:
        n.add(0x31)
    if spec["security_levels"]:
        n |= {0x33, 0x35, 0x36, 0x37, 0x24}
    return n


def coverage(spec: dict, tests: list[dict]) -> dict:
    live = [t for t in tests if t["status"] != "rejected"]
    rows = []
    for el in spec_elements(spec):
        mine = [t for t in live if any(c[0] == el for c in t["payload"]["covers"])]
        kinds = {c[1] for t in mine for c in t["payload"]["covers"] if c[0] == el}
        pos, neg = bool(kinds & {"positive", "suppressed"}), "negative" in kinds
        rows.append({"element": el, "tests": len(mine), "approved": sum(t["status"] == "approved" for t in mine),
                     "positive": pos, "negative": neg, "covered": pos and neg})
    exercised = {t["payload"]["expected_nrc"] for t in live if t["payload"]["expected_nrc"]}
    for t in live:
        exercised |= {s["nrc"] for s in t["payload"]["steps"] if s.get("nrc")}
    need = applicable_nrcs(spec)
    return {"elements": rows, "covered": sum(r["covered"] for r in rows), "total": len(rows),
            "coverage": round(sum(r["covered"] for r in rows) / len(rows), 3) if rows else 0.0,
            "nrc_applicable": [f"0x{n:02X} {NRC[n]}" for n in sorted(need)],
            "nrc_exercised": [f"0x{n:02X} {NRC[n]}" for n in sorted(exercised & need)],
            "nrc_missing": [f"0x{n:02X} {NRC[n]}" for n in sorted(need - exercised)]}
