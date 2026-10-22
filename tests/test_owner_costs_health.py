"""Phase C: running costs and car health in the CarSaathi owner app (owner/costs.py, odometer.py, obd.py, health.py).
Database tests run on SQLite, and on PostgreSQL too when OWNER_TEST_DATABASE_URL is set (CI job owner-postgres)."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sa = pytest.importorskip("sqlalchemy")
from fastapi.testclient import TestClient  # noqa: E402

from owner import config as C  # noqa: E402
from owner import costs as K  # noqa: E402
from owner import db as D  # noqa: E402
from owner import helpcontent as H  # noqa: E402
from owner import mailer  # noqa: E402
from owner import obd  # noqa: E402
from owner.api import create_app  # noqa: E402
from owner.health import DEMO_ANSWERS, battery_finding, scan_findings  # noqa: E402
from owner.odometer import km_per_day, predicted_on  # noqa: E402
from owner.security import RateLimiter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.getenv("OWNER_TEST_DATABASE_URL", "").strip()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + bytes(range(256)) * 4
TODAY = date.today()


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


@pytest.fixture
def oc(db_url, tmp_path, outbox):
    app = create_app(C.OwnerSettings(env="development", database_url=db_url, secret_key="k" * 40, otp_delivery="console",
                                     static_dir=tmp_path / "none", data_dir=tmp_path))
    yield TestClient(app)
    app.state.svc.e.dispose()


def sign_in(client, outbox, email, name="Asha Patil") -> dict:
    client.app.state.svc.limits = RateLimiter()
    client.post("/api/auth/code", json={"email": email})
    h = {"Authorization": f"Bearer {client.post('/api/auth/verify', json={'email': email, 'code': outbox[email]}).json()['token']}"}
    client.patch("/api/me", json={"name": name, "consent": True}, headers=h)
    return h


def add_car(client, h, reg="MH12AB1234", **kw) -> dict:
    r = client.post("/api/cars", json={"make": "Tata", "model": "Nexon", "fuel": "petrol", "reg_number": reg, "odometer_km": 10000, **kw}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def fill(client, h, car_id, odo, qty, amount, on=None, full=True, **kw):
    return client.post(f"/api/cars/{car_id}/fuel", json={"odometer_km": odo, "quantity": qty, "amount": amount, "full": full,
                                                         "filled_on": (on or TODAY).isoformat(), **kw}, headers=h)


def backdate_readings(client, car_id, days_per_reading):
    """Spread a car's odometer readings back in time, oldest first, so km per day can be worked out."""
    with client.app.state.svc.e.begin() as c:
        rows = c.execute(sa.select(D.odometer_readings.c.id).where(D.odometer_readings.c.car_id == car_id).order_by(D.odometer_readings.c.km)).all()
        for i, r in enumerate(rows):
            c.execute(D.odometer_readings.update().where(D.odometer_readings.c.id == r.id)
                      .values(at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_per_reading * (len(rows) - 1 - i))))


# ---------------------------------------------------------------------------------------------------------------- calculations
def F(odo, qty, amount, full=True, d=0):
    return {"odometer_km": odo, "quantity_milli": round(qty * 1000), "amount_paise": round(amount * 100), "full": full,
            "filled_on": TODAY - timedelta(days=100 - d), "unit": "L"}


def test_full_to_full_mileage_by_hand():
    fills = [F(10500, 20, 2000, full=False, d=0),          # before the first full fill: ignored
             F(11000, 30, 3100, d=10), F(11250, 10, 1040, full=False, d=15), F(11600, 30, 3150, d=20),
             F(12000, 25, 2600, d=30), F(12000, 1, 104, d=31)]   # same reading again: no distance, skipped
    s = K.mileage_series(list(reversed(fills)))          # order doesn't matter
    assert [p["mileage"] for p in s] == [15.0, 16.0]      # (11600-11000)/(10+30) and (12000-11600)/25
    assert s[0]["distance_km"] == 600 and s[0]["quantity"] == 40.0 and s[0]["cost_per_km"] == round((1040 + 3150) / 600, 2)
    assert K.mileage_check(s) is None                     # fewer than 4 points
    series = [{"mileage": m, "on": "2026-10-01"} for m in (15.0, 15.4, 14.8, 15.2, 12.6)]
    assert K.mileage_check(series) == {"latest": 12.6, "usual": 15.1, "drop_pct": 17, "on": "2026-10-01"}
    assert K.mileage_check(series[:-1] + [{"mileage": 13.0, "on": "x"}]) is None   # 14% below: within normal variation


