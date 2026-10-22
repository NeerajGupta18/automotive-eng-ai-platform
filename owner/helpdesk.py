"""Help for car problems (Phase B): search, guides, help requests, and reports to the manufacturer.

A guide runs in the app, step by step. When it ends, the app records the outcome anonymously (which guide, fixed or not,
at which step: no user, no car) so the guides can be improved. When nothing helped, the owner creates a help request: the
steps tried and the answers given, with photos, as a summary to send to their service centre. For driver-assistance
problems the owner can also report it, anonymised and with their consent, to the engineering platform (owner/bridge.py).

The transcript is rebuilt on the server from the guide: the app sends which step and which answer, and the facts (what the
car did, where) come from the guide's own options, never from the request."""
from __future__ import annotations

import json
import logging
from datetime import date

import sqlalchemy as sa

from owner import bridge
from owner import db as T
from owner import helpcontent as H
from owner import helpsearch
from owner import validators as V
from owner.db import now
from owner.service import Conflict, Forbidden, Invalid, NotFound, OwnerService, TooMany, _iso, _row, _uid

log = logging.getLogger("owner.help")
MAX_OPEN_CASES = 30
MAX_PHOTOS = 3
ANSWERS = {"check": ("fixed", "not_fixed", "done"), "info": ("seen",), "ask": ("option",)}
ANSWER_TEXT = {"fixed": "That fixed it", "not_fixed": "Didn't help", "done": "Done", "seen": "Read"}


