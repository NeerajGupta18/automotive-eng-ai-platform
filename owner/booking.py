"""Service booking (Phase D): the owner asks a workshop for a service slot, the workshop answers through a private link,
and the finished service flows back into the car's costs and reminders.

    owner: book  -> requested --share link (WhatsApp, SMS, copy)--> workshop opens /w#<token>, no account needed
    workshop:       confirm a preferred slot -> confirmed | offer another time -> proposed -> owner accepts -> confirmed
                    estimate, message, car received (in_service), car ready (with the bill), or decline
    owner:          complete -> the bill becomes a service expense, the odometer is recorded, due reminders move to the next one

What needs doing is chosen from the car's own data and rebuilt here, never taken from the client's text: due reminders,
fault codes (with their plain explanation for the mechanic) and open help requests, plus the owner's own words.

The workshop link is a capability: a random token (192 bits) that only its SHA-256 is stored of. It travels in the URL
fragment (/w#...), which browsers never send to a server, so it stays out of access logs and Referer headers; the page sends
it in a header. The owner can rotate or revoke it; it expires after 30 days, and 7 days after the booking ends it is read-only
and then gone. The workshop sees the car (model, number, km), the jobs, the preferred days, and the contact name and phone
the owner chose for this booking: nothing else from the account."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa

from owner import db as T
from owner import obd
from owner import validators as V
from owner.costs import CostService, _money
from owner.db import now, now_precise
from owner.service import Conflict, Forbidden, Invalid, NotFound, OwnerError, OwnerService, TooMany

IST = timezone(timedelta(hours=5, minutes=30), "IST")
PARTS = {"morning": ("08:00", "12:59", "Morning (8 am to 1 pm)"), "afternoon": ("13:00", "19:59", "Afternoon (1 pm to 8 pm)"),
         "any": ("07:00", "21:00", "Any time")}
ACTIVE = ("requested", "proposed", "confirmed", "in_service", "ready")
STATUS = {"requested": "Waiting for the workshop", "proposed": "The workshop offered another time", "confirmed": "Confirmed",
          "in_service": "At the workshop", "ready": "Ready to collect", "completed": "Done", "cancelled": "Cancelled",
          "declined": "The workshop can't take it"}
WORKSHOP_CAN = {"requested": ("confirm", "estimate", "message", "decline"), "proposed": ("confirm", "estimate", "message", "decline"),
                "confirmed": ("confirm", "estimate", "message", "received", "ready", "decline"),
                "in_service": ("estimate", "message", "ready"), "ready": ("message",)}
MAX_ACTIVE_PER_CAR = 3
MAX_WORKSHOPS = 20
MAX_EVENTS = 80
LINK_DAYS, AFTER_END_DAYS = 30, 7
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _uid() -> str:
    return str(uuid.uuid4())


def _row(r) -> dict | None:
    return dict(r._mapping) if r is not None else None


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def nice_day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hhmm(v, name: str = "Time") -> str:
    s = str(v or "").strip()
    if len(s) == 4 and s[1] == ":":
        s = "0" + s
    if not TIME_RE.match(s):
        raise Invalid(f"{name}: use 24-hour time like 10:30")
    if not "07:00" <= s <= "21:00":
        raise Invalid("Pick a time between 7:00 and 21:00")
    return s


def ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def ics_fold(line: str) -> str:
    """Lines longer than 75 octets continue on the next line after a space (RFC 5545 3.1), never splitting a character."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode())
            cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


class BookingError(OwnerError):
    status = 409


def rupees(v, name: str) -> int:
    """-> paise, from ₹1 to ₹5,00,000 (the expense limit)."""
    try:
        paise = _money(v, name)
    except V.ValidationError as e:
        raise Invalid(str(e)) from e
    if paise < 100:
        raise Invalid(f"{name} must be at least ₹1")
    return paise


