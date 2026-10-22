"""Validation of what car owners type in. Every value is normalised or rejected with a message a non-engineer understands."""
from __future__ import annotations

import re
from datetime import date

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(\.[A-Za-z0-9\-]{1,63})+$")
# Indian registration: state code, district (1-2 digits), series (0-3 letters), number (1-4 digits), e.g. MH12AB1234, DL3CAB0001
REG_RE = re.compile(r"^([A-Z]{2})(\d{1,2})([A-Z]{0,3})(\d{1,4})$")
BH_RE = re.compile(r"^(\d{2})(BH)(\d{4})([A-Z]{1,2})$")                     # Bharat series, e.g. 22BH1234AA
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")                              # no I, O, Q
PHONE_RE = re.compile(r"^[6-9]\d{9}$")
FUELS = ("petrol", "diesel", "cng", "electric", "hybrid")
DOC_KINDS = {"rc": "Registration certificate (RC)", "insurance": "Insurance", "puc": "PUC certificate", "warranty": "Warranty",
             "service": "Service record", "fastag": "FASTag", "other": "Other"}
REPEATS = ("none", "monthly", "quarterly", "yearly")
ROLES = ("owner", "driver")


class ValidationError(ValueError):
    pass


def text(v, name: str, lo: int, hi: int) -> str:
    s = " ".join(str(v if v is not None else "").split())
    if not lo <= len(s) <= hi:
        raise ValidationError(f"{name}: {lo}-{hi} characters" if lo else f"{name}: at most {hi} characters")
    if any(ord(ch) < 32 for ch in s):
        raise ValidationError(f"{name} contains control characters")
    return s


def email(v) -> str:
    s = str(v or "").strip().lower()
    if len(s) > 254 or not EMAIL_RE.match(s):
        raise ValidationError("Enter a valid email address")
    return s


def phone(v) -> str:
    s = re.sub(r"[\s\-()]", "", str(v or ""))
    if not s:
        return ""
    if s.startswith("+91"):
        s = s[3:]
    elif s.startswith("0") and len(s) == 11:
        s = s[1:]
    if not PHONE_RE.match(s):
        raise ValidationError("Enter a 10-digit Indian mobile number")
    return "+91" + s


def helpline(v) -> str:
    """A roadside assistance number as printed on the insurance or RSA card: toll-free (1800 ...), landline or mobile."""
    s = " ".join(str(v or "").split())
    if not s:
        return ""
    digits = re.sub(r"[\s\-()]", "", s)
    if len(s) > 20 or not re.fullmatch(r"\+?\d{3,15}", digits):
        raise ValidationError("Enter the roadside assistance number with digits only, e.g. 1800 209 1234")
    return s


def reg_number(v) -> str:
    s = re.sub(r"[\s\-.]", "", str(v or "")).upper()
    if not (REG_RE.match(s) or BH_RE.match(s)):
        raise ValidationError("Enter the registration number as on the number plate, e.g. MH 12 AB 1234 or 22 BH 1234 AA")
    return s


def reg_display(s: str) -> str:
    m = BH_RE.match(s) or REG_RE.match(s)
    return " ".join(p for p in m.groups() if p) if m else s


def vin(v) -> str:
    s = re.sub(r"\s", "", str(v or "")).upper()
    if not s:
        return ""
    if not VIN_RE.match(s):
        raise ValidationError("The VIN (chassis number) has 17 letters and digits, without I, O or Q")
    return s


def year(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        y = int(v)
    except (TypeError, ValueError) as e:
        raise ValidationError("Year must be a number") from e
    if not 1980 <= y <= date.today().year + 1:
        raise ValidationError(f"Year must be between 1980 and {date.today().year + 1}")
    return y


def km(v, name: str = "Odometer") -> int | None:
    if v in (None, ""):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError) as e:
        raise ValidationError(f"{name} must be a whole number of km") from e
    if not 0 <= n <= 2_000_000:
        raise ValidationError(f"{name} must be between 0 and 2,000,000 km")
    return n


def day(v, name: str) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    try:
        d = date.fromisoformat(str(v)[:10])
    except ValueError as e:
        raise ValidationError(f"{name}: use a date like 2026-12-31") from e
    if not 1980 <= d.year <= 2100:
        raise ValidationError(f"{name}: the year looks wrong")
    return d


def choice(v, name: str, allowed) -> str:
    s = str(v or "").strip().lower()
    if s not in allowed:
        raise ValidationError(f"{name} must be one of: {', '.join(allowed)}")
    return s


# file uploads: the type is decided from the content, never from the name or the browser's claim
SIGNATURES = ((b"%PDF-", "application/pdf", ".pdf"), (b"\xff\xd8\xff", "image/jpeg", ".jpg"), (b"\x89PNG\r\n\x1a\n", "image/png", ".png"))


def file_type(content: bytes) -> tuple[str, str]:
    for sig, mime, ext in SIGNATURES:
        if content.startswith(sig):
            return mime, ext
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValidationError("Upload a photo (JPEG, PNG, WebP) or a PDF")


def file_name(name: str, ext: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "document").rsplit("/", 1)[-1].rsplit("\\", 1)[-1])[:80].strip("._") or "document"
    base = (base.rsplit(".", 1)[0] if "." in base else base).strip("._-") or "document"
    return base + ext
