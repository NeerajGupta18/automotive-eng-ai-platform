"""How much a car is driven, from its odometer history (Phase C): km per day, and when a km-based reminder will fall due."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import sqlalchemy as sa

from owner import db as T


def km_per_day(readings: list[tuple[int, datetime]], today: date) -> float | None:
    """Average km per day over the last 120 days (readings at least 7 days and 50 km apart), or None."""
    recent = sorted(((km, at) for km, at in readings if (today - at.date()).days <= 120), key=lambda r: r[1])
    if len(recent) < 2:
        return None
    (k0, t0), (k1, t1) = recent[0], recent[-1]
    days = (t1 - t0).total_seconds() / 86400
    if days < 7 or k1 - k0 < 50:
        return None
    return round((k1 - k0) / days, 1)


def predicted_on(due_km: int | None, odometer: int | None, kmpd: float | None, today: date) -> date | None:
    if due_km is None or odometer is None or not kmpd:
        return None
    left = due_km - odometer
    return today if left <= 0 else today + timedelta(days=round(left / kmpd))


def car_km_per_day(c, car_id: str, today: date | None = None) -> float | None:
    rows = c.execute(sa.select(T.odometer_readings.c.km, T.odometer_readings.c.at).where(T.odometer_readings.c.car_id == car_id)).all()
    return km_per_day([(r.km, r.at) for r in rows], today or date.today())