def test_km_per_day_and_predicted_dates():
    now = datetime(2026, 10, 22, 12)
    readings = [(10000, now - timedelta(days=200)), (20000, now - timedelta(days=60)), (21500, now - timedelta(days=30)), (23000, now)]
    assert km_per_day(readings, now.date()) == 50.0       # the 200-day-old reading is outside the 120-day window
    assert km_per_day([(100, now - timedelta(days=3)), (400, now)], now.date()) is None       # under 7 days
    assert km_per_day([(100, now - timedelta(days=30)), (130, now)], now.date()) is None      # under 50 km
    assert predicted_on(25000, 23000, 50.0, now.date()) == now.date() + timedelta(days=40)
    assert predicted_on(25000, 25100, 50.0, now.date()) == now.date()
    assert predicted_on(25000, 23000, None, now.date()) is None


# ---------------------------------------------------------------------------------------------------------------- OBD-II answers
CAN_ANSWERS = {"ATI": "ATI\rELM327 v1.5\r\r>", "ATRV": "12.4V\r\r>", "ATDPN": "A6\r\r>", "0101": "SEARCHING...\r41 01 83 07 E1 00\r\r>",
               "03": "00A\r0: 43 04 01 71 03 00\r1: 04 20 40 35 00 00\r\r>", "07": "47 01 03 01\r\r>", "0A": "NO DATA\r\r>",
               "0105": "41 05 5A\r\r>", "010C": "41 0C 0C 80\r\r>", "0142": "41 42 38 A4\r\r>", "0121": "41 21 00 2A\r\r>", "0131": "41 31 03 E8\r\r>"}


def test_decode_can_answers_with_multi_frame_codes():
    d = obd.decode(CAN_ANSWERS)
    assert (d["adapter"], d["protocol"], d["can"], d["connected"]) == ("ELM327 v1.5", "ISO 15765-4 CAN (11 bit, 500 kbit/s)", True, True)
    assert (d["mil_on"], d["stored_count"]) == (True, 3)
    assert d["codes"] == [{"code": "P0171", "kind": "stored"}, {"code": "P0300", "kind": "stored"}, {"code": "P0420", "kind": "stored"},
                          {"code": "C0035", "kind": "stored"}, {"code": "P0301", "kind": "pending"}]
    assert (d["coolant_c"], d["rpm"], d["engine_running"]) == (50, 800, True)
    assert (d["battery_mv"], d["battery_source"]) == (14500, "car")             # 0142 (the car) wins over ATRV (the adapter)
    assert (d["km_with_mil"], d["km_since_clear"], d["errors"]) == (42, 1000, {"0A": "NO DATA"})


def test_decode_older_protocols_and_odd_formats():
    iso = obd.decode({"ATDPN": "A3", "0101": "41 01 82 07 65 04", "03": "43 01 33 03 00 00 00\r43 01 71 00 00 00 00", "010C": "4101", "ATRV": "11.8 V"})
    assert iso["protocol"] == "ISO 9141-2" and iso["can"] is False
    assert [c["code"] for c in iso["codes"]] == ["P0133", "P0300", "P0171"]     # three slots per frame, zero padding dropped
    assert (iso["battery_mv"], iso["battery_source"], iso["rpm"]) == (11800, "adapter", None)
    nospace = obd.decode({"0101": "0101\r410100076500\r>", "03": "4300", "0105": "41057B"})    # echo on, spaces off
    assert (nospace["mil_on"], nospace["stored_count"], nospace["codes"], nospace["coolant_c"]) == (False, 0, [], 83)
    two_units = obd.decode({"0101": "41 01 00 07 65 00\r41 01 81 07 65 00"})   # two control units answer
    assert two_units["mil_on"] is False                                         # the first answer is used
    car_off = obd.decode({"ATI": "ELM327 v2.1", "ATRV": "12.6V", "0101": "UNABLE TO CONNECT", "03": "BUS INIT: ...ERROR"})
    assert car_off["connected"] is False and car_off["battery_mv"] == 12600 and car_off["errors"]["0101"] == "UNABLE TO CONNECT"
    assert obd.decode({"0142": "41 42 FF FF", "ATRV": "12.2V"})["battery_mv"] == 12200   # an impossible 65.5 V is ignored
    for bad in ({}, {"0902": "49 02 ..."}, {"0101": "4" * 2000}, {"0101": 5}):
        with pytest.raises(obd.ObdError):
            obd.decode(bad)


