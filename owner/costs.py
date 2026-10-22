"""Running costs (Phase C): fuel and charging log, other expenses, odometer history.

Mileage uses the full-to-full method, the one fuel-economy tests use: between two fills to the brim, the distance driven
divided by everything put in after the first of them (including part fills in between). A part fill alone says nothing
about mileage. For electric cars the same works with charges to the car's limit ("full").

Money is stored in paise and quantities in thousandths (ml, g, Wh), so totals are exact; floats appear only in results.

Every odometer reading (typed in, from a fill, from an expense) is kept, so the app knows how much the car is driven:
km per day over the last 120 days, which turns "service at 25,000 km" into "around 14 Nov"."""
from __future__ import annotations

import csv
import io
import statistics
from datetime import date

import sqlalchemy as sa

from owner import db as T
from owner import validators as V
from owner.odometer import km_per_day
from owner.db import now
from owner.service import Conflict, Forbidden, Invalid, NotFound, OwnerService, _iso, _row, _uid

UNITS = {"petrol": ("L",), "diesel": ("L",), "hybrid": ("L",), "cng": ("kg", "L"), "electric": ("kWh",)}
UNIT_NAME = {"L": "litre", "kg": "kg", "kWh": "kWh"}
MAX_QTY = {"L": 150, "kg": 30, "kWh": 200}                      # per fill
PRICE = {"L": (40, 250), "kg": (30, 200), "kWh": (0, 60)}       # plausible rupees per unit in India
CATEGORIES = {"service": "Service", "repair": "Repair", "tyres": "Tyres", "battery": "Battery", "insurance": "Insurance",
              "puc": "PUC", "parking": "Parking", "toll": "Toll", "fastag": "FASTag recharge", "fine": "Challan / fine",
              "wash": "Cleaning", "accessories": "Accessories", "other": "Other"}
MAX_ENTRIES = 2000                                               # per car and kind
DROP = 0.85                                                      # latest mileage below 85% of the usual one: worth a look


def _money(v, name: str = "Amount") -> int:
    try:
        x = round(float(str(v).replace(",", "").replace("₹", "").strip()) * 100)
    except (TypeError, ValueError, OverflowError) as e:                           # OverflowError: "inf"
        raise V.ValidationError(f"{name} must be a number of rupees") from e
    if not 0 <= x <= 50_000_000:                                 # up to 5 lakh
        raise V.ValidationError(f"{name} must be between 0 and 5,00,000 rupees")
    return x


def _milli(v, unit: str) -> int:
    try:
        x = round(float(str(v).replace(",", "").strip()) * 1000)
    except (TypeError, ValueError, OverflowError) as e:
        raise V.ValidationError(f"Quantity must be a number of {UNIT_NAME[unit]}s") from e
    if not 100 <= x <= MAX_QTY[unit] * 1000:
        raise V.ValidationError(f"Quantity must be between 0.1 and {MAX_QTY[unit]} {unit}")
    return x


# ---------------------------------------------------------------------------------------------------------------- calculations
def mileage_series(fills: list[dict]) -> list[dict]:
    """Full-to-full mileage, one point per full fill that has an earlier full fill. fills: rows with odometer_km,
    quantity_milli, amount_paise, full, unit, filled_on (any order; one unit)."""
    fs = sorted(fills, key=lambda f: (f["odometer_km"], f["filled_on"]))
    out, last_full, qty, cost = [], None, 0, 0
    for f in fs:
        if last_full is not None:
            qty += f["quantity_milli"]
            cost += f["amount_paise"]
        if f["full"]:
            if last_full is not None:
                dist = f["odometer_km"] - last_full["odometer_km"]
                if dist > 0 and qty > 0:
                    out.append({"on": _iso(f["filled_on"]), "from_km": last_full["odometer_km"], "to_km": f["odometer_km"], "distance_km": dist,
                                "quantity": qty / 1000, "mileage": round(dist / (qty / 1000), 2), "cost_per_km": round(cost / 100 / dist, 2)})
            last_full, qty, cost = f, 0, 0
    return out


def mileage_check(series: list[dict]) -> dict | None:
    """The latest mileage against the usual one (median of up to 5 before it): a drop worth a look, or None."""
    if len(series) < 4:
        return None
    last, before = series[-1]["mileage"], [p["mileage"] for p in series[-6:-1]]
    usual = statistics.median(before)
    if last < DROP * usual:
        return {"latest": last, "usual": round(usual, 2), "drop_pct": round(100 * (1 - last / usual)), "on": series[-1]["on"]}
    return None


