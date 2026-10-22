"""HTTP API and web app of the car-owner service.

    uvicorn --factory owner.api:create_app      # API under /api, the React app (owner-app/dist) on every other path

One origin for the app and the API (no cookies, bearer tokens kept by the app), strict security headers and a content
security policy, request size and rate limits, and the Android download link the app's login page offers."""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from owner import config as C
from owner import db as D
from owner import obd
from owner.booking import BookingService
from owner.catalog import catalog
from owner.costs import CostService
from owner.health import HealthService
from owner.helpdesk import HelpService
from owner.notify import NotifyService
from owner.service import OwnerError, OwnerService, TooMany, dumps
from owner.validators import DOC_KINDS

log = logging.getLogger("owner")
CSP = ("default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
       "font-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
NO_STORE = {"Cache-Control": "no-store"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodeIn(Strict):
    email: str = Field(..., max_length=254)


class VerifyIn(Strict):
    email: str = Field(..., max_length=254)
    code: str = Field(..., max_length=12)


class MeIn(Strict):
    name: str | None = Field(None, max_length=120)
    phone: str | None = Field(None, max_length=20)
    consent: bool | None = None


class DeleteIn(Strict):
    confirm: str


class CarIn(Strict):
    nickname: str | None = Field(None, max_length=80)
    make: str | None = Field(None, max_length=80)
    model: str | None = Field(None, max_length=80)
    variant: str | None = Field(None, max_length=80)
    fuel: str | None = Field(None, max_length=20)
    year: int | str | None = None
    reg_number: str | None = Field(None, max_length=30)
    vin: str | None = Field(None, max_length=30)
    colour: str | None = Field(None, max_length=60)
    odometer_km: int | str | None = None
    purchase_date: str | None = Field(None, max_length=20)
    roadside_phone: str | None = Field(None, max_length=30)


class InviteIn(Strict):
    email: str = Field(..., max_length=254)
    role: str = "driver"


class ReminderIn(Strict):
    title: str | None = Field(None, max_length=160)
    due_on: str | None = Field(None, max_length=20)
    due_km: int | str | None = None
    repeat: str | None = Field(None, max_length=20)
    every_km: int | str | None = None
    notes: str | None = Field(None, max_length=600)


class OutcomeIn(Strict):
    guide_id: str = Field(..., max_length=40)
    outcome: str = Field(..., max_length=10)
    step_id: str | None = Field(None, max_length=20)


class SearchIn(Strict):
    q: str = Field(..., max_length=600)
    car_id: str | None = Field(None, max_length=36)


class StepIn(Strict):
    step: str = Field(..., max_length=20)
    answer: str = Field(..., max_length=10)
    option: int | None = None


class FuelIn(Strict):
    filled_on: str | None = Field(None, max_length=20)
    odometer_km: int | str | None = None
    quantity: float | str | None = None
    amount: float | str | None = None
    unit: str | None = Field(None, max_length=4)
    full: bool = True
    station: str = Field("", max_length=120)
    notes: str = Field("", max_length=300)


class ScanIn(Strict):
    source: str = Field("adapter", max_length=10)
    answers: dict[str, str] = Field(..., max_length=len(obd.COMMANDS))


class CodesIn(Strict):
    codes: list[str] = Field(..., max_length=20)
    mil_on: bool | None = None


class PrefsIn(Strict):
    email: bool | None = None
    push: bool | None = None
    kinds: list[str] | None = Field(None, max_length=10)


class PushKeys(Strict):
    p256dh: str = Field(..., max_length=100)
    auth: str = Field(..., max_length=40)


class PushIn(Strict):
    endpoint: str = Field(..., max_length=500)
    keys: PushKeys
    expirationTime: float | None = None                                         # noqa: N815 - the browser's own field name
    label: str = Field("", max_length=80)


class EndpointIn(Strict):
    endpoint: str = Field(..., max_length=500)


class ReadIn(Strict):
    ids: list[str] | None = Field(None, max_length=200)


class WorkshopIn(Strict):
    name: str = Field(..., max_length=120)
    phone: str = Field("", max_length=30)
    address: str = Field("", max_length=300)


class JobIn(Strict):
    kind: str = Field(..., max_length=10)
    ref: str | None = Field(None, max_length=40)
    text: str | None = Field(None, max_length=200)


class SlotIn(Strict):
    on: str = Field(..., max_length=20)
    part: str = Field("any", max_length=10)


class BookingIn(Strict):
    workshop_id: str | None = Field(None, max_length=36)
    workshop: WorkshopIn | None = None
    jobs: list[JobIn] = Field(..., max_length=20)
    slots: list[SlotIn] = Field(..., max_length=5)
    pickup: bool = False
    notes: str = Field("", max_length=600)
    odometer_km: int | str | None = None
    contact_phone: str = Field("", max_length=20)


class SlotsIn(Strict):
    slots: list[SlotIn] = Field(..., max_length=5)


class NoteIn(Strict):
    note: str = Field("", max_length=400)


class CompleteIn(Strict):
    amount: float | str | None = None
    odometer_km: int | str | None = None
    add_expense: bool = True
    done_reminders: list[str] = Field(default_factory=list, max_length=20)


class WorkshopActionIn(Strict):
    on: str | None = Field(None, max_length=20)
    time: str | None = Field(None, max_length=8)
    amount: float | str | None = None
    note: str = Field("", max_length=400)


class CaseIn(Strict):
    car_id: str | None = Field(None, max_length=36)
    guide_id: str | None = Field(None, max_length=40)
    title: str | None = Field(None, max_length=160)
    description: str = Field("", max_length=2400)
    happened_on: str | None = Field(None, max_length=20)
    transcript: list[StepIn] | None = Field(None, max_length=60)


class CasePatch(Strict):
    title: str | None = Field(None, max_length=160)
    description: str | None = Field(None, max_length=2400)
    status: str | None = Field(None, max_length=10)


class ReportIn(Strict):
    consent: bool = False
    include_vin: bool = False
    speed_kmh: int | str | None = None
    sw_version: str | None = Field(None, max_length=20)


def create_app(settings: C.OwnerSettings | None = None) -> FastAPI:
    s = settings or C.load()
    errors, warnings = C.check(s)
    if errors:
        raise RuntimeError("Owner service configuration: " + "; ".join(errors))
    for w in warnings:
        log.warning(w)
    eng = D.engine(s.database_url)
    D.init(eng)
    svc = OwnerService(s, eng)
    help_ = HelpService(svc)
    costs_ = CostService(svc)
    health_ = HealthService(svc, costs_)
    notify_ = NotifyService(svc, health_)
    booking_ = BookingService(svc, costs_, notify_)

    @asynccontextmanager
    async def lifespan(_app):
        """Live reports to the manufacturer: retry undelivered ones every 5 minutes."""
        task = None
        if s.field_live and s.field_api_url:
            async def retry_loop():
                while True:
                    await asyncio.sleep(300)
                    try:
                        await run_in_threadpool(help_.flush_reports)
                    except Exception:                                          # noqa: BLE001 - keep the loop alive
                        log.exception("Retrying field reports failed")
            task = asyncio.create_task(retry_loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title=f"{s.app_name} API", version="1.1.0", docs_url="/api/docs" if s.env != "production" else None,
                  redoc_url=None, openapi_url="/api/openapi.json" if s.env != "production" else None, lifespan=lifespan)
    app.state.svc, app.state.help, app.state.costs, app.state.health, app.state.settings = svc, help_, costs_, health_, s
    app.state.notify, app.state.booking = notify_, booking_
    if s.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(s.cors_origins), allow_methods=["*"],
                           allow_headers=["Authorization", "Content-Type", "X-Booking-Token"])
    max_body = (s.max_upload_mb + 1) * 1024 * 1024

    def ip_of(request: Request) -> str:
        """The visitor's address for rate limits. Behind N proxies, each proxy appends the address it received the request
        from, so the Nth entry from the right was written by our own proxy; entries further left are whatever the client sent."""
        if s.proxy_hops:
            hops = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
            if hops:
                return hops[-min(s.proxy_hops, len(hops))][:64]
        return request.client.host if request.client else "?"

    @app.middleware("http")
    async def guard(request: Request, call_next):
        ip = ip_of(request)
        if request.url.path.startswith("/api/"):
            ok, retry = svc.limits.allow(f"req:{ip}", 300, 60)
            if not ok:
                return JSONResponse({"detail": "Too many requests"}, 429, headers={"Retry-After": str(retry)})
            cl = request.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > max_body:
                return JSONResponse({"detail": f"Request larger than {s.max_upload_mb} MB"}, 413)
        try:
            resp = await call_next(request)
        except Exception:                                                       # noqa: BLE001 - never leak internals
            log.exception("Unhandled error on %s", request.url.path)
            resp = JSONResponse({"detail": "Something went wrong on our side. Please try again."}, 500)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Permissions-Policy", "camera=(self), geolocation=(), microphone=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if s.env == "production":
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if h.get("content-type", "").startswith("text/html"):
            h.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/api/") and "cache-control" not in h:
            h["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(OwnerError)
    async def owner_error(request: Request, e: OwnerError):
        hdr = {"Retry-After": str(e.retry_after)} if isinstance(e, TooMany) and e.retry_after else None
        return JSONResponse({"detail": str(e)}, e.status, headers=hdr)

    # ------------------------------------------------------------------ identity
    def current(request: Request) -> dict:
        h = request.headers.get("authorization", "")
        token = h[7:].strip() if h[:7].lower() == "bearer " else ""
        user = svc.authenticate(token)
        if not user:
            raise _Unauthorized()
        return user

    def active(user: dict = Depends(current)) -> dict:
        if not (user["name"] and user["consent_at"]):
            raise _ProfileIncomplete()
        return user

    @app.exception_handler(_Unauthorized)
    async def unauth(request: Request, e):
        return JSONResponse({"detail": "Please sign in"}, 401, headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(_ProfileIncomplete)
    async def incomplete(request: Request, e):
        return JSONResponse({"detail": "Complete your profile first (name and privacy consent)", "code": "profile_incomplete"}, 403)

    # ------------------------------------------------------------------ public
    @app.api_route("/api/health", methods=["GET", "HEAD"])
    def health():
        return {"status": "ok", "database": "sqlite" if s.is_sqlite else "postgresql", "schema": D.SCHEMA_VERSION}

    @app.get("/api/config")
    def public_config():
        return {"app_name": s.app_name, "email_signin": s.otp_delivery == "console" or s.email_configured,
                "delivery": s.otp_delivery, "demo": s.demo_login, "demo_hours": s.demo_ttl_hours,
                "max_upload_mb": s.max_upload_mb, "document_kinds": DOC_KINDS, "android_app": bool(s.android_url),
                "persistent": not s.is_sqlite or s.env != "production", "push": s.push_enabled}

    @app.get("/api/app/info")
    def app_info():
        return {"name": s.app_name, "android": {"available": bool(s.android_url), "url": "/get-app/android" if s.android_url else None},
                "pwa": True, "ios": "Install from Safari: Share > Add to Home Screen"}

    @app.api_route("/get-app/android", methods=["GET", "HEAD"])
    def get_android():
        if not s.android_url:
            return PlainTextResponse("The Android app is not published yet.", 404)
        return RedirectResponse(s.android_url, 302)

    @app.get("/api/catalog")
    def get_catalog():
        return catalog()

    # ------------------------------------------------------------------ sign-in
    @app.post("/api/auth/code", status_code=202)
    def request_code(b: CodeIn, request: Request):
        return svc.request_code(b.email, ip_of(request))

    @app.post("/api/auth/verify")
    def verify(b: VerifyIn, request: Request):
        return JSONResponse(svc.verify_code(b.email, b.code, ip_of(request), request.headers.get("user-agent", "")), headers=NO_STORE)

    @app.post("/api/auth/demo")
    def demo(request: Request):
        out = svc.demo(ip_of(request), request.headers.get("user-agent", ""))
        notify_.collect(out["user"]["id"], deliver=False)                       # the demo's inbox shows what the daily run sends
        return JSONResponse(out, headers=NO_STORE)

    @app.post("/api/auth/logout", status_code=204)
    def logout(everywhere: bool = False, user: dict = Depends(current)):
        svc.logout(user, everywhere)
        return Response(status_code=204)

    # ------------------------------------------------------------------ account (allowed before the profile is complete)
    @app.get("/api/me")
    def me(user: dict = Depends(current)):
        return svc.me(user["id"])

    @app.patch("/api/me")
    def update_me(b: MeIn, user: dict = Depends(current)):
        return svc.update_me(user, b.model_dump(exclude_unset=True))

    @app.get("/api/me/export")
    def export(user: dict = Depends(current)):
        ids = [c["id"] for c in svc.cars(user)]
        data = {**svc.export(user), **help_.export_rows(user["id"]), **costs_.export_rows(ids), "health_checks": health_.export_rows(ids),
                **notify_.export_rows(user["id"]), **booking_.export_rows(user["id"], ids)}
        return Response(dumps(data), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="my-car-data.json"', **NO_STORE})

    @app.post("/api/me/delete")
    def delete_me(b: DeleteIn, user: dict = Depends(current)):
        if b.confirm != "DELETE":
            return JSONResponse({"detail": "Type DELETE to confirm"}, 422)
        return svc.delete_account(user)

    @app.get("/api/invitations")
    def invitations(user: dict = Depends(active)):
        return svc.my_invitations(user)

    @app.post("/api/invitations/{inv_id}/accept")
    def accept(inv_id: str, user: dict = Depends(active)):
        return svc.respond(user, inv_id, True)

    @app.post("/api/invitations/{inv_id}/decline")
    def decline(inv_id: str, user: dict = Depends(active)):
        return svc.respond(user, inv_id, False)

    # ------------------------------------------------------------------ garage
    @app.get("/api/dashboard")
    def dashboard(user: dict = Depends(active)):
        d = svc.dashboard(user)
        hs = health_.summary_for(user, [c["id"] for c in d["cars"]])
        d["cars"] = [{**c, "health": hs[c["id"]]} for c in d["cars"]]
        d["unread"] = notify_.unread(user)
        d["bookings"] = [{k: b[k] for k in ("id", "status", "status_label", "car", "workshop", "appointment", "proposed", "updated_at")}
                         for b in booking_.list(user, active_only=True)]
        return d

    @app.get("/api/cars")
    def cars(user: dict = Depends(active)):
        return svc.cars(user)

    @app.post("/api/cars", status_code=201)
    def add_car(b: CarIn, user: dict = Depends(active)):
        return svc.add_car(user, b.model_dump(exclude_unset=True))

    @app.get("/api/cars/{car_id}")
    def car(car_id: str, user: dict = Depends(active)):
        return svc.car(user, car_id)

    @app.patch("/api/cars/{car_id}")
    def update_car(car_id: str, b: CarIn, user: dict = Depends(active)):
        return svc.update_car(user, car_id, b.model_dump(exclude_unset=True))

    @app.delete("/api/cars/{car_id}", status_code=204)
    def delete_car(car_id: str, user: dict = Depends(active)):
        svc.delete_car(user, car_id)
        return Response(status_code=204)

    @app.get("/api/cars/{car_id}/members")
    def members(car_id: str, user: dict = Depends(active)):
        return svc.members(user, car_id)

    @app.post("/api/cars/{car_id}/members", status_code=201)
    def invite(car_id: str, b: InviteIn, user: dict = Depends(active)):
        return svc.invite(user, car_id, b.email, b.role)

    @app.delete("/api/cars/{car_id}/members/{member_id}", status_code=204)
    def remove_member(car_id: str, member_id: str, user: dict = Depends(active)):
        svc.remove_member(user, car_id, member_id)
        return Response(status_code=204)

    @app.delete("/api/cars/{car_id}/invitations/{inv_id}", status_code=204)
    def revoke(car_id: str, inv_id: str, user: dict = Depends(active)):
        svc.revoke_invitation(user, car_id, inv_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------ documents
    async def _read(file: UploadFile | None) -> tuple[bytes | None, str]:
        if file is None or not file.filename:
            return None, ""
        content = await file.read(s.max_upload_mb * 1024 * 1024 + 1)
        return content, file.filename

    def _fields(**kw) -> dict:
        return {k: v for k, v in kw.items() if v is not None}

    @app.get("/api/cars/{car_id}/documents")
    def documents(car_id: str, user: dict = Depends(active)):
        return svc.documents(user, car_id)

    @app.post("/api/cars/{car_id}/documents", status_code=201)
    async def add_document(car_id: str, kind: str = Form(...), title: str = Form(""), number: str = Form(""), issuer: str = Form(""),
                           issued_on: str = Form(""), expires_on: str = Form(""), notes: str = Form(""),
                           file: UploadFile | None = File(None), user: dict = Depends(active)):
        content, name = await _read(file)
        return svc.add_document(user, car_id, {"kind": kind, "title": title, "number": number, "issuer": issuer, "issued_on": issued_on,
                                               "expires_on": expires_on, "notes": notes}, content, name)

    @app.get("/api/documents/{doc_id}")
    def document(doc_id: str, user: dict = Depends(active)):
        return svc.document(user, doc_id)

    @app.patch("/api/documents/{doc_id}")
    async def update_document(doc_id: str, kind: str | None = Form(None), title: str | None = Form(None), number: str | None = Form(None),
                              issuer: str | None = Form(None), issued_on: str | None = Form(None), expires_on: str | None = Form(None),
                              notes: str | None = Form(None), file: UploadFile | None = File(None), user: dict = Depends(active)):
        content, name = await _read(file)
        return svc.update_document(user, doc_id, _fields(kind=kind, title=title, number=number, issuer=issuer, issued_on=issued_on,
                                                         expires_on=expires_on, notes=notes), content, name)

    @app.delete("/api/documents/{doc_id}", status_code=204)
    def delete_document(doc_id: str, user: dict = Depends(active)):
        svc.delete_document(user, doc_id)
        return Response(status_code=204)

    @app.get("/api/documents/{doc_id}/file")
    def document_file(doc_id: str, user: dict = Depends(active)):
        content, mime, name = svc.document_file(user, doc_id)
        return Response(content, media_type=mime, headers={"Content-Disposition": f'inline; filename="{name}"',
                                                           "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'",
                                                           "Cache-Control": "private, no-store"})

    # ------------------------------------------------------------------ reminders
    @app.get("/api/reminders")
    def reminders(car_id: str | None = None, include_done: bool = False, user: dict = Depends(active)):
        return svc.reminders(user, car_id, include_done)

    @app.post("/api/cars/{car_id}/reminders", status_code=201)
    def add_reminder(car_id: str, b: ReminderIn, user: dict = Depends(active)):
        return svc.add_reminder(user, car_id, b.model_dump(exclude_unset=True))

    @app.patch("/api/reminders/{rid}")
    def update_reminder(rid: str, b: ReminderIn, user: dict = Depends(active)):
        return svc.update_reminder(user, rid, b.model_dump(exclude_unset=True))

    @app.post("/api/reminders/{rid}/done")
    def done(rid: str, user: dict = Depends(active)):
        return svc.complete_reminder(user, rid)

    @app.delete("/api/reminders/{rid}", status_code=204)
    def delete_reminder(rid: str, user: dict = Depends(active)):
        svc.delete_reminder(user, rid)
        return Response(status_code=204)

    # ------------------------------------------------------------------ running costs (Phase C)
    @app.get("/api/cars/{car_id}/fuel")
    def fuel(car_id: str, user: dict = Depends(active)):
        return costs_.fuel(user, car_id)

    @app.post("/api/cars/{car_id}/fuel", status_code=201)
    def add_fuel(car_id: str, b: FuelIn, user: dict = Depends(active)):
        return costs_.add_fuel(user, car_id, b.model_dump(exclude_unset=True))

    @app.delete("/api/fuel/{fid}", status_code=204)
    def delete_fuel(fid: str, user: dict = Depends(active)):
        costs_.delete_fuel(user, fid)
        return Response(status_code=204)

    @app.get("/api/cars/{car_id}/expenses")
    def expenses(car_id: str, user: dict = Depends(active)):
        return costs_.expenses(user, car_id)

    @app.post("/api/cars/{car_id}/expenses", status_code=201)
    async def add_expense(car_id: str, category: str = Form(...), amount: str = Form(...), spent_on: str = Form(""),
                          odometer_km: str = Form(""), vendor: str = Form(""), notes: str = Form(""),
                          file: UploadFile | None = File(None), user: dict = Depends(active)):
        content, name = await _read(file)
        return costs_.add_expense(user, car_id, {"category": category, "amount": amount, "spent_on": spent_on, "odometer_km": odometer_km,
                                                 "vendor": vendor, "notes": notes}, content, name)

    @app.delete("/api/expenses/{eid}", status_code=204)
    def delete_expense(eid: str, user: dict = Depends(active)):
        costs_.delete_expense(user, eid)
        return Response(status_code=204)

    @app.get("/api/expenses/{eid}/receipt")
    def receipt(eid: str, user: dict = Depends(active)):
        content, mime, name = costs_.receipt(user, eid)
        return Response(content, media_type=mime, headers={"Content-Disposition": f'inline; filename="{name}"',
                                                           "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'",
                                                           "Cache-Control": "private, no-store"})

    @app.get("/api/cars/{car_id}/costs")
    def car_costs(car_id: str, months: int = 12, user: dict = Depends(active)):
        return costs_.summary(user, car_id, max(3, min(24, months)))

    @app.get("/api/cars/{car_id}/costs.csv")
    def car_costs_csv(car_id: str, user: dict = Depends(active)):
        return Response(costs_.csv(user, car_id), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="car-costs.csv"', **NO_STORE})

    # ------------------------------------------------------------------ car health (Phase C)
    @app.get("/api/obd/commands")
    def obd_commands():
        return {"commands": list(obd.COMMANDS), "init": ["ATZ", "ATE0", "ATL0", "ATS1", "ATH0", "ATSP0"]}

    @app.get("/api/obd/codes/{code}")
    def obd_code(code: str, user: dict = Depends(active)):
        try:
            return obd.explain(code)
        except obd.ObdError as e:
            return JSONResponse({"detail": str(e)}, 422)

    @app.get("/api/cars/{car_id}/health")
    def car_health(car_id: str, user: dict = Depends(active)):
        return health_.health(user, car_id)

    @app.get("/api/cars/{car_id}/health/scans")
    def car_scans(car_id: str, user: dict = Depends(active)):
        return health_.scans(user, car_id)

    @app.post("/api/cars/{car_id}/health/scans", status_code=201)
    def add_scan(car_id: str, b: ScanIn, user: dict = Depends(active)):
        return health_.add_scan(user, car_id, b.source, b.answers)

    @app.post("/api/cars/{car_id}/health/codes", status_code=201)
    def add_codes(car_id: str, b: CodesIn, user: dict = Depends(active)):
        return health_.add_codes(user, car_id, b.codes, b.mil_on)

    @app.delete("/api/health/scans/{sid}", status_code=204)
    def delete_scan(sid: str, user: dict = Depends(active)):
        health_.delete_scan(user, sid)
        return Response(status_code=204)

    # ------------------------------------------------------------------ notifications (Phase D)
    @app.get("/api/notifications")
    def notifications(user: dict = Depends(active)):
        return notify_.inbox(user)

    @app.get("/api/notifications/unread")
    def notifications_unread(user: dict = Depends(active)):
        return {"unread": notify_.unread(user)}

    @app.post("/api/notifications/read")
    def notifications_read(b: ReadIn, user: dict = Depends(active)):
        return notify_.mark_read(user, b.ids)

    @app.get("/api/notifications/prefs")
    def notification_prefs(user: dict = Depends(active)):
        return notify_.prefs(user)

    @app.patch("/api/notifications/prefs")
    def notification_prefs_update(b: PrefsIn, user: dict = Depends(active)):
        return notify_.update_prefs(user, b.model_dump(exclude_unset=True))

    @app.post("/api/notifications/test")
    def notification_test(user: dict = Depends(active)):
        return notify_.test(user)

    @app.get("/api/notifications/schedule")
    def notification_schedule(user: dict = Depends(active)):
        """For the Android app: the date-based notifications of the coming weeks, which it schedules on the phone."""
        return notify_.schedule(user)

    @app.get("/api/push/key")
    def push_key():
        return {"public_key": s.vapid_public_key if s.push_enabled else None}

    @app.post("/api/push/subscriptions", status_code=201)
    def push_subscribe(b: PushIn, user: dict = Depends(active)):
        return notify_.subscribe(user, b.endpoint, b.keys.p256dh, b.keys.auth, b.label)

    @app.post("/api/push/unsubscribe", status_code=204)
    def push_unsubscribe(b: EndpointIn, user: dict = Depends(active)):
        notify_.unsubscribe(user, endpoint=b.endpoint)
        return Response(status_code=204)

    @app.delete("/api/push/subscriptions/{sid}", status_code=204)
    def push_device_delete(sid: str, user: dict = Depends(active)):
        notify_.unsubscribe(user, device_id_=sid)
        return Response(status_code=204)

    def bearer(request: Request) -> str:
        h = request.headers.get("authorization", "")
        return h[7:].strip() if h[:7].lower() == "bearer " else ""

    @app.post("/api/notify/run")
    def notify_run(request: Request):
        """The daily run, started by a scheduler (GitHub Actions, cron) with OWNER_CRON_TOKEN. Unknown without the token."""
        token = bearer(request)
        if not s.cron_token or not hmac.compare_digest(token.encode(), s.cron_token.encode()):
            return JSONResponse({"detail": "Not found"}, 404)
        return notify_.run()

    UNSUB_PAGE = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                  "<meta name='robots' content='noindex'><title>{title}</title></head>"
                  "<body style='font-family:system-ui,sans-serif;max-width:480px;margin:48px auto;padding:0 18px;color:#1e2a32'>"
                  "<h1 style='font-size:1.4rem'>{title}</h1><p>{text}</p>{form}</body></html>")

    @app.get("/api/notify/unsubscribe", response_class=HTMLResponse)
    def unsubscribe_page(u: str = "", t: str = ""):
        """The link in the email asks first (link scanners open links); the one-click POST (RFC 8058) turns emails off."""
        import html as H
        if not (u and t and hmac.compare_digest(notify_.unsubscribe_token(u).encode(), t.encode())):
            return HTMLResponse(UNSUB_PAGE.format(title="Link not valid", text="This unsubscribe link is not valid.", form=""), 404)
        form = (f"<form method='post' action='/api/notify/unsubscribe?u={H.escape(u)}&amp;t={H.escape(t)}'>"
                "<button style='font:inherit;padding:10px 18px;border-radius:999px;border:0;background:#1f4fa3;color:#fff'>Turn off emails</button></form>")
        return HTMLResponse(UNSUB_PAGE.format(title=f"Stop {H.escape(s.app_name)} emails?",
                                              text="You won't get reminder or booking emails any more. The app keeps showing them.", form=form))

    @app.post("/api/notify/unsubscribe", response_class=HTMLResponse)
    def unsubscribe(u: str = "", t: str = ""):
        notify_.email_unsubscribe(u, t)
        return HTMLResponse(UNSUB_PAGE.format(title="Emails turned off",
                                              text="Done. You can turn them on again in the app: Profile, Notifications.", form=""))

    # ------------------------------------------------------------------ service booking (Phase D)
    @app.get("/api/workshops")
    def workshops(user: dict = Depends(active)):
        return booking_.workshops(user)

    @app.post("/api/workshops", status_code=201)
    def add_workshop(b: WorkshopIn, user: dict = Depends(active)):
        return booking_.add_workshop(user, b.model_dump())

    @app.patch("/api/workshops/{wid}")
    def update_workshop(wid: str, b: WorkshopIn, user: dict = Depends(active)):
        return booking_.update_workshop(user, wid, b.model_dump())

    @app.delete("/api/workshops/{wid}", status_code=204)
    def delete_workshop(wid: str, user: dict = Depends(active)):
        booking_.delete_workshop(user, wid)
        return Response(status_code=204)

    @app.get("/api/bookings")
    def all_bookings(active_only: bool = False, user: dict = Depends(active)):
        return booking_.list(user, None, active_only)

    @app.get("/api/cars/{car_id}/bookings")
    def car_bookings(car_id: str, user: dict = Depends(active)):
        return booking_.list(user, car_id)

    @app.get("/api/cars/{car_id}/booking-options")
    def booking_options(car_id: str, user: dict = Depends(active)):
        return booking_.options(user, car_id)

    @app.post("/api/cars/{car_id}/bookings", status_code=201)
    def create_booking(car_id: str, b: BookingIn, user: dict = Depends(active)):
        data = b.model_dump(exclude_unset=True)
        return booking_.create(user, car_id, data)

    @app.get("/api/bookings/{bid}")
    def get_booking(bid: str, user: dict = Depends(active)):
        return booking_.get(user, bid)

    @app.delete("/api/bookings/{bid}", status_code=204)
    def delete_booking(bid: str, user: dict = Depends(active)):
        booking_.delete(user, bid)
        return Response(status_code=204)

    @app.post("/api/bookings/{bid}/share")
    def share_booking(bid: str, user: dict = Depends(active)):
        return JSONResponse(booking_.share(user, bid), headers=NO_STORE)

    @app.post("/api/bookings/{bid}/unshare")
    def unshare_booking(bid: str, user: dict = Depends(active)):
        return booking_.unshare(user, bid)

    @app.post("/api/bookings/{bid}/accept")
    def accept_booking(bid: str, user: dict = Depends(active)):
        return booking_.accept(user, bid)

    @app.post("/api/bookings/{bid}/reschedule")
    def reschedule_booking(bid: str, b: SlotsIn, user: dict = Depends(active)):
        return booking_.reschedule(user, bid, [x.model_dump() for x in b.slots])

    @app.post("/api/bookings/{bid}/handed-over")
    def handed_over(bid: str, user: dict = Depends(active)):
        return booking_.handed_over(user, bid)

    @app.post("/api/bookings/{bid}/cancel")
    def cancel_booking(bid: str, b: NoteIn, user: dict = Depends(active)):
        return booking_.cancel(user, bid, b.note)

    @app.post("/api/bookings/{bid}/complete")
    def complete_booking(bid: str, b: CompleteIn, user: dict = Depends(active)):
        return booking_.complete(user, bid, b.model_dump())

    @app.get("/api/bookings/{bid}/calendar.ics")
    def booking_ics(bid: str, user: dict = Depends(active)):
        return Response(booking_.ics(user, bid), media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="car-service.ics"', **NO_STORE})

    # the workshop's side: no account; the link's token comes in a header (it travels in the URL fragment, never in logs)
    def workshop_token(request: Request) -> str:
        ok, retry = svc.limits.allow(f"workshop-ip:{ip_of(request)}", 120, 600)
        if not ok:
            raise _WorkshopLimited(retry)
        return request.headers.get("x-booking-token", "").strip()

    @app.exception_handler(_WorkshopLimited)
    async def workshop_limited(request: Request, e):
        return JSONResponse({"detail": "Too many requests"}, 429, headers={"Retry-After": str(e.retry)})

    @app.get("/api/w/booking")
    def workshop_booking(request: Request):
        return JSONResponse(booking_.workshop_view(workshop_token(request)), headers={**NO_STORE, "X-Robots-Tag": "noindex"})

    @app.post("/api/w/booking/{action}")
    def workshop_booking_action(action: str, b: WorkshopActionIn, request: Request):
        return booking_.workshop_action(workshop_token(request), action, b.model_dump())

    # ------------------------------------------------------------------ help (Phase B)
    def fuel_of(user: dict, car_id: str | None) -> tuple[str | None, str]:
        if not car_id:
            return None, ""
        car = svc.car(user, car_id)
        return car["fuel"], car.get("roadside_phone", "")

    @app.get("/api/help/topics")
    def help_topics(car_id: str | None = None, user: dict = Depends(active)):
        fuel, roadside = fuel_of(user, car_id)
        calls = [{"label": "Emergency", "number": "112"}] + ([{"label": "Your roadside assistance", "number": roadside}] if roadside else []) \
            + [{"label": "National highway helpline (NHAI)", "number": "1033"}]
        return {**help_.topics(fuel), "calls": calls}

    @app.get("/api/help/guides/{gid}")
    def help_guide(gid: str, user: dict = Depends(active)):
        return help_.guide(gid)

    @app.get("/api/help/lights")
    def help_lights(car_id: str | None = None, user: dict = Depends(active)):
        return help_.lights(fuel_of(user, car_id)[0])

    @app.post("/api/help/search")
    def help_search(b: SearchIn, user: dict = Depends(active)):
        """POST, so the owner's description never appears in URLs or access logs."""
        return help_.search(user, b.q, b.car_id)

    @app.post("/api/help/outcome")
    def help_outcome(b: OutcomeIn, user: dict = Depends(active)):
        return help_.record_outcome(user, b.guide_id, b.outcome, b.step_id)

    @app.get("/api/help/stats")
    def help_stats(request: Request):
        h = request.headers.get("authorization", "")
        token = h[7:].strip() if h[:7].lower() == "bearer " else ""
        if not s.admin_token or not hmac.compare_digest(token.encode(), s.admin_token.encode()):
            return JSONResponse({"detail": "Not found"}, 404)
        return help_.stats()

    @app.get("/api/help/requests")
    def help_requests(user: dict = Depends(active)):
        return help_.cases(user)

    @app.post("/api/help/requests", status_code=201)
    def help_create(b: CaseIn, user: dict = Depends(active)):
        data = b.model_dump(exclude_unset=True)
        if b.transcript is not None:
            data["transcript"] = [t.model_dump() for t in b.transcript]
        return help_.create_case(user, data)

    @app.get("/api/help/requests/{case_id}")
    def help_case(case_id: str, user: dict = Depends(active)):
        return help_.case(user, case_id)

    @app.patch("/api/help/requests/{case_id}")
    def help_update(case_id: str, b: CasePatch, user: dict = Depends(active)):
        return help_.update_case(user, case_id, b.model_dump(exclude_unset=True))

    @app.delete("/api/help/requests/{case_id}", status_code=204)
    def help_delete(case_id: str, user: dict = Depends(active)):
        help_.delete_case(user, case_id)
        return Response(status_code=204)

    @app.post("/api/help/requests/{case_id}/photos", status_code=201)
    async def help_photo_add(case_id: str, file: UploadFile | None = File(None), user: dict = Depends(active)):
        content, name = await _read(file)
        return help_.add_photo(user, case_id, content, name)

    @app.get("/api/help/requests/{case_id}/photos/{photo_id}")
    def help_photo(case_id: str, photo_id: str, user: dict = Depends(active)):
        content, mime, name = help_.photo(user, case_id, photo_id)
        return Response(content, media_type=mime, headers={"Content-Disposition": f'inline; filename="{name}"',
                                                           "Content-Security-Policy": "sandbox; default-src 'none'",
                                                           "Cache-Control": "private, no-store"})

    @app.delete("/api/help/requests/{case_id}/photos/{photo_id}", status_code=204)
    def help_photo_delete(case_id: str, photo_id: str, user: dict = Depends(active)):
        help_.delete_photo(user, case_id, photo_id)
        return Response(status_code=204)

    @app.get("/api/help/requests/{case_id}/summary")
    def help_summary(case_id: str, user: dict = Depends(active)):
        return PlainTextResponse(help_.summary(user, case_id))

    @app.post("/api/help/requests/{case_id}/report/preview")
    def help_report_preview(case_id: str, b: ReportIn, user: dict = Depends(active)):
        return help_.report_preview(user, case_id, b.model_dump())

    @app.post("/api/help/requests/{case_id}/report", status_code=201)
    def help_report(case_id: str, b: ReportIn, user: dict = Depends(active)):
        return help_.report(user, case_id, b.model_dump())

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def api_404(rest: str):
        return JSONResponse({"detail": "Not found"}, 404)

    # ------------------------------------------------------------------ the web app (single-page app with client-side routes)
    static = Path(s.static_dir).resolve()

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(path: str):
        index = static / "index.html"
        if not index.is_file():
            return PlainTextResponse(f"{s.app_name} API is running. Build the web app: cd owner-app && npm ci && npm run build", 200)
        target = (static / path).resolve() if path else index
        if path and static in target.parents and target.is_file():
            if path.startswith("assets/"):
                return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})
            hdr = {"Cache-Control": "no-cache"}
            if path == "sw.js":
                hdr["Service-Worker-Allowed"] = "/"
            return FileResponse(target, headers=hdr)
        if path.startswith("assets/") or "." in path.rsplit("/", 1)[-1]:
            return PlainTextResponse("Not found", 404)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


class _Unauthorized(Exception):
    pass


class _ProfileIncomplete(Exception):
    pass


class _WorkshopLimited(Exception):
    def __init__(self, retry: int):
        super().__init__("rate limited")
        self.retry = retry