def test_fault_code_dictionary():
    codes = obd._dictionary()
    guides = set(H.load()["guides"])
    assert len(codes) >= 90
    for c, d in codes.items():
        assert obd.CODE_RE.match(c), c
        assert d["drive"] in ("yes", "carefully", "no") and d["title"] and d["plain"].endswith((".", ":")), c
        assert d.get("guide") in (None, *guides), c
    assert obd.explain(" p0171 ")["title"] == "Engine running too lean (bank 1)"
    assert obd.explain("P0524")["severity"] == "stop" and obd.explain("P0457")["severity"] == "info"
    unknown = obd.explain("P1A23")
    assert not unknown["known"] and unknown["manufacturer_specific"] and "service centre" in unknown["plain"]
    assert obd.explain("B0012")["title"] == "Airbag or seat belt fault" and obd.explain("U0300")["severity"] == "soon"
    for bad in ("P017", "X0171", "P4000", "P0171X", ""):
        with pytest.raises(obd.ObdError):
            obd.normalise_code(bad)


@pytest.mark.parametrize("mv,running,level", [(12700, False, None), (12300, False, "amber"), (11800, False, "red"),
                                               (14100, True, None), (12600, True, "red"), (15600, True, "amber"), (None, None, None)])
def test_battery_thresholds(mv, running, level):
    f = battery_finding(mv, running)
    assert (f["level"] if f else None) == level


