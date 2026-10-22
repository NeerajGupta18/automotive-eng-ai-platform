"""Database of the car-owner service: SQLAlchemy Core tables that run on PostgreSQL (production) and SQLite (development,
tests). Every car-owned row cascades from its car and every account row from its user, so deleting an account removes
the person's data (DPDP Act 2023: erasure)."""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import event

SCHEMA_VERSION = 4                                  # 2: Phase B help, roadside number; 3: Phase C running costs, odometer history, health scans;
                                                    # 4: Phase D notifications, workshops and service bookings
md = sa.MetaData()


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)        # naive UTC everywhere (portable)


def now_precise() -> datetime:
    """With microseconds: for records ordered by time that can come in the same second (health checks)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _id(name: str = "id"):
    return sa.Column(name, sa.String(36), primary_key=True)


users = sa.Table(
    "users", md, _id(),
    sa.Column("email", sa.String(254), nullable=False, unique=True),
    sa.Column("name", sa.String(80), nullable=False, server_default=""),
    sa.Column("phone", sa.String(16), nullable=False, server_default=""),
    sa.Column("consent_version", sa.String(20)), sa.Column("consent_at", sa.DateTime),
    sa.Column("is_demo", sa.Boolean, nullable=False, server_default=sa.false()), sa.Column("demo_expires_at", sa.DateTime),
    sa.Column("created_at", sa.DateTime, nullable=False), sa.Column("last_login_at", sa.DateTime))

otps = sa.Table(
    "otps", md, sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("email", sa.String(254), nullable=False, index=True), sa.Column("code_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False), sa.Column("expires_at", sa.DateTime, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"), sa.Column("consumed_at", sa.DateTime),
    sa.Column("ip", sa.String(64), nullable=False, server_default=""))

sessions = sa.Table(
    "sessions", md, sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True), sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("expires_at", sa.DateTime, nullable=False), sa.Column("last_used_at", sa.DateTime), sa.Column("revoked_at", sa.DateTime),
    sa.Column("user_agent", sa.String(200), nullable=False, server_default=""))

cars = sa.Table(
    "cars", md, _id(),
    sa.Column("nickname", sa.String(40), nullable=False, server_default=""), sa.Column("make", sa.String(40), nullable=False),
    sa.Column("model", sa.String(40), nullable=False), sa.Column("variant", sa.String(40), nullable=False, server_default=""),
    sa.Column("fuel", sa.String(10), nullable=False), sa.Column("year", sa.Integer), sa.Column("reg_number", sa.String(16), nullable=False),
    sa.Column("vin", sa.String(17), nullable=False, server_default=""), sa.Column("colour", sa.String(30), nullable=False, server_default=""),
    sa.Column("odometer_km", sa.Integer), sa.Column("odometer_at", sa.DateTime), sa.Column("purchase_date", sa.Date),
    sa.Column("created_by", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.Column("roadside_phone", sa.String(20), nullable=False, server_default=""))                        # schema 2

car_members = sa.Table(
    "car_members", md,
    sa.Column("car_id", sa.String(36), sa.ForeignKey("cars.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True),
    sa.Column("role", sa.String(10), nullable=False), sa.Column("created_at", sa.DateTime, nullable=False))

invitations = sa.Table(
    "invitations", md, _id(),
    sa.Column("car_id", sa.String(36), sa.ForeignKey("cars.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("email", sa.String(254), nullable=False, index=True), sa.Column("role", sa.String(10), nullable=False),
    sa.Column("invited_by", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("status", sa.String(10), nullable=False), sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("responded_at", sa.DateTime))

documents = sa.Table(
    "documents", md, _id(),
    sa.Column("car_id", sa.String(36), sa.ForeignKey("cars.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("kind", sa.String(12), nullable=False), sa.Column("title", sa.String(80), nullable=False),
    sa.Column("number", sa.String(40), nullable=False, server_default=""), sa.Column("issuer", sa.String(60), nullable=False, server_default=""),
    sa.Column("issued_on", sa.Date), sa.Column("expires_on", sa.Date), sa.Column("notes", sa.String(500), nullable=False, server_default=""),
    sa.Column("file_name", sa.String(120)), sa.Column("file_type", sa.String(40)), sa.Column("file_size", sa.Integer),
    sa.Column("created_by", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("updated_at", sa.DateTime, nullable=False))

document_files = sa.Table(
    "document_files", md,
    sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("content", sa.LargeBinary, nullable=False))

reminders = sa.Table(
    "reminders", md, _id(),
    sa.Column("car_id", sa.String(36), sa.ForeignKey("cars.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("title", sa.String(80), nullable=False), sa.Column("due_on", sa.Date), sa.Column("due_km", sa.Integer),
    sa.Column("repeat", sa.String(10), nullable=False, server_default="none"), sa.Column("every_km", sa.Integer), sa.Column("notes", sa.String(300), nullable=False, server_default=""),
    sa.Column("done_at", sa.DateTime), sa.Column("created_by", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime, nullable=False))

events = sa.Table(
    "events", md, sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), index=True),
    sa.Column("kind", sa.String(30), nullable=False), sa.Column("at", sa.DateTime, nullable=False),
    sa.Column("detail", sa.String(300), nullable=False, server_default=""))

# ---- Phase B: help
help_cases = sa.Table(                                                         # "Get help" requests: the owner's own record
    "help_cases", md, _id(),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("car_id", sa.String(36), sa.ForeignKey("cars.id", ondelete="SET NULL"), index=True),
    sa.Column("car_label", sa.String(80), nullable=False, server_default=""),
    sa.Column("guide_id", sa.String(40)), sa.Column("title", sa.String(120), nullable=False),
    sa.Column("description", sa.String(2000), nullable=False, server_default=""),
    sa.Column("transcript", sa.Text, nullable=False, server_default="[]"), sa.Column("facts", sa.Text, nullable=False, server_default="{}"),
    sa.Column("status", sa.String(10), nullable=False), sa.Column("happened_on", sa.Date),
    sa.Column("created_at", sa.DateTime, nullable=False), sa.Column("updated_at", sa.DateTime, nullable=False), sa.Column("resolved_at", sa.DateTime))

case_photos = sa.Table(
    "case_photos", md, _id(),
    sa.Column("case_id", sa.String(36), sa.ForeignKey("help_cases.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("file_name", sa.String(120), nullable=False), sa.Column("file_type", sa.String(40), nullable=False),
    sa.Column("file_size", sa.Integer, nullable=False), sa.Column("content", sa.LargeBinary, nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False))

help_outcomes = sa.Table(                                                      # anonymous: which guide helped, at which step
    "help_outcomes", md, sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("guide_id", sa.String(40), nullable=False, index=True), sa.Column("outcome", sa.String(10), nullable=False),
    sa.Column("step_id", sa.String(20)), sa.Column("day", sa.Date, nullable=False))

field_reports = sa.Table(                                                      # consented, anonymised reports to the engineering platform
    "field_reports", md, _id(),
    sa.Column("case_id", sa.String(36), sa.ForeignKey("help_cases.id", ondelete="CASCADE"), nullable=False, unique=True),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("status", sa.String(16), nullable=False), sa.Column("payload", sa.Text, nullable=False),     # not_connected / dry_run / pending / sent / failed
    sa.Column("include_vin", sa.Boolean, nullable=False, server_default=sa.false()), sa.Column("consent_at", sa.DateTime, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"), sa.Column("last_error", sa.String(200), nullable=False, server_default=""),
    sa.Column("created_at", sa.DateTime, nullable=False), sa.Column("sent_at", sa.DateTime), sa.Column("remote_ref", sa.String(40)))

# ---- Phase C: running costs and car health. Money in paise and quantities in thousandths (ml, g, Wh): exact integers.
fuel_logs = sa.Table(                                                          # fills and charges
    "fuel_logs", md, _id(),
    sa.Column("car_id", sa.String(36), sa.ForeignKey("cars.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("filled_on", sa.Date, nullable=False), sa.Column("odometer_km", sa.Integer, nullable=False),
    sa.Column("quantity_milli", sa.Integer, nullable=False), sa.Column("unit", sa.String(4), nullable=False),       # L, kg, kWh
    sa.Column("amount_paise", sa.Integer, nullable=False), sa.Column("full", sa.Boolean, nullable=False),
    sa.Column("station", sa.String(80), nullable=False, server_default=""), sa.Column("notes", sa.String(200), nullable=False, server_default=""),
    sa.Column("created_by", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime, nullable=False))

expenses = sa.Table(
    "expenses", md, _id(),
    sa.Column("car_id", sa.String(36), sa.ForeignKey("cars.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("spent_on", sa.Date, nullable=False), sa.Column("category", sa.String(16), nullable=False),
    sa.Column("amount_paise", sa.Integer, nullable=False), sa.Column("odometer_km", sa.Integer),
    sa.Column("vendor", sa.String(80), nullable=False, server_default=""), sa.Column("notes", sa.String(300), nullable=False, server_default=""),
    sa.Column("receipt_name", sa.String(120)), sa.Column("receipt_type", sa.String(40)), sa.Column("receipt_size", sa.Integer),
    sa.Column("created_by", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime, nullable=False))

expense_files = sa.Table(
    "expense_files", md,
    sa.Column("expense_id", sa.String(36), sa.ForeignKey("expenses.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("content", sa.LargeBinary, nullable=False))

odometer_readings = sa.Table(                                                  # every reading, for km per day and predicted due dates
    "odometer_readings", md, sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("car_id", sa.String(36), sa.ForeignKey("cars.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("km", sa.Integer, nullable=False), sa.Column("at", sa.DateTime, nullable=False),
    sa.Column("source", sa.String(10), nullable=False))                                                         # manual, fuel, expense

health_scans = sa.Table(                                                       # OBD-II readings (adapter, demo adapter) or codes typed in
    "health_scans", md, _id(),
    sa.Column("car_id", sa.String(36), sa.ForeignKey("cars.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("source", sa.String(10), nullable=False), sa.Column("scanned_at", sa.DateTime, nullable=False),
    sa.Column("mil_on", sa.Boolean), sa.Column("codes", sa.Text, nullable=False, server_default="[]"),
    sa.Column("battery_mv", sa.Integer), sa.Column("engine_running", sa.Boolean), sa.Column("coolant_c", sa.Integer),
    sa.Column("rpm", sa.Integer), sa.Column("km_since_clear", sa.Integer), sa.Column("km_with_mil", sa.Integer),
    sa.Column("adapter", sa.String(60), nullable=False, server_default=""), sa.Column("protocol", sa.String(60), nullable=False, server_default=""),
    sa.Column("raw", sa.Text), sa.Column("created_by", sa.String(36), nullable=False))

# ---- Phase D: notifications and service booking
notification_prefs = sa.Table(                                                 # one row per person; no row = the defaults
    "notification_prefs", md,
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("email", sa.Boolean, nullable=False, server_default=sa.true()),           # reminder and booking emails
    sa.Column("push", sa.Boolean, nullable=False, server_default=sa.true()),            # on the devices where it was turned on
    sa.Column("kinds", sa.String(120), nullable=False, server_default="reminders,documents,health,bookings"),
    sa.Column("updated_at", sa.DateTime, nullable=False))

push_subscriptions = sa.Table(                                                 # one per browser that turned push on
    "push_subscriptions", md, _id(),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("endpoint", sa.String(500), nullable=False, unique=True), sa.Column("p256dh", sa.String(100), nullable=False),
    sa.Column("auth", sa.String(40), nullable=False), sa.Column("label", sa.String(60), nullable=False, server_default=""),
    sa.Column("created_at", sa.DateTime, nullable=False), sa.Column("last_ok_at", sa.DateTime),
    sa.Column("failures", sa.Integer, nullable=False, server_default="0"))

notifications = sa.Table(                                                      # the in-app inbox; the key makes each one happen once
    "notifications", md, _id(),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("car_id", sa.String(36), sa.ForeignKey("cars.id", ondelete="CASCADE"), index=True),
    sa.Column("kind", sa.String(12), nullable=False), sa.Column("key", sa.String(160), nullable=False),
    sa.Column("title", sa.String(160), nullable=False), sa.Column("body", sa.String(400), nullable=False, server_default=""),
    sa.Column("path", sa.String(160), nullable=False, server_default="/"), sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("read_at", sa.DateTime),
    sa.Column("email_status", sa.String(10), nullable=False, server_default=""),     # "", sent, off, failed
    sa.Column("push_status", sa.String(10), nullable=False, server_default=""),      # "", sent, off, failed
    sa.UniqueConstraint("user_id", "key", name="uq_notifications_user_key"))

workshops = sa.Table(                                                          # the person's own list of garages and service centres
    "workshops", md, _id(),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("name", sa.String(80), nullable=False), sa.Column("phone", sa.String(20), nullable=False, server_default=""),
    sa.Column("address", sa.String(200), nullable=False, server_default=""), sa.Column("created_at", sa.DateTime, nullable=False))

bookings = sa.Table(
    "bookings", md, _id(),
    sa.Column("car_id", sa.String(36), sa.ForeignKey("cars.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("created_by", sa.String(36), nullable=False),
    sa.Column("workshop_id", sa.String(36), sa.ForeignKey("workshops.id", ondelete="SET NULL")),
    sa.Column("workshop_name", sa.String(80), nullable=False), sa.Column("workshop_phone", sa.String(20), nullable=False, server_default=""),
    sa.Column("workshop_address", sa.String(200), nullable=False, server_default=""),
    sa.Column("contact_name", sa.String(60), nullable=False), sa.Column("contact_phone", sa.String(16), nullable=False),
    sa.Column("status", sa.String(12), nullable=False),          # requested, proposed, confirmed, in_service, ready, completed, cancelled, declined
    sa.Column("jobs", sa.Text, nullable=False), sa.Column("slots", sa.Text, nullable=False),
    sa.Column("pickup", sa.Boolean, nullable=False, server_default=sa.false()), sa.Column("odometer_km", sa.Integer),
    sa.Column("notes", sa.String(500), nullable=False, server_default=""),
    sa.Column("appointment_on", sa.Date), sa.Column("appointment_time", sa.String(5)),            # local time (India)
    sa.Column("proposed_on", sa.Date), sa.Column("proposed_time", sa.String(5)),
    sa.Column("estimate_paise", sa.Integer), sa.Column("bill_paise", sa.Integer),
    sa.Column("workshop_note", sa.String(500), nullable=False, server_default=""),
    sa.Column("expense_id", sa.String(36)),
    sa.Column("token_hash", sa.String(64), unique=True), sa.Column("token_expires_at", sa.DateTime),
    sa.Column("created_at", sa.DateTime, nullable=False), sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.Column("completed_at", sa.DateTime))

booking_events = sa.Table(                                                     # the booking's timeline
    "booking_events", md, sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("at", sa.DateTime, nullable=False), sa.Column("actor", sa.String(10), nullable=False),   # owner, workshop, system
    sa.Column("kind", sa.String(16), nullable=False), sa.Column("detail", sa.String(300), nullable=False, server_default=""))

schema_version = sa.Table("schema_version", md, sa.Column("version", sa.Integer, primary_key=True),
                          sa.Column("applied_at", sa.DateTime, nullable=False))


def engine(url: str) -> sa.Engine:
    if url.startswith("sqlite"):
        eng = sa.create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(eng, "connect")
        def _fk(dbapi_conn, _):                                                  # SQLite enforces cascades only when asked
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
        return eng
    return sa.create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=300)


def _m2(c) -> None:
    """Schema 2 (Phase B): the roadside assistance number on the car. New tables come from create_all."""
    if "roadside_phone" not in {col["name"] for col in sa.inspect(c).get_columns("cars")}:
        c.execute(sa.text("ALTER TABLE cars ADD COLUMN roadside_phone VARCHAR(20) NOT NULL DEFAULT ''"))


def _m3(c) -> None:
    """Schema 3 (Phase C): new tables come from create_all; start each car's odometer history with its current reading."""
    c.execute(sa.text("INSERT INTO odometer_readings (car_id, km, at, source) SELECT id, odometer_km, COALESCE(odometer_at, updated_at), 'manual' "
                      "FROM cars WHERE odometer_km IS NOT NULL"))


def _m4(c) -> None:
    """Schema 4 (Phase D): only new tables (notifications, push subscriptions, workshops, bookings), made by create_all;
    nothing to change in existing rows. Everyone starts with the default notification settings (no row needed)."""


MIGRATIONS = {2: _m2, 3: _m3, 4: _m4}


def init(eng: sa.Engine) -> int:
    """Create missing tables, then bring an older database up to SCHEMA_VERSION with the numbered migrations (each runs
    once, in its own transaction, and is recorded). A new database is created at the current version."""
    if eng.url.get_backend_name() == "sqlite" and eng.url.database:
        from pathlib import Path
        Path(eng.url.database).parent.mkdir(parents=True, exist_ok=True)
    md.create_all(eng)
    with eng.begin() as c:
        have = {r.version for r in c.execute(sa.select(schema_version.c.version))}
        if not have:
            c.execute(schema_version.insert().values(version=SCHEMA_VERSION, applied_at=now()))
            return SCHEMA_VERSION
    for v in range(max(have) + 1, SCHEMA_VERSION + 1):
        with eng.begin() as c:
            MIGRATIONS[v](c)
            c.execute(schema_version.insert().values(version=v, applied_at=now()))
    return SCHEMA_VERSION
