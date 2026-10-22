"""Notifications (Phase D): the in-app inbox, reminder emails, and push to phones and browsers.

What is notified, once each. The key of a notification names the item, its due date or km and the stage, and is unique per
person, so starting the daily run twice (or on two servers) never repeats one:
  documents   30, 7 and 1 day before the expiry date, on the day, then once a week for four weeks
  reminders   7 and 1 day before the due date (or the date the km will be reached at the car's km per day), on the day,
              then weekly for four weeks; a km-only reminder on a car without enough odometer history at 500 km before,
              and when the km are passed
  health      a car's health check finds something that needs attention now (red), once per check
  bookings    every step the workshop takes, and the day before the appointment (booking.py)

Channels: the inbox always; email (one digest per run; never to demo accounts); Web Push to every browser where the person
turned it on; the Android app, which can't receive Web Push, schedules the date-based ones on the phone itself
(GET /api/notifications/schedule). The daily run is started from outside, because a free instance sleeps:
POST /api/notify/run with OWNER_CRON_TOKEN (a GitHub Actions schedule is included), or `python -m owner.notify run`."""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import uuid
import zlib
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

import sqlalchemy as sa

from owner import db as T
from owner import mailer, webpush
from owner.db import now, now_precise
from owner.service import Invalid, NotFound, OwnerService, TooMany

log = logging.getLogger("owner.notify")
IST = timezone(timedelta(hours=5, minutes=30), "IST")             # India has one time zone and no daylight saving
KINDS = ("reminders", "documents", "health", "bookings")
KIND_LABELS = {"reminders": "Service and other reminders", "documents": "Document expiry (insurance, PUC, ...)",
               "health": "Car health problems", "bookings": "Service bookings"}
DOC_LEAD, REM_LEAD = (30, 7, 1), (7, 1)
LATE_WEEKS = 4
MAX_INBOX = 200
MAX_DEVICES = 10
SEND_HOUR = 9                                                     # local time of the phone notifications the app schedules
DOC_HINT = {"insurance": "Renew it before the date to stay covered.", "puc": "Any authorised PUC centre issues a new one in minutes.",
            "rc": "Renew the registration at the RTO.", "fastag": "Recharge or renew it in your bank's app."}


def _uid() -> str:
    return str(uuid.uuid4())


def _row(r) -> dict | None:
    return dict(r._mapping) if r is not None else None


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _day(d: date) -> str:
    return f"{d.day} {d:%b}"


def stage(days: int | None, lead: tuple) -> str | None:
    """The furthest stage reached by a date: 'd30', 'd7', 'd1', 'due', then 'late0'..'late4' (weeks overdue); None if not yet."""
    if days is None:
        return None
    if days < 0:
        return f"late{min(LATE_WEEKS, (-days) // 7)}"
    if days == 0:
        return "due"
    for d in sorted(lead):
        if days <= d:
            return f"d{d}"
    return None


def when_text(days: int) -> str:
    if days < -1:
        return f"{-days} days ago"
    return {-1: "yesterday", 0: "today", 1: "tomorrow"}.get(days, f"in {days} days")


def device_id(key: str) -> int:
    """A stable positive 31-bit id for the phone's notification of this key (Android needs an int)."""
    return zlib.crc32(key.encode()) & 0x7FFFFFFF