# ---------------------------------------------------------------------------------------------------------------- service
class CostService:
    def __init__(self, owner: OwnerService):
        self.o, self.e = owner, owner.e

    @staticmethod
    def record_odometer(c, car_id: str, km: int, source: str) -> None:
        """Keep the reading and raise the car's odometer if this one is higher (readings never lower it)."""
        t = now()
        c.execute(T.odometer_readings.insert().values(car_id=car_id, km=km, at=t, source=source))
        cur = c.execute(sa.select(T.cars.c.odometer_km).where(T.cars.c.id == car_id)).scalar()
        if cur is None or km > cur:
            c.execute(T.cars.update().where(T.cars.c.id == car_id).values(odometer_km=km, odometer_at=t, updated_at=t))

    # ------------------------------------------------------------------ fuel
    def _fuel_values(self, car: dict, data: dict) -> dict:
        try:
            unit = data.get("unit") or UNITS[car["fuel"]][0]
            if unit not in UNITS[car["fuel"]]:
                raise V.ValidationError(f"A {car['fuel']} car is filled in {', '.join(UNITS[car['fuel']])}")
            on = V.day(data.get("filled_on"), "Date") or date.today()
            odo = V.km(data.get("odometer_km"))
            if odo is None:
                raise V.ValidationError("Enter the odometer reading at this fill: mileage needs it")
            qty, amount = _milli(data.get("quantity"), unit), _money(data.get("amount"))
            lo, hi = PRICE[unit]
            price = amount / 100 / (qty / 1000)
            if not lo <= price <= hi:
                raise V.ValidationError(f"That is ₹{price:.2f} per {unit}; check the quantity and the amount")
            vals = {"filled_on": on, "odometer_km": odo, "quantity_milli": qty, "unit": unit, "amount_paise": amount,
                    "full": data.get("full", True) is not False, "station": V.text(data.get("station", ""), "Station", 0, 80),
                    "notes": V.text(data.get("notes", ""), "Notes", 0, 200)}
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        if on > date.today():
            raise Invalid("The date can't be in the future")
        return vals

    def _fuel_out(self, r: dict) -> dict:
        out = {k: _iso(v) for k, v in r.items()}
        out["quantity"], out["amount"] = r["quantity_milli"] / 1000, r["amount_paise"] / 100
        out["price"] = round(r["amount_paise"] / 100 / (r["quantity_milli"] / 1000), 2)
        return out

    def add_fuel(self, user: dict, car_id: str, data: dict) -> dict:
        car = self.o.car(user, car_id)
        v = self._fuel_values(car, data)
        fid = _uid()
        with self.e.begin() as c:
            rows = c.execute(sa.select(T.fuel_logs.c.filled_on, T.fuel_logs.c.odometer_km).where(T.fuel_logs.c.car_id == car_id)).all()
            if len(rows) >= MAX_ENTRIES:
                raise Conflict(f"At most {MAX_ENTRIES} fills per car")
            for r in rows:                                            # the odometer only goes up as the dates go on
                if (r.filled_on < v["filled_on"] and r.odometer_km > v["odometer_km"]) or (r.filled_on > v["filled_on"] and r.odometer_km < v["odometer_km"]):
                    raise Invalid(f"The odometer doesn't fit the fill of {r.filled_on.day} {r.filled_on:%b %Y} at {r.odometer_km:,} km")
                if r.odometer_km == v["odometer_km"] and r.filled_on == v["filled_on"]:
                    raise Conflict("A fill at this odometer reading on this date is already logged")
            c.execute(T.fuel_logs.insert().values(id=fid, car_id=car_id, created_by=user["id"], created_at=now(), **v))
            self.record_odometer(c, car_id, v["odometer_km"], "fuel")
        return self.fuel_entry(user, fid)

    def fuel_entry(self, user: dict, fid: str) -> dict:
        with self.e.connect() as c:
            r = _row(c.execute(sa.select(T.fuel_logs).where(T.fuel_logs.c.id == fid)).first())
            if not r:
                raise NotFound("Fill not found")
            self.o._membership(c, user["id"], r["car_id"])
        return self._fuel_out(r)

    def fuel(self, user: dict, car_id: str) -> list[dict]:
        with self.e.connect() as c:
            self.o._membership(c, user["id"], car_id)
            rows = c.execute(sa.select(T.fuel_logs).where(T.fuel_logs.c.car_id == car_id)
                             .order_by(T.fuel_logs.c.filled_on.desc(), T.fuel_logs.c.odometer_km.desc()))
            return [self._fuel_out(_row(r)) for r in rows]

    def delete_fuel(self, user: dict, fid: str) -> None:
        self._delete(user, T.fuel_logs, fid, "Fill")

    def _delete(self, user: dict, table, rid: str, what: str) -> None:
        with self.e.begin() as c:
            r = c.execute(sa.select(table.c.car_id, table.c.created_by).where(table.c.id == rid)).first()
            if not r:
                raise NotFound(f"{what} not found")
            role = self.o._membership(c, user["id"], r.car_id)
            if role != "owner" and r.created_by != user["id"]:
                raise Forbidden(f"Only an owner or the person who added it can delete this {what.lower()}")
            c.execute(table.delete().where(table.c.id == rid))

    # ------------------------------------------------------------------ expenses
    def add_expense(self, user: dict, car_id: str, data: dict, content: bytes | None = None, filename: str = "") -> dict:
        try:
            v = {"spent_on": V.day(data.get("spent_on"), "Date") or date.today(), "category": V.choice(data.get("category"), "Category", tuple(CATEGORIES)),
                 "amount_paise": _money(data.get("amount")), "odometer_km": V.km(data.get("odometer_km")),
                 "vendor": V.text(data.get("vendor", ""), "Paid to", 0, 80), "notes": V.text(data.get("notes", ""), "Notes", 0, 300)}
        except V.ValidationError as e:
            raise Invalid(str(e)) from e
        if v["spent_on"] > date.today():
            raise Invalid("The date can't be in the future")
        if v["amount_paise"] == 0:
            raise Invalid("Enter the amount")
        f = self.o._check_file(content, filename)
        eid = _uid()
        with self.e.begin() as c:
            self.o._membership(c, user["id"], car_id)
            n = c.execute(sa.select(sa.func.count()).select_from(T.expenses).where(T.expenses.c.car_id == car_id)).scalar()
            if n >= MAX_ENTRIES:
                raise Conflict(f"At most {MAX_ENTRIES} expenses per car")
            extra = {"receipt_name": f["file_name"], "receipt_type": f["file_type"], "receipt_size": f["file_size"]} if f else {}
            c.execute(T.expenses.insert().values(id=eid, car_id=car_id, created_by=user["id"], created_at=now(), **v, **extra))
            if f:
                c.execute(T.expense_files.insert().values(expense_id=eid, content=content))
            if v["odometer_km"] is not None:
                self.record_odometer(c, car_id, v["odometer_km"], "expense")
        return self.expense(user, eid)

    @staticmethod
    def _exp_out(r: dict) -> dict:
        out = {k: _iso(v) for k, v in r.items()}
        out["amount"], out["category_label"], out["has_receipt"] = r["amount_paise"] / 100, CATEGORIES.get(r["category"], r["category"]), bool(r["receipt_name"])
        return out

    def expense(self, user: dict, eid: str) -> dict:
        with self.e.connect() as c:
            r = _row(c.execute(sa.select(T.expenses).where(T.expenses.c.id == eid)).first())
            if not r:
                raise NotFound("Expense not found")
            self.o._membership(c, user["id"], r["car_id"])
        return self._exp_out(r)

    def expenses(self, user: dict, car_id: str) -> list[dict]:
        with self.e.connect() as c:
            self.o._membership(c, user["id"], car_id)
            rows = c.execute(sa.select(T.expenses).where(T.expenses.c.car_id == car_id).order_by(T.expenses.c.spent_on.desc(), T.expenses.c.created_at.desc()))
            return [self._exp_out(_row(r)) for r in rows]

    def delete_expense(self, user: dict, eid: str) -> None:
        self._delete(user, T.expenses, eid, "Expense")

    def receipt(self, user: dict, eid: str) -> tuple[bytes, str, str]:
        e = self.expense(user, eid)
        with self.e.connect() as c:
            content = c.execute(sa.select(T.expense_files.c.content).where(T.expense_files.c.expense_id == eid)).scalar()
        if content is None:
            raise NotFound("This expense has no receipt")
        return bytes(content), e["receipt_type"], e["receipt_name"]

    # ------------------------------------------------------------------ summary
    def summary(self, user: dict, car_id: str, months: int = 12) -> dict:
        """Spend by month and category, mileage and cost per km, for the car page."""
        self.o.car(user, car_id)  # access check: raises NotFound for someone else's car
        today = date.today()
        with self.e.connect() as c:
            fills = [_row(r) for r in c.execute(sa.select(T.fuel_logs).where(T.fuel_logs.c.car_id == car_id))]
            exps = [_row(r) for r in c.execute(sa.select(T.expenses).where(T.expenses.c.car_id == car_id))]
            readings = [(r.km, r.at) for r in c.execute(sa.select(T.odometer_readings.c.km, T.odometer_readings.c.at)
                                                        .where(T.odometer_readings.c.car_id == car_id))]
        keys = []
        y, m = today.year, today.month
        for _ in range(months):
            keys.append(f"{y}-{m:02d}")
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        keys.reverse()
        by_month = {k: {"month": k, "fuel": 0, "other": 0, "categories": {}} for k in keys}
        for f in fills:
            k = f"{f['filled_on'].year}-{f['filled_on'].month:02d}"
            if k in by_month:
                by_month[k]["fuel"] += f["amount_paise"]
        for x in exps:
            k = f"{x['spent_on'].year}-{x['spent_on'].month:02d}"
            if k in by_month:
                by_month[k]["other"] += x["amount_paise"]
                by_month[k]["categories"][x["category"]] = by_month[k]["categories"].get(x["category"], 0) + x["amount_paise"]
        months_out = [{"month": b["month"], "fuel": b["fuel"] / 100, "other": b["other"] / 100,
                       "categories": {k: v / 100 for k, v in b["categories"].items()}} for b in by_month.values()]
        start = date(int(keys[0][:4]), int(keys[0][5:]), 1)
        period_total = sum(f["amount_paise"] for f in fills if f["filled_on"] >= start) + sum(x["amount_paise"] for x in exps if x["spent_on"] >= start)
        cats: dict[str, int] = {}
        for x in exps:
            if x["spent_on"] >= start:
                cats[x["category"]] = cats.get(x["category"], 0) + x["amount_paise"]
        in_period = [km for km, at in readings if at.date() >= start]
        driven = (max(in_period) - min(in_period)) if len(in_period) >= 2 else None
        units = sorted({f["unit"] for f in fills}, key=lambda u: -sum(1 for f in fills if f["unit"] == u))
        mileage = {}
        for u in units:
            s = mileage_series([f for f in fills if f["unit"] == u])
            mileage[u] = {"unit": u, "label": f"km/{u}", "series": s[-24:], "latest": s[-1]["mileage"] if s else None,
                          "average": round(sum(p["distance_km"] for p in s) / sum(p["quantity"] for p in s), 2) if s else None,
                          "check": mileage_check(s)}
        this_month = by_month[keys[-1]]
        kmpd = km_per_day(readings, today)
        return {"car_id": car_id, "months": months_out,
                "this_month": (this_month["fuel"] + this_month["other"]) / 100, "period_total": period_total / 100, "period_months": months,
                "by_category": sorted(({"category": k, "label": CATEGORIES[k], "amount": v / 100} for k, v in cats.items()), key=lambda x: -x["amount"]),
                "fuel_total": sum(f["amount_paise"] for f in fills if f["filled_on"] >= start) / 100,
                "km_driven": driven, "cost_per_km": round(period_total / 100 / driven, 2) if driven and driven >= 300 else None,
                "km_per_day": kmpd, "mileage": mileage, "fills": len(fills), "expenses": len(exps)}

    def csv(self, user: dict, car_id: str) -> str:
        car = self.o.car(user, car_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "type", "category", "amount_inr", "quantity", "unit", "price_per_unit", "full", "odometer_km", "paid_to", "notes"])
        rows = [(f["filled_on"], "fuel", "fuel" if f["unit"] != "kWh" else "charging", f"{f['amount']:.2f}", f"{f['quantity']:g}", f["unit"],
                 f"{f['price']:.2f}", "yes" if f["full"] else "no", f["odometer_km"], f["station"], f["notes"]) for f in self.fuel(user, car_id)]
        rows += [(x["spent_on"], "expense", x["category"], f"{x['amount']:.2f}", "", "", "", "", x["odometer_km"] or "", x["vendor"], x["notes"])
                 for x in self.expenses(user, car_id)]
        for r in sorted(rows, key=lambda r: r[0]):
            w.writerow([_csv_safe(str(v)) for v in r])
        return f"# {car['make']} {car['model']} {car['reg_display']}\n" + buf.getvalue()

    def export_rows(self, car_ids: list[str]) -> dict:
        if not car_ids:
            return {}
        with self.e.connect() as c:
            out = {}
            for name, t, drop in (("fuel_logs", T.fuel_logs, ()), ("expenses", T.expenses, ()), ("odometer_readings", T.odometer_readings, ())):
                out[name] = [{k: _iso(v) for k, v in _row(r).items() if k not in drop} for r in c.execute(sa.select(t).where(t.c.car_id.in_(car_ids)))]
        return out


