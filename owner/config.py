"""Settings of the car-owner service (Phase A), from environment variables.

The owner service is a separate product from the engineering platform: its own users (car owners, not engineers), its own
database and its own deployment. Production needs a persistent PostgreSQL database (OWNER_DATABASE_URL) and a secret key;
without them it runs on SQLite for local development."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def normalize_db_url(url: str) -> str:
    """Neon / Supabase / Render give postgres:// or postgresql:// URLs; SQLAlchemy needs the psycopg 3 driver name."""
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _secret(data_dir: Path) -> str:
    """Development only: a random key kept in the data directory so sessions survive restarts."""
    f = data_dir / ".owner_secret"
    if f.is_file():
        return f.read_text().strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    f.write_text(key)
    os.chmod(f, 0o600)
    return key


@dataclass(frozen=True)
class OwnerSettings:
    app_name: str = "CarSaathi"
    env: str = "development"                        # development | production
    database_url: str = ""
    secret_key: str = ""
    otp_delivery: str = "console"                   # email | console (console prints the code in the server log: development)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_ssl: bool = False                          # True: implicit TLS (port 465); False: STARTTLS
    brevo_api_key: str = ""                         # set: send through Brevo's HTTPS API instead of SMTP (Render's free plan
                                                    # blocks outgoing SMTP ports 25/465/587)
    mail_from: str = ""                             # sender, e.g. "CarSaathi <you@gmail.com>" (SMTP_FROM is used when empty)
    demo_login: bool = True                         # "Try the demo": a private sandbox account with sample data, deleted after demo_ttl_hours
    demo_ttl_hours: int = 24
    max_demo_accounts: int = 300
    session_days: int = 30
    otp_ttl_min: int = 10
    max_upload_mb: int = 5
    cors_origins: tuple = ()
    static_dir: Path = ROOT / "owner-app" / "dist"
    apk_url: str = ""
    github_repo: str = ""                           # owner/repo: the Android APK is the latest GitHub release asset
    # Phase B: help
    llm_base_url: str = ""                          # optional OpenAI-compatible API that may pick a guide when the search is unsure
    llm_api_key: str = ""
    llm_model: str = ""
    field_api_url: str = ""                         # the engineering platform's API (Phase 6 field issues), e.g. http://api:8000
    field_project: str = "bcm-demo"
    field_token: str = ""                           # API token of a platform account with the engineer role (AUTH_MODE=enforced)
    field_live: bool = False                        # False: reports are prepared and shown, never sent (dry run)
    admin_token: str = ""                           # bearer token for the anonymous help statistics (/api/help/stats)
    proxy_hops: int = 0                             # reverse proxies in front (Render: 1): the client address is that many entries
                                                    # from the right of X-Forwarded-For, which the client cannot forge
    # Phase D: notifications and service booking
    public_url: str = ""                            # the site's address for links in emails, e.g. https://carsaathi.onrender.com
    reminder_emails: bool = True                    # the daily run emails reminders (when email is set up and the person allows it)
    cron_token: str = ""                            # bearer token of POST /api/notify/run (the daily run, called by a scheduler)
    vapid_public_key: str = ""                      # Web Push (VAPID) key pair: python -m owner.notify vapid
    vapid_private_key: str = ""
    vapid_subject: str = ""                         # mailto: or https: contact the push services can reach
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./storage")))

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def sender(self) -> str:
        return self.mail_from or self.smtp_from

    @property
    def mail_transport(self) -> str:
        return "brevo" if self.brevo_api_key else "smtp"

    @property
    def email_configured(self) -> bool:
        return bool(self.sender and (self.brevo_api_key or self.smtp_host))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    @property
    def push_enabled(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key)

    @property
    def android_url(self) -> str:
        if self.apk_url:
            return self.apk_url
        if self.github_repo:
            return f"https://github.com/{self.github_repo}/releases/latest/download/carsaathi.apk"
        return ""


def load_dotenv(path: Path | None = None) -> list[str]:
    """Read the owner service's settings (OWNER_*, SMTP_* and BREVO_* only) from .env for local development. Real environment
    variables always win; AEAI_DOTENV=0 turns it off (the tests do)."""
    if os.getenv("AEAI_DOTENV", "1") == "0":
        return []
    path = path or Path(os.getenv("ENV_FILE", ".env"))
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        key = key.strip().removeprefix("export ").strip()
        if not sep or key.startswith("#") or not key.startswith(("OWNER_", "SMTP_", "BREVO_")) or key in os.environ:
            continue
        os.environ[key] = value.split(" #", 1)[0].strip().strip('"').strip("'")
        loaded.append(key)
    return loaded