class NotifyService:
    def __init__(self, owner: OwnerService, health=None):
        self.o, self.e, self.s = owner, owner.e, owner.s
        self.health = health
        self.push_post = webpush.post                     # replaced in tests by a stand-in push service
        self.send_email = mailer.send_email

    # ================================================================== settings
    def _prefs(self, c, uid: str) -> dict:
        r = _row(c.execute(sa.select(T.notification_prefs).where(T.notification_prefs.c.user_id == uid)).first())
        if not r:
            return {"email": True, "push": True, "kinds": list(KINDS)}
        return {"email": r["email"], "push": r["push"], "kinds": [k for k in r["kinds"].split(",") if k in KINDS]}

    def prefs(self, user: dict) -> dict:
        with self.e.connect() as c:
            p = self._prefs(c, user["id"])
            devices = [{"id": r.id, "label": r.label, "created_at": _iso(r.created_at), "last_ok_at": _iso(r.last_ok_at)}
                       for r in c.execute(sa.select(T.push_subscriptions).where(T.push_subscriptions.c.user_id == user["id"])
                                          .order_by(T.push_subscriptions.c.created_at))]
        email_ok = self.s.email_configured and not user["is_demo"]           # OWNER_REMINDER_EMAILS=0 stops only the digest
        return {**p, "kind_labels": KIND_LABELS, "email_address": user["email"] if not user["is_demo"] else "",
                "email_available": email_ok,
                "email_note": "" if email_ok else ("Demo accounts get no emails." if user["is_demo"] else "This server does not send emails yet."),
                "push_available": self.s.push_enabled, "devices": devices}

    def update_prefs(self, user: dict, data: dict) -> dict:
        self._save_prefs(user["id"], data)
        return self.prefs(user)

    def _save_prefs(self, uid: str, data: dict) -> None:
        with self.e.begin() as c:
            p = self._prefs(c, uid)
            if "email" in data:
                p["email"] = bool(data["email"])
            if "push" in data:
                p["push"] = bool(data["push"])
            if "kinds" in data:
                kinds = data["kinds"]
                if not isinstance(kinds, list) or any(k not in KINDS for k in kinds):
                    raise Invalid(f"Kinds must be some of: {', '.join(KINDS)}")
                p["kinds"] = [k for k in KINDS if k in kinds]
            vals = {"email": p["email"], "push": p["push"], "kinds": ",".join(p["kinds"]), "updated_at": now()}
            if c.execute(sa.select(T.notification_prefs.c.user_id).where(T.notification_prefs.c.user_id == uid)).first():
                c.execute(T.notification_prefs.update().where(T.notification_prefs.c.user_id == uid).values(**vals))
            else:
                c.execute(T.notification_prefs.insert().values(user_id=uid, **vals))

    # ================================================================== devices (Web Push)
    def subscribe(self, user: dict, endpoint: str, p256dh: str, auth: str, label: str = "") -> dict:
        if not self.s.push_enabled:
            raise Invalid("Push notifications are not set up on this server")
        try:
            webpush.check_subscription(endpoint, p256dh, auth)
        except webpush.PushError as e:
            raise Invalid(str(e)) from e
        label = " ".join(str(label or "").split())[:60] or "This browser"
        t = now()
        with self.e.begin() as c:
            old = c.execute(sa.select(T.push_subscriptions.c.id).where(T.push_subscriptions.c.endpoint == endpoint)).first()
            if old:            # the same browser, perhaps now signed in as someone else: it belongs to the person signing up now
                c.execute(T.push_subscriptions.update().where(T.push_subscriptions.c.id == old.id).values(
                    user_id=user["id"], p256dh=p256dh, auth=auth, label=label, failures=0))
                sid = old.id
            else:
                mine = c.execute(sa.select(T.push_subscriptions.c.id).where(T.push_subscriptions.c.user_id == user["id"])
                                 .order_by(T.push_subscriptions.c.created_at)).all()
                for r in mine[:max(0, len(mine) - MAX_DEVICES + 1)]:                 # keep the newest devices
                    c.execute(T.push_subscriptions.delete().where(T.push_subscriptions.c.id == r.id))
                sid = _uid()
                c.execute(T.push_subscriptions.insert().values(id=sid, user_id=user["id"], endpoint=endpoint, p256dh=p256dh, auth=auth,
                                                               label=label, created_at=t, failures=0))
        return {"id": sid, "label": label}

    def unsubscribe(self, user: dict, endpoint: str | None = None, device_id_: str | None = None) -> None:
        q = T.push_subscriptions.delete().where(T.push_subscriptions.c.user_id == user["id"])
        if device_id_:
            q = q.where(T.push_subscriptions.c.id == device_id_)
        elif endpoint:
            q = q.where(T.push_subscriptions.c.endpoint == endpoint)
        else:
            raise Invalid("Say which device")
        with self.e.begin() as c:
            if not c.execute(q).rowcount and device_id_:
                raise NotFound("Device not found")

    # ================================================================== inbox
    def inbox(self, user: dict, limit: int = 60) -> dict:
        with self.e.connect() as c:
            rows = c.execute(sa.select(T.notifications).where(T.notifications.c.user_id == user["id"])
                             .order_by(T.notifications.c.created_at.desc(), T.notifications.c.id).limit(limit))
            items = [{k: _iso(v) for k, v in _row(r).items() if k not in ("user_id", "key")} for r in rows]
        return {"items": items, "unread": self.unread(user)}

    def unread(self, user: dict) -> int:
        with self.e.connect() as c:
            return c.execute(sa.select(sa.func.count()).select_from(T.notifications).where(
                T.notifications.c.user_id == user["id"], T.notifications.c.read_at.is_(None))).scalar()

    def mark_read(self, user: dict, ids: list[str] | None = None) -> dict:
        q = T.notifications.update().where(T.notifications.c.user_id == user["id"], T.notifications.c.read_at.is_(None))
        if ids is not None:
            if not isinstance(ids, list) or len(ids) > 200:
                raise Invalid("Give up to 200 ids")
            q = q.where(T.notifications.c.id.in_([str(i) for i in ids]))
        with self.e.begin() as c:
            c.execute(q.values(read_at=now()))
        return {"unread": self.unread(user)}

    # ================================================================== what is due
    def candidates(self, c, user: dict, today: date) -> list[dict]:
        """The notifications this person should have by now (before removing the ones already made)."""
        out: list[dict] = []
        cars = {x["id"]: x for x in self.o._cars(c, user["id"])}
        if not cars:
            return out
        ids = list(cars)

        def label(cid):
            car = cars[cid]
            return f"{car['title']} ({car['reg_display']})"

        for r in c.execute(sa.select(T.documents).where(T.documents.c.car_id.in_(ids), T.documents.c.expires_on.is_not(None))):
            days = (r.expires_on - today).days
            st = stage(days, DOC_LEAD)
            if st:
                title = f"{r.title} {'expired' if days < 0 else 'expires'} {when_text(days)}"
                out.append({"kind": "documents", "car_id": r.car_id, "key": f"doc:{r.id}:{r.expires_on}:{st}",
                            "title": title, "body": f"{label(r.car_id)}. {DOC_HINT.get(r.kind, 'Open Documents to update it.')}",
                            "path": f"/cars/{r.car_id}?tab=documents"})
        from owner.odometer import car_km_per_day
        for cid in ids:
            kmpd = car_km_per_day(c, cid, today)
            odo = cars[cid]["odometer_km"]
            for r in c.execute(sa.select(T.reminders).where(T.reminders.c.car_id == cid, T.reminders.c.done_at.is_(None))):
                x = self.o._rem_out(_row(r), today, odo, kmpd)
                due_sig = f"{r.due_on or ''}|{r.due_km if r.due_km is not None else ''}"
                days = [d for d in (x["days_left"], (date.fromisoformat(x["predicted_on"]) - today).days if x["predicted_on"] else None)
                        if d is not None]
                k = x["km_left"]
                if k is not None and k < 0 and not (x["days_left"] is not None and x["days_left"] < 0):
                    st, text = "kmlate", f"overdue by {-k:,} km"
                elif days:
                    d = min(days)
                    st = stage(d, REM_LEAD)
                    text = (f"overdue by {-d} day{'s' if d < -1 else ''}" if d < 0 else "due today" if d == 0 else
                            "due tomorrow" if d == 1 else f"due in {d} days")
                    if x["days_left"] is None and d >= 0 and k is not None:         # predicted from the km
                        text = f"due in about {k:,} km ({'today' if d == 0 else 'about ' + _day(today + timedelta(days=d))})"
                elif k is not None and k <= 500:
                    st, text = "km500", f"due in {k:,} km"
                else:
                    st = None
                if st:
                    at_km = f", at {r.due_km:,} km" if r.due_km is not None else ""
                    out.append({"kind": "reminders", "car_id": cid, "key": f"rem:{r.id}:{due_sig}:{st}",
                                "title": f"{r.title} {text}", "body": f"{label(cid)}{at_km}.", "path": f"/cars/{cid}?tab=reminders"})
        if self.health is not None:
            for cid in ids:
                last = c.execute(sa.select(T.health_scans.c.id).where(T.health_scans.c.car_id == cid)
                                 .order_by(T.health_scans.c.scanned_at.desc()).limit(1)).first()
                if not last:
                    continue
                h = self.health.health(user, cid)
                red = [f for f in h["findings"] if f["level"] == "red" and f["kind"] in ("code", "battery", "charging", "coolant")]
                if red:
                    out.append({"kind": "health", "car_id": cid, "key": f"health:{cid}:{last.id}:red",
                                "title": f"{cars[cid]['title']} needs attention", "body": f"{red[0]['title']}. Open Health to see what to do.",
                                "path": f"/cars/{cid}?tab=health"})
        for r in c.execute(sa.select(T.bookings).where(T.bookings.c.car_id.in_(ids), T.bookings.c.status == "confirmed",
                                                         T.bookings.c.appointment_on == today + timedelta(days=1))):
            out.append({"kind": "bookings", "car_id": r.car_id, "key": f"booking:{r.id}:{r.appointment_on}:{r.appointment_time}:d1",
                        "title": f"Service tomorrow at {r.appointment_time}", "body": f"{r.workshop_name}, {label(r.car_id)}.",
                        "path": f"/bookings/{r.id}"})
        return out

    # ================================================================== making and delivering
    def _insert_new(self, uid: str, items: list[dict]) -> list[dict]:
        """Store the ones not made before (-> the new rows). The unique key settles a race with a concurrent run."""
        if not items:
            return []
        with self.e.connect() as c:
            have = {r.key for r in c.execute(sa.select(T.notifications.c.key).where(
                T.notifications.c.user_id == uid, T.notifications.c.key.in_([i["key"] for i in items])))}
        new = []
        for i in items:
            if i["key"] in have:
                continue
            row = {"id": _uid(), "user_id": uid, "car_id": i.get("car_id"), "kind": i["kind"], "key": i["key"][:160],
                   "title": i["title"][:160], "body": i.get("body", "")[:400], "path": i.get("path", "/")[:160], "created_at": now_precise()}
            try:
                with self.e.begin() as c:
                    c.execute(T.notifications.insert().values(**row))
                new.append(row)
            except sa.exc.IntegrityError:
                continue
        return new

    def _trim(self, c, uid: str) -> None:
        old = c.execute(sa.select(T.notifications.c.id).where(T.notifications.c.user_id == uid)
                        .order_by(T.notifications.c.created_at.desc()).offset(MAX_INBOX)).all()
        if old:
            c.execute(T.notifications.delete().where(T.notifications.c.id.in_([r.id for r in old])))
        c.execute(T.notifications.delete().where(T.notifications.c.user_id == uid, T.notifications.c.created_at < now() - timedelta(days=120)))

    def collect(self, uid: str, today: date | None = None, deliver: bool = True) -> list[dict]:
        """Make this person's due notifications (-> the new ones), and send them unless deliver is False."""
        today = today or date.today()
        with self.e.connect() as c:
            user = _row(c.execute(sa.select(T.users).where(T.users.c.id == uid)).first())
            if not user:
                return []
            kinds = set(self._prefs(c, uid)["kinds"])
            items = [i for i in self.candidates(c, user, today) if i["kind"] in kinds]
        new = self._insert_new(uid, items)
        with self.e.begin() as c:
            self._trim(c, uid)
        if new and deliver:
            self.deliver(user, new, digest=True)
        elif new:
            self._mark(new, email="off", push="off")
        return new

    def run(self, today: date | None = None) -> dict:
        """The daily run: every person with a car. One person's failure never stops the others."""
        today = today or date.today()
        self.o.cleanup_demo()
        stats = Counter(people=0, created=0, emailed=0, pushed=0, errors=0)
        with self.e.connect() as c:
            uids = [r.user_id for r in c.execute(sa.select(T.car_members.c.user_id).distinct())]
        for uid in uids:
            stats["people"] += 1
            try:
                new = self.collect(uid, today)
            except Exception:                                                   # noqa: BLE001 - next person
                log.exception("Notifications for one person failed")
                stats["errors"] += 1
                continue
            stats["created"] += len(new)
            with self.e.connect() as c:
                st = c.execute(sa.select(T.notifications.c.email_status, T.notifications.c.push_status).where(
                    T.notifications.c.id.in_([n["id"] for n in new]))).all() if new else []
            stats["emailed"] += int(any(r.email_status == "sent" for r in st))
            stats["pushed"] += int(any(r.push_status == "sent" for r in st))
        log.info("Daily notification run: %s", dict(stats))
        return {"date": today.isoformat(), **stats}

    def send_now(self, uids: list[str], item: dict) -> int:
        """An event that can't wait for the daily run (the workshop confirmed, ...): to each of these people, by every
        channel they allow. -> how many people got it."""
        n = 0
        for uid in dict.fromkeys(uids):
            with self.e.connect() as c:
                user = _row(c.execute(sa.select(T.users).where(T.users.c.id == uid)).first())
                if not user or item["kind"] not in self._prefs(c, uid)["kinds"]:
                    continue
            new = self._insert_new(uid, [item])
            if new:
                self.deliver(user, new, digest=False)
                n += 1
        return n

    def _mark(self, rows: list[dict], email: str | None = None, push: str | None = None) -> None:
        vals = {k: v for k, v in (("email_status", email), ("push_status", push)) if v is not None}
        if rows and vals:
            with self.e.begin() as c:
                c.execute(T.notifications.update().where(T.notifications.c.id.in_([r["id"] for r in rows])).values(**vals))

    def deliver(self, user: dict, rows: list[dict], digest: bool) -> None:
        with self.e.connect() as c:
            p = self._prefs(c, user["id"])
        self._mark(rows, email=self._email(user, rows, p, digest))
        self._mark(rows, push=self._push(user, rows, p))

    # ------------------------------------------------------------------ email
    def unsubscribe_token(self, uid: str) -> str:
        return hmac.new(self.s.secret_key.encode(), f"unsubscribe:{uid}".encode(), hashlib.sha256).hexdigest()[:40]

    def unsubscribe_url(self, uid: str) -> str:
        return f"{self.s.public_url}/api/notify/unsubscribe?u={uid}&t={self.unsubscribe_token(uid)}" if self.s.public_url else ""

    def email_unsubscribe(self, uid: str, token: str) -> bool:
        """One-click unsubscribe from the link in the email (RFC 8058): reminder and booking emails off."""
        if not (uid and token and hmac.compare_digest(self.unsubscribe_token(uid).encode(), str(token).encode())):
            raise NotFound("This link is not valid")
        with self.e.connect() as c:
            if not c.execute(sa.select(T.users.c.id).where(T.users.c.id == uid)).first():
                raise NotFound("This link is not valid")
        self._save_prefs(uid, {"email": False})
        return True

    def _email(self, user: dict, rows: list[dict], p: dict, digest: bool) -> str:
        if not (p["email"] and self.s.email_configured and not user["is_demo"]) or (digest and not self.s.reminder_emails):
            return "off"
        subject, text, body = self.compose_email(user, rows)
        unsub = self.unsubscribe_url(user["id"])
        headers = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"} if unsub else {}
        try:
            self.send_email(self.s, user["email"], subject, text, body, headers, tag="reminders" if digest else "booking")
            return "sent"
        except mailer.MailError:
            return "failed"

    def compose_email(self, user: dict, rows: list[dict]) -> tuple[str, str, str]:
        app, base = self.s.app_name, self.s.public_url
        first = (user.get("name") or "").split(" ")[0] or "there"
        subject = f"{app}: {rows[0]['title']}" + (f" and {len(rows) - 1} more" if len(rows) > 1 else "")
        lines = [f"Hello {first},", "", "This needs your attention:" if len(rows) == 1 else "These need your attention:", ""]
        for r in rows:
            lines += [f"- {r['title']}", f"  {r['body']}"] + ([f"  {base}{r['path']}"] if base else [])
        unsub = self.unsubscribe_url(user["id"])
        lines += ["", f"You get these emails because notifications are on in {app} (Profile > Notifications)."]
        if unsub:
            lines.append(f"Turn off these emails: {unsub}")
        e = html.escape
        items = "".join(
            f"<tr><td style='padding:10px 0;border-bottom:1px solid #dde3e1'><strong>{e(r['title'])}</strong><br>"
            f"<span style='color:#4b5a64'>{e(r['body'])}</span>"
            + (f"<br><a href='{e(base + r['path'])}' style='color:#1f4fa3'>Open in {e(app)}</a>" if base else "") + "</td></tr>" for r in rows)
        body = (f"<div style='font-family:Arial,sans-serif;font-size:15px;color:#1e2a32;max-width:560px'>"
                f"<p>Hello {e(first)},</p><p>{'This needs' if len(rows) == 1 else 'These need'} your attention:</p>"
                f"<table style='border-collapse:collapse;width:100%'>{items}</table>"
                f"<p style='color:#74838c;font-size:13px;margin-top:24px'>You get these emails because notifications are on in {e(app)} "
                f"(Profile &gt; Notifications)." + (f" <a href='{e(unsub)}' style='color:#74838c'>Turn off these emails</a>." if unsub else "")
                + "</p></div>")
        return subject, "\n".join(lines) + "\n", body

    # ------------------------------------------------------------------ push
    def _push(self, user: dict, rows: list[dict], p: dict) -> str:
        if not (p["push"] and self.s.push_enabled):
            return "off"
        with self.e.connect() as c:
            subs = [_row(r) for r in c.execute(sa.select(T.push_subscriptions).where(T.push_subscriptions.c.user_id == user["id"]))]
        if not subs:
            return "off"
        if len(rows) > 2:                                       # one summary instead of a burst
            messages = [{"title": f"{len(rows)} things need your attention", "body": "; ".join(r["title"] for r in rows[:3])[:180],
                         "path": "/notifications", "tag": "digest"}]
        else:
            messages = [{"title": r["title"], "body": r["body"][:180], "path": r["path"], "tag": r["key"][:60]} for r in rows]
        ok = False
        for sub in subs:
            for m in messages:
                try:
                    url, headers, body = webpush.request(sub, m, subject=self.s.vapid_subject, pub_b64=self.s.vapid_public_key,
                                                         priv_b64=self.s.vapid_private_key,
                                                         # the push service sees the topic: a hash, not our ids
                                                         topic=None if len(messages) > 1 else hashlib.sha256(m["tag"].encode()).hexdigest()[:32])
                except webpush.PushError:
                    status = 400
                else:
                    status = self.push_post(url, headers, body)
                with self.e.begin() as c:
                    q = T.push_subscriptions.c.id == sub["id"]
                    if status in (200, 201, 202):
                        ok = True
                        c.execute(T.push_subscriptions.update().where(q).values(last_ok_at=now(), failures=0))
                    elif status in (404, 410) or status == 400:         # gone (unsubscribed, expired) or unusable: forget it
                        c.execute(T.push_subscriptions.delete().where(q))
                        break
                    else:
                        c.execute(T.push_subscriptions.update().where(q).values(failures=T.push_subscriptions.c.failures + 1))
                        if sub["failures"] + 1 >= 10:
                            c.execute(T.push_subscriptions.delete().where(q))
                        break
        return "sent" if ok else "failed"

    def test(self, user: dict) -> dict:
        """'Send a test': to every channel the person has on, now."""
        ok, retry = self.o.limits.allow(f"notify-test:{user['id']}", 3, 600)
        if not ok:
            raise TooMany("You sent 3 tests in the last 10 minutes. Try again later.", retry)
        item = {"kind": "test", "key": f"test:{uuid.uuid4().hex}", "title": "Test notification",
                "body": f"Notifications from {self.s.app_name} reach you here.", "path": "/notifications", "car_id": None}
        new = self._insert_new(user["id"], [item])
        self.deliver(user, new, digest=False)
        with self.e.connect() as c:
            r = c.execute(sa.select(T.notifications.c.email_status, T.notifications.c.push_status).where(T.notifications.c.id == new[0]["id"])).first()
        return {"email": r.email_status, "push": r.push_status}

    # ================================================================== the phone's own schedule (Android app)
    def schedule(self, user: dict, today: date | None = None, horizon_days: int = 45) -> dict:
        """Date-based notifications for the next weeks, at 9:00 India time, for the Android app to schedule on the phone (it
        can't receive Web Push). The app replaces its whole schedule with this list each time it opens."""
        today = today or date.today()
        out = []
        with self.e.connect() as c:
            p = self._prefs(c, user["id"])
            if not p["push"]:
                return {"items": [], "push": False}
            cars = {x["id"]: x for x in self.o._cars(c, user["id"])}
            ids = list(cars)
            if not ids:
                return {"items": [], "push": True}
            from owner.odometer import car_km_per_day

            def add(kind, key, on: date, title, body, path):
                if kind in p["kinds"] and today <= on <= today + timedelta(days=horizon_days):
                    at = datetime.combine(on, time(SEND_HOUR, 0), IST)
                    out.append({"id": device_id(key), "key": key, "at": at.isoformat(), "title": title, "body": body, "path": path})

            for r in c.execute(sa.select(T.documents).where(T.documents.c.car_id.in_(ids), T.documents.c.expires_on.is_not(None))):
                car = cars[r.car_id]
                for lead in (*DOC_LEAD, 0):
                    on = r.expires_on - timedelta(days=lead)
                    add("documents", f"doc:{r.id}:{r.expires_on}:{'due' if lead == 0 else f'd{lead}'}", on,
                        f"{r.title} expires {when_text(lead)}", f"{car['title']} ({car['reg_display']}). {DOC_HINT.get(r.kind, '')}".strip(),
                        f"/cars/{r.car_id}?tab=documents")
            for cid in ids:
                kmpd = car_km_per_day(c, cid, today)
                car = cars[cid]
                for r in c.execute(sa.select(T.reminders).where(T.reminders.c.car_id == cid, T.reminders.c.done_at.is_(None))):
                    x = self.o._rem_out(_row(r), today, car["odometer_km"], kmpd)
                    dates = [d for d in (r.due_on, date.fromisoformat(x["predicted_on"]) if x["predicted_on"] else None) if d]
                    if not dates:
                        continue
                    due = min(dates)
                    for lead in (*REM_LEAD, 0):
                        add("reminders", f"rem:{r.id}:{r.due_on or ''}|{r.due_km if r.due_km is not None else ''}:{'due' if lead == 0 else f'd{lead}'}",
                            due - timedelta(days=lead), f"{r.title} due {when_text(lead)}",
                            f"{car['title']} ({car['reg_display']}){f', at {r.due_km:,} km' if r.due_km is not None else ''}.", f"/cars/{cid}?tab=reminders")
            for r in c.execute(sa.select(T.bookings).where(T.bookings.c.car_id.in_(ids), T.bookings.c.status == "confirmed",
                                                             T.bookings.c.appointment_on.is_not(None))):
                add("bookings", f"booking:{r.id}:{r.appointment_on}:{r.appointment_time}:d1", r.appointment_on - timedelta(days=1),
                    f"Service tomorrow at {r.appointment_time}", f"{r.workshop_name}, {cars[r.car_id]['title']}.", f"/bookings/{r.id}")
        out.sort(key=lambda i: i["at"])
        return {"items": out[:60], "push": True}

    # ================================================================== privacy
    def export_rows(self, uid: str) -> dict:
        with self.e.connect() as c:
            return {"notification_settings": self._prefs(c, uid),
                    "notifications": [{k: _iso(v) for k, v in _row(r).items() if k != "user_id"}
                                      for r in c.execute(sa.select(T.notifications).where(T.notifications.c.user_id == uid))],
                    "push_devices": [{"label": r.label, "service": r.endpoint.split("/")[2], "created_at": _iso(r.created_at)}
                                     for r in c.execute(sa.select(T.push_subscriptions).where(T.push_subscriptions.c.user_id == uid))]}


def main(argv: list[str] | None = None) -> int:
    """python -m owner.notify run     the daily run (cron, Render cron job, or by hand)
       python -m owner.notify vapid   print a new Web Push key pair for OWNER_VAPID_PUBLIC_KEY / OWNER_VAPID_PRIVATE_KEY"""
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python -m owner.notify", description="CarSaathi notifications")
    ap.add_argument("command", choices=["run", "vapid"])
    a = ap.parse_args(argv)
    if a.command == "vapid":
        pub, priv = webpush.generate_vapid()
        print(f"OWNER_VAPID_PUBLIC_KEY={pub}\nOWNER_VAPID_PRIVATE_KEY={priv}\nOWNER_VAPID_SUBJECT=mailto:you@example.com")
        return 0
    from owner import config as C
    from owner.costs import CostService
    from owner.health import HealthService
    s = C.load()
    errors, _ = C.check(s)
    if errors:
        print("Configuration: " + "; ".join(errors))
        return 2
    eng = T.engine(s.database_url)
    T.init(eng)
    svc = OwnerService(s, eng)
    print(json.dumps(NotifyService(svc, HealthService(svc, CostService(svc))).run()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
