"""Outbound webhooks: which events are delivered, URL safety checks, signing and sending.

Events are rows of the tamper-evident audit log (change data capture): every state change the platform makes
is already recorded there with actor, record and detail, so integrations need no hooks in the modules and
cannot see anything the audit log does not contain. Receivers can match `audit_hash` against the log.

Security
  * URL check (SSRF): https only; no credentials in the URL; the host must resolve to public addresses unless
    WEBHOOK_ALLOW_PRIVATE=1 (on-premise receivers); WEBHOOK_ALLOW_HOSTS restricts targets to an allow-list.
    The check is repeated right before every send; redirects are not followed.
  * Signature: HMAC-SHA256 over "<timestamp>.<body>" with a per-webhook secret, sent as
    X-AEAI-Signature-256: sha256=<hex> and X-AEAI-Timestamp (receivers reject old timestamps: replay protection).
  * Authentication, user administration and maintenance events are never delivered."""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import ipaddress
import json
import socket
import time
from urllib.parse import urlsplit

DEFAULT_EVENTS = ["*.decision", "*.release", "*.suspect", "adas.campaign.run", "vuln.triage", "vuln.triage.second",
                  "agent.run.*", "integration.reqif.import", "integration.ticket.*"]
NEVER = ("auth.", "audit.", "user.", "member.", "maintenance.", "project.", "integration.webhook.")
BACKOFF_S = [60, 300, 1800, 7200]                       # attempts 2..5; after the 5th failure the delivery is dead
MAX_ATTEMPTS = 5
TIMEOUT_S = 5


class UnsafeURL(ValueError):
    pass


def deliverable(action: str) -> bool:
    return not action.startswith(NEVER)


def matches(action: str, patterns: list[str]) -> bool:
    return deliverable(action) and any(fnmatch.fnmatchcase(action, p) for p in patterns)


def validate_patterns(patterns: list[str]) -> list[str]:
    out = []
    for p in patterns or DEFAULT_EVENTS:
        p = str(p).strip()
        if not p or len(p) > 60 or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789._*-" for ch in p):
            raise ValueError(f"invalid event pattern '{p}' (lower-case letters, digits, '.', '_', '-', '*')")
        out.append(p)
    return list(dict.fromkeys(out))[:30]


def event_body(row: dict) -> dict:
    detail = row.get("detail")
    try:
        data = json.loads(detail) if detail else None
    except (TypeError, ValueError):
        data = detail
    return {"id": f"evt_{row['id']}", "type": row["action"], "project": row["project"], "ref": row["entity"],
            "actor": row["actor"], "occurred_at": row["ts"], "data": data, "audit_hash": row.get("hash")}


def _host_allowed(host: str, allow_hosts: tuple[str, ...]) -> bool:
    return not allow_hosts or any(host == h or host.endswith("." + h) for h in allow_hosts)


def _addresses(host: str) -> list[ipaddress._BaseAddress]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"host '{host}' does not resolve") from e
    return [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]


def check_url(url: str, allow_hosts: tuple[str, ...] = (), allow_private: bool = False, *, resolve: bool = True) -> str:
    u = urlsplit((url or "").strip())
    host = (u.hostname or "").lower()
    if u.scheme not in ("https", "http") or not host:
        raise UnsafeURL("webhook URL must be an absolute https:// URL")
    if u.username or u.password:
        raise UnsafeURL("credentials in the URL are not allowed; use the signing secret")
    if u.scheme == "http" and not allow_private:
        raise UnsafeURL("plain http is only allowed for on-premise receivers (WEBHOOK_ALLOW_PRIVATE=1)")
    if not _host_allowed(host, allow_hosts):
        raise UnsafeURL(f"host '{host}' is not in WEBHOOK_ALLOW_HOSTS")
    if not allow_private:
        try:
            literal = [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            literal = None
        if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal", ".localhost")):
            raise UnsafeURL(f"host '{host}' is internal; blocked to prevent SSRF")
        for a in literal or (_addresses(host) if resolve else []):
            if a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified:
                raise UnsafeURL(f"host '{host}' resolves to a non-public address ({a}); blocked to prevent SSRF")
    if len(url) > 500:
        raise UnsafeURL("URL too long")
    return url.strip()


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, max_age_s: int = 300, now: float | None = None) -> bool:
    """Reference implementation of the receiver side (also used in the tests and the docs)."""
    try:
        if abs((now or time.time()) - int(timestamp)) > max_age_s:
            return False
    except ValueError:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


def headers(event: str, delivery_id: str, timestamp: str, signature: str) -> dict:
    return {"Content-Type": "application/json", "User-Agent": "automotive-eng-ai-webhooks/1.0",
            "X-AEAI-Event": event, "X-AEAI-Delivery": delivery_id, "X-AEAI-Timestamp": timestamp,
            "X-AEAI-Signature-256": signature}


def post(url: str, body: bytes, hdrs: dict) -> tuple[int | None, str]:
    import requests
    try:
        r = requests.post(url, data=body, headers=hdrs, timeout=TIMEOUT_S, allow_redirects=False)
        return r.status_code, "" if 200 <= r.status_code < 300 else f"HTTP {r.status_code}: {r.text[:200]}"
    except requests.RequestException as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"