def _csv_safe(s: str) -> str:
    """Spreadsheet formula injection: a cell starting with = + - @ is shown as text."""
    return "'" + s if s[:1] in ("=", "+", "-", "@") and not s.lstrip("-").replace(".", "").isdigit() else s


def seed_demo(c, car_id: str, uid: str, fuel: str, odometer: int) -> None:
    """Demo data: about five months of fills (or charges) and expenses, ending a few days ago at a little under the current
    odometer. The petrol car's last full-to-full mileage drops (15.2 -> 12.6 km/l), which the health view points out."""
    from datetime import datetime, time, timedelta
    today = date.today()
    if fuel == "electric":
        mileages, sizes, unit = [7.6, 7.9, 7.4, 7.8, 7.5, 7.7, 7.6], [19.5, 18.0, 20.5, 17.5, 19.0, 21.0, 18.5, 19.5], "kWh"
        price = lambda i: 8.0 if i % 3 else 22.0                                 # noqa: E731 - home, sometimes a fast charger
    else:
        mileages = [15.4, 15.1, 15.6, 14.9, 15.3, 15.0, 15.5, 15.2, 14.8, 15.3, 15.1, 15.4, 12.6]
        sizes, unit = [32.0 + (i % 4) * 1.5 for i in range(len(mileages) + 1)], "L"
        price = lambda i: 103.4 + (i % 5) * 0.35                                 # noqa: E731
    n = len(mileages) + 1
    odo = [odometer - 120]
    for i in range(n - 2, -1, -1):
        odo.insert(0, round(odo[0] - mileages[i] * sizes[i + 1]))
    t = now()
    entries = []
    for i in range(n):
        on = today - timedelta(days=3 + 12 * (n - 1 - i))
        qty = sizes[i]
        if fuel != "electric" and i == n - 5:      # a part fill half way: the next full fill takes less, mileage still counts full to full
            part = sizes[i] / 2
            entries.append((on - timedelta(days=6), round((odo[i - 1] + odo[i]) / 2), part, False, i))
            qty -= part
        entries.append((on, odo[i], qty, True, i))
    for on, o, qty, full, i in entries:
        c.execute(T.fuel_logs.insert().values(
            id=_uid(), car_id=car_id, filled_on=on, odometer_km=o, quantity_milli=round(qty * 1000), unit=unit,
            amount_paise=round(qty * price(i) * 100), full=full, station="Home charger" if fuel == "electric" and price(i) < 10 else "",
            notes="Sample entry (demo)", created_by=uid, created_at=t))
        c.execute(T.odometer_readings.insert().values(car_id=car_id, km=o, at=datetime.combine(on, time(9)), source="fuel"))
    c.execute(T.odometer_readings.insert().values(car_id=car_id, km=odometer, at=t, source="manual"))
    items = ([("service", 685000, 60, odo[n // 2], "Authorised service centre", "Second free service plus oil filter"), ("fastag", 100000, 40, None, "", ""),
              ("fastag", 100000, 12, None, "", ""), ("parking", 30000, 8, None, "Mall parking", ""), ("wash", 40000, 20, None, "", "")]
             if fuel != "electric" else [("service", 245000, 75, odo[2], "Authorised service centre", "Annual check"), ("parking", 12000, 15, None, "", "")])
    for cat, paise, ago, km, vendor, notes in items:
        c.execute(T.expenses.insert().values(id=_uid(), car_id=car_id, spent_on=today - timedelta(days=ago), category=cat, amount_paise=paise,
                                             odometer_km=km, vendor=vendor, notes=notes or "Sample entry (demo)", created_by=uid, created_at=t))
