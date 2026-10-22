"""Phase B: help for car problems in the CarSaathi owner app (owner/helpcontent.py, safety.py, helpsearch.py, helpdesk.py,
bridge.py). Database tests run on SQLite, and on PostgreSQL too when OWNER_TEST_DATABASE_URL is set (CI job owner-postgres)."""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sa = pytest.importorskip("sqlalchemy")
from fastapi.testclient import TestClient  # noqa: E402

from owner import bridge  # noqa: E402
from owner import config as C  # noqa: E402
from owner import db as D  # noqa: E402
from owner import helpcontent as H  # noqa: E402
from owner import helpsearch as S  # noqa: E402
from owner import mailer  # noqa: E402
from owner import safety  # noqa: E402
from owner.api import create_app  # noqa: E402
from owner.security import RateLimiter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.getenv("OWNER_TEST_DATABASE_URL", "").strip()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + bytes(range(256)) * 4
PDF = b"%PDF-1.4\n" + b"1 0 obj << >> endobj\n" * 10
VIN = "MAT62564KNA123456"
# the path an owner takes through "Car braked or warned with nothing ahead": no crash, braked on its own, on a flyover
PHANTOM = [{"step": "s0", "answer": "option", "option": 1}, {"step": "s1", "answer": "option", "option": 0},
           {"step": "s2", "answer": "option", "option": 0}, {"step": "s3", "answer": "seen"}, {"step": "s4", "answer": "done"},
           {"step": "s5", "answer": "done"}, {"step": "s6", "answer": "seen"}]


# ---------------------------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(params=["sqlite"] + (["postgresql"] if PG_URL else []))
def db_url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'owner.db'}"
    url = C.normalize_db_url(PG_URL)
    eng = sa.create_engine(url)
    D.md.drop_all(eng)
    with eng.begin() as c:                                                      # a table left by an older schema test
        c.execute(sa.text("DROP TABLE IF EXISTS schema_version"))
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


@pytest.fixture
def make(db_url, tmp_path, outbox):
    apps = []

    def _make(**kw) -> TestClient:
        base = dict(env="development", database_url=db_url, secret_key="k" * 40, otp_delivery="console", static_dir=tmp_path / "none",
                    data_dir=tmp_path, cors_origins=())
        base.update(kw)
        app = create_app(C.OwnerSettings(**base))
        apps.append(app)
        return TestClient(app)
    yield _make
    for app in apps:
        app.state.svc.e.dispose()


@pytest.fixture
def oc(make):
    return make()


