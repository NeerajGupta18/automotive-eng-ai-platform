"""Phase A: the CarSaathi car-owner service (owner/) and its app packaging (owner-app/, android-app/).

Every service test runs on SQLite, and also on PostgreSQL when OWNER_TEST_DATABASE_URL points at an empty test database
(the CI job `owner-postgres` does this):

    OWNER_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/owner_test python -m pytest tests/test_owner_app.py
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

sa = pytest.importorskip("sqlalchemy")
from fastapi.testclient import TestClient  # noqa: E402

from owner import config as C  # noqa: E402
from owner import db as D  # noqa: E402
from owner import mailer  # noqa: E402
from owner import validators as V  # noqa: E402
from owner.api import create_app  # noqa: E402
from owner.security import RateLimiter  # noqa: E402
from owner.service import _add_months  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.getenv("OWNER_TEST_DATABASE_URL", "").strip()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + bytes(range(256)) * 4
PNG = b"\x89PNG\r\n\x1a\n" + bytes(200)
PDF = b"%PDF-1.4\n" + b"1 0 obj << >> endobj\n" * 10
TODAY = date.today()


# ---------------------------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(params=["sqlite"] + (["postgresql"] if PG_URL else []))
def db_url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'owner.db'}"
    url = C.normalize_db_url(PG_URL)
    eng = sa.create_engine(url)
    D.md.drop_all(eng)                                                          # every test starts from an empty database
    eng.dispose()
    return url


@pytest.fixture
def static_dir(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>CarSaathi</title><div id=root></div>")
    (d / "assets" / "app-1234.js").write_text("console.log('app')")
    (d / "sw.js").write_text("self.addEventListener('fetch', () => {})")
    (d / "manifest.webmanifest").write_text("{}")
    return d


@pytest.fixture
def outbox(monkeypatch):
    """Sign-in codes 'sent' by the service, by address (the real mailer is tested separately)."""
    box = {}

    def fake(settings, to, code):
        if settings.otp_delivery == "email" and not settings.email_configured:
            raise mailer.MailError("Email sign-in is not configured on this server")
        box[to] = code
        return settings.otp_delivery
    monkeypatch.setattr(mailer, "send_code", fake)
    return box


@pytest.fixture
def make(db_url, tmp_path, static_dir, outbox):
    """make(**settings) -> TestClient of a fresh owner service on the test database."""
    apps = []

    def _make(raise_errors: bool = True, **kw) -> TestClient:
        base = dict(env="development", database_url=db_url, secret_key="k" * 40, otp_delivery="console", static_dir=static_dir,
                    data_dir=tmp_path, cors_origins=())
        base.update(kw)
        app = create_app(C.OwnerSettings(**base))
        apps.append(app)
        return TestClient(app, raise_server_exceptions=raise_errors)
    yield _make
    for app in apps:
        app.state.svc.e.dispose()


@pytest.fixture
def oc(make):
    return make()


def svc(client):
    return client.app.state.svc


def sign_in(client, outbox, email, name="Asha Patil", consent=True) -> dict:
    svc(client).limits = RateLimiter()                                          # tests sign in faster than people do
    r = client.post("/api/auth/code", json={"email": email})
    assert r.status_code == 202, r.text
    r = client.post("/api/auth/verify", json={"email": email, "code": outbox[email.lower()]})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    if name:
        assert client.patch("/api/me", json={"name": name, "consent": consent}, headers=h).status_code == 200
    return h


def add_car(client, h, reg="MH12AB1234", **kw) -> dict:
    body = {"make": "Tata", "model": "Nexon", "fuel": "petrol", "reg_number": reg, **kw}
    r = client.post("/api/cars", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def add_doc(client, h, car_id, kind="insurance", expires_on=None, content=None, filename="policy.jpg", **fields):
    data = {"kind": kind, **({"expires_on": expires_on.isoformat()} if expires_on else {}), **fields}
    files = {"file": (filename, content, "image/jpeg")} if content is not None else None
    return client.post(f"/api/cars/{car_id}/documents", data=data, files=files, headers=h)


def rows(client, table, **where):
    t = getattr(D, table)
    q = sa.select(sa.func.count()).select_from(t)
    for k, v in where.items():
        q = q.where(t.c[k] == v)
    with svc(client).e.connect() as c:
        return c.execute(q).scalar()


# ---------------------------------------------------------------------------------------------------------------- validation
def test_registration_numbers_vin_phone_and_dates():
    assert V.reg_number("mh 12 ab 1234") == "MH12AB1234"
    assert V.reg_display("MH12AB1234") == "MH 12 AB 1234"
    assert V.reg_display(V.reg_number("DL-3C-AB-0001")) == "DL 3 CAB 0001"
    assert V.reg_display(V.reg_number("22 BH 1234 AA")) == "22 BH 1234 AA"          # Bharat series
    for bad in ("", "ABC", "MH12AB12345", "12MH1234"):
        with pytest.raises(V.ValidationError):
            V.reg_number(bad)
    assert V.vin("mat62564kna123456") == "MAT62564KNA123456"
    with pytest.raises(V.ValidationError):
        V.vin("MAT62564KNO123456")                                                  # O is never used in a VIN
    assert V.phone("98765 43210") == V.phone("+91 98765-43210") == V.phone("09876543210") == "+919876543210"
    with pytest.raises(V.ValidationError):
        V.phone("12345")
    assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert _add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert _add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)


def test_uploads_are_typed_by_content_not_name():
    assert V.file_type(JPEG) == ("image/jpeg", ".jpg")
    assert V.file_type(PNG) == ("image/png", ".png")
    assert V.file_type(PDF) == ("application/pdf", ".pdf")
    assert V.file_type(b"RIFF\x00\x00\x00\x00WEBPVP8 " + bytes(20)) == ("image/webp", ".webp")
    with pytest.raises(V.ValidationError):
        V.file_type(b"<html><script>alert(1)</script></html>")
    assert V.file_name("../../etc/passwd.html", ".jpg") == "passwd.jpg"
    assert V.file_name('in"voice <1>.png', ".png") == "in_voice_1.png"


def test_configuration_check_and_database_url():
    ok = C.OwnerSettings(secret_key="s" * 40)
    assert C.check(ok) == ([], [])
    errors, _ = C.check(C.OwnerSettings(secret_key="short"))
    assert any("OWNER_SECRET_KEY" in e for e in errors)
    errors, _ = C.check(C.OwnerSettings(secret_key="s" * 40, proxy_hops=9, github_repo="nope", env="staging"))
    assert len(errors) == 3
    _, warnings = C.check(C.OwnerSettings(secret_key="s" * 40, env="production", database_url="sqlite:///x.db", otp_delivery="email"))
    assert any("SQLite" in w for w in warnings) and any("SMTP" in w for w in warnings)
    assert C.normalize_db_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert C.normalize_db_url("postgresql://u:p@h/db?sslmode=require") == "postgresql+psycopg://u:p@h/db?sslmode=require"
    with pytest.raises(RuntimeError, match="OWNER_SECRET_KEY"):
        create_app(C.OwnerSettings(secret_key="", database_url="sqlite://"))


def test_dotenv_reads_only_owner_settings(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OWNER_DEMO_LOGIN=0  # no demo\nSMTP_HOST='smtp.example.com'\nLLM_BACKEND=ollama\nexport OWNER_ENV=production\nBREVO_API_KEY=k\n")
    for k in ("OWNER_DEMO_LOGIN", "SMTP_HOST", "LLM_BACKEND", "BREVO_API_KEY"):
        monkeypatch.setenv(k, "")                                                   # recorded, so the test leaves no variables behind
        monkeypatch.delenv(k)
    monkeypatch.setenv("OWNER_ENV", "development")                                  # real environment variables win
    monkeypatch.setenv("AEAI_DOTENV", "0")
    assert C.load_dotenv(env) == []
    monkeypatch.setenv("AEAI_DOTENV", "1")
    assert sorted(C.load_dotenv(env)) == ["BREVO_API_KEY", "OWNER_DEMO_LOGIN", "SMTP_HOST"]
    assert os.environ["SMTP_HOST"] == "smtp.example.com" and os.environ["OWNER_DEMO_LOGIN"] == "0"
    assert os.environ["OWNER_ENV"] == "development" and "LLM_BACKEND" not in os.environ


# ---------------------------------------------------------------------------------------------------------------- sign-in
def test_email_code_sign_in_creates_account_and_needs_profile(oc, outbox):
    r = oc.post("/api/auth/code", json={"email": " Asha@Example.com "})
    assert r.status_code == 202 and r.json() == {"sent": True, "delivery": "console", "expires_in_min": 10}
    code = outbox["asha@example.com"]
    assert len(code) == 6 and code.isdigit()
    with svc(oc).e.connect() as c:
        stored = c.execute(sa.select(D.otps.c.code_hash)).scalar()
    assert code not in stored and len(stored) == 64                                # only an HMAC of the code is kept

    wrong = "000000" if code != "000000" else "111111"
    r = oc.post("/api/auth/verify", json={"email": "asha@example.com", "code": wrong})
    assert r.status_code == 422 and "4 attempt(s) left" in r.json()["detail"]
    r = oc.post("/api/auth/verify", json={"email": "ASHA@example.com", "code": f"{code[:3]} {code[3:]}"})
    assert r.status_code == 200
    body = r.json()
    assert body["new_user"] is True and body["token"].startswith("cs_") and body["user"]["profile_complete"] is False
    h = {"Authorization": f"Bearer {body['token']}"}
    assert r.headers["cache-control"] == "no-store"

    r = oc.get("/api/cars", headers=h)                                              # name and consent first
    assert r.status_code == 403 and r.json()["code"] == "profile_incomplete"
    assert oc.patch("/api/me", json={"consent": True}, headers=h).status_code == 422          # a name is needed
    r = oc.patch("/api/me", json={"name": "Asha Patil", "phone": "98765 43210", "consent": True}, headers=h)
    assert r.json()["profile_complete"] is True and r.json()["phone"] == "+919876543210" and r.json()["consent_version"] == "2026-10"
    assert oc.get("/api/cars", headers=h).json() == []

    # the code cannot be used twice; a second sign-in finds the same account
    assert oc.post("/api/auth/verify", json={"email": "asha@example.com", "code": code}).status_code == 422
    svc(oc).limits = RateLimiter()
    oc.post("/api/auth/code", json={"email": "asha@example.com"})
    again = oc.post("/api/auth/verify", json={"email": "asha@example.com", "code": outbox["asha@example.com"]}).json()
    assert again["new_user"] is False and again["user"]["id"] == body["user"]["id"]


def test_codes_expire_and_lock_after_five_wrong_tries(oc, outbox):
    oc.post("/api/auth/code", json={"email": "a@example.com"})
    with svc(oc).e.begin() as c:
        c.execute(D.otps.update().values(expires_at=D.now() - timedelta(seconds=1)))
    r = oc.post("/api/auth/verify", json={"email": "a@example.com", "code": outbox["a@example.com"]})
    assert r.status_code == 422 and "expired" in r.json()["detail"]

    svc(oc).limits = RateLimiter()
    oc.post("/api/auth/code", json={"email": "a@example.com"})
    code = outbox["a@example.com"]
    wrong = "000000" if code != "000000" else "111111"
    statuses = [oc.post("/api/auth/verify", json={"email": "a@example.com", "code": wrong}).status_code for _ in range(5)]
    assert statuses == [422, 422, 422, 422, 429]
    assert oc.post("/api/auth/verify", json={"email": "a@example.com", "code": code}).status_code == 422    # locked: new code needed


def test_sign_in_does_not_reveal_who_has_an_account(oc, outbox):
    sign_in(oc, outbox, "known@example.com")
    svc(oc).limits = RateLimiter()
    known = oc.post("/api/auth/code", json={"email": "known@example.com"})
    unknown = oc.post("/api/auth/code", json={"email": "nobody@example.com"})
    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())


def test_sign_in_rate_limits(oc, outbox):
    assert oc.post("/api/auth/code", json={"email": "a@example.com"}).status_code == 202
    r = oc.post("/api/auth/code", json={"email": "a@example.com"})                  # one code per 30 s per address
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    for i in range(18):                                     # refused requests count too: 20 per hour per visitor in all
        assert oc.post("/api/auth/code", json={"email": f"user{i}@example.com"}).status_code == 202
    assert oc.post("/api/auth/code", json={"email": "one-more@example.com"}).status_code == 429
    assert oc.post("/api/auth/code", json={"email": "not-an-email"}).status_code in (422, 429)


def test_rate_limits_trust_only_the_configured_proxy(make, outbox):
    behind = make(proxy_hops=1)
    for i in range(20):                       # the visitor forges the left part of X-Forwarded-For; Render's entry is the last
        r = behind.post("/api/auth/code", json={"email": f"u{i}@example.com"}, headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"})
        assert r.status_code == 202
    r = behind.post("/api/auth/code", json={"email": "x@example.com"}, headers={"X-Forwarded-For": "10.9.9.9, 203.0.113.7"})
    assert r.status_code == 429
    r = behind.post("/api/auth/code", json={"email": "y@example.com"}, headers={"X-Forwarded-For": "203.0.113.8"})
    assert r.status_code == 202                                                     # another visitor is not affected

    direct = make()                                                                 # no proxy configured: the header is ignored
    for i in range(20):
        direct.post("/api/auth/code", json={"email": f"d{i}@example.com"}, headers={"X-Forwarded-For": f"198.51.100.{i}"})
    assert direct.post("/api/auth/code", json={"email": "z@example.com"}, headers={"X-Forwarded-For": "198.51.100.99"}).status_code == 429


def test_email_sign_in_unavailable_without_smtp(make):
    c = make(otp_delivery="email")
    assert c.get("/api/config").json()["email_signin"] is False
    r = c.post("/api/auth/code", json={"email": "a@example.com"})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]
    assert c.post("/api/auth/demo").status_code == 200                              # the demo still works


def test_mailer_console_and_smtp(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="owner.mail"):
        assert mailer.send_code(C.OwnerSettings(otp_delivery="console"), "a@example.com", "123456") == "console"
    assert "123456" in caplog.text
    with pytest.raises(mailer.MailError):
        mailer.send_code(C.OwnerSettings(otp_delivery="email"), "a@example.com", "123456")

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = user

        def send_message(self, msg):
            sent["msg"] = msg
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    s = C.OwnerSettings(otp_delivery="email", smtp_host="smtp.example.com", smtp_port=587, smtp_user="bot", smtp_password="pw",
                        smtp_from="CarSaathi <bot@example.com>")
    assert mailer.send_code(s, "a@example.com", "654321") == "email"
    assert sent["server"] == ("smtp.example.com", 587) and sent["tls"] and sent["login"] == "bot"
    assert sent["msg"]["To"] == "a@example.com" and "654321" in sent["msg"]["Subject"]

    class Broken(FakeSMTP):
        def starttls(self, context):
            raise OSError("connection reset")
    monkeypatch.setattr(mailer.smtplib, "SMTP", Broken)
    with pytest.raises(mailer.MailError, match="could not be sent"):
        mailer.send_code(s, "a@example.com", "654321")


def test_mailer_brevo_https_api(monkeypatch):
    """Render's free plan blocks outgoing SMTP: with BREVO_API_KEY the code goes through Brevo's HTTPS API."""
    calls = []

    class Resp:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        calls.append((req.full_url, req.get_method(), dict(req.header_items()), json.loads(req.data), timeout))
        return Resp()
    monkeypatch.setattr(mailer.urllib.request, "urlopen", fake_urlopen)
    s = C.OwnerSettings(otp_delivery="email", brevo_api_key="xkeysib-test", mail_from="CarSaathi <bot@example.com>",
                        smtp_host="smtp.example.com")
    assert s.email_configured and s.mail_transport == "brevo"
    assert mailer.send_code(s, "a@example.com", "246810") == "email"
    url, method, headers, body, timeout = calls[0]
    assert (url, method, timeout) == ("https://api.brevo.com/v3/smtp/email", "POST", 15)
    assert headers["Api-key"] == "xkeysib-test"
    assert body["sender"] == {"email": "bot@example.com", "name": "CarSaathi"} and body["to"] == [{"email": "a@example.com"}]
    assert "246810" in body["subject"] and "246810" in body["htmlContent"]

    def refused(req, timeout):
        raise mailer.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(mailer.urllib.request, "urlopen", refused)
    with pytest.raises(mailer.MailError, match="could not be sent"):
        mailer.send_code(s, "a@example.com", "246810")
    assert not C.OwnerSettings(otp_delivery="email", brevo_api_key="k").email_configured        # a sender is needed too


def test_sessions_are_hashed_and_revocable(oc, outbox):
    for bad in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Bearer cs_" + "x" * 43}, {"Authorization": "Basic abc"}):
        r = oc.get("/api/me", headers=bad)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    h1 = sign_in(oc, outbox, "a@example.com")
    token = h1["Authorization"][7:]
    with svc(oc).e.connect() as c:
        hashes = [r.token_hash for r in c.execute(sa.select(D.sessions.c.token_hash))]
    assert hashes == [hashlib.sha256(token.encode()).hexdigest()]                   # the token itself is never stored

    h2 = sign_in(oc, outbox, "a@example.com", name=None)
    h3 = sign_in(oc, outbox, "a@example.com", name=None)
    assert oc.post("/api/auth/logout", headers=h1).status_code == 204
    assert oc.get("/api/me", headers=h1).status_code == 401
    assert oc.get("/api/me", headers=h2).status_code == 200
    assert oc.post("/api/auth/logout?everywhere=true", headers=h2).status_code == 204
    assert oc.get("/api/me", headers=h3).status_code == 401
    with svc(oc).e.begin() as c:                                                    # expired sessions stop working
        c.execute(D.sessions.update().values(revoked_at=None, expires_at=D.now() - timedelta(seconds=1)))
    assert oc.get("/api/me", headers=h3).status_code == 401


# ---------------------------------------------------------------------------------------------------------------- demo
def test_demo_is_a_private_sandbox_that_expires(oc, outbox):
    d1 = oc.post("/api/auth/demo").json()
    h1 = {"Authorization": f"Bearer {d1['token']}"}
    assert d1["user"]["is_demo"] and d1["user"]["profile_complete"] and d1["user"]["email"].endswith("@demo.invalid")
    dash = oc.get("/api/dashboard", headers=h1).json()
    nexon, tiago = dash["cars"]
    assert (nexon["reg_display"], nexon["odometer_km"]) == ("MH 12 AB 1234", 24310)
    assert nexon["missing_documents"] == [] and tiago["missing_documents"] == []         # an EV needs no PUC
    titles = [a["title"] for a in dash["attention"]]
    assert any(t.startswith("PUC certificate expired on") for t in titles) and any(t.startswith("Insurance expires on") for t in titles)
    assert dash["attention"][0]["status"] == "overdue" and dash["counts"]["overdue"] >= 1

    d2 = oc.post("/api/auth/demo").json()                                           # another visitor: their own copy
    h2 = {"Authorization": f"Bearer {d2['token']}"}
    assert oc.get(f"/api/cars/{nexon['id']}", headers=h2).status_code == 404
    r = oc.post(f"/api/cars/{nexon['id']}/members", json={"email": "friend@example.com"}, headers=h1)
    assert r.status_code == 403                                                     # no emails to real people from the demo

    with svc(oc).e.begin() as c:
        c.execute(D.users.update().where(D.users.c.id == d1["user"]["id"]).values(demo_expires_at=D.now() - timedelta(minutes=1)))
    assert oc.get("/api/me", headers=h1).status_code == 401
    assert svc(oc).cleanup_demo() == 1
    assert rows(oc, "cars") == 2 and rows(oc, "documents") == 5                    # only the second demo's data is left


def test_demo_switches_and_limits(make):
    assert make(demo_login=False).post("/api/auth/demo").status_code == 403
    one = make(max_demo_accounts=1)
    assert one.post("/api/auth/demo").status_code == 200
    assert one.post("/api/auth/demo").status_code == 503
    c = make()
    with svc(c).e.begin() as conn:
        conn.execute(D.users.delete())
    assert [c.post("/api/auth/demo").status_code for _ in range(11)][-1] == 429    # 10 per hour per visitor


# ---------------------------------------------------------------------------------------------------------------- garage
def test_add_car_validates_and_normalises(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h, reg="mh 12 ab 1234", vin="mat62564kna123456", year=2023, odometer_km="24310", nickname="  Family   car ")
    assert (car["reg_number"], car["reg_display"], car["vin"], car["role"]) == ("MH12AB1234", "MH 12 AB 1234", "MAT62564KNA123456", "owner")
    assert car["title"] == "Family car" and car["odometer_km"] == 24310

    def status(**kw):
        return oc.post("/api/cars", json={"make": "Tata", "model": "Punch", "fuel": "petrol", "reg_number": "MH14ZZ0001", **kw},
                       headers=h).status_code
    assert status(reg_number="MH-12-AB-1234") == 409                               # already in the garage
    assert status(reg_number="NOT A PLATE") == 422
    assert status(vin="12345") == 422
    assert status(year=1970) == 422
    assert status(fuel="steam") == 422
    assert status(odometer_km=-5) == 422
    assert status(owner_id="someone-else") == 422                                  # unknown fields are rejected
    for i in range(9):
        add_car(oc, h, reg=f"KA0{i + 1}AB{1000 + i}")
    assert status() == 409                                                         # at most 10 cars

    r = oc.patch(f"/api/cars/{car['id']}", json={"reg_number": "KA01AB1000"}, headers=h)
    assert r.status_code == 409
    r = oc.patch(f"/api/cars/{car['id']}", json={"colour": "Daytona Grey", "odometer_km": 24500}, headers=h)
    assert r.json()["colour"] == "Daytona Grey" and r.json()["odometer_km"] == 24500
    assert oc.delete(f"/api/cars/{car['id']}", headers=h).status_code == 204
    assert oc.get(f"/api/cars/{car['id']}", headers=h).status_code == 404


def test_other_peoples_records_are_not_found(oc, outbox):
    a = sign_in(oc, outbox, "a@example.com")
    b = sign_in(oc, outbox, "b@example.com", name="Bala")
    car = add_car(oc, a)
    doc = add_doc(oc, a, car["id"], expires_on=TODAY + timedelta(days=90), content=JPEG).json()
    rem = oc.post(f"/api/cars/{car['id']}/reminders", json={"title": "Service", "due_on": (TODAY + timedelta(days=9)).isoformat()},
                  headers=a).json()
    probes = [("GET", f"/api/cars/{car['id']}"), ("PATCH", f"/api/cars/{car['id']}"), ("DELETE", f"/api/cars/{car['id']}"),
              ("GET", f"/api/cars/{car['id']}/documents"), ("GET", f"/api/cars/{car['id']}/members"),
              ("GET", f"/api/documents/{doc['id']}"), ("GET", f"/api/documents/{doc['id']}/file"), ("DELETE", f"/api/documents/{doc['id']}"),
              ("PATCH", f"/api/reminders/{rem['id']}"), ("POST", f"/api/reminders/{rem['id']}/done"), ("DELETE", f"/api/reminders/{rem['id']}"),
              ("GET", f"/api/reminders?car_id={car['id']}"), ("POST", f"/api/cars/{car['id']}/reminders")]
    for method, path in probes:
        body = {"colour": "Red"} if path.startswith("/api/cars/") and method == "PATCH" else {"title": "Service", "due_on": TODAY.isoformat()}
        r = oc.request(method, path, headers=b, **({"json": body} if method in ("PATCH", "POST") else {}))
        assert r.status_code == 404, (method, path, r.status_code)
    assert add_doc(oc, b, car["id"]).status_code == 404
    assert oc.get("/api/dashboard", headers=b).json()["cars"] == []
    assert oc.get(f"/api/documents/{doc['id']}", headers=a).status_code == 200      # still there for its owner


def test_sharing_with_owner_and_driver_roles(oc, outbox):
    a = sign_in(oc, outbox, "a@example.com", name="Asha")
    car = add_car(oc, a, odometer_km=20000)
    a_doc = add_doc(oc, a, car["id"]).json()
    assert oc.post(f"/api/cars/{car['id']}/members", json={"email": "a@example.com"}, headers=a).status_code == 422
    inv = oc.post(f"/api/cars/{car['id']}/members", json={"email": "Bala@Example.com", "role": "driver"}, headers=a)
    assert inv.status_code == 201
    assert oc.post(f"/api/cars/{car['id']}/members", json={"email": "bala@example.com"}, headers=a).status_code == 409
    assert oc.post(f"/api/cars/{car['id']}/members", json={"email": "c@example.com", "role": "admin"}, headers=a).status_code == 422

    b = sign_in(oc, outbox, "bala@example.com", name="Bala")
    assert oc.get("/api/me", headers=b).json()["pending_invitations"] == 1
    mine = oc.get("/api/invitations", headers=b).json()
    assert mine[0]["from"] == "Asha" and mine[0]["reg_display"] == "MH 12 AB 1234" and mine[0]["role"] == "driver"
    assert oc.post(f"/api/invitations/{mine[0]['id']}/accept", headers=b).json()["car_id"] == car["id"]
    assert oc.post(f"/api/invitations/{mine[0]['id']}/accept", headers=b).status_code == 404      # used
    assert oc.get(f"/api/cars/{car['id']}", headers=b).json()["role"] == "driver"

    # a driver logs kilometres, adds papers and reminders, and manages only what they added
    assert oc.patch(f"/api/cars/{car['id']}", json={"odometer_km": 20450}, headers=b).json()["odometer_km"] == 20450
    assert oc.patch(f"/api/cars/{car['id']}", json={"odometer_km": 100}, headers=b).status_code == 422
    assert oc.patch(f"/api/cars/{car['id']}", json={"make": "Maruti Suzuki"}, headers=b).status_code == 403
    assert oc.delete(f"/api/cars/{car['id']}", headers=b).status_code == 403
    assert oc.post(f"/api/cars/{car['id']}/members", json={"email": "c@example.com"}, headers=b).status_code == 403
    b_doc = add_doc(oc, b, car["id"], kind="fastag").json()
    assert oc.patch(f"/api/documents/{a_doc['id']}", data={"number": "X1"}, headers=b).status_code == 403
    assert oc.delete(f"/api/documents/{a_doc['id']}", headers=b).status_code == 403
    assert oc.patch(f"/api/documents/{b_doc['id']}", data={"number": "FT-77"}, headers=b).json()["number"] == "FT-77"
    assert oc.delete(f"/api/documents/{b_doc['id']}", headers=b).status_code == 204

    # invitations can be withdrawn or declined; the last owner cannot leave
    c_inv = oc.post(f"/api/cars/{car['id']}/members", json={"email": "c@example.com"}, headers=a).json()
    assert oc.delete(f"/api/cars/{car['id']}/invitations/{c_inv['id']}", headers=b).status_code == 403
    assert oc.delete(f"/api/cars/{car['id']}/invitations/{c_inv['id']}", headers=a).status_code == 204
    c = sign_in(oc, outbox, "c@example.com", name="Chitra")
    assert oc.get("/api/invitations", headers=c).json() == []
    d_inv = oc.post(f"/api/cars/{car['id']}/members", json={"email": "c@example.com"}, headers=a).json()
    assert oc.post(f"/api/invitations/{d_inv['id']}/decline", headers=c).json()["status"] == "declined"
    assert oc.get(f"/api/cars/{car['id']}", headers=c).status_code == 404

    a_id = oc.get("/api/me", headers=a).json()["id"]
    b_id = oc.get("/api/me", headers=b).json()["id"]
    assert oc.delete(f"/api/cars/{car['id']}/members/{a_id}", headers=b).status_code == 403
    assert oc.delete(f"/api/cars/{car['id']}/members/{a_id}", headers=a).status_code == 409
    people = oc.get(f"/api/cars/{car['id']}/members", headers=a).json()
    assert [(m["name"], m["role"], m["you"]) for m in people["members"]] == [("Asha", "owner", True), ("Bala", "driver", False)]
    assert oc.delete(f"/api/cars/{car['id']}/members/{b_id}", headers=b).status_code == 204     # a driver may leave
    assert oc.get(f"/api/cars/{car['id']}", headers=b).status_code == 404


# ---------------------------------------------------------------------------------------------------------------- documents
def test_documents_with_photos_and_expiry_status(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    r = add_doc(oc, h, car["id"], kind="insurance", expires_on=TODAY + timedelta(days=10), content=JPEG, filename="../../My Policy.HTML",
                number="POL-778", issuer="Tata AIG", issued_on=(TODAY - timedelta(days=355)).isoformat())
    assert r.status_code == 201, r.text
    d = r.json()
    assert (d["status"], d["days_left"], d["has_file"], d["file_type"], d["file_name"]) == ("expiring", 10, True, "image/jpeg", "My_Policy.jpg")
    assert d["title"] == "Insurance" and d["kind_label"] == "Insurance"

    f = oc.get(f"/api/documents/{d['id']}/file", headers=h)
    assert f.content == JPEG and f.headers["content-type"] == "image/jpeg"
    assert f.headers["content-security-policy"].startswith("sandbox") and "no-store" in f.headers["cache-control"]
    assert f.headers["x-content-type-options"] == "nosniff"

    assert add_doc(oc, h, car["id"], kind="puc", expires_on=TODAY - timedelta(days=1)).json()["status"] == "expired"
    assert add_doc(oc, h, car["id"], kind="rc").json()["status"] == "no_expiry"
    assert add_doc(oc, h, car["id"], kind="warranty", expires_on=TODAY + timedelta(days=400)).json()["status"] == "valid"
    assert add_doc(oc, h, car["id"], kind="passport").status_code == 422
    assert add_doc(oc, h, car["id"], content=b"<html><script>alert(1)</script></html>", filename="a.jpg").status_code == 422
    assert add_doc(oc, h, car["id"], content=b"\xff\xd8\xff", filename="tiny.jpg").status_code == 422
    r = add_doc(oc, h, car["id"], expires_on=TODAY, issued_on=(TODAY + timedelta(days=1)).isoformat())
    assert r.status_code == 422 and "before" in r.json()["detail"]
    r = oc.patch(f"/api/documents/{d['id']}", data={"issued_on": (TODAY + timedelta(days=30)).isoformat()}, headers=h)
    assert r.status_code == 422

    # replace the photo with a PDF scan; deleting the document deletes the file
    r = oc.patch(f"/api/documents/{d['id']}", data={"number": "POL-779"}, files={"file": ("scan.pdf", PDF, "application/pdf")}, headers=h)
    assert (r.json()["number"], r.json()["file_type"], r.json()["file_name"]) == ("POL-779", "application/pdf", "scan.pdf")
    assert oc.get(f"/api/documents/{d['id']}/file", headers=h).content == PDF
    assert rows(oc, "document_files") == 1
    assert oc.delete(f"/api/documents/{d['id']}", headers=h).status_code == 204
    assert rows(oc, "document_files") == 0
    rc = oc.get(f"/api/cars/{car['id']}/documents", headers=h).json()
    assert {x["kind"] for x in rc} == {"puc", "rc", "warranty"}
    assert oc.get(f"/api/documents/{rc[0]['id']}/file", headers=h).status_code == 404         # no file attached


def test_upload_size_limits(make, outbox):
    c = make(max_upload_mb=1)
    h = sign_in(c, outbox, "a@example.com")
    car = add_car(c, h)
    r = add_doc(c, h, car["id"], content=JPEG + bytes(1024 * 1024))
    assert r.status_code == 422 and "larger than 1 MB" in r.json()["detail"]
    r = add_doc(c, h, car["id"], content=JPEG + bytes(3 * 1024 * 1024))
    assert r.status_code == 413
    assert add_doc(c, h, car["id"], content=JPEG + bytes(900 * 1024)).status_code == 201


# ---------------------------------------------------------------------------------------------------------------- reminders
def test_reminders_by_date_and_odometer(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h, odometer_km=24310)
    cid = car["id"]

    def add(**body):
        return oc.post(f"/api/cars/{cid}/reminders", json=body, headers=h)
    assert add(title="Nothing due").status_code == 422
    assert add(title="Insurance", due_km=30000, repeat="yearly").status_code == 422          # repeating by date needs a date
    assert add(title="Service", due_on=TODAY.isoformat(), every_km=10000).status_code == 422  # repeating by km needs a reading
    assert add(title="Service", due_km=25000, every_km=100).status_code == 422
    assert add(title="Service", due_on="31-12-2026").status_code == 422

    soon = add(title="Wiper blades", due_on=(TODAY + timedelta(days=5)).isoformat()).json()
    assert (soon["status"], soon["days_left"]) == ("due_soon", 5)
    km_only = add(title="Tyre rotation", due_km=24000).json()
    assert (km_only["status"], km_only["km_left"]) == ("overdue", -310)
    later = add(title="Battery check", due_on=(TODAY + timedelta(days=200)).isoformat()).json()
    assert later["status"] == "upcoming"
    service = add(title="Periodic service", due_on=(TODAY + timedelta(days=20)).isoformat(), due_km=25000, repeat="yearly",
                  every_km=10000).json()
    assert (service["status"], service["km_left"]) == ("due_soon", 690)
    monthly = add(title="Car wash", due_on=(TODAY - timedelta(days=70)).isoformat(), repeat="monthly").json()
    assert monthly["status"] == "overdue"
    add_doc(oc, h, cid, kind="puc", expires_on=TODAY + timedelta(days=3))

    items = oc.get(f"/api/reminders?car_id={cid}", headers=h).json()
    assert [i["status"] for i in items] == sorted((i["status"] for i in items), key=["overdue", "due_soon", "upcoming"].index)
    assert items[0]["title"] == "Car wash"                                          # most overdue first
    puc = next(i for i in items if i["type"] == "document")
    assert puc["status"] == "due_soon" and puc["title"].startswith("PUC certificate expires on")

    # done: a repeating reminder moves to the next date and/or reading on its schedule, past today and the odometer
    nxt = oc.post(f"/api/reminders/{monthly['id']}/done", headers=h).json()
    assert nxt["status"] != "done" and date.fromisoformat(nxt["due_on"]) > TODAY
    assert date.fromisoformat(nxt["due_on"]).day == date.fromisoformat(monthly["due_on"]).day or date.fromisoformat(monthly["due_on"]).day > 28
    nxt = oc.post(f"/api/reminders/{service['id']}/done", headers=h).json()
    assert nxt["due_km"] == 35000 and nxt["due_on"] == _add_months(TODAY + timedelta(days=20), 12).isoformat()
    oc.patch(f"/api/cars/{cid}", json={"odometer_km": 47000}, headers=h)
    assert oc.post(f"/api/reminders/{service['id']}/done", headers=h).json()["due_km"] == 55000
    done = oc.post(f"/api/reminders/{soon['id']}/done", headers=h).json()
    assert done["status"] == "done"
    assert oc.post(f"/api/reminders/{soon['id']}/done", headers=h).status_code == 409
    ids = [i["id"] for i in oc.get("/api/reminders", headers=h).json()]
    assert soon["id"] not in ids and soon["id"] in [i["id"] for i in oc.get("/api/reminders?include_done=true", headers=h).json()]

    # edits are checked against the whole reminder
    assert oc.patch(f"/api/reminders/{later['id']}", json={"due_on": None}, headers=h).status_code == 422
    assert oc.patch(f"/api/reminders/{km_only['id']}", json={"due_km": 50000}, headers=h).json()["status"] == "upcoming"
    assert oc.patch(f"/api/reminders/{later['id']}", json={"bogus": 1}, headers=h).status_code == 422
    assert oc.delete(f"/api/reminders/{later['id']}", headers=h).status_code == 204


def test_dashboard_summarises_every_car(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com", name="Asha Patil")
    petrol = add_car(oc, h)
    ev = add_car(oc, h, reg="MH14CD5678", model="Tiago EV", fuel="electric")
    add_doc(oc, h, petrol["id"], kind="insurance", expires_on=TODAY - timedelta(days=2))
    add_doc(oc, h, petrol["id"], kind="rc")
    add_doc(oc, h, ev["id"], kind="insurance", expires_on=TODAY + timedelta(days=300))
    dash = oc.get("/api/dashboard", headers=h).json()
    p, e = dash["cars"]
    assert p["documents"] == {"valid": 0, "expiring": 0, "expired": 1, "no_expiry": 1} and p["missing_documents"] == ["puc"]
    assert e["missing_documents"] == ["rc"] and e["attention"] == 0 and p["attention"] == 1
    assert dash["counts"] == {"overdue": 1, "due_soon": 0} and dash["user"]["name"] == "Asha Patil"
    assert dash["attention"][0]["title"].startswith("Insurance expired on")


# ---------------------------------------------------------------------------------------------------------------- privacy
def test_export_contains_everything_but_secrets(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    add_doc(oc, h, car["id"], content=JPEG)
    oc.post(f"/api/cars/{car['id']}/reminders", json={"title": "Service", "due_km": 30000}, headers=h)
    r = oc.get("/api/me/export", headers=h)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["account"]["email"] == "a@example.com" and data["account"]["consent_version"] == "2026-10"
    assert len(data["cars"]) == 1 and len(data["cars"][0]["documents"]) == 1 and len(data["cars"][0]["reminders"]) == 1
    assert {e["kind"] for e in data["events"]} >= {"login", "consent"}
    text = r.text
    assert "token_hash" not in text and "code_hash" not in text and data["sessions"]


def test_account_deletion_erases_data_but_keeps_shared_cars(oc, outbox):
    a = sign_in(oc, outbox, "a@example.com")
    b = sign_in(oc, outbox, "b@example.com", name="Bala")
    solo = add_car(oc, a)
    shared = add_car(oc, a, reg="MH14CD5678")
    add_doc(oc, a, solo["id"], content=JPEG)
    oc.post(f"/api/cars/{solo['id']}/reminders", json={"title": "Service", "due_km": 30000}, headers=a)
    inv = oc.post(f"/api/cars/{shared['id']}/members", json={"email": "b@example.com", "role": "owner"}, headers=a).json()
    oc.post(f"/api/invitations/{inv['id']}/accept", headers=b)
    oc.post(f"/api/cars/{solo['id']}/members", json={"email": "later@example.com"}, headers=a)

    assert oc.post("/api/me/delete", json={"confirm": "yes"}, headers=a).status_code == 422
    r = oc.post("/api/me/delete", json={"confirm": "DELETE"}, headers=a)
    assert r.json() == {"deleted": True, "cars_deleted": 1}
    assert oc.get("/api/me", headers=a).status_code == 401
    assert rows(oc, "users", email="a@example.com") == 0 and rows(oc, "sessions") == 1        # Bala's session only
    assert rows(oc, "documents") == 0 and rows(oc, "document_files") == 0 and rows(oc, "reminders") == 0
    assert rows(oc, "invitations", email="later@example.com") == 0 and rows(oc, "events") == rows(oc, "events", kind="login") + rows(oc, "events", kind="consent")
    left = oc.get("/api/cars", headers=b).json()
    assert [c["reg_number"] for c in left] == ["MH14CD5678"] and left[0]["role"] == "owner"


# ---------------------------------------------------------------------------------------------------------------- web
def test_security_headers_and_api_errors(make, outbox):
    oc = make(raise_errors=False)
    r = oc.get("/api/health")
    assert r.json()["status"] == "ok" and r.headers["cache-control"] == "no-store"
    for k, v in (("x-frame-options", "DENY"), ("x-content-type-options", "nosniff"), ("referrer-policy", "no-referrer")):
        assert r.headers[k] == v
    assert "strict-transport-security" not in r.headers                             # only in production (HTTPS)
    assert oc.get("/api/nothing-here").status_code == 404
    assert oc.post("/api/auth/code", json={"email": "a@example.com", "admin": True}).status_code == 422
    h = sign_in(oc, outbox, "a@example.com")

    def boom(user):
        raise RuntimeError("database password is hunter2")
    svc(oc).cars = boom
    r = oc.get("/api/cars", headers=h)
    assert r.status_code == 500 and "hunter2" not in r.text and r.json()["detail"].startswith("Something went wrong")
    big = oc.post("/api/auth/code", content=b"x" * (7 * 1024 * 1024), headers={"Content-Type": "application/json"})
    assert big.status_code == 413

    prod = make(env="production")
    r = prod.get("/api/health")
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert prod.get("/api/docs").status_code == 404 and prod.get("/api/openapi.json").status_code == 404
    assert make().get("/api/docs").status_code == 200


def test_web_app_routes_and_caching(oc):
    page = oc.get("/")
    assert page.status_code == 200 and "<div id=root>" in page.text
    csp = page.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "script-src 'self';" in csp
    assert page.headers["cache-control"] == "no-cache"
    for route in ("/garage", "/cars/123", "/reminders", "/me", "/privacy"):
        assert oc.get(route).text == page.text                                      # client-side routes get the app
    assert "immutable" in oc.get("/assets/app-1234.js").headers["cache-control"]
    assert oc.get("/assets/missing.js").status_code == 404
    assert oc.get("/favicon-missing.png").status_code == 404
    assert oc.get("/sw.js").headers["service-worker-allowed"] == "/"
    assert oc.head("/garage").status_code == 200 and oc.head("/api/health").status_code == 200     # link previews, uptime checks
    assert oc.get("/..%2F..%2Fowner.db").status_code == 404
    assert "owner.db" not in oc.get("/%2e%2e/owner.db").text


def test_without_a_build_the_api_still_runs(make, tmp_path):
    c = make(static_dir=tmp_path / "missing")
    r = c.get("/")
    assert r.status_code == 200 and "npm run build" in r.text
    assert c.get("/api/health").status_code == 200


def test_android_download_and_app_info(make):
    none = make()
    assert none.get("/get-app/android", follow_redirects=False).status_code == 404
    assert none.get("/api/app/info").json()["android"] == {"available": False, "url": None}
    gh = make(github_repo="neeraj/automotive-eng-ai")
    r = gh.get("/get-app/android", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://github.com/neeraj/automotive-eng-ai/releases/latest/download/carsaathi.apk"
    assert gh.get("/api/app/info").json()["android"]["url"] == "/get-app/android" and gh.get("/api/config").json()["android_app"]
    own = make(github_repo="neeraj/automotive-eng-ai", apk_url="https://example.com/carsaathi-1.2.apk")
    assert own.get("/get-app/android", follow_redirects=False).headers["location"] == "https://example.com/carsaathi-1.2.apk"


def test_cors_only_for_the_development_web_server(make):
    dev = make(cors_origins=("http://localhost:5173",))
    r = dev.options("/api/config", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    r = dev.options("/api/config", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers
    prod = make(env="production")
    assert "access-control-allow-origin" not in prod.get("/api/config", headers={"Origin": "https://evil.example"}).headers


def test_catalog_lists_indian_makes(oc):
    makes = {m["make"]: m["models"] for m in oc.get("/api/catalog").json()["makes"]}
    assert "Nexon" in makes["Tata"] and "Swift" in makes["Maruti Suzuki"] and len(makes) >= 10


# ---------------------------------------------------------------------------------------------------------------- packaging
def test_android_wrapper_matches_the_site():
    cap = json.loads((ROOT / "android-app" / "capacitor.config.json").read_text())
    assert cap["server"]["url"].startswith("https://") and cap["server"]["androidScheme"] == "https"
    assert cap["appendUserAgent"] == "CarSaathiApp"
    assert "CarSaathiApp" in (ROOT / "owner-app" / "src" / "lib" / "install.js").read_text()   # the site hides its download button in the app
    pkg = json.loads((ROOT / "android-app" / "package.json").read_text())
    assert all(v.startswith("^7.") for v in {**pkg["dependencies"], **pkg["devDependencies"]}.values())
    brand = (ROOT / "android-app" / "scripts" / "brand.sh").read_text()
    assert "android.permission.$1" in brand and "add_permission CAMERA" in brand and "#1F4FA3" in brand
    for d in ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi"):
        for name in ("ic_launcher", "ic_launcher_round", "ic_launcher_foreground"):
            assert (ROOT / "android-app" / "res" / f"mipmap-{d}" / f"{name}.png").is_file()
    wf = (ROOT / ".github" / "workflows" / "android-app.yml").read_text()
    assert "npx cap add android" in wf and "bash scripts/brand.sh" in wf and "assembleDebug" in wf and "java-version: \"21\"" in wf
    assert "android-${{ github.run_number }}" in wf and "make_latest: true" in wf and "carsaathi.apk" in wf
    assert C.OwnerSettings(github_repo="o/r").android_url.endswith("/carsaathi.apk")     # the name the workflow publishes


def test_pwa_and_deployment_files():
    pub = ROOT / "owner-app" / "public"
    manifest = json.loads((pub / "manifest.webmanifest").read_text())
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    for icon in manifest["icons"]:
        assert (pub / icon["src"].lstrip("/")).is_file()
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])
    sw = (pub / "sw.js").read_text()
    assert "url.pathname.startsWith('/api/')" in sw                                  # personal data is never cached offline
    yaml = pytest.importorskip("yaml")
    services = {s["name"]: s for s in yaml.safe_load((ROOT / "render.yaml").read_text())["services"]}
    cs = services["carsaathi"]
    env = {e["key"]: e for e in cs["envVars"]}
    assert cs["runtime"] == "docker" and cs["dockerfilePath"] == "./owner/Dockerfile" and cs["healthCheckPath"] == "/api/health"
    assert env["OWNER_SECRET_KEY"].get("generateValue") is True and env["OWNER_ENV"]["value"] == "production"
    for secret in ("OWNER_DATABASE_URL", "BREVO_API_KEY", "OWNER_MAIL_FROM", "OWNER_GITHUB_REPO"):
        assert env[secret].get("sync") is False and "value" not in env[secret]
    assert services["automotive-eng-ai"]["envVars"] and {e["key"]: e for e in services["automotive-eng-ai"]["envVars"]}["INTEGRATIONS_LIVE"]["value"] == "0"
    docker = (ROOT / "owner" / "Dockerfile").read_text()
    assert "USER app" in docker and "owner.api:create_app" in docker and "--factory" in docker
    assert "COPY owner-app/src" in docker and "COPY owner-app/ " not in docker      # never copies local node_modules
    ignore = (ROOT / ".gitignore").read_text()
    for path in ("owner-app/node_modules/", "owner-app/dist/", "android-app/android/", "keys/"):
        assert path in ignore
