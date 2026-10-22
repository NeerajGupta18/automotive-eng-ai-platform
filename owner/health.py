"""Car health (Phase C): one status per car, with the reasons and what to do about each.

Inputs: the latest OBD-II reading (from an adapter, the demo adapter, or fault codes typed in from a garage's scan), the
battery voltage, the coolant temperature, the mileage trend from the fuel log, the service reminders, and the insurance
and PUC. Each finding has a level: red (needs attention now), amber (check soon) or info. The car's status is the worst
level: "Needs attention", "Check soon" or "Good". There is deliberately no score out of 100: a number would suggest a
precision the inputs don't have.

Battery thresholds (12 V lead-acid): resting 12.4 V and above is charged, 12.0 to 12.4 V is low, below 12.0 V is flat
enough to fail a start; with the engine running the alternator should give 13.2 to 15.0 V."""
from __future__ import annotations

import json
from datetime import date, datetime

import sqlalchemy as sa

from owner import costs as K
from owner import db as T
from owner import obd
from owner.db import now_precise
from owner.odometer import car_km_per_day
from owner.service import Conflict, Invalid, NotFound, OwnerService, _iso, _row, _uid

STALE_DAYS = 60
LEVEL = {"red": 0, "amber": 1, "info": 2}
STATUS = {"red": "Needs attention", "amber": "Check soon", "green": "Good"}


def battery_finding(mv: int | None, running: bool | None) -> dict | None:
    if mv is None:
        return None
    v = mv / 1000
    if running:
        if v < 13.2:
            return {"level": "red", "kind": "charging", "title": f"Battery not charging ({v:.1f} V with the engine running)",
                    "detail": "The alternator should give 13.2 to 15.0 V. The car may stop when the battery runs down: get the alternator and belt checked.",
                    "action": {"type": "guide", "target": "battery-weak"}}
        if v > 15.2:
            return {"level": "amber", "kind": "charging", "title": f"Charging voltage high ({v:.1f} V)",
                    "detail": "Over-charging can damage the battery and electronics: get the alternator's regulator checked.", "action": None}
        return None
    if v < 12.0:
        return {"level": "red", "kind": "battery", "title": f"Battery very low ({v:.1f} V)",
                "detail": "It may not start the car. Charge it or get it tested; batteries usually last 3 to 5 years.",
                "action": {"type": "guide", "target": "battery-weak"}}
    if v < 12.4:
        return {"level": "amber", "kind": "battery", "title": f"Battery low ({v:.1f} V)",
                "detail": "A charged battery rests at 12.4 V or more. Short trips, a parked car or an old battery can cause this.",
                "action": {"type": "guide", "target": "battery-weak"}}
    return None


def scan_findings(scan: dict, today: date) -> list[dict]:
    out = []
    codes = json.loads(scan["codes"]) if isinstance(scan["codes"], str) else scan["codes"]
    for c in codes:
        e = obd.explain(c["code"])
        if c["kind"] == "pending":
            out.append({"level": "info", "kind": "code", "title": f"{e['code']}: {e['title']} (seen once)",
                        "detail": "The car noticed this once. If it happens again it becomes a stored fault and the engine light comes on.",
                        "action": {"type": "code", "target": e["code"]}})
        else:
            out.append({"level": "red" if e["severity"] == "stop" else "amber",            # a stored code is always worth a look
                        "kind": "code", "title": f"{e['code']}: {e['title']}", "detail": f"{e['plain']} {e['drive_text']}",
                        "action": {"type": "code", "target": e["code"]}})
    if scan["mil_on"] and not [c for c in codes if c["kind"] != "pending"]:
        out.append({"level": "amber", "kind": "mil", "title": "Engine warning light is on",
                    "detail": "The car reports its engine light on but gave no code to this adapter. Get it read at a workshop.",
                    "action": {"type": "light", "target": "engine"}})
    b = battery_finding(scan["battery_mv"], scan["engine_running"])
    if b:
        out.append(b)
    if scan["engine_running"] and scan["coolant_c"] is not None:
        if scan["coolant_c"] >= 113:
            out.append({"level": "red", "kind": "coolant", "title": f"Engine very hot ({scan['coolant_c']} °C)",
                        "detail": "Stop and let it cool; check the coolant level and the radiator fan.", "action": {"type": "light", "target": "coolant"}})
        elif scan["coolant_c"] >= 106:
            out.append({"level": "amber", "kind": "coolant", "title": f"Engine running hot ({scan['coolant_c']} °C)",
                        "detail": "Normal is about 85 to 105 °C. Check the coolant level and that the radiator fan runs.", "action": None})
    age = (today - scan["scanned_at"].date()).days
    if age > STALE_DAYS:
        out.append({"level": "info", "kind": "stale", "title": f"Last health check {age} days ago",
                    "detail": "Plug in the adapter for a fresh reading.", "action": {"type": "scan"}})
    return out