class HelpService:
    def __init__(self, owner: OwnerService):
        self.o, self.s, self.e = owner, owner.s, owner.e
        self.llm = (helpsearch.openai_compatible(self.s.llm_base_url, self.s.llm_api_key, self.s.llm_model)
                    if self.s.llm_enabled else None)

    # ------------------------------------------------------------------ content
    def topics(self, fuel: str | None = None) -> dict:
        c = H.load()
        return {"topics": [{**t, "guides": [H.summary(g) for g in c["guides"].values() if g["category"] == t["id"] and H.applies(g, fuel)]}
                           for t in c["topics"]],
                "lights": len([li for li in c["lights"] if H.applies(li, fuel)])}

    def guide(self, gid: str) -> dict:
        g = H.load()["guides"].get(gid)
        if not g:
            raise NotFound("Guide not found")
        return {**g, "reportable": "report" in g}

    def lights(self, fuel: str | None = None) -> list[dict]:
        return [li for li in H.load()["lights"] if H.applies(li, fuel)]

    def _car_for(self, user: dict, car_id: str | None) -> dict | None:
        return self.o.car(user, car_id) if car_id else None                    # membership checked: others' cars are "not found"

    def search(self, user: dict, q: str, car_id: str | None = None) -> dict:
        q = " ".join(str(q or "").split())
        if not 2 <= len(q) <= 500:
            raise Invalid("Describe the problem in 2 to 500 characters")
        ok, retry = self.o.limits.allow(f"help-search:{user['id']}", 120, 3600)
        if not ok:
            raise TooMany("Too many searches. Wait a little and try again.", retry)
        car = self._car_for(user, car_id)
        llm = self.llm if self.llm and self.o.limits.allow(f"help-llm:{user['id']}", 30, 3600)[0] else None
        return helpsearch.search(q, fuel=car["fuel"] if car else None, roadside=(car or {}).get("roadside_phone", ""), llm=llm)

    def record_outcome(self, user: dict, guide_id: str, outcome: str, step_id: str | None) -> dict:
        g = H.load()["guides"].get(guide_id)
        if not g or outcome not in ("fixed", "escalated", "stopped"):
            raise Invalid("Unknown guide or outcome")
        if step_id is not None and step_id not in {s["id"] for s in g["steps"]}:
            raise Invalid("Unknown step")
        ok, _ = self.o.limits.allow(f"help-outcome:{user['id']}", 60, 3600)
        if ok:                                                                 # past the limit: accepted but not counted
            with self.e.begin() as c:
                c.execute(T.help_outcomes.insert().values(guide_id=guide_id, outcome=outcome, step_id=step_id, day=date.today()))
        return {"recorded": ok}

    def stats(self) -> dict:
        guides = H.load()["guides"]
        with self.e.connect() as c:
            rows = c.execute(sa.select(T.help_outcomes.c.guide_id, T.help_outcomes.c.outcome, T.help_outcomes.c.step_id,
                                       sa.func.count().label("n")).group_by(T.help_outcomes.c.guide_id, T.help_outcomes.c.outcome,
                                                                            T.help_outcomes.c.step_id)).all()
        out: dict[str, dict] = {}
        for r in rows:
            g = out.setdefault(r.guide_id, {"id": r.guide_id, "title": guides.get(r.guide_id, {}).get("title", r.guide_id),
                                            "fixed": 0, "escalated": 0, "stopped": 0, "fixed_at": {}})
            g[r.outcome] += r.n
            if r.outcome == "fixed" and r.step_id:
                g["fixed_at"][r.step_id] = g["fixed_at"].get(r.step_id, 0) + r.n
        for g in out.values():
            done = g["fixed"] + g["escalated"]
            g["fix_rate"] = round(g["fixed"] / done, 3) if done else None
        return {"guides": sorted(out.values(), key=lambda g: -(g["fixed"] + g["escalated"] + g["stopped"])),
                "note": "Anonymous counts: no user, car or description is stored with an outcome."}

    # ------------------------------------------------------------------ help requests
    def _transcript(self, g: dict | None, raw) -> tuple[list[dict], dict]:
        if not raw:
            return [], {}
        if g is None:
            raise Invalid("Steps can only be recorded for a guide")
        if not isinstance(raw, list) or len(raw) > 60:
            raise Invalid("At most 60 recorded steps")
        steps = {s["id"]: s for s in g["steps"]}
        out, facts = [], {}
        for t in raw:
            if not isinstance(t, dict) or t.get("step") not in steps:
                raise Invalid("Unknown step in the recorded steps")
            s = steps[t["step"]]
            ans = t.get("answer")
            if ans not in ANSWERS[s["type"]]:
                raise Invalid(f"Step {s['id']}: answer must be one of {', '.join(ANSWERS[s['type']])}")
            entry = {"step": s["id"], "type": s["type"], "text": s["text"], "answer": ans}
            if ans == "option":
                i = t.get("option")
                if not isinstance(i, int) or not 0 <= i < len(s["options"]):
                    raise Invalid(f"Step {s['id']}: unknown option")
                opt = s["options"][i]
                entry.update(option=i, answer_text=opt["label"])
                facts.update(opt.get("facts") or {})
            else:
                entry["answer_text"] = ANSWER_TEXT[ans]
            out.append(entry)
        return out, facts

    def _case_out(self, r: dict, report: dict | None = None, photos: list | None = None) -> dict:
        out = {k: _iso(v) for k, v in r.items() if k not in ("transcript", "facts", "user_id")}
        out["ref"] = "CS-" + r["id"][:6].upper()
        out["transcript"] = json.loads(r["transcript"] or "[]")
        out["facts"] = json.loads(r["facts"] or "{}")
        g = H.load()["guides"].get(r["guide_id"] or "")
        out["guide_title"] = g["title"] if g else None
        out["reportable"] = bool(g and "report" in g)
        out["tell_service"] = g["escalate"]["tell_service"] if g else []
        out["report"] = report
        out["photos"] = photos if photos is not None else []
        return out

    def _case(self, c, user: dict, case_id: str) -> dict:
        r = _row(c.execute(sa.select(T.help_cases).where(T.help_cases.c.id == case_id, T.help_cases.c.user_id == user["id"])).first())
        if not r:
            raise NotFound("Help request not found")
        return r

    def create_case(self, user: dict, data: dict) -> dict:
        g = H.load()["guides"].get(data.get("guide_id") or "")
        if data.get("guide_id") and not g:
            raise Invalid("Unknown guide")
        car = self._car_for(user, data.get("car_id"))
        try:
            description = V.text(data.get("description", ""), "Description", 0, 2000)
            title = V.text(data.get("title") or (g["title"] if g else description[:80]), "Title", 3, 120)
            happened = V.day(data.get("happened_on"), "Date")
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        if happened and happened > date.today():
            raise Invalid("The date can't be in the future")
        transcript, facts = self._transcript(g, data.get("transcript"))
        t, cid = now(), _uid()
        with self.e.begin() as c:
            n = c.execute(sa.select(sa.func.count()).select_from(T.help_cases).where(T.help_cases.c.user_id == user["id"],
                                                                                   T.help_cases.c.status == "open")).scalar()
            if n >= MAX_OPEN_CASES:
                raise Conflict(f"At most {MAX_OPEN_CASES} open help requests; close some first")
            c.execute(T.help_cases.insert().values(
                id=cid, user_id=user["id"], car_id=car["id"] if car else None,
                car_label=f"{car['make']} {car['model']}, {car['reg_display']}"[:80] if car else "", guide_id=g["id"] if g else None,
                title=title, description=description, transcript=json.dumps(transcript), facts=json.dumps(facts), status="open",
                happened_on=happened or date.today(), created_at=t, updated_at=t))
            OwnerService._event(c, user["id"], "help_request", g["id"] if g else "")
        return self.case(user, cid)

    def cases(self, user: dict) -> list[dict]:
        with self.e.connect() as c:
            rows = [_row(r) for r in c.execute(sa.select(T.help_cases).where(T.help_cases.c.user_id == user["id"])
                                               .order_by(T.help_cases.c.created_at.desc()))]
            reps = {r.case_id: r.status for r in c.execute(sa.select(T.field_reports.c.case_id, T.field_reports.c.status)
                                                           .where(T.field_reports.c.user_id == user["id"]))}
        return [{"id": r["id"], "ref": "CS-" + r["id"][:6].upper(), "title": r["title"], "car_label": r["car_label"], "status": r["status"],
                 "created_at": _iso(r["created_at"]), "report_status": reps.get(r["id"])} for r in rows]

    def case(self, user: dict, case_id: str) -> dict:
        with self.e.connect() as c:
            r = self._case(c, user, case_id)
            rep = _row(c.execute(sa.select(T.field_reports).where(T.field_reports.c.case_id == case_id)).first())
            vin = c.execute(sa.select(T.cars.c.vin).where(T.cars.c.id == r["car_id"])).scalar() if r["car_id"] else ""
            photos = [{"id": p.id, "file_name": p.file_name, "file_type": p.file_type, "file_size": p.file_size}
                      for p in c.execute(sa.select(T.case_photos.c.id, T.case_photos.c.file_name, T.case_photos.c.file_type,
                                                   T.case_photos.c.file_size).where(T.case_photos.c.case_id == case_id)
                                         .order_by(T.case_photos.c.created_at))]
        return {**self._case_out(r, self._report_out(rep), photos), "car_has_vin": bool(vin)}

    def update_case(self, user: dict, case_id: str, data: dict) -> dict:
        vals = {}
        try:
            if "description" in data:
                vals["description"] = V.text(data["description"], "Description", 0, 2000)
            if "title" in data:
                vals["title"] = V.text(data["title"], "Title", 3, 120)
            if "status" in data:
                vals["status"] = V.choice(data["status"], "Status", ("open", "resolved"))
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        with self.e.begin() as c:
            r = self._case(c, user, case_id)
            if vals.get("status") and vals["status"] != r["status"]:
                vals["resolved_at"] = now() if vals["status"] == "resolved" else None
            if vals:
                c.execute(T.help_cases.update().where(T.help_cases.c.id == case_id).values(updated_at=now(), **vals))
        return self.case(user, case_id)

    def delete_case(self, user: dict, case_id: str) -> None:
        with self.e.begin() as c:
            self._case(c, user, case_id)
            c.execute(T.help_cases.delete().where(T.help_cases.c.id == case_id))   # photos and an unsent report go with it

    def add_photo(self, user: dict, case_id: str, content: bytes | None, filename: str) -> dict:
        if not content:
            raise Invalid("Choose a photo")
        f = self.o._check_file(content, filename)
        if not f["file_type"].startswith("image/"):
            raise Invalid("Add a photo (JPEG, PNG or WebP)")
        with self.e.begin() as c:
            self._case(c, user, case_id)
            n = c.execute(sa.select(sa.func.count()).select_from(T.case_photos).where(T.case_photos.c.case_id == case_id)).scalar()
            if n >= MAX_PHOTOS:
                raise Conflict(f"At most {MAX_PHOTOS} photos per help request")
            c.execute(T.case_photos.insert().values(id=_uid(), case_id=case_id, content=content, created_at=now(), **f))
            c.execute(T.help_cases.update().where(T.help_cases.c.id == case_id).values(updated_at=now()))
        return self.case(user, case_id)

    def photo(self, user: dict, case_id: str, photo_id: str) -> tuple[bytes, str, str]:
        with self.e.connect() as c:
            self._case(c, user, case_id)
            p = c.execute(sa.select(T.case_photos).where(T.case_photos.c.id == photo_id, T.case_photos.c.case_id == case_id)).first()
        if not p:
            raise NotFound("Photo not found")
        return bytes(p.content), p.file_type, p.file_name

    def delete_photo(self, user: dict, case_id: str, photo_id: str) -> None:
        with self.e.begin() as c:
            self._case(c, user, case_id)
            if not c.execute(T.case_photos.delete().where(T.case_photos.c.id == photo_id, T.case_photos.c.case_id == case_id)).rowcount:
                raise NotFound("Photo not found")

    def summary(self, user: dict, case_id: str) -> str:
        """Plain text to send to a service centre (WhatsApp, email): the problem, the car, what was tried, what to check."""
        cs = self.case(user, case_id)
        car = None
        if cs["car_id"]:
            try:
                car = self.o.car(user, cs["car_id"])
            except NotFound:
                car = None
        lines = [f"Help request {cs['ref']} (CarSaathi)", f"Problem: {cs['title']}"]
        if car:
            details = ", ".join(x for x in (car["fuel"], str(car["year"] or ""), f"{car['odometer_km']:,} km" if car.get("odometer_km") else "") if x)
            lines.append(f"Car: {car['make']} {car['model']} ({car['reg_display']}), {details}")
            if car.get("vin"):
                lines.append(f"VIN: {car['vin']}")
        elif cs["car_label"]:
            lines.append(f"Car: {cs['car_label']}")
        if cs["happened_on"]:
            d = date.fromisoformat(cs["happened_on"])
            lines.append(f"Noticed on: {d.day} {d:%b} {d.year}")
        if cs["description"]:
            lines += ["", cs["description"]]
        answers = [t for t in cs["transcript"] if t["type"] == "ask"]
        if answers:
            lines += ["", "Answers:"] + [f"- {t['text']} {t['answer_text']}" for t in answers]
        tried = [t for t in cs["transcript"] if t["type"] == "check"]
        if tried:
            lines += ["", "Already tried:"] + [f"- {t['text']} ({t['answer_text']})" for t in tried]
        if cs["tell_service"]:
            lines += ["", "Details for the service centre:"] + [f"- {x}" for x in cs["tell_service"]]
        if cs["photos"]:
            lines += ["", f"{len(cs['photos'])} photo(s) attached in the app."]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ reports to the manufacturer
    def _mode(self) -> str:
        if not self.s.field_api_url:
            return "not_connected"
        return "live" if self.s.field_live else "dry_run"

    @staticmethod
    def _report_out(r: dict | None) -> dict | None:
        if not r:
            return None
        return {"status": r["status"], "created_at": _iso(r["created_at"]), "sent_at": _iso(r["sent_at"]), "remote_ref": r["remote_ref"],
                "include_vin": r["include_vin"], "payload": json.loads(r["payload"])}

    def _compose(self, user: dict, case_id: str, form: dict) -> tuple[dict, dict]:
        cs = self.case(user, case_id)
        if not cs["reportable"]:
            raise Invalid("Only driver-assistance problems (automatic braking and collision warnings) can be reported to the manufacturer")
        car = None
        if cs["car_id"]:
            try:
                car = self.o.car(user, cs["car_id"])
            except NotFound:
                car = None
        speed = form.get("speed_kmh")
        try:
            speed = None if speed in (None, "") else int(speed)
            item = bridge.compose(H.load()["guides"][cs["guide_id"]], car, cs["facts"], cs["transcript"],
                                  happened_on=cs["happened_on"] or date.today().isoformat(), speed_kmh=speed,
                                  sw_version=str(form.get("sw_version") or ""), include_vin=bool(form.get("include_vin")))
        except (ValueError, TypeError, bridge.BridgeError) as e:
            raise Invalid(str(e) if isinstance(e, bridge.BridgeError) else "Speed must be a whole number") from e
        if form.get("include_vin") and not item["vin"]:
            raise Invalid("Add the VIN to the car first (Details > Edit), or leave it out")
        return cs, item

    def report_preview(self, user: dict, case_id: str, form: dict) -> dict:
        _, item = self._compose(user, case_id, form)
        return {"item": item, "mode": self._mode(), "destination": self.s.field_project if self._mode() != "not_connected" else None}

    def report(self, user: dict, case_id: str, form: dict) -> dict:
        if form.get("consent") is not True:
            raise Invalid("Tick the box to agree to share this report")
        if user["is_demo"] and self._mode() == "live":
            raise Forbidden("Reports from the demo are not sent")
        cs, item = self._compose(user, case_id, form)
        mode = self._mode()
        t, rid = now(), _uid()
        with self.e.begin() as c:
            if c.execute(sa.select(T.field_reports.c.id).where(T.field_reports.c.case_id == case_id)).first():
                raise Conflict("This problem has already been reported")
            c.execute(T.field_reports.insert().values(
                id=rid, case_id=case_id, user_id=user["id"], status={"live": "pending"}.get(mode, mode),
                payload=json.dumps(item), include_vin=bool(item["vin"]), consent_at=t, created_at=t))
            OwnerService._event(c, user["id"], "field_report", f"{mode}; vin {'shared' if item['vin'] else 'not shared'}")
        if mode == "live":
            self._deliver(rid)
        return self.case(user, case_id)

    def _deliver(self, rid: str) -> str:
        with self.e.connect() as c:
            r = _row(c.execute(sa.select(T.field_reports).where(T.field_reports.c.id == rid)).first())
        if not r or r["status"] != "pending":
            return r["status"] if r else "missing"
        ok, ref, err = bridge.send(self.s, json.loads(r["payload"]))
        attempts = r["attempts"] + 1
        status = "sent" if ok else ("failed" if attempts >= bridge.MAX_TRIES else "pending")
        with self.e.begin() as c:
            c.execute(T.field_reports.update().where(T.field_reports.c.id == rid).values(
                status=status, attempts=attempts, last_error=err[:200], sent_at=now() if ok else None, remote_ref=ref or None))
        if not ok:
            log.warning("Field report not delivered (%s), attempt %s of %s", err, attempts, bridge.MAX_TRIES)
        return status

    def flush_reports(self) -> dict:
        """Retry pending reports (live mode). -> counts by resulting status."""
        if self._mode() != "live":
            return {}
        with self.e.connect() as c:
            ids = [r.id for r in c.execute(sa.select(T.field_reports.c.id).where(T.field_reports.c.status == "pending"))]
        out: dict[str, int] = {}
        for rid in ids:
            st = self._deliver(rid)
            out[st] = out.get(st, 0) + 1
        return out

    def export_rows(self, uid: str) -> dict:
        """For the data export (DPDP): help requests without photo contents, and reports to the manufacturer."""
        with self.e.connect() as c:
            cases = [{k: _iso(v) for k, v in _row(r).items()} for r in c.execute(sa.select(T.help_cases).where(T.help_cases.c.user_id == uid))]
            for x in cases:
                x["transcript"], x["facts"] = json.loads(x["transcript"]), json.loads(x["facts"])
                x["photos"] = [p.file_name for p in c.execute(sa.select(T.case_photos.c.file_name).where(T.case_photos.c.case_id == x["id"]))]
            reports = [{k: _iso(v) for k, v in _row(r).items()} for r in c.execute(sa.select(T.field_reports).where(T.field_reports.c.user_id == uid))]
        for x in reports:
            x["payload"] = json.loads(x["payload"])
        return {"help_requests": cases, "reports_to_manufacturer": reports}