def test_scan_findings():
    base = {"scanned_at": datetime.now(timezone.utc).replace(tzinfo=None), "mil_on": True, "battery_mv": 12700, "engine_running": False, "coolant_c": 30}
    f = scan_findings({**base, "codes": [{"code": "P0524", "kind": "stored"}, {"code": "P0457", "kind": "stored"}, {"code": "P0301", "kind": "pending"}]}, TODAY)
    assert [(x["level"], x["title"][:5]) for x in f] == [("red", "P0524"), ("amber", "P0457"), ("info", "P0301")]
    f = scan_findings({**base, "codes": [], "engine_running": True, "battery_mv": 14200, "coolant_c": 115,
                       "scanned_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=90)}, TODAY)
    assert [x["kind"] for x in f] == ["mil", "coolant", "stale"] and f[1]["level"] == "red"


# ---------------------------------------------------------------------------------------------------------------- API: costs
def test_fuel_log_rules_and_odometer(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    r = fill(oc, h, car["id"], 10400, "32.50", "3,380.00", on=TODAY - timedelta(days=20))
    assert r.status_code == 201 and (r.json()["quantity"], r.json()["amount"], r.json()["price"], r.json()["unit"]) == (32.5, 3380.0, 104.0, "L")
    assert fill(oc, h, car["id"], 10900, 30, 3120, on=TODAY - timedelta(days=10)).status_code == 201
    assert oc.get(f"/api/cars/{car['id']}", headers=h).json()["odometer_km"] == 10900               # raised by the fill
    bad = {"odometer before an earlier fill": fill(oc, h, car["id"], 10300, 30, 3100, on=TODAY - timedelta(days=5)),
           "odometer after a later fill": fill(oc, h, car["id"], 11000, 30, 3100, on=TODAY - timedelta(days=15)),
           "future": fill(oc, h, car["id"], 11500, 30, 3100, on=TODAY + timedelta(days=1)),
           "no odometer": oc.post(f"/api/cars/{car['id']}/fuel", json={"quantity": 30, "amount": 3100}, headers=h),
           "too much": fill(oc, h, car["id"], 11500, 400, 41000), "price": fill(oc, h, car["id"], 11500, 30, 300),
           "kg in a petrol car": fill(oc, h, car["id"], 11500, 8, 700, unit="kg"), "not a number": fill(oc, h, car["id"], 11500, "lots", 3100)}
    for why, r in bad.items():
        assert r.status_code == 422, why
    assert fill(oc, h, car["id"], 10900, 30, 3120, on=TODAY - timedelta(days=10)).status_code == 409    # the same fill twice
    cng = add_car(oc, h, reg="MH14CD5678", fuel="cng")
    assert fill(oc, h, cng["id"], 10100, 8.5, 765, unit="kg").json()["unit"] == "kg"
    assert fill(oc, h, cng["id"], 10200, 20, 2080, unit="L").json()["unit"] == "L"                     # CNG cars also run on petrol
    ev = add_car(oc, h, reg="MH14EV0001", fuel="electric", model="Tiago EV")
    assert fill(oc, h, ev["id"], 10150, 19.5, 156).json()["price"] == 8.0
    assert fill(oc, h, ev["id"], 10300, 19.5, 0).status_code == 201                                     # free charging is fine


def test_costs_permissions(oc, outbox):
    a = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, a)
    mine = fill(oc, a, car["id"], 10400, 30, 3100).json()
    inv = oc.post(f"/api/cars/{car['id']}/members", json={"email": "b@example.com", "role": "driver"}, headers=a).json()
    b = sign_in(oc, outbox, "b@example.com", name="Bala")
    oc.post(f"/api/invitations/{inv['id']}/accept", headers=b)
    theirs = fill(oc, b, car["id"], 10800, 30, 3100).json()                    # drivers log fills
    assert oc.delete(f"/api/fuel/{mine['id']}", headers=b).status_code == 403
    assert oc.delete(f"/api/fuel/{theirs['id']}", headers=b).status_code == 204
    assert oc.delete(f"/api/fuel/{mine['id']}", headers=a).status_code == 204
    c = sign_in(oc, outbox, "c@example.com", name="Chitra")
    for path in (f"/api/cars/{car['id']}/fuel", f"/api/cars/{car['id']}/expenses", f"/api/cars/{car['id']}/costs",
                 f"/api/cars/{car['id']}/costs.csv", f"/api/cars/{car['id']}/health", f"/api/cars/{car['id']}/health/scans"):
        assert oc.get(path, headers=c).status_code == 404, path
    assert fill(oc, c, car["id"], 11000, 30, 3100).status_code == 404
    assert oc.post(f"/api/cars/{car['id']}/health/codes", json={"codes": ["P0171"]}, headers=c).status_code == 404


def test_expenses_receipts_summary_and_csv(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h, odometer_km=9500)
    for i, (odo, qty) in enumerate(((10000, 30), (10450, 30), (10900, 30), (11350, 30))):
        fill(oc, h, car["id"], odo, qty, qty * 104, on=TODAY - timedelta(days=60 - 15 * i))
    url = f"/api/cars/{car['id']}/expenses"
    r = oc.post(url, data={"category": "service", "amount": "6850", "odometer_km": "11400", "vendor": "=HYPERLINK(\"http://x\")",
                           "spent_on": (TODAY - timedelta(days=2)).isoformat()}, files={"file": ("bill.jpg", JPEG, "image/jpeg")}, headers=h)
    assert r.status_code == 201 and r.json()["has_receipt"] and r.json()["category_label"] == "Service"
    f = oc.get(f"/api/expenses/{r.json()['id']}/receipt", headers=h)
    assert f.content == JPEG and f.headers["content-security-policy"].startswith("sandbox")
    assert oc.get(f"/api/cars/{car['id']}", headers=h).json()["odometer_km"] == 11400                  # the expense's reading counts
    for bad in ({"category": "snacks", "amount": "100"}, {"category": "toll", "amount": "0"}, {"category": "toll", "amount": "-5"},
                {"category": "toll", "amount": "100", "spent_on": (TODAY + timedelta(days=3)).isoformat()}):
        assert oc.post(url, data=bad, headers=h).status_code == 422, bad
    oc.post(url, data={"category": "toll", "amount": "235.50"}, headers=h)
    s = oc.get(f"/api/cars/{car['id']}/costs", headers=h).json()
    assert len(s["months"]) == 12 and s["months"][-1]["month"] == f"{TODAY.year}-{TODAY.month:02d}"
    assert s["period_total"] == 4 * 30 * 104 + 6850 + 235.5
    assert sum(m["fuel"] + m["other"] for m in s["months"]) == s["period_total"]
    assert s["mileage"]["L"]["series"][-1]["mileage"] == 15.0 and s["mileage"]["L"]["average"] == 15.0
    assert s["km_driven"] == 11400 - 9500 and s["cost_per_km"] == round(s["period_total"] / 1900, 2)
    assert [c["category"] for c in s["by_category"]] == ["service", "toll"]
    assert len(oc.get(f"/api/cars/{car['id']}/costs?months=3", headers=h).json()["months"]) == 3
    text = oc.get(f"/api/cars/{car['id']}/costs.csv", headers=h)
    assert "attachment" in text.headers["content-disposition"] and text.headers["content-type"].startswith("text/csv")
    lines = text.text.splitlines()
    assert lines[0] == "# Tata Nexon MH 12 AB 1234" and lines[1].startswith("date,type,category,amount_inr")
    assert len(lines) == 2 + 4 + 2 and any("'=HYPERLINK" in ln for ln in lines)                    # a formula is shown as text


def test_km_reminders_get_a_predicted_date(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h, odometer_km=20000)
    for km in (21000, 22000, 23000):
        oc.patch(f"/api/cars/{car['id']}", json={"odometer_km": km}, headers=h)
    backdate_readings(oc, car["id"], 10)                                          # 3,000 km in 30 days: 100 km a day
    r = oc.post(f"/api/cars/{car['id']}/reminders", json={"title": "Service", "due_km": 24500}, headers=h).json()
    assert r["km_left"] == 1500 and r["status"] == "due_soon"                     # 1,500 km is only 15 days away at this rate
    assert r["predicted_on"] == (TODAY + timedelta(days=15)).isoformat()
    item = next(i for i in oc.get("/api/reminders", headers=h).json() if i["id"] == r["id"])
    assert item["predicted_on"] == r["predicted_on"]
    assert oc.get(f"/api/cars/{car['id']}/health", headers=h).json()["km_per_day"] == 100.0


# ---------------------------------------------------------------------------------------------------------------- API: health
def test_health_from_scans_and_codes(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    first = oc.get(f"/api/cars/{car['id']}/health", headers=h).json()
    assert first["status"] == "green" and first["findings"][0]["kind"] == "no_scan"
    r = oc.post(f"/api/cars/{car['id']}/health/scans", json={"source": "demo", "answers": DEMO_ANSWERS}, headers=h)
    assert r.status_code == 201
    scan, health = r.json()["scan"], r.json()["health"]
    assert [c["code"] for c in scan["codes"]] == ["P0171"] and scan["battery_v"] == 12.3 and scan["engine_running"] is False
    assert scan["codes"][0]["title"] == "Engine running too lean (bank 1)" and "raw" not in scan
    assert health["status"] == "amber" and {f["kind"] for f in health["findings"]} >= {"code", "battery"}
    no_car = {"ATI": "ELM327 v1.5", "0101": "UNABLE TO CONNECT", "03": "UNABLE TO CONNECT"}
    assert oc.post(f"/api/cars/{car['id']}/health/scans", json={"source": "adapter", "answers": no_car}, headers=h).status_code == 422
    assert oc.post(f"/api/cars/{car['id']}/health/scans", json={"source": "garage", "answers": DEMO_ANSWERS}, headers=h).status_code == 422
    assert oc.post(f"/api/cars/{car['id']}/health/scans", json={"source": "demo", "answers": {**DEMO_ANSWERS, "0902": "x"}}, headers=h).status_code == 422
    r = oc.post(f"/api/cars/{car['id']}/health/codes", json={"codes": ["p0524", "P0524", "P0457"], "mil_on": True}, headers=h).json()
    assert [c["code"] for c in r["scan"]["codes"]] == ["P0524", "P0457"] and r["health"]["status"] == "red"
    assert r["health"]["findings"][0]["title"].startswith("P0524")
    for bad in ({"codes": []}, {"codes": ["P0171"] * 21}, {"codes": ["hello"]}):
        assert oc.post(f"/api/cars/{car['id']}/health/codes", json=bad, headers=h).status_code == 422
    scans = oc.get(f"/api/cars/{car['id']}/health/scans", headers=h).json()
    assert [s["source"] for s in scans] == ["manual", "demo"]
    assert oc.delete(f"/api/health/scans/{scans[0]['id']}", headers=h).status_code == 204
    assert oc.get(f"/api/cars/{car['id']}/health", headers=h).json()["status"] == "amber"
    assert oc.get("/api/obd/codes/P0300", headers=h).json()["severity"] == "soon"
    assert oc.get("/api/obd/codes/XYZ", headers=h).status_code == 422
    assert oc.get("/api/obd/commands").json()["commands"] == list(obd.COMMANDS)


def test_demo_account_tells_a_consistent_story(oc):
    h = {"Authorization": f"Bearer {oc.post('/api/auth/demo').json()['token']}"}
    dash = oc.get("/api/dashboard", headers=h).json()
    nexon, ev = dash["cars"]
    assert nexon["health"]["status"] == "amber" and nexon["health"]["top"].startswith("P0171")
    assert ev["health"]["status"] == "green"
    s = oc.get(f"/api/cars/{nexon['id']}/costs", headers=h).json()
    m = s["mileage"]["L"]
    assert m["check"]["drop_pct"] >= 15 and m["latest"] == 12.6 and 25 < s["km_per_day"] < 60
    kinds = [f["kind"] for f in oc.get(f"/api/cars/{nexon['id']}/health", headers=h).json()["findings"]]
    assert {"code", "battery", "mileage", "papers"} <= set(kinds)
    e = oc.get(f"/api/cars/{ev['id']}/costs", headers=h).json()
    assert list(e["mileage"]) == ["kWh"] and 7 < e["mileage"]["kWh"]["average"] < 8.5
    rems = {i["title"]: i for i in oc.get("/api/reminders", headers=h).json() if i["type"] == "reminder"}
    assert rems["Tyre rotation"]["predicted_on"] and rems["Tyre rotation"]["status"] == "due_soon"


def test_export_and_deletion_include_costs_and_health(oc, outbox):
    h = sign_in(oc, outbox, "a@example.com")
    car = add_car(oc, h)
    fill(oc, h, car["id"], 10400, 30, 3100)
    oc.post(f"/api/cars/{car['id']}/expenses", data={"category": "toll", "amount": "100"}, files={"file": ("t.jpg", JPEG, "image/jpeg")}, headers=h)
    oc.post(f"/api/cars/{car['id']}/health/scans", json={"source": "demo", "answers": DEMO_ANSWERS}, headers=h)
    data = oc.get("/api/me/export", headers=h).json()
    assert len(data["fuel_logs"]) == 1 and len(data["expenses"]) == 1 and len(data["health_checks"]) == 1
    assert {r["source"] for r in data["odometer_readings"]} == {"manual", "fuel"}
    assert oc.post("/api/me/delete", json={"confirm": "DELETE"}, headers=h).status_code == 200
    with oc.app.state.svc.e.connect() as c:
        for t in (D.fuel_logs, D.expenses, D.expense_files, D.odometer_readings, D.health_scans):
            assert c.execute(sa.select(sa.func.count()).select_from(t)).scalar() == 0, t.name


def test_a_schema_2_database_gets_odometer_history(db_url):
    eng = D.engine(db_url)
    D.init(eng)
    t = D.now()
    with eng.begin() as c:
        c.execute(D.cars.insert().values(id="c1", make="Tata", model="Nexon", fuel="petrol", reg_number="MH12AB1234", odometer_km=24310,
                                         odometer_at=t, created_by="u", created_at=t, updated_at=t))
        c.execute(D.odometer_readings.delete())
        c.execute(D.schema_version.delete().where(D.schema_version.c.version >= 3))
        if not c.execute(sa.select(D.schema_version.c.version)).first():
            c.execute(D.schema_version.insert().values(version=2, applied_at=t))
    assert D.init(eng) == D.SCHEMA_VERSION
    with eng.connect() as c:
        assert [(r.km, r.source) for r in c.execute(sa.select(D.odometer_readings.c.km, D.odometer_readings.c.source))] == [(24310, "manual")]
    eng.dispose()


# ---------------------------------------------------------------------------------------------------------------- packaging
def test_bluetooth_plugin_in_both_apps():
    web = json.loads((ROOT / "owner-app" / "package.json").read_text())
    android = json.loads((ROOT / "android-app" / "package.json").read_text())
    assert web["dependencies"]["@capacitor-community/bluetooth-le"].startswith("^7.") and web["dependencies"]["@capacitor/core"].startswith("^7.")
    assert android["dependencies"]["@capacitor-community/bluetooth-le"].startswith("^7.")
    assert web["scripts"]["test"].startswith("node --test")
    js = (ROOT / "owner-app" / "src" / "lib" / "obd.js").read_text()
    assert "0000fff0-0000-1000-8000-00805f9b34fb" in js and "import('@capacitor-community/bluetooth-le')" in js   # loaded only when used
