"""Phase D: notifications (owner/notify.py, webpush.py) and service booking (owner/booking.py) in the CarSaathi owner app.
Database tests run on SQLite, and on PostgreSQL too when OWNER_TEST_DATABASE_URL is set (CI job owner-postgres)."""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sa = pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from owner import config as C  # noqa: E402
from owner import db as D  # noqa: E402
from owner import mailer  # noqa: E402
from owner import notify as N  # noqa: E402
from owner import webpush as W  # noqa: E402
from owner.api import create_app  # noqa: E402
from owner.booking import ics_fold  # noqa: E402
from owner.health import DEMO_ANSWERS  # noqa: E402
from owner.security import RateLimiter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.getenv("OWNER_TEST_DATABASE_URL", "").strip()
TODAY = date.today()
CRON = "c" * 40
VAPID_PUB, VAPID_PRIV = W.generate_vapid()


# ---------------------------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(params=["sqlite"] + (["postgresql"] if PG_URL else []))
def db_url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'owner.db'}"
    url = C.normalize_db_url(PG_URL)
    eng = sa.create_engine(url)
    D.md.drop_all(eng)
    eng.dispose()
    return url


@pytest.fixture
def outbox(monkeypatch):
    box = {}

    def fake(settings, to, code):
        box[to] = code
        return "console"
    monkeypatch.setattr(mailer, "send_code", fake)
    return box


class Sent:
    """Stand-ins for the email service and the push services."""

    def __init__(self):
        self.emails, self.pushes, self.push_status = [], [], 201

    def email(self, settings, to, subject, text, html, headers=None, tag=""):
        self.emails.append({"to": to, "subject": subject, "text": text, "html": html, "headers": headers or {}, "tag": tag})
        return "email"

    def push(self, url, headers, body):
        self.pushes.append({"url": url, "headers": headers, "body": body})
        return self.push_status


@pytest.fixture
def oc(db_url, tmp_path, outbox):
    app = create_app(C.OwnerSettings(env="development", database_url=db_url, secret_key="k" * 40, otp_delivery="console",
                                     static_dir=tmp_path / "none", data_dir=tmp_path, smtp_host="smtp.example.com",
                                     mail_from="CarSaathi <noreply@example.com>", public_url="https://cs.example", cron_token=CRON,
                                     vapid_public_key=VAPID_PUB, vapid_private_key=VAPID_PRIV, vapid_subject="mailto:ops@example.com"))
    sent = Sent()
    app.state.notify.send_email, app.state.notify.push_post = sent.email, sent.push
    client = TestClient(app)
    client.sent = sent
    yield client
    app.state.svc.e.dispose()


def sign_in(client, outbox, email, name="Asha Patil", phone="98765 43210") -> dict:
    client.app.state.svc.limits = RateLimiter()
    client.post("/api/auth/code", json={"email": email})
    h = {"Authorization": f"Bearer {client.post('/api/auth/verify', json={'email': email, 'code': outbox[email]}).json()['token']}"}
    client.patch("/api/me", json={"name": name, "consent": True, "phone": phone}, headers=h)
    return h