def sign_in(client, outbox, email, name="Asha Patil") -> dict:
    client.app.state.svc.limits = RateLimiter()
    client.post("/api/auth/code", json={"email": email})
    tok = client.post("/api/auth/verify", json={"email": email, "code": outbox[email]}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    client.patch("/api/me", json={"name": name, "consent": True}, headers=h)
    return h


def add_car(client, h, reg="MH12AB1234", **kw) -> dict:
    r = client.post("/api/cars", json={"make": "Tata", "model": "Nexon", "fuel": "petrol", "reg_number": reg, "year": 2023,
                                       "odometer_km": 24310, **kw}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def phantom_case(client, h, car_id, transcript=PHANTOM) -> dict:
    r = client.post("/api/help/requests", json={"car_id": car_id, "guide_id": "adas-phantom", "transcript": transcript,
                                                "description": "Twice on the same flyover, my number is 9876543210"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


class Receiver:
    """A stand-in for the engineering platform's complaint import: records requests, answers with a set status."""

    def __init__(self):
        self.requests, self.status = [], 201
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):                                                  # noqa: N802
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if outer.status < 300:
                    self.wfile.write(json.dumps({"complaints": [f"FC-{100 + len(outer.requests):03d}"], "issues": ["FI-001"], "runs": []}).encode())

            def log_message(self, *a):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.close()


# ---------------------------------------------------------------------------------------------------------------- content
def test_every_guide_and_light_is_valid():
    c = H.load()
    assert len(c["guides"]) >= 25 and len(c["lights"]) >= 15
    assert {g["category"] for g in c["guides"].values()} == {t["id"] for t in c["topics"]}
    reportable = {gid for gid, g in c["guides"].items() if "report" in g}
    assert reportable == {"adas-phantom", "adas-no-brake"}
    for g in c["guides"].values():                                              # every guide can end as fixed or escalated
        assert any(s["type"] == "check" and s.get("confirm") != "do" for s in g["steps"]) or g["category"] == "adas" or "report" in g


def test_broken_guides_are_rejected():
    good = copy.deepcopy(H.load()["guides"]["bt-pair"])
    tids = {t["id"] for t in H.load()["topics"]}
    H._check_guide(good, tids)

    def broken(change):
        g = copy.deepcopy(good)
        change(g)
        with pytest.raises(H.GuideError):
            H._check_guide(g, tids)
    broken(lambda g: g["steps"][0].update(next="nowhere"))                      # unknown target
    broken(lambda g: g["steps"][2].update(next="s1"))                           # a loop
    broken(lambda g: g["steps"][0].update(next="s3"))                           # s2 unreachable
    broken(lambda g: g.update(category="spaceship"))
    broken(lambda g: g.update(symptoms=["one"]))
    broken(lambda g: g["steps"].append({"id": "s9", "type": "ask", "text": "Which one?", "options": [{"label": "A", "facts": {"symptom": "aliens"}}, {"label": "B"}]}))
    broken(lambda g: g["steps"].append({"id": "done", "type": "info", "text": "Reserved name"}))


# ---------------------------------------------------------------------------------------------------------------- search
@pytest.mark.parametrize("q,expected", [
    ("bluetooth connect nahi ho raha", "bt-pair"),
    ("andriod auto not starting", "aa-start"),                                  # typo
    ("AA keeps dropping", "projection-drops"),                                  # abbreviation
    ("flyover pe achanak brake lag gaya", "adas-phantom"),                      # Hinglish
    ("speaker se awaaz nahi aa rahi", "audio-no-sound"),
    ("reverse camera blurry", "camera-reverse"),
])
def test_search_finds_the_guide(q, expected):
    r = S.search(q)
    assert r["results"][0]["id"] == expected and r["confident"] and r["urgent"] is None


def test_search_uses_the_car_and_says_when_unsure():
    assert "ev-charging" not in [x["id"] for x in S.search("not charging", fuel="petrol")["results"]]
    assert S.search("not charging at home", fuel="electric")["results"][0]["id"] == "ev-charging"
    unsure = S.search("what is the resale value of my car")
    assert unsure["confident"] is False and unsure["urgent"] is None
    lights = S.search("abs light on the dashboard")
    assert lights["lights"][0]["id"] == "abs"
    assert [li["id"] for li in S.search("dpf warning light", fuel="petrol")["lights"]] == []   # a diesel light, petrol car


@pytest.mark.parametrize("q,card", [
    ("brakes are not working", "brakes"), ("brake nahi lag rahe", "brakes"), ("pedal went to the floor when I braked", "brakes"),
    ("smoke from under the bonnet", "fire"), ("bonnet se dhuan aa raha", "fire"), ("strong petrol smell", "fuel"),
    ("steering suddenly very heavy", "steering"), ("engine overheating", "overheat"), ("car accelerating by itself", "acceleration"),
    ("tyre burst on the expressway", "wheel"), ("red oil light", "oil"), ("we hit the car ahead", "accident"),
])
def test_safety_cards_come_first(q, card):
    r = S.search(q, roadside="1800 209 1234")
    assert r["urgent"]["id"] == card
    numbers = [c["number"] for c in r["urgent"]["call"]]
    assert numbers[0] == "112" and "1800 209 1234" in numbers and "1033" in numbers


@pytest.mark.parametrize("q", ["no smoke, but the screen is frozen", "i hit the brakes and the music stopped",
                               "automatic emergency brake didn't work", "adaptive cruise speeds up by itself to the set speed",
                               "collision warning beeps on the flyover", "steering wheel vibrates on the highway"])
def test_no_false_alarms(q):
    assert safety.check(S.normalise(q)) is None


def test_the_labelled_sets_pass_their_gates():
    sys.path.insert(0, str(ROOT / "scripts"))
    import eval_owner_help as E
    for name in E.SETS:
        res = E.evaluate(name)
        assert res["passed"], (name, res["metrics"], res["failures"][:5])


def test_llm_may_only_pick_an_existing_guide():
    seen = []

    def llm(system, user):
        seen.append(user)
        return '{"guide_id": "voice", "confidence": 0.9}'
    q = "when i say call mom it rings someone else, reach me at asha@example.com or 9876543210, car MH 12 AB 1234, VIN " + VIN
    r = S.search(q, llm=llm)
    assert r["by"] == "llm" and r["results"][0]["id"] == "voice"
    for secret in ("asha@example.com", "9876543210", "MH 12 AB 1234", VIN):
        assert secret not in seen[0]                                            # removed before the text leaves
    for answer in ('{"guide_id": "rm -rf", "confidence": 1}', "not json", '{"guide_id": "voice", "confidence": 0.2}'):
        assert S.search(q, llm=lambda s, u, a=answer: a)["by"] == "rules"

    def down(s, u):
        raise TimeoutError
    assert S.search(q, llm=down)["by"] == "rules"
    calls = []
    S.search("bluetooth connect nahi ho raha", llm=lambda s, u: calls.append(1) or "{}")
    assert calls == []                                                          # a confident search never asks the LLM


# ---------------------------------------------------------------------------------------------------------------- reports to the platform
def test_reports_read_correctly_by_the_platform_rules():
    triage = pytest.importorskip("app.modules.field.triage")
    guides = H.load()["guides"]
    car = {"make": "Tata", "model": "Nexon", "year": 2023, "fuel": "petrol", "odometer_km": 24310, "vin": VIN}
    n = 0
    for gid, symptoms in (("adas-phantom", ["unexpected_braking", "false_warning"]), ("adas-no-brake", ["no_braking"])):
        g = guides[gid]
        locations = [o["facts"]["location"] for s in g["steps"] if s["type"] == "ask" for o in s["options"] if "location" in o.get("facts", {})]
        for symptom in symptoms:
            for loc in locations:
                for crash in (True, False):
                    for speed in (None, 72):
                        item = bridge.compose(g, car, {"symptom": symptom, "location": loc, "crash": crash},
                                              [{"step": "s4", "answer": "done"}], happened_on="2026-10-20", speed_kmh=speed,
                                              sw_version="2.4.0", include_vin=False)
                        u = triage.rules(item["text"])
                        assert (u["symptom"], u["location"], u["speed_kmh"], u["crash"], u["injury"]) == (symptom, loc, speed, crash, False), item["text"]
                        n += 1
    assert n == 64


def test_report_payload_is_anonymous_and_validated():
    g = H.load()["guides"]["adas-phantom"]
    car = {"make": "Tata", "model": "Nexon", "year": 2023, "fuel": "petrol", "odometer_km": 24310, "vin": VIN, "reg_number": "MH12AB1234"}
    item = bridge.compose(g, car, {"location": "gantry"}, [], happened_on="2026-10-20", speed_kmh=None, sw_version="", include_vin=False)
    assert item == {"text": item["text"], "vin": "", "build": "", "reported_at": "2026-10-20", "dealer": "CarSaathi owner app"}
    assert "MH12AB1234" not in item["text"] and "about 24,000 km" in item["text"] and "braked hard" in item["text"]   # default symptom
    for bad in ({"speed_kmh": 400}, {"sw_version": "latest"}):
        with pytest.raises(bridge.BridgeError):
            bridge.compose(g, car, {}, [], happened_on="2026-10-20", **{"speed_kmh": None, "sw_version": "", "include_vin": False, **bad})


# ---------------------------------------------------------------------------------------------------------------- API
def test_help_content_api(oc, outbox):
    assert oc.get("/api/help/topics").status_code == 401
    h = sign_in(oc, outbox, "a@example.com")
    petrol = add_car(oc, h, roadside_phone="1800 209 1234")
    ev = add_car(oc, h, reg="MH14CD5678", model="Tiago EV", fuel="electric")
    t = oc.get(f"/api/help/topics?car_id={petrol['id']}", headers=h).json()
    guides = [g["id"] for topic in t["topics"] for g in topic["guides"]]
    assert "ev-charging" not in guides and "bt-pair" in guides
    assert [c["number"] for c in t["calls"]] == ["112", "1800 209 1234", "1033"]
    t = oc.get(f"/api/help/topics?car_id={ev['id']}", headers=h).json()
    assert "ev-charging" in [g["id"] for topic in t["topics"] for g in topic["guides"]] and len(t["calls"]) == 2
    assert {li["id"] for li in oc.get(f"/api/help/lights?car_id={ev['id']}", headers=h).json()} >= {"ev-system", "oil"}
    assert "glow" not in {li["id"] for li in oc.get(f"/api/help/lights?car_id={ev['id']}", headers=h).json()}
    g = oc.get("/api/help/guides/adas-phantom", headers=h).json()
    assert g["reportable"] and g["steps"][0]["type"] == "ask"
    assert oc.get("/api/help/guides/nope", headers=h).status_code == 404
    r = oc.post("/api/help/search", json={"q": "android auto nahi chal raha", "car_id": petrol["id"]}, headers=h).json()
    assert r["results"][0]["id"] == "aa-start"
    assert oc.post("/api/help/search", json={"q": "x"}, headers=h).status_code == 422
    assert oc.get("/api/help/search", params={"q": "bluetooth"}, headers=h).status_code == 404     # never in a URL
    assert oc.patch(f"/api/cars/{petrol['id']}", json={"roadside_phone": "call me maybe"}, headers=h).status_code == 422
    b = sign_in(oc, outbox, "b@example.com", name="Bala")
    assert oc.post("/api/help/search", json={"q": "bluetooth", "car_id": petrol["id"]}, headers=b).status_code == 404
    assert oc.get(f"/api/help/topics?car_id={petrol['id']}", headers=b).status_code == 404


def test_help_request_rebuilds_the_steps_from_the_guide(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h, vin=VIN)
    cs = phantom_case(oc, h, car["id"])
    assert cs["ref"].startswith("CS-") and cs["status"] == "open" and cs["car_label"] == "Tata Nexon, MH 12 AB 1234"
    assert cs["facts"] == {"crash": False, "symptom": "unexpected_braking", "location": "flyover"}  # from the guide's options
    assert [t["answer_text"] for t in cs["transcript"][:3]] == ["No", "It braked hard on its own", "On a flyover or bridge"]
    assert cs["reportable"] and cs["car_has_vin"] and cs["tell_service"]

    def status(**body):
        return oc.post("/api/help/requests", json={"car_id": car["id"], **body}, headers=h).status_code
    assert status(guide_id="adas-phantom", transcript=[{"step": "s0", "answer": "option", "option": 1}], facts={"crash": True}) == 422
    assert status(guide_id="adas-phantom", transcript=[{"step": "zz", "answer": "seen"}]) == 422
    assert status(guide_id="adas-phantom", transcript=[{"step": "s0", "answer": "option", "option": 7}]) == 422
    assert status(guide_id="adas-phantom", transcript=[{"step": "s3", "answer": "fixed"}]) == 422   # an info step is only read
    assert status(guide_id="no-such-guide") == 422
    assert status(title="Strange noise", transcript=[{"step": "s1", "answer": "seen"}]) == 422      # steps need a guide
    assert status(title="Strange noise", happened_on=(date.today() + timedelta(days=2)).isoformat()) == 422
    free = oc.post("/api/help/requests", json={"title": "Rattle from the dashboard", "description": "Over speed breakers"}, headers=h)
    assert free.status_code == 201 and free.json()["guide_id"] is None and not free.json()["reportable"]

    text = oc.get(f"/api/help/requests/{cs['id']}/summary", headers=h).text
    assert "Car: Tata Nexon (MH 12 AB 1234), petrol, 2023, 24,310 km" in text and f"VIN: {VIN}" in text
    assert "- What did the car do? It braked hard on its own" in text and "(Done)" in text and "Details for the service centre:" in text
    rows = oc.get("/api/help/requests", headers=h).json()
    assert {r["title"] for r in rows} == {"Rattle from the dashboard", "Car braked or warned with nothing ahead"}
    r = oc.patch(f"/api/help/requests/{cs['id']}", json={"status": "resolved"}, headers=h).json()
    assert r["status"] == "resolved" and r["resolved_at"]
    assert oc.patch(f"/api/help/requests/{cs['id']}", json={"status": "open"}, headers=h).json()["resolved_at"] is None
    assert oc.patch(f"/api/help/requests/{cs['id']}", json={"status": "closed"}, headers=h).status_code == 422


def test_help_requests_are_private(oc, outbox):
    a = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, a)
    cs = phantom_case(oc, a, car["id"])
    photo = oc.post(f"/api/help/requests/{cs['id']}/photos", files={"file": ("screen.jpg", JPEG, "image/jpeg")}, headers=a).json()["photos"][0]
    b = sign_in(oc, outbox, "b@example.com", name="Bala")
    base = f"/api/help/requests/{cs['id']}"
    for method, path, kw in (("GET", base, {}), ("PATCH", base, {"json": {"status": "resolved"}}), ("DELETE", base, {}),
                             ("GET", f"{base}/summary", {}), ("GET", f"{base}/photos/{photo['id']}", {}),
                             ("DELETE", f"{base}/photos/{photo['id']}", {}), ("POST", f"{base}/report/preview", {"json": {}}),
                             ("POST", f"{base}/report", {"json": {"consent": True}})):
        assert oc.request(method, path, headers=b, **kw).status_code == 404, (method, path)
    assert oc.post(f"{base}/photos", files={"file": ("x.jpg", JPEG, "image/jpeg")}, headers=b).status_code == 404
    assert oc.get("/api/help/requests", headers=b).json() == []
    assert oc.post("/api/help/requests", json={"car_id": car["id"], "title": "Not my car"}, headers=b).status_code == 404


def test_help_request_photos(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    cs = oc.post("/api/help/requests", json={"title": "Warning on the screen"}, headers=h).json()
    url = f"/api/help/requests/{cs['id']}/photos"
    assert oc.post(url, files={"file": ("scan.pdf", PDF, "application/pdf")}, headers=h).status_code == 422
    assert oc.post(url, files={"file": ("x.jpg", b"<script>alert(1)</script>" * 3, "image/jpeg")}, headers=h).status_code == 422
    for i in range(3):
        assert oc.post(url, files={"file": (f"p{i}.jpg", JPEG, "image/jpeg")}, headers=h).status_code == 201
    assert oc.post(url, files={"file": ("p4.jpg", JPEG, "image/jpeg")}, headers=h).status_code == 409
    photos = oc.get(f"/api/help/requests/{cs['id']}", headers=h).json()["photos"]
    f = oc.get(f"{url}/{photos[0]['id']}", headers=h)
    assert f.content == JPEG and f.headers["content-security-policy"].startswith("sandbox") and "no-store" in f.headers["cache-control"]
    assert oc.delete(f"{url}/{photos[0]['id']}", headers=h).status_code == 204
    assert oc.delete(f"/api/help/requests/{cs['id']}", headers=h).status_code == 204
    with oc.app.state.svc.e.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(D.case_photos)).scalar() == 0


def test_report_needs_consent_and_shows_what_is_shared(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    cs = phantom_case(oc, h, car["id"])
    url = f"/api/help/requests/{cs['id']}/report"
    p = oc.post(f"{url}/preview", json={"speed_kmh": 70, "sw_version": "2.4.0"}, headers=h).json()
    assert p["mode"] == "not_connected" and p["item"]["build"] == "2.4.0" and "about 70 km/h" in p["item"]["text"]
    shared = json.dumps(p)
    for private in ("Asha", "a@example.com", "MH12AB1234", "MH 12 AB 1234", "9876543210", "Twice on the same flyover"):
        assert private not in shared
    assert oc.post(url, json={"speed_kmh": 70}, headers=h).status_code == 422                     # no consent
    assert oc.post(url, json={"consent": True, "include_vin": True}, headers=h).status_code == 422   # the car has no VIN
    assert oc.post(url, json={"consent": True, "sw_version": "v2"}, headers=h).status_code == 422
    assert oc.post(url, json={"consent": True, "speed_kmh": "fast"}, headers=h).status_code == 422
    r = oc.post(url, json={"consent": True, "speed_kmh": 70}, headers=h)
    assert r.status_code == 201 and r.json()["report"]["status"] == "not_connected"
    assert oc.post(url, json={"consent": True}, headers=h).status_code == 409
    other = oc.post("/api/help/requests", json={"car_id": car["id"], "guide_id": "bt-pair"}, headers=h).json()
    assert oc.post(f"/api/help/requests/{other['id']}/report", json={"consent": True}, headers=h).status_code == 422
    assert {r["id"]: r["report_status"] for r in oc.get("/api/help/requests", headers=h).json()}[cs["id"]] == "not_connected"


def test_reports_in_dry_run_are_never_sent(make, outbox, receiver):
    c = make(field_api_url=receiver.url, field_live=False)
    h = sign_in(c, outbox, "a@example.com")
    cs = phantom_case(c, h, add_car(c, h)["id"])
    r = c.post(f"/api/help/requests/{cs['id']}/report", json={"consent": True}, headers=h).json()
    assert r["report"]["status"] == "dry_run" and receiver.requests == []
    assert c.app.state.help.flush_reports() == {} and receiver.requests == []


def test_live_reports_reach_the_platform_and_retry(make, outbox, receiver):
    c = make(field_api_url=receiver.url, field_live=True, field_project="bcm-demo", field_token="aeai_" + "t" * 40)
    h = sign_in(c, outbox, "a@example.com")
    car = add_car(c, h, vin=VIN)
    cs = phantom_case(c, h, car["id"])
    r = c.post(f"/api/help/requests/{cs['id']}/report", json={"consent": True, "speed_kmh": 68, "sw_version": "2.4.0", "include_vin": True}, headers=h)
    rep = r.json()["report"]
    assert (rep["status"], rep["remote_ref"], rep["include_vin"]) == ("sent", "FC-101", True)
    req = receiver.requests[0]
    assert req["path"] == "/projects/bcm-demo/field/complaints/import"
    assert req["headers"]["Authorization"] == "Bearer aeai_" + "t" * 40 and req["headers"]["X-User"] == "carsaathi"
    item = req["body"]["complaints"][0]
    assert req["body"]["investigate"] is True and item["vin"] == VIN and item["build"] == "2.4.0" and item["dealer"] == "CarSaathi owner app"

    receiver.status = 503                                                       # the platform is down: kept and retried
    cs2 = phantom_case(c, h, car["id"])
    assert c.post(f"/api/help/requests/{cs2['id']}/report", json={"consent": True}, headers=h).json()["report"]["status"] == "pending"
    assert c.app.state.help.flush_reports() == {"pending": 1}
    receiver.status = 201
    assert c.app.state.help.flush_reports() == {"sent": 1}
    assert c.get(f"/api/help/requests/{cs2['id']}", headers=h).json()["report"]["status"] == "sent"

    receiver.status = 500
    cs3 = phantom_case(c, h, car["id"])
    c.post(f"/api/help/requests/{cs3['id']}/report", json={"consent": True}, headers=h)
    for _ in range(bridge.MAX_TRIES):
        c.app.state.help.flush_reports()
    assert c.get(f"/api/help/requests/{cs3['id']}", headers=h).json()["report"]["status"] == "failed"
    assert len(receiver.requests) == 4 + bridge.MAX_TRIES                      # 1 + 3 for the first two, then 8 tries

    demo = {"Authorization": f"Bearer {c.post('/api/auth/demo').json()['token']}"}
    dcar = c.get("/api/cars", headers=demo).json()[0]
    dcs = phantom_case(c, demo, dcar["id"])
    assert c.post(f"/api/help/requests/{dcs['id']}/report", json={"consent": True}, headers=demo).status_code == 403


def test_outcomes_are_anonymous_and_stats_need_the_admin_token(make, outbox):
    c = make(admin_token="a" * 40)
    h = sign_in(c, outbox, "a@example.com")
    for outcome, step in (("fixed", "s4"), ("fixed", "s4"), ("fixed", "s2"), ("escalated", "s6")):
        assert c.post("/api/help/outcome", json={"guide_id": "bt-pair", "outcome": outcome, "step_id": step}, headers=h).json() == {"recorded": True}
    assert c.post("/api/help/outcome", json={"guide_id": "bt-pair", "outcome": "loved"}, headers=h).status_code == 422
    assert c.post("/api/help/outcome", json={"guide_id": "bt-pair", "outcome": "fixed", "step_id": "s99"}, headers=h).status_code == 422
    assert c.get("/api/help/stats", headers=h).status_code == 404
    st = c.get("/api/help/stats", headers={"Authorization": "Bearer " + "a" * 40}).json()
    g = st["guides"][0]
    assert (g["id"], g["fixed"], g["escalated"], g["fix_rate"], g["fixed_at"]) == ("bt-pair", 3, 1, 0.75, {"s4": 2, "s2": 1})
    with c.app.state.svc.e.connect() as conn:
        assert set(conn.execute(sa.select(D.help_outcomes)).keys()) == {"id", "guide_id", "outcome", "step_id", "day"}
    assert make().get("/api/help/stats", headers={"Authorization": "Bearer " + "a" * 40}).status_code == 404


def test_export_and_erasure_include_help(make, outbox):
    c = make()
    h = sign_in(c, outbox, "a@example.com")
    cs = phantom_case(c, h, add_car(c, h)["id"])
    c.post(f"/api/help/requests/{cs['id']}/photos", files={"file": ("screen.jpg", JPEG, "image/jpeg")}, headers=h)
    c.post(f"/api/help/requests/{cs['id']}/report", json={"consent": True}, headers=h)
    data = c.get("/api/me/export", headers=h).json()
    assert data["help_requests"][0]["photos"] == ["screen.jpg"] and data["help_requests"][0]["facts"]["location"] == "flyover"
    assert data["reports_to_manufacturer"][0]["payload"]["dealer"] == "CarSaathi owner app"
    assert c.post("/api/me/delete", json={"confirm": "DELETE"}, headers=h).status_code == 200
    with c.app.state.svc.e.connect() as conn:
        for t in (D.help_cases, D.case_photos, D.field_reports):
            assert conn.execute(sa.select(sa.func.count()).select_from(t)).scalar() == 0


def test_a_phase_a_database_is_migrated(db_url):
    eng = D.engine(db_url)
    D.init(eng)
    with eng.begin() as c:                                                      # make it look like a schema-1 database
        c.execute(sa.text("ALTER TABLE cars DROP COLUMN roadside_phone"))
        c.execute(D.schema_version.delete())
        c.execute(D.schema_version.insert().values(version=1, applied_at=D.now()))
    assert "roadside_phone" not in {col["name"] for col in sa.inspect(eng).get_columns("cars")}
    assert D.init(eng) == D.SCHEMA_VERSION and D.init(eng) == D.SCHEMA_VERSION    # runs once
    assert "roadside_phone" in {col["name"] for col in sa.inspect(eng).get_columns("cars")}
    with eng.connect() as c:
        assert sorted(r.version for r in c.execute(sa.select(D.schema_version.c.version))) == list(range(1, D.SCHEMA_VERSION + 1))
    eng.dispose()


def test_help_settings_are_checked():
    errors, warnings = C.check(C.OwnerSettings(secret_key="s" * 40, field_live=True))
    assert any("OWNER_FIELD_API_URL" in e for e in errors)
    errors, _ = C.check(C.OwnerSettings(secret_key="s" * 40, field_api_url="ftp://x", admin_token="short"))
    assert len(errors) == 2
    _, warnings = C.check(C.OwnerSettings(secret_key="s" * 40, llm_base_url="https://api.groq.com/openai/v1", llm_model="llama"))
    assert any("LLM" in w for w in warnings)
