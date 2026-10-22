"""Business rules of the car-owner service: accounts, garage, sharing, documents, reminders, privacy.

Access control is object-level: every car-owned record is reached through the car, and the caller must be a member of
that car (owner or driver). Records of cars the caller cannot see answer "not found", never "forbidden", so ids cannot
be probed. Owners manage the car and its people; drivers see everything and may add documents, reminders and the
odometer reading."""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import sqlalchemy as sa

from owner import db as T
from owner import mailer
from owner import security as S
from owner import validators as V
from owner.db import now
from owner.odometer import car_km_per_day, predicted_on

CONSENT_VERSION = "2026-10"
MAX_CARS = 10
MAX_MEMBERS = 6
MAX_DOCS_PER_CAR = 50
MAX_REMINDERS_PER_CAR = 50
DUE_SOON_DAYS = 30


class OwnerError(Exception):
    status = 400

    def __init__(self, msg: str, retry_after: int | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class NotFound(OwnerError):
    status = 404


class Forbidden(OwnerError):
    status = 403


class Conflict(OwnerError):
    status = 409


class Invalid(OwnerError):
    status = 422


class TooMany(OwnerError):
    status = 429


class Unavailable(OwnerError):
    status = 503


def _uid() -> str:
    return str(uuid.uuid4())


def _row(r) -> dict | None:
    return dict(r._mapping) if r is not None else None


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _out(d: dict, drop: tuple = ()) -> dict:
    return {k: _iso(v) for k, v in d.items() if k not in drop}


class OwnerService:
    def __init__(self, settings, engine: sa.Engine):
        self.s, self.e = settings, engine
        self.limits = S.RateLimiter()

    # ================================================================== sign-in
    def request_code(self, email: str, ip: str) -> dict:
        try:
            email = V.email(email)
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        for key, limit, secs in ((f"ip:{ip}", 20, 3600), (f"mail:{email}", 5, 3600), (f"mail30:{email}", 1, 30)):
            ok, retry = self.limits.allow("otp:" + key, limit, secs)
            if not ok:
                raise TooMany("Too many codes requested. Wait a little and try again.", retry)
        code = S.new_code()
        t = now()
        with self.e.begin() as c:
            c.execute(T.otps.update().where(T.otps.c.email == email, T.otps.c.consumed_at.is_(None)).values(consumed_at=t))
            c.execute(T.otps.insert().values(email=email, code_hash=S.code_hash(self.s.secret_key, email, code), created_at=t,
                                             expires_at=t + timedelta(minutes=self.s.otp_ttl_min), attempts=0, ip=ip[:64]))
        try:
            how = mailer.send_code(self.s, email, code)
        except mailer.MailError as e:
            raise Unavailable(str(e)) from e
        # the same answer for known and unknown addresses: nobody learns who has an account
        return {"sent": True, "delivery": how, "expires_in_min": self.s.otp_ttl_min}

    def verify_code(self, email: str, code: str, ip: str, user_agent: str) -> dict:
        try:
            email = V.email(email)
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        code = "".join(ch for ch in str(code or "") if ch.isdigit())
        ok, retry = self.limits.allow(f"verify:{ip}", 30, 600)
        if not ok:
            raise TooMany("Too many attempts. Wait a little and try again.", retry)
        t = now()
        with self.e.begin() as c:
            o = _row(c.execute(sa.select(T.otps).where(T.otps.c.email == email, T.otps.c.consumed_at.is_(None))
                               .order_by(T.otps.c.id.desc()).limit(1)).first())
            if not o or o["expires_at"] < t:
                raise Invalid("The code has expired or was not requested. Ask for a new one.")
            attempts = o["attempts"] + 1
            good = len(code) == 6 and S.same(S.code_hash(self.s.secret_key, email, code), o["code_hash"])
            # the attempt is recorded (committed) before a wrong code is reported: raising inside the transaction would undo it
            c.execute(T.otps.update().where(T.otps.c.id == o["id"]).values(attempts=attempts,
                                                                            consumed_at=t if good or attempts >= 5 else None))
            if good:
                token, new, u = self._sign_in(c, email, user_agent, t)
        if not good:
            if attempts >= 5:
                raise TooMany("Too many wrong codes. Ask for a new one.")
            raise Invalid(f"Wrong code. {5 - attempts} attempt(s) left.")
        return {"token": token, "new_user": new, "user": self.me(u["id"])}

    def _sign_in(self, c, email: str, user_agent: str, t) -> tuple[str, bool, dict]:
        u = _row(c.execute(sa.select(T.users).where(T.users.c.email == email)).first())
        new = u is None
        if new:
            u = {"id": _uid(), "email": email, "name": "", "phone": "", "created_at": t, "is_demo": False}
            c.execute(T.users.insert().values(**u))
        c.execute(T.users.update().where(T.users.c.id == u["id"]).values(last_login_at=t))
        token = self._session(c, u["id"], user_agent)
        self._event(c, u["id"], "login", "new account" if new else "")
        return token, new, u

    def demo(self, ip: str, user_agent: str) -> dict:
        """A private sandbox account with sample data, deleted after demo_ttl_hours. No email, no code."""
        if not self.s.demo_login:
            raise Forbidden("The demo is switched off on this server")
        ok, retry = self.limits.allow(f"demo:{ip}", 10, 3600)
        if not ok:
            raise TooMany("Too many demo accounts from this network. Try again later.", retry)
        self.cleanup_demo()
        t = now()
        with self.e.begin() as c:
            n = c.execute(sa.select(sa.func.count()).select_from(T.users).where(T.users.c.is_demo.is_(True))).scalar()
            if n >= self.s.max_demo_accounts:
                raise Unavailable("The demo is busy. Try again in a few minutes.")
            uid = _uid()
            c.execute(T.users.insert().values(id=uid, email=f"demo-{uid[:8]}@demo.invalid", name="Demo driver", phone="", created_at=t,
                                              is_demo=True, demo_expires_at=t + timedelta(hours=self.s.demo_ttl_hours),
                                              consent_version="demo", consent_at=t, last_login_at=t))
            self._seed_demo(c, uid)
            token = self._session(c, uid, user_agent)
            self._event(c, uid, "login", "demo")
        return {"token": token, "new_user": False, "user": self.me(uid)}

    def cleanup_demo(self) -> int:
        """Delete expired demo accounts with their cars (-> number of accounts deleted)."""
        with self.e.begin() as c:
            old = c.execute(sa.select(T.users.c.id, T.users.c.email).where(T.users.c.is_demo.is_(True), T.users.c.demo_expires_at < now())).all()
            for r in old:
                self._erase(c, r.id, r.email)
        return len(old)

    def _session(self, c, uid: str, user_agent: str) -> str:
        token = S.new_token()
        t = now()
        c.execute(T.sessions.insert().values(user_id=uid, token_hash=S.token_hash(token), created_at=t, last_used_at=t,
                                             expires_at=t + timedelta(days=self.s.session_days), user_agent=(user_agent or "")[:200]))
        return token

    def authenticate(self, token: str) -> dict | None:
        if not token or not token.startswith("cs_") or len(token) > 100:
            return None
        t = now()
        with self.e.begin() as c:
            r = _row(c.execute(sa.select(T.sessions.c.id.label("session_id"), T.sessions.c.last_used_at.label("session_used_at"), T.users)
                               .join(T.users, T.users.c.id == T.sessions.c.user_id)
                               .where(T.sessions.c.token_hash == S.token_hash(token), T.sessions.c.revoked_at.is_(None),
                                      T.sessions.c.expires_at > t)).first())
            if not r:
                return None
            if r["is_demo"] and r["demo_expires_at"] and r["demo_expires_at"] < t:
                return None
            if not r["session_used_at"] or (t - r["session_used_at"]).total_seconds() > 300:
                c.execute(T.sessions.update().where(T.sessions.c.id == r["session_id"]).values(last_used_at=t))
        return r

    def logout(self, user: dict, everywhere: bool = False) -> None:
        with self.e.begin() as c:
            q = T.sessions.update().where(T.sessions.c.user_id == user["id"], T.sessions.c.revoked_at.is_(None))
            if not everywhere:
                q = q.where(T.sessions.c.id == user["session_id"])
            c.execute(q.values(revoked_at=now()))
            self._event(c, user["id"], "logout", "all devices" if everywhere else "")

    @staticmethod
    def _event(c, uid: str, kind: str, detail: str = "") -> None:
        c.execute(T.events.insert().values(user_id=uid, kind=kind, at=now(), detail=detail[:300]))

    # ================================================================== account
    def me(self, uid: str) -> dict:
        with self.e.connect() as c:
            u = _row(c.execute(sa.select(T.users).where(T.users.c.id == uid)).first())
            if not u:
                raise NotFound("Account not found")
            invites = c.execute(sa.select(sa.func.count()).select_from(T.invitations)
                                .where(T.invitations.c.email == u["email"], T.invitations.c.status == "pending")).scalar()
        return {"id": u["id"], "email": u["email"], "name": u["name"], "phone": u["phone"], "is_demo": u["is_demo"],
                "profile_complete": bool(u["name"] and u["consent_at"]), "consent_version": u["consent_version"],
                "created_at": _iso(u["created_at"]), "demo_expires_at": _iso(u["demo_expires_at"]), "pending_invitations": invites}

    def update_me(self, user: dict, data: dict) -> dict:
        vals = {}
        try:
            if "name" in data:
                vals["name"] = V.text(data["name"], "Name", 2, 60)
            if "phone" in data:
                vals["phone"] = V.phone(data["phone"])
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        if data.get("consent") is True and not user["consent_at"]:
            vals.update(consent_version=CONSENT_VERSION, consent_at=now())
        if not (vals.get("name") or user["name"]) and data.get("consent"):
            raise Invalid("Enter your name")
        if vals:
            with self.e.begin() as c:
                c.execute(T.users.update().where(T.users.c.id == user["id"]).values(**vals))
                if "consent_at" in vals:
                    self._event(c, user["id"], "consent", CONSENT_VERSION)
        return self.me(user["id"])

    def export(self, user: dict) -> dict:
        """Everything stored about the person (DPDP Act 2023: right to access)."""
        uid = user["id"]
        with self.e.begin() as c:
            self._event(c, uid, "export")
        with self.e.connect() as c:
            u = _row(c.execute(sa.select(T.users).where(T.users.c.id == uid)).first())
            out = {"exported_at": _iso(now()), "service": self.s.app_name,
                   "account": _out(u, ("demo_expires_at",)), "cars": [], "invitations_received": [],
                   "sessions": [_out(_row(r), ("token_hash",)) for r in c.execute(sa.select(T.sessions).where(T.sessions.c.user_id == uid))],
                   "events": [_out(_row(r)) for r in c.execute(sa.select(T.events).where(T.events.c.user_id == uid).order_by(T.events.c.id))]}
            for car in self._cars(c, uid):
                cid = car["id"]
                out["cars"].append({**car,
                                    "documents": [_out(_row(r)) for r in c.execute(sa.select(T.documents).where(T.documents.c.car_id == cid))],
                                    "reminders": [_out(_row(r)) for r in c.execute(sa.select(T.reminders).where(T.reminders.c.car_id == cid))]})
            out["invitations_received"] = [_out(_row(r)) for r in c.execute(sa.select(T.invitations).where(T.invitations.c.email == u["email"]))]
        out["note"] = "Document files are not embedded; download them from the app. Sign-in codes are never stored readable."
        return out

    def delete_account(self, user: dict) -> dict:
        """Erasure: the account, its sessions and events, invitations to it, and every car where it is the only owner (with
        their documents and reminders). Cars shared with another owner stay with that owner."""
        with self.e.begin() as c:
            removed = self._erase(c, user["id"], user["email"])
        return {"deleted": True, "cars_deleted": removed}

    @staticmethod
    def _erase(c, uid: str, email: str) -> int:
        owned = [r.car_id for r in c.execute(sa.select(T.car_members.c.car_id).where(T.car_members.c.user_id == uid,
                                                                                        T.car_members.c.role == "owner"))]
        removed = 0
        for cid in owned:
            others = c.execute(sa.select(sa.func.count()).select_from(T.car_members).where(
                T.car_members.c.car_id == cid, T.car_members.c.role == "owner", T.car_members.c.user_id != uid)).scalar()
            if not others:
                c.execute(T.cars.delete().where(T.cars.c.id == cid))                  # cascades: members, documents, files, reminders
                removed += 1
        c.execute(T.invitations.delete().where(sa.or_(T.invitations.c.email == email, T.invitations.c.invited_by == uid)))
        # service bookings this person made on cars that stay with a co-owner: their name and phone go, the workshop link too
        c.execute(T.bookings.update().where(T.bookings.c.created_by == uid).values(contact_name="", contact_phone="", token_hash=None,
                                                                                   token_expires_at=None))
        c.execute(T.otps.delete().where(T.otps.c.email == email))
        c.execute(T.users.delete().where(T.users.c.id == uid))                       # cascades: sessions, events, memberships
        return removed

    # ================================================================== garage
    def _membership(self, c, uid: str, car_id: str) -> str:
        r = c.execute(sa.select(T.car_members.c.role).where(T.car_members.c.car_id == car_id, T.car_members.c.user_id == uid)).first()
        if not r:
            raise NotFound("Car not found")
        return r.role

    def _cars(self, c, uid: str) -> list[dict]:
        rows = c.execute(sa.select(T.cars, T.car_members.c.role).join(T.car_members, T.car_members.c.car_id == T.cars.c.id)
                         .where(T.car_members.c.user_id == uid).order_by(T.cars.c.created_at))
        return [self._car_out(_row(r)) for r in rows]

    @staticmethod
    def _car_out(r: dict) -> dict:
        out = _out(r)
        out["reg_display"] = V.reg_display(r["reg_number"])
        out["title"] = r["nickname"] or f"{r['make']} {r['model']}"
        return out

    def _car_values(self, data: dict, partial: bool) -> dict:
        v = {}
        try:
            if not partial or "make" in data:
                v["make"] = V.text(data.get("make"), "Make", 2, 40)
            if not partial or "model" in data:
                v["model"] = V.text(data.get("model"), "Model", 1, 40)
            if not partial or "fuel" in data:
                v["fuel"] = V.choice(data.get("fuel"), "Fuel", V.FUELS)
            if not partial or "reg_number" in data:
                v["reg_number"] = V.reg_number(data.get("reg_number"))
            for k, name, hi in (("nickname", "Nickname", 40), ("variant", "Variant", 40), ("colour", "Colour", 30)):
                if k in data or not partial:
                    v[k] = V.text(data.get(k, ""), name, 0, hi)
            if "vin" in data or not partial:
                v["vin"] = V.vin(data.get("vin"))
            if "year" in data or not partial:
                v["year"] = V.year(data.get("year"))
            if "purchase_date" in data or not partial:
                v["purchase_date"] = V.day(data.get("purchase_date"), "Purchase date")
            if "roadside_phone" in data or not partial:
                v["roadside_phone"] = V.helpline(data.get("roadside_phone"))
            if "odometer_km" in data or not partial:
                v["odometer_km"] = V.km(data.get("odometer_km"))
                if v["odometer_km"] is not None:
                    v["odometer_at"] = now()
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        return v

    def cars(self, user: dict) -> list[dict]:
        with self.e.connect() as c:
            return self._cars(c, user["id"])

    def add_car(self, user: dict, data: dict) -> dict:
        v = self._car_values(data, partial=False)
        t = now()
        with self.e.begin() as c:
            mine = self._cars(c, user["id"])
            if len(mine) >= MAX_CARS:
                raise Conflict(f"At most {MAX_CARS} cars per account")
            if any(x["reg_number"] == v["reg_number"] for x in mine):
                raise Conflict(f"{V.reg_display(v['reg_number'])} is already in your garage")
            cid = _uid()
            c.execute(T.cars.insert().values(id=cid, created_by=user["id"], created_at=t, updated_at=t, **v))
            c.execute(T.car_members.insert().values(car_id=cid, user_id=user["id"], role="owner", created_at=t))
            if v.get("odometer_km") is not None:
                c.execute(T.odometer_readings.insert().values(car_id=cid, km=v["odometer_km"], at=t, source="manual"))
        return self.car(user, cid)

    def car(self, user: dict, car_id: str) -> dict:
        with self.e.connect() as c:
            role = self._membership(c, user["id"], car_id)
            r = _row(c.execute(sa.select(T.cars).where(T.cars.c.id == car_id)).first())
        return {**self._car_out({**r, "role": role})}

    def update_car(self, user: dict, car_id: str, data: dict) -> dict:
        with self.e.begin() as c:
            role = self._membership(c, user["id"], car_id)
            if role != "owner" and set(data) - {"odometer_km"}:
                raise Forbidden("Drivers can only update the odometer reading")
            v = self._car_values(data, partial=True)
            if "odometer_km" in v and v["odometer_km"] is not None:
                cur = c.execute(sa.select(T.cars.c.odometer_km).where(T.cars.c.id == car_id)).scalar()
                if cur is not None and v["odometer_km"] < cur and role != "owner":
                    raise Invalid("The odometer cannot go down")
            if "reg_number" in v:
                dup = c.execute(sa.select(T.cars.c.id).join(T.car_members, T.car_members.c.car_id == T.cars.c.id).where(
                    T.car_members.c.user_id == user["id"], T.cars.c.reg_number == v["reg_number"], T.cars.c.id != car_id)).first()
                if dup:
                    raise Conflict(f"{V.reg_display(v['reg_number'])} is already in your garage")
            if v:
                c.execute(T.cars.update().where(T.cars.c.id == car_id).values(updated_at=now(), **v))
            if v.get("odometer_km") is not None:
                c.execute(T.odometer_readings.insert().values(car_id=car_id, km=v["odometer_km"], at=now(), source="manual"))
        return self.car(user, car_id)

    def delete_car(self, user: dict, car_id: str) -> None:
        with self.e.begin() as c:
            if self._membership(c, user["id"], car_id) != "owner":
                raise Forbidden("Only an owner can remove the car")
            c.execute(T.cars.delete().where(T.cars.c.id == car_id))

    # ================================================================== sharing
    def members(self, user: dict, car_id: str) -> dict:
        with self.e.connect() as c:
            self._membership(c, user["id"], car_id)
            people = [{"user_id": r.user_id, "name": r.name, "email": r.email, "role": r.role, "you": r.user_id == user["id"]}
                      for r in c.execute(sa.select(T.car_members.c.user_id, T.car_members.c.role, T.users.c.name, T.users.c.email)
                                         .join(T.users, T.users.c.id == T.car_members.c.user_id).where(T.car_members.c.car_id == car_id)
                                         .order_by(T.car_members.c.role.desc(), T.car_members.c.created_at, T.users.c.name))]
            pending = [{"id": r.id, "email": r.email, "role": r.role, "created_at": _iso(r.created_at)}
                       for r in c.execute(sa.select(T.invitations).where(T.invitations.c.car_id == car_id, T.invitations.c.status == "pending"))]
        return {"members": people, "pending": pending}

    def invite(self, user: dict, car_id: str, email: str, role: str) -> dict:
        try:
            email, role = V.email(email), V.choice(role, "Role", V.ROLES)
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        if user["is_demo"]:
            raise Forbidden("Sharing is not available in the demo")
        with self.e.begin() as c:
            if self._membership(c, user["id"], car_id) != "owner":
                raise Forbidden("Only an owner can share the car")
            if email == user["email"]:
                raise Invalid("You already have this car")
            if c.execute(sa.select(T.car_members.c.user_id).join(T.users, T.users.c.id == T.car_members.c.user_id)
                         .where(T.car_members.c.car_id == car_id, T.users.c.email == email)).first():
                raise Conflict("This person already has access")
            if c.execute(sa.select(T.invitations.c.id).where(T.invitations.c.car_id == car_id, T.invitations.c.email == email,
                                                             T.invitations.c.status == "pending")).first():
                raise Conflict("An invitation to this email is already waiting")
            n = c.execute(sa.select(sa.func.count()).select_from(T.car_members).where(T.car_members.c.car_id == car_id)).scalar()
            if n >= MAX_MEMBERS:
                raise Conflict(f"At most {MAX_MEMBERS} people per car")
            iid = _uid()
            c.execute(T.invitations.insert().values(id=iid, car_id=car_id, email=email, role=role, invited_by=user["id"],
                                                    status="pending", created_at=now()))
        return {"id": iid, "email": email, "role": role, "status": "pending",
                "note": "They see the invitation after signing in with this email address."}

    def my_invitations(self, user: dict) -> list[dict]:
        with self.e.connect() as c:
            rows = c.execute(sa.select(T.invitations.c.id, T.invitations.c.role, T.invitations.c.created_at, T.cars.c.make, T.cars.c.model,
                                       T.cars.c.reg_number, T.users.c.name.label("from_name"))
                             .join(T.cars, T.cars.c.id == T.invitations.c.car_id).join(T.users, T.users.c.id == T.invitations.c.invited_by)
                             .where(T.invitations.c.email == user["email"], T.invitations.c.status == "pending"))
            return [{"id": r.id, "role": r.role, "created_at": _iso(r.created_at), "car": f"{r.make} {r.model}",
                     "reg_display": V.reg_display(r.reg_number), "from": r.from_name} for r in rows]

    def respond(self, user: dict, inv_id: str, accept: bool) -> dict:
        with self.e.begin() as c:
            inv = _row(c.execute(sa.select(T.invitations).where(T.invitations.c.id == inv_id, T.invitations.c.email == user["email"],
                                                                 T.invitations.c.status == "pending")).first())
            if not inv:
                raise NotFound("Invitation not found")
            if accept:
                if len(self._cars(c, user["id"])) >= MAX_CARS:
                    raise Conflict(f"At most {MAX_CARS} cars per account")
                if not c.execute(sa.select(T.car_members.c.user_id).where(T.car_members.c.car_id == inv["car_id"],
                                                                          T.car_members.c.user_id == user["id"])).first():
                    c.execute(T.car_members.insert().values(car_id=inv["car_id"], user_id=user["id"], role=inv["role"], created_at=now()))
            c.execute(T.invitations.update().where(T.invitations.c.id == inv_id).values(status="accepted" if accept else "declined",
                                                                                         responded_at=now()))
        return {"id": inv_id, "status": "accepted" if accept else "declined", "car_id": inv["car_id"] if accept else None}

    def revoke_invitation(self, user: dict, car_id: str, inv_id: str) -> None:
        with self.e.begin() as c:
            if self._membership(c, user["id"], car_id) != "owner":
                raise Forbidden("Only an owner can withdraw invitations")
            n = c.execute(T.invitations.update().where(T.invitations.c.id == inv_id, T.invitations.c.car_id == car_id,
                                                       T.invitations.c.status == "pending").values(status="revoked", responded_at=now())).rowcount
            if not n:
                raise NotFound("Invitation not found")

    def remove_member(self, user: dict, car_id: str, member_id: str) -> None:
        with self.e.begin() as c:
            role = self._membership(c, user["id"], car_id)
            if member_id != user["id"] and role != "owner":
                raise Forbidden("Only an owner can remove people")
            target = c.execute(sa.select(T.car_members.c.role).where(T.car_members.c.car_id == car_id,
                                                                     T.car_members.c.user_id == member_id)).first()
            if not target:
                raise NotFound("Person not found")
            if target.role == "owner":
                owners = c.execute(sa.select(sa.func.count()).select_from(T.car_members).where(
                    T.car_members.c.car_id == car_id, T.car_members.c.role == "owner")).scalar()
                if owners <= 1:
                    raise Conflict("The last owner cannot leave; remove the car instead")
            c.execute(T.car_members.delete().where(T.car_members.c.car_id == car_id, T.car_members.c.user_id == member_id))

    # ================================================================== documents
    def _doc_values(self, data: dict, partial: bool) -> dict:
        v = {}
        try:
            if not partial or "kind" in data:
                v["kind"] = V.choice(data.get("kind"), "Document type", tuple(V.DOC_KINDS))
            if not partial or "title" in data:
                v["title"] = V.text(data.get("title") or V.DOC_KINDS.get(v.get("kind", ""), ""), "Title", 2, 80)
            for k, name, hi in (("number", "Number", 40), ("issuer", "Issued by", 60), ("notes", "Notes", 500)):
                if k in data or not partial:
                    v[k] = V.text(data.get(k, ""), name, 0, hi)
            for k, name in (("issued_on", "Issued on"), ("expires_on", "Valid until")):
                if k in data or not partial:
                    v[k] = V.day(data.get(k), name)
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        if v.get("issued_on") and v.get("expires_on") and v["expires_on"] < v["issued_on"]:
            raise Invalid("'Valid until' is before 'Issued on'")
        return v

    def _check_file(self, content: bytes | None, name: str) -> dict | None:
        if content is None:
            return None
        if len(content) > self.s.max_upload_mb * 1024 * 1024:
            raise Invalid(f"The file is larger than {self.s.max_upload_mb} MB")
        if len(content) < 16:
            raise Invalid("The file is empty")
        try:
            mime, ext = V.file_type(content)
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        return {"file_name": V.file_name(name, ext), "file_type": mime, "file_size": len(content)}

    @staticmethod
    def _doc_out(r: dict, today: date) -> dict:
        out = _out(r)
        out["kind_label"] = V.DOC_KINDS.get(r["kind"], r["kind"])
        out["has_file"] = bool(r.get("file_name"))
        exp = r.get("expires_on")
        if exp is None:
            out["status"], out["days_left"] = "no_expiry", None
        else:
            d = (exp - today).days
            out["days_left"] = d
            out["status"] = "expired" if d < 0 else "expiring" if d <= DUE_SOON_DAYS else "valid"
        return out

    def documents(self, user: dict, car_id: str) -> list[dict]:
        today = date.today()
        with self.e.connect() as c:
            self._membership(c, user["id"], car_id)
            rows = c.execute(sa.select(T.documents).where(T.documents.c.car_id == car_id).order_by(T.documents.c.created_at))
            return [self._doc_out(_row(r), today) for r in rows]

    def add_document(self, user: dict, car_id: str, data: dict, content: bytes | None = None, filename: str = "") -> dict:
        v = self._doc_values(data, partial=False)
        f = self._check_file(content, filename)
        t = now()
        did = _uid()
        with self.e.begin() as c:
            self._membership(c, user["id"], car_id)
            n = c.execute(sa.select(sa.func.count()).select_from(T.documents).where(T.documents.c.car_id == car_id)).scalar()
            if n >= MAX_DOCS_PER_CAR:
                raise Conflict(f"At most {MAX_DOCS_PER_CAR} documents per car")
            c.execute(T.documents.insert().values(id=did, car_id=car_id, created_by=user["id"], created_at=t, updated_at=t, **v, **(f or {})))
            if f:
                c.execute(T.document_files.insert().values(document_id=did, content=content))
        return self.document(user, did)

    def _doc(self, c, uid: str, doc_id: str) -> tuple[dict, str]:
        r = _row(c.execute(sa.select(T.documents).where(T.documents.c.id == doc_id)).first())
        if not r:
            raise NotFound("Document not found")
        role = self._membership(c, uid, r["car_id"])                         # not a member -> "not found"
        return r, role

    def document(self, user: dict, doc_id: str) -> dict:
        with self.e.connect() as c:
            r, _ = self._doc(c, user["id"], doc_id)
        return self._doc_out(r, date.today())

    def update_document(self, user: dict, doc_id: str, data: dict, content: bytes | None = None, filename: str = "") -> dict:
        v = self._doc_values(data, partial=True)
        f = self._check_file(content, filename)
        with self.e.begin() as c:
            r, role = self._doc(c, user["id"], doc_id)
            if role != "owner" and r["created_by"] != user["id"]:
                raise Forbidden("Only an owner or the person who added it can change this document")
            merged = {**r, **v}
            if merged["issued_on"] and merged["expires_on"] and merged["expires_on"] < merged["issued_on"]:
                raise Invalid("'Valid until' is before 'Issued on'")
            c.execute(T.documents.update().where(T.documents.c.id == doc_id).values(updated_at=now(), **v, **(f or {})))
            if f:
                c.execute(T.document_files.delete().where(T.document_files.c.document_id == doc_id))
                c.execute(T.document_files.insert().values(document_id=doc_id, content=content))
        return self.document(user, doc_id)

    def delete_document(self, user: dict, doc_id: str) -> None:
        with self.e.begin() as c:
            r, role = self._doc(c, user["id"], doc_id)
            if role != "owner" and r["created_by"] != user["id"]:
                raise Forbidden("Only an owner or the person who added it can delete this document")
            c.execute(T.documents.delete().where(T.documents.c.id == doc_id))

    def document_file(self, user: dict, doc_id: str) -> tuple[bytes, str, str]:
        with self.e.connect() as c:
            r, _ = self._doc(c, user["id"], doc_id)
            content = c.execute(sa.select(T.document_files.c.content).where(T.document_files.c.document_id == doc_id)).scalar()
        if content is None:
            raise NotFound("This document has no file")
        return bytes(content), r["file_type"], r["file_name"]

    # ================================================================== reminders
    def _rem_values(self, data: dict, partial: bool) -> dict:
        v = {}
        try:
            if not partial or "title" in data:
                v["title"] = V.text(data.get("title"), "Title", 2, 80)
            if "due_on" in data or not partial:
                v["due_on"] = V.day(data.get("due_on"), "Due date")
            if "due_km" in data or not partial:
                v["due_km"] = V.km(data.get("due_km"), "Due at km")
            if "repeat" in data or not partial:
                v["repeat"] = V.choice(data.get("repeat") or "none", "Repeat", V.REPEATS)
            if "every_km" in data or not partial:
                v["every_km"] = V.km(data.get("every_km"), "Repeat every")
                if v["every_km"] is not None and not 500 <= v["every_km"] <= 100_000:
                    raise V.ValidationError("Repeat every must be between 500 and 100,000 km")
            if "notes" in data or not partial:
                v["notes"] = V.text(data.get("notes", ""), "Notes", 0, 300)
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        return v

    @staticmethod
    def _rem_check(r: dict) -> None:
        """Rules on the whole reminder (after an update is merged into the stored one)."""
        if r.get("due_on") is None and r.get("due_km") is None:
            raise Invalid("Give a due date, a due odometer reading, or both")
        if r.get("repeat", "none") != "none" and r.get("due_on") is None:
            raise Invalid("Repeating by date needs a due date")
        if r.get("every_km") is not None and r.get("due_km") is None:
            raise Invalid("Repeating by km needs a due odometer reading")

    def add_reminder(self, user: dict, car_id: str, data: dict) -> dict:
        v = self._rem_values(data, partial=False)
        self._rem_check(v)
        rid = _uid()
        with self.e.begin() as c:
            self._membership(c, user["id"], car_id)
            n = c.execute(sa.select(sa.func.count()).select_from(T.reminders).where(T.reminders.c.car_id == car_id)).scalar()
            if n >= MAX_REMINDERS_PER_CAR:
                raise Conflict(f"At most {MAX_REMINDERS_PER_CAR} reminders per car")
            c.execute(T.reminders.insert().values(id=rid, car_id=car_id, created_by=user["id"], created_at=now(), **v))
        return self._reminder(user, rid)

    def _rem(self, c, uid: str, rid: str) -> dict:
        r = _row(c.execute(sa.select(T.reminders).where(T.reminders.c.id == rid)).first())
        if not r:
            raise NotFound("Reminder not found")
        self._membership(c, uid, r["car_id"])
        return r

    def _reminder(self, user: dict, rid: str) -> dict:
        with self.e.connect() as c:
            r = self._rem(c, user["id"], rid)
            odo = c.execute(sa.select(T.cars.c.odometer_km).where(T.cars.c.id == r["car_id"])).scalar()
            kmpd = car_km_per_day(c, r["car_id"])
        return self._rem_out(r, date.today(), odo, kmpd)

    @staticmethod
    def _rem_out(r: dict, today: date, odo: int | None, kmpd: float | None = None) -> dict:
        """Status from the date, the km left, and (Phase C) the date the km will be reached at the car's usual km per day."""
        out = _out(r)
        d = (r["due_on"] - today).days if r.get("due_on") else None
        k = (r["due_km"] - odo) if (r.get("due_km") is not None and odo is not None) else None
        p = predicted_on(r.get("due_km"), odo, kmpd, today)
        out["days_left"], out["km_left"], out["predicted_on"] = d, k, _iso(p)
        pd = (p - today).days if p else None
        if r.get("done_at"):
            out["status"] = "done"
        elif (d is not None and d < 0) or (k is not None and k < 0):
            out["status"] = "overdue"
        elif (d is not None and d <= DUE_SOON_DAYS) or (k is not None and k <= 1000) or (pd is not None and pd <= DUE_SOON_DAYS):
            out["status"] = "due_soon"
        else:
            out["status"] = "upcoming"
        return out

    def update_reminder(self, user: dict, rid: str, data: dict) -> dict:
        v = self._rem_values(data, partial=True)
        with self.e.begin() as c:
            self._rem_check({**self._rem(c, user["id"], rid), **v})
            if v:
                c.execute(T.reminders.update().where(T.reminders.c.id == rid).values(**v))
        return self._reminder(user, rid)

    def complete_reminder(self, user: dict, rid: str) -> dict:
        """Done: a repeating reminder moves to its next due date and/or odometer reading (on the schedule, past today and the
        current reading, so a late service does not leave it overdue); a one-off reminder is closed."""
        step = {"monthly": 1, "quarterly": 3, "yearly": 12}
        today = date.today()
        with self.e.begin() as c:
            r = self._rem(c, user["id"], rid)
            if r["done_at"]:
                raise Conflict("This reminder is already done")
            vals = {}
            if r["repeat"] in step and r["due_on"]:
                nxt, n = _add_months(r["due_on"], step[r["repeat"]]), 1
                while nxt <= today and n < 600:
                    n += 1
                    nxt = _add_months(r["due_on"], step[r["repeat"]] * n)
                vals["due_on"] = nxt
            if r["every_km"] and r["due_km"] is not None:
                odo = c.execute(sa.select(T.cars.c.odometer_km).where(T.cars.c.id == r["car_id"])).scalar() or 0
                nxt = r["due_km"] + r["every_km"]
                while nxt <= odo:
                    nxt += r["every_km"]
                vals["due_km"] = nxt
            c.execute(T.reminders.update().where(T.reminders.c.id == rid).values(**(vals or {"done_at": now()})))
            self._event(c, user["id"], "reminder_done", r["title"])
        return self._reminder(user, rid)

    def delete_reminder(self, user: dict, rid: str) -> None:
        with self.e.begin() as c:
            self._rem(c, user["id"], rid)
            c.execute(T.reminders.delete().where(T.reminders.c.id == rid))

    def reminders(self, user: dict, car_id: str | None = None, include_done: bool = False) -> list[dict]:
        """Everything that needs attention, most urgent first: document expiries and the person's reminders."""
        today = date.today()
        out = []
        with self.e.connect() as c:
            cars = {x["id"]: x for x in self._cars(c, user["id"])}
            if car_id is not None and car_id not in cars:
                raise NotFound("Car not found")
            ids = [car_id] if car_id else list(cars)
            if not ids:
                return []
            for r in c.execute(sa.select(T.documents).where(T.documents.c.car_id.in_(ids), T.documents.c.expires_on.is_not(None))):
                d = self._doc_out(_row(r), today)
                exp = r.expires_on
                out.append({"type": "document", "id": d["id"], "car_id": d["car_id"], "car": cars[d["car_id"]]["title"],
                            "title": f"{d['title']} {'expired on' if d['status'] == 'expired' else 'expires on'} {exp.day} {exp:%b} {exp.year}",
                            "due_on": d["expires_on"], "days_left": d["days_left"], "km_left": None,
                            "status": {"expired": "overdue", "expiring": "due_soon", "valid": "upcoming"}[d["status"]], "repeat": None})
            kmpd = {cid: car_km_per_day(c, cid, today) for cid in ids}
            for r in c.execute(sa.select(T.reminders).where(T.reminders.c.car_id.in_(ids))):
                x = self._rem_out(_row(r), today, cars[r.car_id]["odometer_km"], kmpd[r.car_id])
                if x["status"] == "done" and not include_done:
                    continue
                out.append({"type": "reminder", "id": x["id"], "car_id": x["car_id"], "car": cars[x["car_id"]]["title"], "title": x["title"],
                            "due_on": x["due_on"], "due_km": x["due_km"], "days_left": x["days_left"], "km_left": x["km_left"],
                            "status": x["status"], "repeat": x["repeat"], "every_km": x["every_km"], "notes": x["notes"],
                            "predicted_on": x["predicted_on"]})
        rank = {"overdue": 0, "due_soon": 1, "upcoming": 2, "done": 3}
        def urgency(x):                    # days: the date, the predicted date for the km, or km at ~50 km a day when nothing else is known
            days = [x["days_left"]] if x["days_left"] is not None else []
            if x.get("predicted_on"):
                days.append((date.fromisoformat(x["predicted_on"]) - today).days)
            if days:
                return min(days)
            return x["km_left"] / 50 if x.get("km_left") is not None else 10**6
        return sorted(out, key=lambda x: (rank[x["status"]], urgency(x)))

    # ================================================================== home
    def dashboard(self, user: dict) -> dict:
        items = self.reminders(user)
        cars = self.cars(user)
        today = date.today()
        out_cars = []
        with self.e.connect() as c:
            for car in cars:
                docs = [self._doc_out(_row(r), today) for r in c.execute(sa.select(T.documents).where(T.documents.c.car_id == car["id"]))]
                counts = {k: sum(1 for d in docs if d["status"] == k) for k in ("valid", "expiring", "expired", "no_expiry")}
                missing = [k for k in ("rc", "insurance", "puc") if not any(d["kind"] == k for d in docs)
                           and not (k == "puc" and car["fuel"] == "electric")]           # electric cars need no PUC
                out_cars.append({**car, "documents": counts, "missing_documents": missing,
                                 "attention": sum(1 for i in items if i["car_id"] == car["id"] and i["status"] in ("overdue", "due_soon"))})
        me = self.me(user["id"])
        return {"user": me, "cars": out_cars, "attention": [i for i in items if i["status"] in ("overdue", "due_soon")][:10],
                "counts": {"overdue": sum(1 for i in items if i["status"] == "overdue"), "due_soon": sum(1 for i in items if i["status"] == "due_soon")}}

    # ================================================================== demo data
    def _seed_demo(self, c, uid: str) -> None:
        t, today = now(), date.today()
        for i, (make, model, fuel, reg, year, odo, nick) in enumerate((
                ("Tata", "Nexon", "petrol", "MH12AB1234", 2023, 24310, "Family car"),
                ("Tata", "Tiago EV", "electric", "MH14CD5678", 2025, 6120, "City runabout"))):
            cid = _uid()
            c.execute(T.cars.insert().values(id=cid, nickname=nick, make=make, model=model, variant="", fuel=fuel, year=year, reg_number=reg,
                                             vin="", colour="", odometer_km=odo, odometer_at=t, created_by=uid, created_at=t, updated_at=t))
            c.execute(T.car_members.insert().values(car_id=cid, user_id=uid, role="owner", created_at=t))
            docs = [("rc", "Registration certificate (RC)", None, today - timedelta(days=730 if i == 0 else 365)),
                    ("insurance", "Insurance", today + timedelta(days=12 if i == 0 else 190), today - timedelta(days=353 if i == 0 else 175))]
            if fuel != "electric":
                docs.append(("puc", "PUC certificate", today - timedelta(days=3), today - timedelta(days=183)))
            for kind, title, exp, iss in docs:
                c.execute(T.documents.insert().values(id=_uid(), car_id=cid, kind=kind, title=title, number="", issuer="", issued_on=iss,
                                                      expires_on=exp, notes="Sample document (demo)", created_by=uid, created_at=t, updated_at=t))
            c.execute(T.reminders.insert().values(id=_uid(), car_id=cid, title="Periodic service" if fuel != "electric" else "Annual service",
                                                  due_on=today + timedelta(days=20 if i == 0 else 140), due_km=(25000 if i == 0 else 10000),
                                                  repeat="yearly", every_km=10000, notes="", created_by=uid, created_at=t))
            if i == 0:
                c.execute(T.reminders.insert().values(id=_uid(), car_id=cid, title="Tyre rotation", due_on=None, due_km=25500, repeat="none",
                                                      notes="", created_by=uid, created_at=t))
            from owner import costs, health                                     # Phase C sample data (imported here: they import this module)
            costs.seed_demo(c, cid, uid, fuel, odo)
            if i == 0:
                health.demo_scan(c, cid, uid, t - timedelta(days=9))
        from owner import booking                                               # Phase D: a sample workshop to book with
        booking.seed_demo(c, uid)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return d.replace(year=y, month=m, day=day)
        except ValueError:
            continue
    return d


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, default=_iso)