class HealthService:
    def __init__(self, owner: OwnerService, costs: K.CostService):
        self.o, self.k, self.e = owner, costs, owner.e

    def _scan_out(self, r: dict) -> dict:
        out = {k: _iso(v) for k, v in r.items() if k != "raw"}
        out["codes"] = [{**c, **{k: v for k, v in obd.explain(c["code"]).items() if k != "code"}} for c in json.loads(r["codes"])]
        out["battery_v"] = r["battery_mv"] / 1000 if r["battery_mv"] else None
        return out

    def add_scan(self, user: dict, car_id: str, source: str, answers: dict) -> dict:
        """A reading from the adapter (or the demo adapter): raw answers, decoded here."""
        if source not in ("adapter", "demo"):
            raise Invalid("Source must be adapter or demo")
        self.o.car(user, car_id)
        try:
            d = obd.decode(answers)
        except obd.ObdError as e:
            raise Invalid(str(e)) from e
        if not d["connected"] and d["battery_mv"] is None:
            raise Invalid("The adapter answered, but the car didn't. Switch the ignition on (or start the engine) and try again.")
        return self._save(user, car_id, source, d, json.dumps(answers)[:16000])

    def add_codes(self, user: dict, car_id: str, codes: list, mil_on: bool | None = None) -> dict:
        """Fault codes typed in (from a garage's scan tool or a phone app)."""
        if not isinstance(codes, list) or not 1 <= len(codes) <= 20:
            raise Invalid("Enter 1 to 20 fault codes")
        try:
            clean = list(dict.fromkeys(obd.normalise_code(c) for c in codes))
        except obd.ObdError as e:
            raise Invalid(str(e)) from e
        self.o.car(user, car_id)
        d = {"mil_on": mil_on, "codes": [{"code": c, "kind": "stored"} for c in clean], "battery_mv": None, "engine_running": None,
             "coolant_c": None, "rpm": None, "km_since_clear": None, "km_with_mil": None, "adapter": "", "protocol": ""}
        return self._save(user, car_id, "manual", d, None)

    def _save(self, user: dict, car_id: str, source: str, d: dict, raw: str | None) -> dict:
        sid = _uid()
        with self.e.begin() as c:
            self.o._membership(c, user["id"], car_id)
            n = c.execute(sa.select(sa.func.count()).select_from(T.health_scans).where(T.health_scans.c.car_id == car_id)).scalar()
            if n >= 500:
                raise Conflict("At most 500 health checks per car; delete old ones first")
            c.execute(T.health_scans.insert().values(
                id=sid, car_id=car_id, source=source, scanned_at=now_precise(), mil_on=d["mil_on"], codes=json.dumps(d["codes"]),
                battery_mv=d["battery_mv"], engine_running=d["engine_running"], coolant_c=d["coolant_c"], rpm=d["rpm"],
                km_since_clear=d["km_since_clear"], km_with_mil=d["km_with_mil"], adapter=d["adapter"], protocol=d["protocol"], raw=raw,
                created_by=user["id"]))
        return {"scan": self.scan(user, sid), "health": self.health(user, car_id)}

    def scan(self, user: dict, sid: str) -> dict:
        with self.e.connect() as c:
            r = _row(c.execute(sa.select(T.health_scans).where(T.health_scans.c.id == sid)).first())
            if not r:
                raise NotFound("Health check not found")
            self.o._membership(c, user["id"], r["car_id"])
        return self._scan_out(r)

    def scans(self, user: dict, car_id: str, limit: int = 20) -> list[dict]:
        with self.e.connect() as c:
            self.o._membership(c, user["id"], car_id)
            rows = c.execute(sa.select(T.health_scans).where(T.health_scans.c.car_id == car_id)
                             .order_by(T.health_scans.c.scanned_at.desc()).limit(limit))
            return [self._scan_out(_row(r)) for r in rows]

    def delete_scan(self, user: dict, sid: str) -> None:
        self.k._delete(user, T.health_scans, sid, "Health check")

    def health(self, user: dict, car_id: str) -> dict:
        car = self.o.car(user, car_id)
        today = date.today()
        findings: list[dict] = []
        with self.e.connect() as c:
            last = _row(c.execute(sa.select(T.health_scans).where(T.health_scans.c.car_id == car_id)
                                  .order_by(T.health_scans.c.scanned_at.desc()).limit(1)).first())
            fills = [_row(r) for r in c.execute(sa.select(T.fuel_logs).where(T.fuel_logs.c.car_id == car_id))]
            docs = [_row(r) for r in c.execute(sa.select(T.documents.c.kind, T.documents.c.expires_on).where(T.documents.c.car_id == car_id))]
            kmpd = car_km_per_day(c, car_id, today)
        if last:
            findings += scan_findings(last, today)
        else:
            findings.append({"level": "info", "kind": "no_scan", "title": "No health check yet",
                             "detail": "Plug a Bluetooth OBD-II adapter into the port under the dashboard, or type in the codes a workshop read.",
                             "action": {"type": "scan"}})
        for u in sorted({f["unit"] for f in fills}):
            chk = K.mileage_check(K.mileage_series([f for f in fills if f["unit"] == u]))
            if chk:
                findings.append({"level": "amber", "kind": "mileage", "title": f"Mileage dropped {chk['drop_pct']}% ({chk['latest']} vs usual {chk['usual']} km/{u})",
                                 "detail": "Common causes: low tyre pressure, more city traffic or AC, a dirty air filter, dragging brakes, or an engine fault. "
                                           "If it stays low for two more fills, get it checked.", "action": {"type": "costs"}})
        for item in self.o.reminders(user, car_id):
            if item["type"] == "reminder" and item["status"] in ("overdue", "due_soon"):
                findings.append({"level": "amber" if item["status"] == "overdue" else "info", "kind": "reminder",
                                 "title": f"{item['title']} {'overdue' if item['status'] == 'overdue' else 'due soon'}",
                                 "detail": "", "action": {"type": "reminders"}})
        for d in docs:
            if d["kind"] in ("insurance", "puc") and d["expires_on"] and d["expires_on"] < today:
                name = "Insurance" if d["kind"] == "insurance" else "PUC certificate"
                findings.append({"level": "red" if d["kind"] == "insurance" else "amber", "kind": "papers", "title": f"{name} expired",
                                 "detail": "Driving without valid insurance or PUC can be fined; insurance also protects you in an accident.",
                                 "action": {"type": "documents"}})
        findings.sort(key=lambda f: LEVEL[f["level"]])
        worst = findings[0]["level"] if findings and findings[0]["level"] in ("red", "amber") else "green"
        return {"car_id": car_id, "status": worst, "label": STATUS[worst], "findings": findings,
                "last_scan": self._scan_out(last) if last else None, "km_per_day": kmpd,
                "car": {"fuel": car["fuel"], "odometer_km": car["odometer_km"]}}

    def summary_for(self, user: dict, car_ids: list[str]) -> dict:
        """Status per car for the home screen."""
        out = {}
        for cid in car_ids:
            h = self.health(user, cid)
            out[cid] = {"status": h["status"], "label": h["label"], "top": h["findings"][0]["title"] if h["status"] != "green" else None}
        return out

    def export_rows(self, car_ids: list[str]) -> list[dict]:
        if not car_ids:
            return []
        with self.e.connect() as c:
            return [{k: _iso(v) for k, v in _row(r).items()} for r in c.execute(sa.select(T.health_scans).where(T.health_scans.c.car_id.in_(car_ids)))]


DEMO_ANSWERS = {
    "ATI": "ELM327 v1.5", "ATRV": "12.3V", "ATDPN": "A6", "0101": "41 01 81 07 E1 00", "03": "43 01 01 71", "07": "47 00",
    "0A": "4A 00", "0105": "41 05 46", "010C": "41 0C 00 00", "0142": "41 42 30 0C", "0121": "41 21 00 8C", "0131": "41 31 05 DC"}


def demo_scan(c, car_id: str, uid: str, when: datetime) -> None:
    """The demo Nexon's last health check: P0171 stored, engine off, battery 12.3 V."""
    d = obd.decode(DEMO_ANSWERS)
    c.execute(T.health_scans.insert().values(
        id=_uid(), car_id=car_id, source="demo", scanned_at=when, mil_on=d["mil_on"], codes=json.dumps(d["codes"]), battery_mv=d["battery_mv"],
        engine_running=d["engine_running"], coolant_c=d["coolant_c"], rpm=d["rpm"], km_since_clear=d["km_since_clear"],
        km_with_mil=d["km_with_mil"], adapter=d["adapter"], protocol=d["protocol"], raw=json.dumps(DEMO_ANSWERS), created_by=uid))