class BookingService:
    def __init__(self, owner: OwnerService, costs: CostService, notify=None):
        self.o, self.k, self.n, self.e, self.s = owner, costs, notify, owner.e, owner.s

    # ================================================================== workshops (the person's own list)
    def _ws_values(self, data: dict) -> dict:
        try:
            return {"name": V.text(data.get("name"), "Workshop name", 2, 80), "phone": V.helpline(data.get("phone")),
                    "address": V.text(data.get("address", ""), "Address", 0, 200)}
        except V.ValidationError as e:
            raise Invalid(str(e).replace("roadside assistance number", "workshop's number")) from e

    def workshops(self, user: dict) -> list[dict]:
        with self.e.connect() as c:
            return [{k: _iso(v) for k, v in _row(r).items() if k != "user_id"}
                    for r in c.execute(sa.select(T.workshops).where(T.workshops.c.user_id == user["id"]).order_by(T.workshops.c.name))]

    def add_workshop(self, user: dict, data: dict, c=None) -> dict:
        v = self._ws_values(data)
        wid = _uid()

        def go(c):
            n = c.execute(sa.select(sa.func.count()).select_from(T.workshops).where(T.workshops.c.user_id == user["id"])).scalar()
            if n >= MAX_WORKSHOPS:
                raise Conflict(f"At most {MAX_WORKSHOPS} workshops in your list")
            c.execute(T.workshops.insert().values(id=wid, user_id=user["id"], created_at=now(), **v))
        if c is not None:
            go(c)
        else:
            with self.e.begin() as c2:
                go(c2)
        return {"id": wid, **v}

    def update_workshop(self, user: dict, wid: str, data: dict) -> dict:
        v = self._ws_values(data)
        with self.e.begin() as c:
            if not c.execute(T.workshops.update().where(T.workshops.c.id == wid, T.workshops.c.user_id == user["id"]).values(**v)).rowcount:
                raise NotFound("Workshop not found")
        return {"id": wid, **v}

    def delete_workshop(self, user: dict, wid: str) -> None:
        with self.e.begin() as c:
            if not c.execute(T.workshops.delete().where(T.workshops.c.id == wid, T.workshops.c.user_id == user["id"])).rowcount:
                raise NotFound("Workshop not found")

    # ================================================================== what can be booked
    def options(self, user: dict, car_id: str) -> dict:
        """Suggestions for 'what needs doing': due reminders, stored fault codes of the last check, open help requests."""
        car = self.o.car(user, car_id)
        jobs = []
        for i in self.o.reminders(user, car_id):
            if i["type"] == "reminder":
                jobs.append({"kind": "reminder", "ref": i["id"], "text": self._reminder_text(i), "due": i["status"] in ("overdue", "due_soon"),
                             "status": i["status"]})
        with self.e.connect() as c:
            last = _row(c.execute(sa.select(T.health_scans).where(T.health_scans.c.car_id == car_id)
                                  .order_by(T.health_scans.c.scanned_at.desc()).limit(1)).first())
            cases = c.execute(sa.select(T.help_cases.c.id, T.help_cases.c.title).where(
                T.help_cases.c.car_id == car_id, T.help_cases.c.user_id == user["id"], T.help_cases.c.status == "open")).all()
        if last:
            for code in json.loads(last["codes"]):
                if code["kind"] != "pending":
                    jobs.append({"kind": "code", "ref": code["code"], "text": self._code_text(code["code"]), "due": True})
        for r in cases:
            jobs.append({"kind": "help", "ref": r.id, "text": f"Problem: {r.title}", "due": True})
        me = self.o.me(user["id"])
        return {"jobs": jobs, "workshops": self.workshops(user), "contact": {"name": me["name"], "phone": me["phone"]},
                "odometer_km": car["odometer_km"], "parts": {k: v[2] for k, v in PARTS.items()},
                "first_day": date.today().isoformat(), "last_day": (date.today() + timedelta(days=60)).isoformat()}

    @staticmethod
    def _reminder_text(r: dict) -> str:
        return r["title"] + (f" (due at {r['due_km']:,} km)" if r.get("due_km") is not None else "")

    @staticmethod
    def _code_text(code: str) -> str:
        e = obd.explain(code)
        return f"{e['code']}: {e['title']}"

    def _jobs(self, c, user: dict, car_id: str, jobs) -> list[dict]:
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= 12:
            raise Invalid("Choose 1 to 12 things for the workshop to do")
        out, seen = [], set()
        for j in jobs:
            kind, ref = (j or {}).get("kind"), (j or {}).get("ref")
            if kind == "reminder":
                r = _row(c.execute(sa.select(T.reminders).where(T.reminders.c.id == str(ref), T.reminders.c.car_id == car_id)).first())
                if not r:
                    raise Invalid("That reminder is not on this car")
                item = {"kind": kind, "ref": r["id"], "text": self._reminder_text(r)}
            elif kind == "code":
                try:
                    code = obd.normalise_code(str(ref or ""))
                except obd.ObdError as e:
                    raise Invalid(str(e)) from e
                item = {"kind": kind, "ref": code, "text": self._code_text(code)}
            elif kind == "help":
                r = c.execute(sa.select(T.help_cases.c.id, T.help_cases.c.title).where(
                    T.help_cases.c.id == str(ref), T.help_cases.c.car_id == car_id, T.help_cases.c.user_id == user["id"])).first()
                if not r:
                    raise Invalid("That help request is not yours or not about this car")
                item = {"kind": kind, "ref": r.id, "text": f"Problem: {r.title}"}
            elif kind == "custom":
                try:
                    item = {"kind": kind, "ref": None, "text": V.text(j.get("text"), "What needs doing", 2, 120)}
                except V.ValidationError as e:
                    raise Invalid(str(e)) from e
            else:
                raise Invalid("Unknown kind of job")
            sig = (item["kind"], item["ref"] or item["text"].lower())
            if sig not in seen:
                seen.add(sig)
                out.append(item)
        return out

    @staticmethod
    def _slots(slots) -> list[dict]:
        if not isinstance(slots, list) or not 1 <= len(slots) <= 3:
            raise Invalid("Give 1 to 3 preferred days")
        today, out = date.today(), []
        for s in slots:
            try:
                on = V.day((s or {}).get("on"), "Preferred day")
            except V.ValidationError as e:
                raise Invalid(str(e)) from e
            part = (s or {}).get("part") or "any"
            if on is None or not today <= on <= today + timedelta(days=60):
                raise Invalid("Preferred days must be from today to 60 days ahead")
            if part not in PARTS:
                raise Invalid("Part of the day must be morning, afternoon or any")
            if (on, part) not in [(x["on"], x["part"]) for x in out]:
                out.append({"on": on, "part": part})
        return sorted(out, key=lambda x: (x["on"], x["part"]))

    # ================================================================== booking
    def create(self, user: dict, car_id: str, data: dict) -> dict:
        car = self.o.car(user, car_id)
        slots = self._slots(data.get("slots"))
        try:
            phone = V.phone(data.get("contact_phone"))
            notes = V.text(data.get("notes", ""), "Notes", 0, 500)
            odo = V.km(data.get("odometer_km"))
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        if not phone:
            raise Invalid("Enter the mobile number the workshop should call")
        if odo is not None and car["odometer_km"] is not None and odo < car["odometer_km"]:
            raise Invalid(f"The odometer can't be lower than the last reading ({car['odometer_km']:,} km)")
        me = self.o.me(user["id"])
        bid, t = _uid(), now()
        with self.e.begin() as c:
            self.o._membership(c, user["id"], car_id)
            jobs = self._jobs(c, user, car_id, data.get("jobs"))
            active = c.execute(sa.select(sa.func.count()).select_from(T.bookings).where(
                T.bookings.c.car_id == car_id, T.bookings.c.status.in_(ACTIVE))).scalar()
            if active >= MAX_ACTIVE_PER_CAR:
                raise Conflict(f"This car already has {active} open bookings; finish or cancel one first")
            if data.get("workshop_id"):
                w = _row(c.execute(sa.select(T.workshops).where(T.workshops.c.id == str(data["workshop_id"]),
                                                                   T.workshops.c.user_id == user["id"])).first())
                if not w:
                    raise NotFound("Workshop not found")
            elif data.get("workshop"):
                w = self.add_workshop(user, data["workshop"], c)
            else:
                raise Invalid("Choose a workshop or add one")
            c.execute(T.bookings.insert().values(
                id=bid, car_id=car_id, created_by=user["id"], workshop_id=w["id"], workshop_name=w["name"], workshop_phone=w["phone"],
                workshop_address=w["address"], contact_name=me["name"][:60], contact_phone=phone, status="requested",
                jobs=json.dumps(jobs), slots=json.dumps([{"on": s["on"].isoformat(), "part": s["part"]} for s in slots]),
                pickup=bool(data.get("pickup")), odometer_km=odo, notes=notes, created_at=t, updated_at=t))
            self._event(c, bid, "owner", "created", f"Booking request for {len(jobs)} job{'s' if len(jobs) > 1 else ''}")
            if odo is not None:
                CostService.record_odometer(c, car_id, odo, "manual")
        return self.get(user, bid)

    @staticmethod
    def _event(c, bid: str, actor: str, kind: str, detail: str = "") -> None:
        n = c.execute(sa.select(sa.func.count()).select_from(T.booking_events).where(T.booking_events.c.booking_id == bid)).scalar()
        if n >= MAX_EVENTS:
            raise TooMany("This booking has too many updates")
        c.execute(T.booking_events.insert().values(booking_id=bid, at=now_precise(), actor=actor, kind=kind, detail=detail[:300]))

    def _load(self, c, user: dict, bid: str) -> tuple[dict, str]:
        b = _row(c.execute(sa.select(T.bookings).where(T.bookings.c.id == bid)).first())
        if not b:
            raise NotFound("Booking not found")
        return b, self.o._membership(c, user["id"], b["car_id"])                # not a member -> "not found"

    def _out(self, c, b: dict, for_workshop: bool = False) -> dict:
        car = _row(c.execute(sa.select(T.cars).where(T.cars.c.id == b["car_id"])).first())
        jobs = json.loads(b["jobs"])
        if for_workshop:
            for j in jobs:
                if j["kind"] == "code":
                    e = obd.explain(j["ref"])
                    j["detail"] = e["plain"] + (f" Common causes: {', '.join(e['causes'])}." if e["causes"] else "")
        slots = [{**s, "label": f"{nice_day(date.fromisoformat(s['on']))}, {PARTS[s['part']][2].split(' (')[0].lower()}"} for s in json.loads(b["slots"])]
        events = [{"at": _iso(r.at), "actor": r.actor, "kind": r.kind, "detail": r.detail}
                  for r in c.execute(sa.select(T.booking_events).where(T.booking_events.c.booking_id == b["id"]).order_by(T.booking_events.c.id))
                  if not (for_workshop and r.kind in ("shared", "unshared"))]                 # the owner's link housekeeping
        out = {"id": b["id"], "status": b["status"], "status_label": STATUS[b["status"]],
               "car": {"id": car["id"], "make": car["make"], "model": car["model"], "variant": car["variant"], "fuel": car["fuel"],
                       "year": car["year"], "reg_display": V.reg_display(car["reg_number"]), "odometer_km": b["odometer_km"] or car["odometer_km"]},
               "workshop": {"id": b["workshop_id"], "name": b["workshop_name"], "phone": b["workshop_phone"], "address": b["workshop_address"]},
               "contact": {"name": b["contact_name"].split(" ")[0] if for_workshop else b["contact_name"], "phone": b["contact_phone"]},
               "jobs": jobs, "slots": slots, "pickup": b["pickup"], "notes": b["notes"],
               "appointment": {"on": _iso(b["appointment_on"]), "time": b["appointment_time"],
                               "label": f"{nice_day(b['appointment_on'])}, {b['appointment_time']}"} if b["appointment_on"] else None,
               "proposed": {"on": _iso(b["proposed_on"]), "time": b["proposed_time"],
                            "label": f"{nice_day(b['proposed_on'])}, {b['proposed_time']}"} if b["proposed_on"] else None,
               "estimate": b["estimate_paise"] / 100 if b["estimate_paise"] is not None else None,
               "bill": b["bill_paise"] / 100 if b["bill_paise"] is not None else None, "workshop_note": b["workshop_note"],
               "created_at": _iso(b["created_at"]), "updated_at": _iso(b["updated_at"]), "completed_at": _iso(b["completed_at"]),
               "events": events}
        if for_workshop:
            out.pop("id")
            out["car"].pop("id")
            out["can"] = list(WORKSHOP_CAN.get(b["status"], ())) if self._writable(b) else []
        else:
            out["shared"] = bool(b["token_hash"]) and (b["token_expires_at"] is None or b["token_expires_at"] > now())
            out["expense_id"] = b["expense_id"]
            out["created_by"] = b["created_by"]
        return out

    def get(self, user: dict, bid: str) -> dict:
        with self.e.connect() as c:
            b, _ = self._load(c, user, bid)
            return self._out(c, b)

    def list(self, user: dict, car_id: str | None = None, active_only: bool = False) -> list[dict]:
        with self.e.connect() as c:
            ids = [car_id] if car_id else [x["id"] for x in self.o._cars(c, user["id"])]
            if car_id:
                self.o._membership(c, user["id"], car_id)
            if not ids:
                return []
            q = sa.select(T.bookings).where(T.bookings.c.car_id.in_(ids))
            if active_only:
                q = q.where(T.bookings.c.status.in_(ACTIVE))
            rows = [_row(r) for r in c.execute(q.order_by(T.bookings.c.created_at.desc()))]
            return [self._out(c, b) for b in rows]

    # ------------------------------------------------------------------ owner actions
    def _owner_step(self, user: dict, bid: str, allowed: tuple, fn, notify_title=None) -> dict:
        with self.e.begin() as c:
            b, _ = self._load(c, user, bid)
            if b["status"] not in allowed:
                raise BookingError(f"Not possible while the booking is '{STATUS[b['status']].lower()}'")
            vals = fn(c, b) or {}
            c.execute(T.bookings.update().where(T.bookings.c.id == bid).values(updated_at=now(), **vals))
            members = [r.user_id for r in c.execute(sa.select(T.car_members.c.user_id).where(T.car_members.c.car_id == b["car_id"]))]
        if notify_title and self.n:
            others = [u for u in members if u != user["id"]]
            if others:
                self.n.send_now(others, {"kind": "bookings", "car_id": b["car_id"], "key": f"booking:{bid}:{uuid.uuid4().hex[:12]}",
                                         "title": notify_title, "body": f"{b['workshop_name']}. Changed by {user['name'] or 'someone'}.",
                                         "path": f"/bookings/{bid}"})
        return self.get(user, bid)

    def accept(self, user: dict, bid: str) -> dict:
        def fn(c, b):
            if not b["proposed_on"]:
                raise BookingError("There is no time to accept")
            self._event(c, bid, "owner", "accepted", f"Accepted {nice_day(b['proposed_on'])}, {b['proposed_time']}")
            return {"status": "confirmed", "appointment_on": b["proposed_on"], "appointment_time": b["proposed_time"],
                    "proposed_on": None, "proposed_time": None}
        return self._owner_step(user, bid, ("proposed",), fn, "Service time accepted")

    def reschedule(self, user: dict, bid: str, slots) -> dict:
        new = self._slots(slots)

        def fn(c, b):
            self._event(c, bid, "owner", "rescheduled", "New preferred days: " + ", ".join(nice_day(s["on"]) for s in new))
            return {"status": "requested", "slots": json.dumps([{"on": s["on"].isoformat(), "part": s["part"]} for s in new]),
                    "appointment_on": None, "appointment_time": None, "proposed_on": None, "proposed_time": None}
        return self._owner_step(user, bid, ("requested", "proposed", "confirmed"), fn, "Booking moved to new days")

    def handed_over(self, user: dict, bid: str) -> dict:
        def fn(c, b):
            self._event(c, bid, "owner", "received", "Car left at the workshop")
            return {"status": "in_service"}
        return self._owner_step(user, bid, ("confirmed",), fn)

    def cancel(self, user: dict, bid: str, note: str = "") -> dict:
        try:
            note = V.text(note or "", "Reason", 0, 200)
        except V.ValidationError as e:
            raise Invalid(str(e)) from e

        def fn(c, b):
            self._event(c, bid, "owner", "cancelled", note or "Cancelled by the owner")
            return {"status": "cancelled", "completed_at": now(), "token_hash": None, "token_expires_at": None}
        return self._owner_step(user, bid, ACTIVE, fn, "Service booking cancelled")

    def complete(self, user: dict, bid: str, data: dict) -> dict:
        """The service is done: the bill becomes a service expense (optional), the km are recorded, and the chosen
        reminders are marked done (a repeating one moves to its next date and km)."""
        with self.e.connect() as c:
            b, _ = self._load(c, user, bid)
        if b["status"] not in ("confirmed", "in_service", "ready"):
            raise BookingError(f"Not possible while the booking is '{STATUS[b['status']].lower()}'")
        amount = data.get("amount")
        paise = rupees(amount, "Bill") if amount not in (None, "") else b["bill_paise"]
        try:
            odo = V.km(data.get("odometer_km"))
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        done_ids = data.get("done_reminders") or []
        if not isinstance(done_ids, list) or len(done_ids) > 12:
            raise Invalid("Choose up to 12 reminders")
        reminder_refs = {j["ref"] for j in json.loads(b["jobs"]) if j["kind"] == "reminder"}
        with self.e.connect() as c:
            car_rems = {r.id for r in c.execute(sa.select(T.reminders.c.id).where(T.reminders.c.car_id == b["car_id"]))}
        bad = [r for r in done_ids if r not in car_rems]
        if bad:
            raise Invalid("A reminder to mark done is not on this car")
        expense_id = None
        if data.get("add_expense", True) and paise:
            jobs = "; ".join(j["text"] for j in json.loads(b["jobs"]))
            exp = self.k.add_expense(user, b["car_id"], {"category": "service", "amount": f"{paise / 100:.2f}", "spent_on": date.today().isoformat(),
                                                          "odometer_km": odo, "vendor": b["workshop_name"], "notes": f"Service booking: {jobs}"[:300]})
            expense_id = exp["id"]
        elif odo is not None:
            with self.e.begin() as c:
                CostService.record_odometer(c, b["car_id"], odo, "manual")
        moved = []
        for rid in dict.fromkeys(done_ids):
            try:
                r = self.o.complete_reminder(user, rid)
                moved.append(r["title"])
            except Conflict:
                continue

        def fn(c, b2):
            self._event(c, bid, "owner", "completed", "Service done" + (f", bill ₹{paise / 100:,.0f}" if paise else "")
                        + (f"; reminders updated: {', '.join(moved)}" if moved else ""))
            return {"status": "completed", "completed_at": now(), "bill_paise": paise, "expense_id": expense_id,
                    "token_expires_at": min(b2["token_expires_at"] or now(), now() + timedelta(days=AFTER_END_DAYS)) if b2["token_hash"] else None}
        out = self._owner_step(user, bid, ("confirmed", "in_service", "ready"), fn)
        out["reminders_updated"] = moved
        out["suggested_reminders"] = sorted(reminder_refs)
        return out

    def delete(self, user: dict, bid: str) -> None:
        with self.e.begin() as c:
            b, role = self._load(c, user, bid)
            if b["status"] in ACTIVE:
                raise BookingError("Cancel the booking first")
            if role != "owner" and b["created_by"] != user["id"]:
                raise Forbidden("Only an owner or the person who booked can delete it")
            c.execute(T.bookings.delete().where(T.bookings.c.id == bid))

    # ------------------------------------------------------------------ the workshop's link
    def share(self, user: dict, bid: str) -> dict:
        """A new private link for the workshop (an older one stops working) and a message to send with it."""
        token = secrets.token_urlsafe(24)
        with self.e.begin() as c:
            b, _ = self._load(c, user, bid)
            if b["status"] not in ACTIVE:
                raise BookingError("The booking is closed")
            c.execute(T.bookings.update().where(T.bookings.c.id == bid).values(
                token_hash=token_hash(token), token_expires_at=now() + timedelta(days=LINK_DAYS), updated_at=now()))
            self._event(c, bid, "owner", "shared", "Link for the workshop created")
            car = _row(c.execute(sa.select(T.cars).where(T.cars.c.id == b["car_id"])).first())
        path = f"/w#{token}"
        url = f"{self.s.public_url}{path}" if self.s.public_url else ""
        jobs = json.loads(b["jobs"])
        slots = json.loads(b["slots"])
        msg = (f"Hello {b['workshop_name']}, I'd like to book a service for my {car['make']} {car['model']} ({V.reg_display(car['reg_number'])}): "
               + "; ".join(j["text"] for j in jobs[:4]) + ("; and more" if len(jobs) > 4 else "") + ". Preferred: "
               + ", ".join(f"{nice_day(date.fromisoformat(s['on']))} {s['part'] if s['part'] != 'any' else ''}".strip() for s in slots)
               + f". Please confirm a time here: {{link}}  - {b['contact_name'].split(' ')[0]}, {b['contact_phone']}")
        wa = re.sub(r"\D", "", b["workshop_phone"]).lstrip("0")
        if len(wa) == 10:
            wa = "91" + wa
        wa = wa if re.fullmatch(r"91[6-9]\d{9}", wa) else ""                      # WhatsApp only for an Indian mobile number
        return {"token": token, "path": path, "url": url, "message": msg, "whatsapp_number": wa,
                "expires_at": _iso(now() + timedelta(days=LINK_DAYS)),
                "shared_data": ["the car's make, model, number and km", "what needs doing (with fault code explanations)",
                                "your preferred days and pickup choice", f"your first name and {b['contact_phone']}", "your notes"]}

    def unshare(self, user: dict, bid: str) -> dict:
        with self.e.begin() as c:
            self._load(c, user, bid)
            c.execute(T.bookings.update().where(T.bookings.c.id == bid).values(token_hash=None, token_expires_at=None, updated_at=now()))
            self._event(c, bid, "owner", "unshared", "Workshop link turned off")
        return self.get(user, bid)

    def _by_token(self, c, token: str) -> dict:
        if not token or len(token) > 64:
            raise NotFound("This link is not valid any more. Ask the car owner for a new one.")
        b = _row(c.execute(sa.select(T.bookings).where(T.bookings.c.token_hash == token_hash(token))).first())
        if not b or (b["token_expires_at"] and b["token_expires_at"] < now()):
            raise NotFound("This link is not valid any more. Ask the car owner for a new one.")
        return b

    @staticmethod
    def _writable(b: dict) -> bool:
        return b["status"] in ACTIVE

    def workshop_view(self, token: str) -> dict:
        with self.e.connect() as c:
            b = self._by_token(c, token)
            return self._out(c, b, for_workshop=True)

    def workshop_action(self, token: str, action: str, data: dict) -> dict:
        title = body = None
        with self.e.begin() as c:
            b = self._by_token(c, token)
            if not self._writable(b) or action not in WORKSHOP_CAN.get(b["status"], ()):
                raise BookingError(f"Not possible now: the booking is '{STATUS[b['status']].lower()}'")
            ok, retry = self.o.limits.allow(f"workshop:{b['id']}", 30, 3600)
            if not ok:
                raise TooMany("Too many updates on this booking. Try again later.", retry)
            vals: dict = {}
            note = ""
            try:
                note = V.text(data.get("note", ""), "Message", 0, 300)
            except V.ValidationError as e:
                raise Invalid(str(e)) from e
            if action == "confirm":
                try:
                    on = V.day(data.get("on"), "Date")
                except V.ValidationError as e:
                    raise Invalid(str(e)) from e
                at = _hhmm(data.get("time"))
                if on is None or not date.today() <= on <= date.today() + timedelta(days=90):
                    raise Invalid("Pick a date from today to 90 days ahead")
                if b["appointment_on"] == on and b["appointment_time"] == at and b["status"] == "confirmed":
                    raise BookingError("That is already the confirmed time")
                fits = any(s["on"] == on.isoformat() and PARTS[s["part"]][0] <= at <= PARTS[s["part"]][1] for s in json.loads(b["slots"]))
                label = f"{nice_day(on)}, {at}"
                if fits and b["status"] in ("requested", "proposed"):
                    vals = {"status": "confirmed", "appointment_on": on, "appointment_time": at, "proposed_on": None, "proposed_time": None}
                    self._event(c, b["id"], "workshop", "confirmed", f"Confirmed {label}" + (f". {note}" if note else ""))
                    title, body = f"Service confirmed: {label}", f"{b['workshop_name']} confirmed your booking."
                else:
                    vals = {"status": "proposed", "proposed_on": on, "proposed_time": at}
                    self._event(c, b["id"], "workshop", "proposed", f"Offered {label}" + (f". {note}" if note else ""))
                    title, body = f"{b['workshop_name']} offers {label}", "Accept the time or choose other days."
            elif action == "estimate":
                paise = rupees(data.get("amount"), "Estimate")
                vals = {"estimate_paise": paise, "workshop_note": note or b["workshop_note"]}
                self._event(c, b["id"], "workshop", "estimate", f"Estimate ₹{paise / 100:,.0f}" + (f". {note}" if note else ""))
                title, body = f"Estimate from {b['workshop_name']}: ₹{paise / 100:,.0f}", note or "Open the booking to see it."
            elif action == "message":
                if not note:
                    raise Invalid("Write a message")
                self._event(c, b["id"], "workshop", "message", note)
                title, body = f"Message from {b['workshop_name']}", note
            elif action == "received":
                vals = {"status": "in_service"}
                self._event(c, b["id"], "workshop", "received", "Car received" + (f". {note}" if note else ""))
                title, body = "Your car is at the workshop", f"{b['workshop_name']} has started." + (f" {note}" if note else "")
            elif action == "ready":
                amount = data.get("amount")
                paise = rupees(amount, "Bill") if amount not in (None, "") else None
                vals = {"status": "ready", "bill_paise": paise}
                self._event(c, b["id"], "workshop", "ready", "Car ready" + (f", bill ₹{paise / 100:,.0f}" if paise else "") + (f". {note}" if note else ""))
                title, body = "Your car is ready", f"{b['workshop_name']}" + (f", bill ₹{paise / 100:,.0f}" if paise else "") + "."
            elif action == "decline":
                vals = {"status": "declined", "completed_at": now(), "token_expires_at": now() + timedelta(days=AFTER_END_DAYS)}
                self._event(c, b["id"], "workshop", "declined", note or "The workshop can't take this booking")
                title, body = f"{b['workshop_name']} can't take the booking", note or "Book another workshop or other days."
            c.execute(T.bookings.update().where(T.bookings.c.id == b["id"]).values(updated_at=now(), **vals))
            members = [r.user_id for r in c.execute(sa.select(T.car_members.c.user_id).where(T.car_members.c.car_id == b["car_id"]))]
        if title and self.n:
            self.n.send_now(members, {"kind": "bookings", "car_id": b["car_id"], "key": f"booking:{b['id']}:{uuid.uuid4().hex[:12]}",
                                      "title": title, "body": body, "path": f"/bookings/{b['id']}"})
        return self.workshop_view(token)

    # ------------------------------------------------------------------ calendar
    def ics(self, user: dict, bid: str) -> str:
        b = self.get(user, bid)
        if not b["appointment"]:
            raise BookingError("The booking has no confirmed time yet")
        on = date.fromisoformat(b["appointment"]["on"])
        hh, mm = map(int, b["appointment"]["time"].split(":"))
        start = datetime(on.year, on.month, on.day, hh, mm, tzinfo=IST).astimezone(timezone.utc)
        fmt = "%Y%m%dT%H%M%SZ"
        w, car = b["workshop"], b["car"]
        desc = "\n".join(f"- {j['text']}" for j in b["jobs"]) + (f"\nWorkshop: {w['phone']}" if w["phone"] else "")
        summary = "Car service: {} {} ({})".format(car["make"], car["model"], car["reg_display"])
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//{self.s.app_name}//Service booking//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
                 "BEGIN:VEVENT", f"UID:{b['id']}@carsaathi", f"DTSTAMP:{datetime.now(timezone.utc):{fmt}}", f"DTSTART:{start:{fmt}}",
                 f"DTEND:{start + timedelta(hours=1):{fmt}}",
                 f"SUMMARY:{ics_escape(summary)}",
                 f"LOCATION:{ics_escape(', '.join(x for x in (w['name'], w['address']) if x))}", f"DESCRIPTION:{ics_escape(desc)}",
                 "BEGIN:VALARM", "TRIGGER:-PT2H", "ACTION:DISPLAY", "DESCRIPTION:Car service in 2 hours", "END:VALARM",
                 "END:VEVENT", "END:VCALENDAR"]
        return "\r\n".join(ics_fold(x) for x in lines) + "\r\n"

    # ------------------------------------------------------------------ privacy
    def export_rows(self, uid: str, car_ids: list[str]) -> dict:
        with self.e.connect() as c:
            ws = [{k: _iso(v) for k, v in _row(r).items() if k != "user_id"} for r in c.execute(sa.select(T.workshops).where(T.workshops.c.user_id == uid))]
            bs = []
            if car_ids:
                for r in c.execute(sa.select(T.bookings).where(T.bookings.c.car_id.in_(car_ids))):
                    b = {k: _iso(v) for k, v in _row(r).items() if k not in ("token_hash",)}
                    b["events"] = [{"at": _iso(e.at), "actor": e.actor, "kind": e.kind, "detail": e.detail}
                                   for e in c.execute(sa.select(T.booking_events).where(T.booking_events.c.booking_id == r.id).order_by(T.booking_events.c.id))]
                    bs.append(b)
        return {"workshops": ws, "service_bookings": bs}


def seed_demo(c, uid: str) -> None:
    """The demo account's workshop list: one sample entry (clearly not a real business)."""
    c.execute(T.workshops.insert().values(id=_uid(), user_id=uid, name="Sample Service Centre (demo)", phone="",
                                          address="Demo address, Pune", created_at=now()))
