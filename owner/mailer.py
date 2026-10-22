"""Sending email: the sign-in code, and (Phase D) reminder digests and booking updates.

  brevo    Brevo's transactional email API over HTTPS (free plan: 300 emails a day). Use it on Render's free plan, which
           blocks outgoing SMTP ports; the sender address must be verified in Brevo.
  smtp     any SMTP server (a Gmail account with an app password, Brevo's SMTP relay, a company server).
  console  prints the code in the server log: local development only."""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr

BREVO_URL = "https://api.brevo.com/v3/smtp/email"

log = logging.getLogger("owner.mail")


class MailError(RuntimeError):
    pass


def _content(settings, code: str) -> tuple[str, str, str]:
    subject = f"{code} is your {settings.app_name} sign-in code"
    text = (f"Your {settings.app_name} sign-in code is {code}.\n\nIt is valid for {settings.otp_ttl_min} minutes. "
            f"If you did not try to sign in, ignore this email; nobody can sign in without the code.\n")
    html = (f"<p>Your {settings.app_name} sign-in code is</p><p style='font-size:28px;font-weight:700;letter-spacing:6px'>{code}</p>"
            f"<p>It is valid for {settings.otp_ttl_min} minutes. If you did not try to sign in, ignore this email.</p>")
    return subject, text, html


def send_code(settings, to: str, code: str) -> str:
    """-> 'email' or 'console'. Raises MailError when the email cannot be sent."""
    if settings.otp_delivery == "console":
        log.warning("Sign-in code for %s: %s (console delivery: development only)", to, code)
        return "console"
    if not settings.email_configured:
        raise MailError("Email sign-in is not configured on this server")
    _send(settings, to, *_content(settings, code), tag="sign-in", what="sign-in email")
    return "email"


def send_email(settings, to: str, subject: str, text: str, html: str, headers: dict | None = None, tag: str = "notification") -> str:
    """A reminder digest or booking update -> 'email', or 'console' when no email service is set (development: logged).
    headers: e.g. List-Unsubscribe and List-Unsubscribe-Post (one-click unsubscribe, RFC 8058). Raises MailError."""
    if not settings.email_configured:
        log.info("Email to %s not sent (no email service set): %s", to, subject)
        return "console"
    _send(settings, to, subject, text, html, headers or {}, tag=tag, what="email")
    return "email"


def _send(settings, to, subject, text, html, headers=None, *, tag: str, what: str) -> None:
    if settings.mail_transport == "brevo":
        _send_brevo(settings, to, subject, text, html, headers, tag, what)
    else:
        _send_smtp(settings, to, subject, text, html, headers, what)


def _send_brevo(settings, to: str, subject: str, text: str, html: str, headers: dict | None = None, tag: str = "sign-in",
                what: str = "sign-in email") -> None:
    name, addr = parseaddr(settings.sender)
    body = {"sender": {"email": addr, **({"name": name} if name else {})}, "to": [{"email": to}], "subject": subject,
            "textContent": text, "htmlContent": html, "tags": [tag]}
    if headers:
        body["headers"] = headers
    req = urllib.request.Request(BREVO_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"api-key": settings.brevo_api_key, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:                      # noqa: S310 - fixed https URL
            if not 200 <= r.status < 300:
                raise MailError(f"The {what} could not be sent. Try again in a minute.")
    except urllib.error.HTTPError as e:
        log.error("Brevo refused the %s: HTTP %s (check BREVO_API_KEY and that the sender is verified)", what, e.code)
        raise MailError(f"The {what} could not be sent. Try again in a minute.") from e
    except (OSError, ValueError) as e:
        log.error("Could not reach Brevo: %s", type(e).__name__)
        raise MailError(f"The {what} could not be sent. Try again in a minute.") from e


def _send_smtp(settings, to: str, subject: str, text: str, html: str, headers: dict | None = None, what: str = "sign-in email") -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.sender
    msg["To"] = to
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    try:
        ctx = ssl.create_default_context()
        if settings.smtp_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx, timeout=15) as s:
                _login_send(s, settings, msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
                s.starttls(context=ctx)
                _login_send(s, settings, msg)
    except (OSError, smtplib.SMTPException) as e:
        log.error("Could not send the %s: %s", what, type(e).__name__)
        raise MailError(f"The {what} could not be sent. Try again in a minute.") from e


def _login_send(s, settings, msg) -> None:
    if settings.smtp_user:
        s.login(settings.smtp_user, settings.smtp_password)
    s.send_message(msg)