def _vapid_dev(data_dir: Path) -> tuple[str, str]:
    """Development only: a Web Push key pair kept in the data directory (production sets OWNER_VAPID_* once)."""
    import json
    f = data_dir / ".owner_vapid.json"
    if f.is_file():
        d = json.loads(f.read_text())
        return d["public"], d["private"]
    from owner.webpush import generate_vapid
    pub, priv = generate_vapid()
    data_dir.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"public": pub, "private": priv}))
    os.chmod(f, 0o600)
    return pub, priv


def load() -> OwnerSettings:
    load_dotenv()
    data_dir = Path(os.getenv("DATA_DIR", "./storage"))
    env = os.getenv("OWNER_ENV", "development").strip().lower()
    db = os.getenv("OWNER_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
    db = normalize_db_url(db) if db else f"sqlite:///{(data_dir / 'owner.db').resolve()}"
    secret = os.getenv("OWNER_SECRET_KEY", "").strip() or ("" if env == "production" else _secret(data_dir))
    cors = tuple(o.strip() for o in os.getenv("OWNER_CORS_ORIGINS", "" if env == "production" else "http://localhost:5173").split(",")
                 if o.strip())
    vpub, vpriv = os.getenv("OWNER_VAPID_PUBLIC_KEY", "").strip(), os.getenv("OWNER_VAPID_PRIVATE_KEY", "").strip()
    if not (vpub or vpriv) and env != "production":
        try:
            vpub, vpriv = _vapid_dev(data_dir)
        except (ImportError, OSError, ValueError):                                 # no cryptography package: no Web Push
            vpub, vpriv = "", ""
    return OwnerSettings(
        app_name=os.getenv("OWNER_APP_NAME", "CarSaathi").strip() or "CarSaathi", env=env, database_url=db, secret_key=secret,
        otp_delivery=os.getenv("OWNER_OTP_DELIVERY", "email" if env == "production" else "console").strip().lower(),
        smtp_host=os.getenv("SMTP_HOST", "").strip(), smtp_port=int(os.getenv("SMTP_PORT", "587")), smtp_user=os.getenv("SMTP_USER", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", ""), smtp_from=os.getenv("SMTP_FROM", "").strip(), smtp_ssl=_bool("SMTP_SSL", False),
        brevo_api_key=os.getenv("BREVO_API_KEY", "").strip(), mail_from=os.getenv("OWNER_MAIL_FROM", "").strip(),
        demo_login=_bool("OWNER_DEMO_LOGIN", True), demo_ttl_hours=int(os.getenv("OWNER_DEMO_TTL_HOURS", "24")),
        max_demo_accounts=int(os.getenv("OWNER_MAX_DEMO_ACCOUNTS", "300")), session_days=int(os.getenv("OWNER_SESSION_DAYS", "30")),
        max_upload_mb=int(os.getenv("OWNER_MAX_UPLOAD_MB", "5")), cors_origins=cors,
        static_dir=Path(os.getenv("OWNER_STATIC_DIR", str(ROOT / "owner-app" / "dist"))), apk_url=os.getenv("OWNER_APK_URL", "").strip(),
        github_repo=os.getenv("OWNER_GITHUB_REPO", "").strip(), proxy_hops=int(os.getenv("OWNER_PROXY_HOPS", "0")), data_dir=data_dir,
        llm_base_url=os.getenv("OWNER_LLM_BASE_URL", "").strip(), llm_api_key=os.getenv("OWNER_LLM_API_KEY", "").strip(),
        llm_model=os.getenv("OWNER_LLM_MODEL", "").strip(), field_api_url=os.getenv("OWNER_FIELD_API_URL", "").strip().rstrip("/"),
        field_project=os.getenv("OWNER_FIELD_PROJECT", "bcm-demo").strip() or "bcm-demo", field_token=os.getenv("OWNER_FIELD_TOKEN", "").strip(),
        field_live=_bool("OWNER_FIELD_LIVE", False), admin_token=os.getenv("OWNER_ADMIN_TOKEN", "").strip(),
        # Render sets RENDER_EXTERNAL_URL (https://<name>.onrender.com) on web services; a custom domain needs OWNER_PUBLIC_URL
        public_url=(os.getenv("OWNER_PUBLIC_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()).rstrip("/"), reminder_emails=_bool("OWNER_REMINDER_EMAILS", True),
        cron_token=os.getenv("OWNER_CRON_TOKEN", "").strip(), vapid_public_key=vpub, vapid_private_key=vpriv,
        vapid_subject=os.getenv("OWNER_VAPID_SUBJECT", "").strip())


def check(s: OwnerSettings) -> tuple[list[str], list[str]]:
    """-> (errors, warnings). Errors stop the service from starting."""
    errors, warnings = [], []
    if s.env not in ("development", "production"):
        errors.append("OWNER_ENV must be development or production")
    if len(s.secret_key) < 32:
        errors.append("OWNER_SECRET_KEY must be set to at least 32 random characters")
    if s.otp_delivery not in ("email", "console"):
        errors.append("OWNER_OTP_DELIVERY must be email or console")
    if s.otp_delivery == "email" and not s.email_configured:
        warnings.append("OWNER_OTP_DELIVERY=email but no email service is set (BREVO_API_KEY or SMTP_HOST, and OWNER_MAIL_FROM): "
                        "email sign-in is unavailable (demo login still works)")
    if not 1 <= s.max_upload_mb <= 20:
        errors.append("OWNER_MAX_UPLOAD_MB must be between 1 and 20")
    if not 1 <= s.session_days <= 365 or not 1 <= s.demo_ttl_hours <= 168:
        errors.append("OWNER_SESSION_DAYS must be 1-365 and OWNER_DEMO_TTL_HOURS 1-168")
    if s.env == "production":
        if s.is_sqlite:
            warnings.append("No OWNER_DATABASE_URL: using SQLite on the instance's disk; on Render free the data is lost at every restart. "
                            "Set a PostgreSQL URL (e.g. Neon or Supabase free tier) for real users.")
        if s.otp_delivery == "console":
            warnings.append("OWNER_OTP_DELIVERY=console in production: codes only appear in the server log")
    if not 0 <= s.proxy_hops <= 5:
        errors.append("OWNER_PROXY_HOPS must be between 0 and 5")
    for name, url in (("OWNER_FIELD_API_URL", s.field_api_url), ("OWNER_LLM_BASE_URL", s.llm_base_url)):
        if url and not url.startswith(("https://", "http://")):
            errors.append(f"{name} must start with https:// or http://")
    if s.field_live and not s.field_api_url:
        errors.append("OWNER_FIELD_LIVE=1 needs OWNER_FIELD_API_URL")
    if s.admin_token and len(s.admin_token) < 32:
        errors.append("OWNER_ADMIN_TOKEN must be at least 32 characters")
    if s.llm_enabled:
        warnings.append("Help search may send owners' problem descriptions (emails, phone numbers, plates and VINs removed) to "
                        f"the LLM at {s.llm_base_url} when the search is unsure; mention it in your privacy notice")
    if s.github_repo and "/" not in s.github_repo:
        errors.append("OWNER_GITHUB_REPO must look like owner/repo")
    if s.public_url and not s.public_url.startswith(("https://", "http://")):
        errors.append("OWNER_PUBLIC_URL must start with https:// or http://")
    if s.cron_token and len(s.cron_token) < 32:
        errors.append("OWNER_CRON_TOKEN must be at least 32 characters")
    if bool(s.vapid_public_key) != bool(s.vapid_private_key):
        errors.append("Set both OWNER_VAPID_PUBLIC_KEY and OWNER_VAPID_PRIVATE_KEY (python -m owner.notify vapid), or neither")
    elif s.push_enabled:
        from owner import webpush
        try:
            webpush.check_vapid(s.vapid_public_key, s.vapid_private_key)
        except ValueError as e:
            errors.append(f"OWNER_VAPID keys: {e}")
    if s.vapid_subject and not s.vapid_subject.startswith(("mailto:", "https://")):
        errors.append("OWNER_VAPID_SUBJECT must start with mailto: or https://")
    if s.env == "production":
        if not s.push_enabled:
            warnings.append("No OWNER_VAPID_PUBLIC_KEY/OWNER_VAPID_PRIVATE_KEY: browser push notifications are off (email and the in-app inbox still work)")
        elif not s.vapid_subject:
            warnings.append("OWNER_VAPID_SUBJECT is empty: push services expect a contact (mailto: or https:)")
        if s.reminder_emails and s.email_configured and not s.public_url:
            warnings.append("OWNER_PUBLIC_URL is empty: reminder emails go out without links to the app")
        if not s.cron_token:
            warnings.append("No OWNER_CRON_TOKEN: nothing can start the daily reminder run (POST /api/notify/run); see docs/PHASE_D_NOTIFICATIONS_BOOKING.md")
    return errors, warnings