def add_car(client, h, reg="MH12AB1234", **kw) -> dict:
    r = client.post("/api/cars", json={"make": "Tata", "model": "Nexon", "fuel": "petrol", "reg_number": reg, "odometer_km": 10000, **kw}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def add_doc(client, h, car_id, kind, expires_on):
    r = client.post(f"/api/cars/{car_id}/documents", data={"kind": kind, "expires_on": expires_on.isoformat()}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def add_rem(client, h, car_id, **kw):
    r = client.post(f"/api/cars/{car_id}/reminders", json=kw, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def browser():
    """A browser's push subscription: its own P-256 key pair and auth secret."""
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    return key, auth, {"endpoint": f"https://fcm.googleapis.com/fcm/send/{os.urandom(8).hex()}", "keys": {"p256dh": W.b64u(pub), "auth": W.b64u(auth)}}


def titles(client, h):
    return [i["title"] for i in client.get("/api/notifications", headers=h).json()["items"]]


def run(client, day=TODAY):
    return client.app.state.notify.run(day)


# ---------------------------------------------------------------------------------------------------------------- Web Push
def test_push_encryption_matches_rfc_8291_example():
    """RFC 8291 Appendix A: the same keys, salt and message give byte-for-byte the published ciphertext."""
    as_key = ec.derive_private_key(int.from_bytes(W.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    out = W.encrypt(W.unb64u("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
                    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4", "BTBZMqHH6r4Tts7J_aSIgg",
                    salt=W.unb64u("DGv6ra1nlYgDCS1FRnbzlw"), server_key=as_key)
    assert W.b64u(out) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_push_request_decrypts_with_an_independent_library_and_vapid_verifies():
    http_ece = pytest.importorskip("http_ece")
    key, auth, sub = browser()
    s = {"endpoint": sub["endpoint"], **sub["keys"]}
    url, headers, body = W.request(s, {"title": "PUC expired", "path": "/x"}, subject="mailto:a@example.com", pub_b64=VAPID_PUB,
                                   priv_b64=VAPID_PRIV, topic="rem:1/x")
    assert json.loads(http_ece.decrypt(body, private_key=key, auth_secret=auth, version="aes128gcm")) == {"title": "PUC expired", "path": "/x"}
    assert headers["Content-Encoding"] == "aes128gcm" and headers["TTL"] == "86400" and headers["Topic"] == "rem1x"
    token = headers["Authorization"].split("t=")[1].split(",")[0]
    hd, cl, sig = token.split(".")
    claims = json.loads(W.unb64u(cl))
    assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"] == "mailto:a@example.com"
    assert 0 < claims["exp"] - datetime.now(timezone.utc).timestamp() <= 24 * 3600
    raw = W.unb64u(sig)
    W._public(VAPID_PUB).verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                                f"{hd}.{cl}".encode(), ec.ECDSA(hashes.SHA256()))
    assert headers["Authorization"].endswith(f"k={VAPID_PUB}")


@pytest.mark.parametrize("endpoint", ["http://fcm.googleapis.com/x", "https://169.254.169.254/latest", "https://localhost/x",
                                      "https://fcm.googleapis.com.evil.example/x", "https://evil.example/fcm.googleapis.com",
                                      "https://fcm.googleapis.com:8443/x", "https://u:p@fcm.googleapis.com/x", "ftp://x"])
def test_push_endpoints_must_be_known_push_services(endpoint):
    """The endpoint comes from the client: anything but a push service is refused (no SSRF through subscriptions)."""
    _, _, sub = browser()
    with pytest.raises(W.PushError):
        W.check_subscription(endpoint, **sub["keys"])


def test_push_keys_are_checked():
    _, _, sub = browser()
    for host in ("https://fcm.googleapis.com/a", "https://updates.push.services.mozilla.com/wpush/v2/x", "https://web.push.apple.com/Q",
                 "https://wns2-par02p.notify.windows.com/w/?token=x"):
        W.check_subscription(host, **sub["keys"])
    with pytest.raises(W.PushError):
        W.check_subscription("https://fcm.googleapis.com/a", sub["keys"]["p256dh"][:-4], sub["keys"]["auth"])
    with pytest.raises(W.PushError):
        W.check_subscription("https://fcm.googleapis.com/a", sub["keys"]["p256dh"], W.b64u(b"short"))
    other_pub, _ = W.generate_vapid()
    with pytest.raises(W.PushError):
        W.check_vapid(other_pub, VAPID_PRIV)
    errors, _ = C.check(C.OwnerSettings(secret_key="k" * 40, vapid_public_key=other_pub, vapid_private_key=VAPID_PRIV))
    assert any("does not belong" in e for e in errors)
    errors, _ = C.check(C.OwnerSettings(secret_key="k" * 40, vapid_public_key=VAPID_PUB))
    assert any("both" in e for e in errors)


# ---------------------------------------------------------------------------------------------------------------- rules
def test_stages_and_wording():
    assert [N.stage(d, N.DOC_LEAD) for d in (31, 30, 8, 7, 2, 1, 0, -1, -6, -7, -13, -14, -100)] == \
        [None, "d30", "d30", "d7", "d7", "d1", "due", "late0", "late0", "late1", "late1", "late2", "late4"]
    assert N.stage(20, N.REM_LEAD) is None and N.stage(7, N.REM_LEAD) == "d7"
    assert [N.when_text(d) for d in (5, 1, 0, -1, -9)] == ["in 5 days", "tomorrow", "today", "yesterday", "9 days ago"]
    assert N.device_id("doc:1") == N.device_id("doc:1") and 0 < N.device_id("doc:1") < 2 ** 31


def test_daily_run_notifies_each_stage_once(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    add_doc(oc, h, car["id"], "insurance", TODAY + timedelta(days=7))
    add_doc(oc, h, car["id"], "puc", TODAY - timedelta(days=10))
    add_doc(oc, h, car["id"], "rc", TODAY + timedelta(days=400))                     # far away: nothing
    service = add_rem(oc, h, car["id"], title="Periodic service", due_on=(TODAY + timedelta(days=1)).isoformat())
    add_rem(oc, h, car["id"], title="Tyre rotation", due_km=10300)                     # 300 km left, no km-per-day history
    add_rem(oc, h, car["id"], title="Battery check", due_on=(TODAY + timedelta(days=50)).isoformat())
    stats = run(oc)
    assert stats["created"] == 4 and stats["errors"] == 0
    assert set(titles(oc, h)) == {"Insurance expires in 7 days", "PUC certificate expired 10 days ago",
                                  "Periodic service due tomorrow", "Tyre rotation due in 300 km"}
    assert run(oc)["created"] == 0                                                    # the same day again: nothing new
    with oc.app.state.svc.e.connect() as c:
        keys = {r.key for r in c.execute(sa.select(D.notifications.c.key))}
    assert len(keys) == 4
    n = run(oc, TODAY + timedelta(days=6))                                            # insurance 1 day before; PUC 16 days late
    new = set(titles(oc, h)) - {"Insurance expires in 7 days", "PUC certificate expired 10 days ago", "Periodic service due tomorrow",
                                 "Tyre rotation due in 300 km"}
    assert {"Insurance expires tomorrow", "PUC certificate expired 16 days ago"} <= new and n["created"] >= 2
    assert "Periodic service overdue by 5 days" in new
    # done: a one-off reminder stops
    oc.post(f"/api/reminders/{service['id']}/done", headers=h)
    count = lambda: sum(t.startswith("Periodic service") for t in titles(oc, h))     # noqa: E731
    k = count()
    run(oc, TODAY + timedelta(days=13))                                               # it would have been a week overdue
    assert count() == k


def test_health_red_and_booking_eve_are_notified(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    oc.post(f"/api/cars/{car['id']}/health/codes", json={"codes": ["P0171"]}, headers=h)         # amber: not a notification
    run(oc)
    assert not [t for t in titles(oc, h) if "needs attention" in t]
    oc.post(f"/api/cars/{car['id']}/health/codes", json={"codes": ["P0524"]}, headers=h)         # oil pressure: don't drive
    run(oc)
    run(oc)
    assert titles(oc, h).count("Tata Nexon needs attention") == 1


def test_kinds_channels_and_the_email_digest(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    add_doc(oc, h, car["id"], "insurance", TODAY + timedelta(days=7))
    add_rem(oc, h, car["id"], title="Periodic service", due_on=TODAY.isoformat())
    assert oc.patch("/api/notifications/prefs", json={"kinds": ["reminders"]}, headers=h).json()["kinds"] == ["reminders"]
    assert oc.patch("/api/notifications/prefs", json={"kinds": ["spam"]}, headers=h).status_code == 422
    run(oc)
    assert titles(oc, h) == ["Periodic service due today"]                           # documents switched off
    mail = oc.sent.emails[-1]
    assert mail["to"] == "a@example.com" and mail["subject"] == "CarSaathi: Periodic service due today" and mail["tag"] == "reminders"
    assert "Hello Asha" in mail["text"] and "https://cs.example/cars/" in mail["text"] and "&lt;" not in mail["text"]
    unsub = mail["headers"]["List-Unsubscribe"]
    assert unsub.startswith("<https://cs.example/api/notify/unsubscribe?u=") and mail["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert oc.get("/api/notifications", headers=h).json()["items"][0]["email_status"] == "sent"
    # email off: the inbox still gets it, no email
    oc.patch("/api/notifications/prefs", json={"email": False, "kinds": ["reminders", "documents"]}, headers=h)
    n = len(oc.sent.emails)
    run(oc)
    assert "Insurance expires in 7 days" in titles(oc, h) and len(oc.sent.emails) == n
    assert oc.get("/api/notifications", headers=h).json()["items"][0]["email_status"] == "off"


def test_html_in_names_is_escaped_in_emails(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com", name="Asha <b>Patil</b>")
    car = add_car(oc, h)
    add_rem(oc, h, car["id"], title="<script>x</script>", due_on=TODAY.isoformat())
    run(oc)
    html = oc.sent.emails[-1]["html"]
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_demo_accounts_get_no_email(oc):
    h = {"Authorization": f"Bearer {oc.post('/api/auth/demo').json()['token']}"}
    assert set(titles(oc, h)) == {"Insurance expires in 12 days", "PUC certificate expired 3 days ago"}   # made at sign-up, not sent
    run(oc, TODAY + timedelta(days=5))
    assert oc.sent.emails == []
    p = oc.get("/api/notifications/prefs", headers=h).json()
    assert p["email_available"] is False and p["email_address"] == ""


def test_one_click_unsubscribe(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    uid = oc.get("/api/me", headers=h).json()["id"]
    tok = oc.app.state.notify.unsubscribe_token(uid)
    assert oc.get(f"/api/notify/unsubscribe?u={uid}&t={'0' * 40}").status_code == 404
    page = oc.get(f"/api/notify/unsubscribe?u={uid}&t={tok}")
    assert page.status_code == 200 and "<form method='post'" in page.text                 # opening the link changes nothing
    assert oc.get("/api/notifications/prefs", headers=h).json()["email"] is True
    assert oc.post(f"/api/notify/unsubscribe?u={uid}&t={tok}").status_code == 200        # the mail client's one-click POST
    assert oc.get("/api/notifications/prefs", headers=h).json()["email"] is False
    assert oc.post(f"/api/notify/unsubscribe?u=someone&t={tok}").status_code == 404


def test_push_to_browsers(oc, outbox):
    pytest.importorskip("http_ece")
    import http_ece
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    key, auth, sub = browser()
    assert oc.get("/api/push/key").json()["public_key"] == VAPID_PUB
    assert oc.post("/api/push/subscriptions", json={**sub, "endpoint": "https://10.0.0.1/x"}, headers=h).status_code == 422
    assert oc.post("/api/push/subscriptions", json={**sub, "label": "Chrome on Android"}, headers=h).status_code == 201
    add_rem(oc, h, car["id"], title="Periodic service", due_on=TODAY.isoformat())
    run(oc)
    p = oc.sent.pushes[-1]
    msg = json.loads(http_ece.decrypt(p["body"], private_key=key, auth_secret=auth, version="aes128gcm"))
    assert p["url"] == sub["endpoint"] and msg["title"] == "Periodic service due today" and msg["path"].startswith("/cars/")
    # the push service sees the Topic header: a hash, never our ids or the reminder's title
    assert re.fullmatch(r"[0-9a-f]{32}", p["headers"]["Topic"]) and car["id"].split("-")[0] not in p["headers"]["Topic"]
    assert oc.get("/api/notifications", headers=h).json()["items"][0]["push_status"] == "sent"
    # several at once: one summary push
    for i in range(3):
        add_rem(oc, h, car["id"], title=f"Check {i}", due_on=TODAY.isoformat())
    n = len(oc.sent.pushes)
    run(oc)
    assert len(oc.sent.pushes) == n + 1
    assert json.loads(http_ece.decrypt(oc.sent.pushes[-1]["body"], private_key=key, auth_secret=auth, version="aes128gcm"))["title"] == "3 things need your attention"
    # the browser unsubscribed: the push service answers 410 and the subscription is forgotten
    oc.sent.push_status = 410
    add_rem(oc, h, car["id"], title="Wipers", due_on=TODAY.isoformat())
    run(oc)
    assert oc.get("/api/notifications/prefs", headers=h).json()["devices"] == []


def test_push_device_moves_to_whoever_subscribes_and_can_be_removed(oc, outbox):
    a = sign_in(oc, outbox, "a@example.com")
    b = sign_in(oc, outbox, "b@example.com", name="Ravi Kumar")
    _, _, sub = browser()
    oc.post("/api/push/subscriptions", json=sub, headers=a)
    oc.post("/api/push/subscriptions", json=sub, headers=b)                             # same browser, now Ravi signed in
    assert oc.get("/api/notifications/prefs", headers=a).json()["devices"] == []
    dev = oc.get("/api/notifications/prefs", headers=b).json()["devices"]
    assert len(dev) == 1
    assert oc.delete(f"/api/push/subscriptions/{dev[0]['id']}", headers=a).status_code == 404
    assert oc.delete(f"/api/push/subscriptions/{dev[0]['id']}", headers=b).status_code == 204


def test_test_notification_and_its_limit(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    r = oc.post("/api/notifications/test", headers=h).json()
    assert r == {"email": "sent", "push": "off"} and oc.sent.emails[-1]["subject"] == "CarSaathi: Test notification"
    oc.post("/api/notifications/test", headers=h)
    oc.post("/api/notifications/test", headers=h)
    assert oc.post("/api/notifications/test", headers=h).status_code == 429
    assert oc.post("/api/notifications/read", json={}, headers=h).json() == {"unread": 0}


def test_the_daily_run_needs_its_token(oc, db_url, tmp_path):
    assert oc.post("/api/notify/run").status_code == 404
    assert oc.post("/api/notify/run", headers={"Authorization": "Bearer " + "x" * 40}).status_code == 404
    r = oc.post("/api/notify/run", headers={"Authorization": f"Bearer {CRON}"})
    assert r.status_code == 200 and set(r.json()) >= {"people", "created", "emailed", "pushed", "errors"}


def test_schedule_for_the_android_app(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    add_doc(oc, h, car["id"], "insurance", TODAY + timedelta(days=20))
    add_rem(oc, h, car["id"], title="Periodic service", due_on=(TODAY + timedelta(days=10)).isoformat())
    s = oc.get("/api/notifications/schedule", headers=h).json()
    got = [(i["at"], i["title"]) for i in s["items"]]
    day = lambda d: f"{(TODAY + timedelta(days=d)).isoformat()}T09:00:00+05:30"   # noqa: E731
    assert got == [(day(3), "Periodic service due in 7 days"), (day(9), "Periodic service due tomorrow"), (day(10), "Periodic service due today"),
                   (day(13), "Insurance expires in 7 days"), (day(19), "Insurance expires tomorrow"), (day(20), "Insurance expires today")]
    assert s["items"] == oc.get("/api/notifications/schedule", headers=h).json()["items"]     # stable ids
    oc.patch("/api/notifications/prefs", json={"push": False}, headers=h)
    assert oc.get("/api/notifications/schedule", headers=h).json()["items"] == []


# ---------------------------------------------------------------------------------------------------------------- booking
def book(oc, h, car, jobs=None, days=(3,), part="morning", **kw):
    body = {"workshop": {"name": "Shree Motors", "phone": "+91 98220 12345", "address": "Baner, Pune"},
            "jobs": [{"kind": "custom", "text": "General service"}] if jobs is None else jobs,
            "slots": [{"on": (TODAY + timedelta(days=d)).isoformat(), "part": part} for d in days], "contact_phone": "98765 43210", **kw}
    return oc.post(f"/api/cars/{car['id']}/bookings", json=body, headers=h)


def test_booking_end_to_end(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h, odometer_km=24310)
    rem = add_rem(oc, h, car["id"], title="Periodic service", due_on=(TODAY + timedelta(days=20)).isoformat(), due_km=25000,
                  repeat="yearly", every_km=10000)
    oc.post(f"/api/cars/{car['id']}/health/scans", json={"source": "demo", "answers": DEMO_ANSWERS}, headers=h)
    opts = oc.get(f"/api/cars/{car['id']}/booking-options", headers=h).json()
    assert [(j["kind"], j["text"]) for j in opts["jobs"]] == [("reminder", "Periodic service (due at 25,000 km)"),
                                                              ("code", "P0171: Engine running too lean (bank 1)")]
    assert opts["contact"]["phone"] == "+919876543210"
    r = book(oc, h, car, jobs=[{"kind": "reminder", "ref": rem["id"], "text": "Free oil change worth 5000"},   # text ignored
                                {"kind": "code", "ref": "p0171"}, {"kind": "custom", "text": "AC not cooling well"}],
             days=(3, 4), pickup=True, odometer_km=24400, notes="Please call before pickup")
    assert r.status_code == 201, r.text
    b = r.json()
    assert [j["text"] for j in b["jobs"]] == ["Periodic service (due at 25,000 km)", "P0171: Engine running too lean (bank 1)", "AC not cooling well"]
    assert b["status"] == "requested" and b["car"]["odometer_km"] == 24400
    assert oc.get("/api/workshops", headers=h).json()[0]["name"] == "Shree Motors"            # saved for next time
    share = oc.post(f"/api/bookings/{b['id']}/share", headers=h).json()
    assert share["url"] == f"https://cs.example/w#{share['token']}" and share["whatsapp_number"] == "919822012345"
    assert "{link}" in share["message"] and "MH 12 AB 1234" in share["message"]
    w = {"X-Booking-Token": share["token"]}
    view = oc.get("/api/w/booking", headers=w)
    assert view.headers["x-robots-tag"] == "noindex" and view.headers["cache-control"] == "no-store"
    v = view.json()
    assert v["contact"] == {"name": "Asha", "phone": "+919876543210"} and "a@example.com" not in json.dumps(v)
    assert "id" not in v and v["jobs"][1]["detail"].startswith("The engine is getting too much air") and v["can"][0] == "confirm"
    assert [e["kind"] for e in v["events"]] == ["created"]                              # not the owner's link housekeeping
    assert "shared" in [e["kind"] for e in oc.get(f"/api/bookings/{b['id']}", headers=h).json()["events"]]
    # a time inside a preferred slot confirms at once
    d3 = (TODAY + timedelta(days=3)).isoformat()
    v = oc.post("/api/w/booking/confirm", headers=w, json={"on": d3, "time": "10:30"}).json()
    assert v["status"] == "confirmed" and v["appointment"]["time"] == "10:30"
    oc.post("/api/w/booking/estimate", headers=w, json={"amount": "4,850", "note": "Includes air filter"})
    oc.post("/api/w/booking/message", headers=w, json={"note": "Please bring the service book"})
    assert {"Service confirmed: " + v["appointment"]["label"], "Estimate from Shree Motors: ₹4,850", "Message from Shree Motors"} <= set(titles(oc, h))
    assert oc.sent.emails[-1]["tag"] == "booking"
    ics = oc.get(f"/api/bookings/{b['id']}/calendar.ics", headers=h).text
    start = datetime.fromisoformat(d3).replace(hour=10, minute=30, tzinfo=N.IST).astimezone(timezone.utc)
    assert f"DTSTART:{start:%Y%m%dT%H%M%SZ}" in ics and "SUMMARY:Car service: Tata Nexon (MH 12 AB 1234)" in ics
    assert "LOCATION:Shree Motors\\, Baner\\, Pune" in ics and ics.endswith("\r\n") and all(len(x.encode()) <= 75 for x in ics.split("\r\n"))
    oc.post("/api/w/booking/received", headers=w, json={})
    v = oc.post("/api/w/booking/ready", headers=w, json={"amount": "5120", "note": "Air filter replaced"}).json()
    assert v["status"] == "ready" and v["bill"] == 5120.0 and v["can"] == ["message"]
    done = oc.post(f"/api/bookings/{b['id']}/complete", headers=h, json={"odometer_km": 24420, "done_reminders": [rem["id"]]}).json()
    assert done["status"] == "completed" and done["reminders_updated"] == ["Periodic service"] and done["expense_id"]
    exp = oc.get(f"/api/cars/{car['id']}/expenses", headers=h).json()[0]
    assert exp["category"] == "service" and exp["amount"] == 5120.0 and exp["vendor"] == "Shree Motors" and exp["odometer_km"] == 24420
    r2 = [i for i in oc.get(f"/api/reminders?car_id={car['id']}", headers=h).json() if i["type"] == "reminder"][0]
    assert r2["due_km"] == 35000                                                         # moved to the next service
    v = oc.get("/api/w/booking", headers=w).json()
    assert v["status"] == "completed" and v["can"] == []                                # read-only for the workshop
    assert oc.post("/api/w/booking/message", headers=w, json={"note": "hi"}).status_code == 409


def test_workshop_offers_another_time_and_owner_accepts(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    b = book(oc, h, car, days=(3,), part="morning").json()
    w = {"X-Booking-Token": oc.post(f"/api/bookings/{b['id']}/share", headers=h).json()["token"]}
    d5 = (TODAY + timedelta(days=5)).isoformat()
    assert oc.post("/api/w/booking/confirm", headers=w, json={"on": (TODAY + timedelta(days=3)).isoformat(), "time": "15:00"}).json()["status"] == "proposed"
    v = oc.post("/api/w/booking/confirm", headers=w, json={"on": d5, "time": "9:30"}).json()
    assert v["status"] == "proposed" and v["proposed"]["time"] == "09:30"
    assert any(t.startswith("Shree Motors offers") for t in titles(oc, h))
    x = oc.post(f"/api/bookings/{b['id']}/accept", headers=h).json()
    assert x["status"] == "confirmed" and x["appointment"] == {**v["proposed"]} and x["proposed"] is None
    assert oc.post(f"/api/bookings/{b['id']}/accept", headers=h).status_code == 409
    x = oc.post(f"/api/bookings/{b['id']}/reschedule", headers=h, json={"slots": [{"on": d5, "part": "afternoon"}]}).json()
    assert x["status"] == "requested" and x["appointment"] is None
    run(oc, TODAY + timedelta(days=4))                                                   # nothing confirmed: no reminder
    assert not [t for t in titles(oc, h) if t.startswith("Service tomorrow")]
    oc.post("/api/w/booking/confirm", headers=w, json={"on": d5, "time": "14:00"})
    run(oc, TODAY + timedelta(days=4))
    assert "Service tomorrow at 14:00" in titles(oc, h)


def test_booking_rules_and_access(oc, outbox):
    a = sign_in(oc, outbox, "a@example.com")
    other = sign_in(oc, outbox, "b@example.com", name="Ravi Kumar")
    car = add_car(oc, a)
    theirs = add_car(oc, other, reg="KA01AB1234")
    their_rem = add_rem(oc, other, theirs["id"], title="Service", due_on=(TODAY + timedelta(days=5)).isoformat())
    their_ws = oc.post("/api/workshops", json={"name": "Ravi's garage"}, headers=other).json()
    assert book(oc, a, car, jobs=[{"kind": "reminder", "ref": their_rem["id"]}]).status_code == 422
    assert book(oc, a, car, workshop_id=their_ws["id"]).status_code == 404
    assert book(oc, a, theirs).status_code == 404
    assert book(oc, a, car, days=(-1,)).status_code == 422 and book(oc, a, car, days=(61,)).status_code == 422
    assert book(oc, a, car, days=(1, 2, 3, 4)).status_code == 422
    assert book(oc, a, car, jobs=[]).status_code == 422 and book(oc, a, car, jobs=[{"kind": "code", "ref": "X123"}]).status_code == 422
    assert book(oc, a, car, contact_phone="").status_code == 422 and book(oc, a, car, odometer_km=5).status_code == 422
    first = book(oc, a, car).json()
    book(oc, a, car)
    book(oc, a, car)
    assert book(oc, a, car).status_code == 409                                           # three open bookings per car
    assert oc.get(f"/api/bookings/{first['id']}", headers=other).status_code == 404
    assert oc.post(f"/api/bookings/{first['id']}/cancel", json={}, headers=other).status_code == 404
    # the workshop link: header only, rotated, revoked with the cancellation
    t1 = oc.post(f"/api/bookings/{first['id']}/share", headers=a).json()["token"]
    assert oc.get(f"/api/w/booking?token={t1}").status_code == 404
    t2 = oc.post(f"/api/bookings/{first['id']}/share", headers=a).json()["token"]
    assert oc.get("/api/w/booking", headers={"X-Booking-Token": t1}).status_code == 404
    assert oc.get("/api/w/booking", headers={"X-Booking-Token": t2}).status_code == 200
    assert oc.post("/api/w/booking/ready", headers={"X-Booking-Token": t2}, json={}).status_code == 409    # not before confirming
    assert oc.post("/api/w/booking/confirm", headers={"X-Booking-Token": t2}, json={"on": TODAY.isoformat(), "time": "23:30"}).status_code == 422
    assert oc.post("/api/w/booking/estimate", headers={"X-Booking-Token": t2}, json={"amount": "inf"}).status_code == 422
    assert oc.post("/api/w/booking/hack", headers={"X-Booking-Token": t2}, json={}).status_code == 409
    assert oc.delete(f"/api/bookings/{first['id']}", headers=a).status_code == 409       # cancel first
    assert oc.post(f"/api/bookings/{first['id']}/cancel", json={"note": "Going elsewhere"}, headers=a).json()["status"] == "cancelled"
    assert oc.get("/api/w/booking", headers={"X-Booking-Token": t2}).status_code == 404
    assert oc.delete(f"/api/bookings/{first['id']}", headers=a).status_code == 204
    # an expired link
    second = oc.get(f"/api/cars/{car['id']}/bookings", headers=a).json()[0]
    t3 = oc.post(f"/api/bookings/{second['id']}/share", headers=a).json()["token"]
    with oc.app.state.svc.e.begin() as c:
        c.execute(D.bookings.update().where(D.bookings.c.id == second["id"]).values(token_expires_at=D.now() - timedelta(seconds=1)))
    assert oc.get("/api/w/booking", headers={"X-Booking-Token": t3}).status_code == 404


def test_a_driver_can_book_and_co_members_are_told(oc, outbox):
    owner = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, owner)
    oc.post(f"/api/cars/{car['id']}/members", json={"email": "d@example.com", "role": "driver"}, headers=owner)
    driver = sign_in(oc, outbox, "d@example.com", name="Dev Shah")
    inv = oc.get("/api/invitations", headers=driver).json()[0]
    oc.post(f"/api/invitations/{inv['id']}/accept", headers=driver)
    b = book(oc, driver, car).json()
    w = {"X-Booking-Token": oc.post(f"/api/bookings/{b['id']}/share", headers=driver).json()["token"]}
    oc.post("/api/w/booking/decline", headers=w, json={"note": "Fully booked this week"})
    assert "Shree Motors can't take the booking" in titles(oc, owner) and "Shree Motors can't take the booking" in titles(oc, driver)
    assert oc.delete(f"/api/bookings/{b['id']}", headers=driver).status_code == 204     # the person who booked may delete it


def test_ics_lines_fold_without_splitting_characters():
    line = "DESCRIPTION:" + "सर्विस " * 30
    folded = ics_fold(line)
    assert all(len(x.encode()) <= 75 for x in folded.split("\r\n")) and folded.replace("\r\n ", "") == line


# ---------------------------------------------------------------------------------------------------------------- privacy, schema
def test_export_and_erasure_include_notifications_and_bookings(oc, outbox):
    a = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, a)
    oc.post(f"/api/cars/{car['id']}/members", json={"email": "b@example.com", "role": "owner"}, headers=a)
    b = sign_in(oc, outbox, "b@example.com", name="Ravi Kumar")
    oc.post(f"/api/invitations/{oc.get('/api/invitations', headers=b).json()[0]['id']}/accept", headers=b)
    _, _, sub = browser()
    oc.post("/api/push/subscriptions", json=sub, headers=a)
    bk = book(oc, a, car).json()
    oc.post(f"/api/bookings/{bk['id']}/share", headers=a)
    oc.post("/api/notifications/test", headers=a)
    data = oc.get("/api/me/export", headers=a).json()
    assert data["workshops"][0]["name"] == "Shree Motors" and len(data["service_bookings"]) == 1
    assert "token_hash" not in data["service_bookings"][0] and data["service_bookings"][0]["events"]
    assert data["notifications"] and data["notification_settings"]["email"] is True
    assert data["push_devices"] == [{"label": "This browser", "service": "fcm.googleapis.com", "created_at": data["push_devices"][0]["created_at"]}]
    assert oc.post("/api/me/delete", json={"confirm": "DELETE"}, headers=a).status_code == 200
    with oc.app.state.svc.e.connect() as c:
        for t in (D.notifications, D.push_subscriptions, D.workshops, D.notification_prefs):
            assert c.execute(sa.select(sa.func.count()).select_from(t).where(t.c.user_id != "x")).scalar() == 0, t.name
        kept = c.execute(sa.select(D.bookings)).first()                                # the car stays with its co-owner
    assert kept.contact_name == "" and kept.contact_phone == "" and kept.token_hash is None


def test_a_schema_3_database_upgrades(db_url):
    eng = D.engine(db_url)
    D.init(eng)
    with eng.begin() as c:
        c.execute(D.schema_version.delete().where(D.schema_version.c.version >= 4))
        if not c.execute(sa.select(D.schema_version.c.version)).first():
            c.execute(D.schema_version.insert().values(version=3, applied_at=D.now()))
    assert D.init(eng) == D.SCHEMA_VERSION == 4
    with eng.connect() as c:
        assert 4 in {r.version for r in c.execute(sa.select(D.schema_version.c.version))}
    assert D.init(eng) == 4
    eng.dispose()


# ---------------------------------------------------------------------------------------------------------------- packaging
def test_notification_plumbing_in_the_apps_and_ci():
    web = json.loads((ROOT / "owner-app" / "package.json").read_text())
    android = json.loads((ROOT / "android-app" / "package.json").read_text())
    assert web["dependencies"]["@capacitor/local-notifications"].startswith("^7.")
    assert android["dependencies"]["@capacitor/local-notifications"].startswith("^7.")
    sw = (ROOT / "owner-app" / "public" / "sw.js").read_text()
    assert "addEventListener('push'" in sw and "addEventListener('notificationclick'" in sw
    lib = (ROOT / "owner-app" / "src" / "lib" / "notify.js").read_text()
    assert "import('@capacitor/local-notifications')" in lib and "pushManager.subscribe" in lib
    wf = (ROOT / ".github" / "workflows" / "owner-reminders.yml").read_text()
    assert "schedule:" in wf and "secrets.OWNER_CRON_TOKEN" in wf and "/api/notify/run" in wf
    assert "cryptography" in (ROOT / "owner" / "requirements.txt").read_text()


def test_app_routes_for_notifications_and_bookings():
    app = (ROOT / "owner-app" / "src" / "App.jsx").read_text()
    for route in ('path="/notifications"', 'path="/me/notifications"', 'path="/cars/:id/book"', 'path="/bookings/:id"'):
        assert route in app
    assert "loc.pathname === '/w'" in app                                   # the workshop's page works without signing in
    w = (ROOT / "owner-app" / "src" / "pages" / "Workshop.jsx").read_text()
    # the link's token is read from the fragment and sent in a header, never in a URL the server logs
    assert "window.location.hash" in w and "'X-Booking-Token'" in w and "?token" not in w


def test_public_url_defaults_to_renders_own_address(monkeypatch, tmp_path):
    monkeypatch.setenv("AEAI_DOTENV", "0")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OWNER_PUBLIC_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://carsaathi.onrender.com/")
    assert C.load().public_url == "https://carsaathi.onrender.com"
    monkeypatch.setenv("OWNER_PUBLIC_URL", "https://carsaathi.example")               # a custom domain wins
    assert C.load().public_url == "https://carsaathi.example"
