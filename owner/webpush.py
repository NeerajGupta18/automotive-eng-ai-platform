"""Web Push to browsers (Chrome, Edge, Firefox, Safari 16.4+ for an app added to the home screen), without a vendor SDK.

    RFC 8291  message encryption: ECDH on P-256 with the browser's key, HKDF-SHA256, AES-128-GCM
    RFC 8188  the aes128gcm content coding (one record)
    RFC 8292  VAPID: the server signs a short-lived ES256 token for each push service, so the service knows who sends

The browser gives the app an endpoint URL (on its vendor's push service) and two keys; the server encrypts the message for
that browser only and POSTs it to the endpoint. The push service never sees the content.

The endpoint comes from the client, so it is checked before the server calls it: HTTPS, and a host of a known push
service only. Anything else (an internal address, a random site) is refused, so subscriptions can't be used to make the
server send requests elsewhere (SSRF)."""
from __future__ import annotations

import base64
import json
import os
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com")    # Chrome/Edge(Android), Firefox, Safari
PUSH_SUFFIXES = (".notify.windows.com", ".push.apple.com")                                      # Edge on Windows, Apple regions
RECORD_SIZE = 4096
MAX_PAYLOAD = 3000                       # the push services accept 4 KB bodies; 86 bytes of header and 17 of tag/padding leave room


class PushError(ValueError):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    s = (s or "").strip()
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError) as e:
        raise PushError("Not base64url") from e


# ---------------------------------------------------------------------------------------------------------------- keys
def generate_vapid() -> tuple[str, str]:
    """-> (public key: 65-byte uncompressed P-256 point, private key: 32-byte scalar), both base64url. The public one is
    the applicationServerKey the browser subscribes with."""
    k = ec.generate_private_key(ec.SECP256R1())
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64u(pub), b64u(k.private_numbers().private_value.to_bytes(32, "big"))


def _private(priv_b64: str) -> ec.EllipticCurvePrivateKey:
    raw = unb64u(priv_b64)
    if len(raw) != 32:
        raise PushError("the private key must be 32 bytes (base64url)")
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def _public(pub_b64: str) -> ec.EllipticCurvePublicKey:
    raw = unb64u(pub_b64)
    if len(raw) != 65 or raw[0] != 4:
        raise PushError("a public key must be a 65-byte uncompressed P-256 point (base64url)")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError as e:
        raise PushError("not a point on P-256") from e


def check_vapid(pub_b64: str, priv_b64: str) -> None:
    """Both keys valid and of the same pair (a mismatch would make every push fail with 403)."""
    derived = _private(priv_b64).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    if derived != unb64u(pub_b64):
        raise PushError("the public key does not belong to the private key")


def check_subscription(endpoint: str, p256dh: str, auth: str) -> None:
    """A browser's subscription: an HTTPS endpoint on a known push service, its P-256 key and a 16-byte auth secret."""
    if not isinstance(endpoint, str) or len(endpoint) > 500:
        raise PushError("The push endpoint is missing or too long")
    u = urlsplit(endpoint)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.port not in (None, 443) or u.username or u.password:
        raise PushError("The push endpoint must be an https:// address")
    if not (host in PUSH_HOSTS or host.endswith(PUSH_SUFFIXES)):
        raise PushError("The push endpoint is not on a known push service")
    _public(p256dh)
    if len(unb64u(auth)) != 16:
        raise PushError("The push auth secret must be 16 bytes")


# ---------------------------------------------------------------------------------------------------------------- RFC 8291
def _hmac(key: bytes, data: bytes) -> bytes:
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """The aes128gcm body for one browser. salt and server_key are random per message (parameters only for test vectors)."""
    if len(payload) > MAX_PAYLOAD:
        raise PushError("The message is too long for a push notification")
    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    ua_key = _public(p256dh)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = as_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ecdh = as_key.exchange(ec.ECDH(), ua_key)
    # HKDF(auth_secret, ecdh, "WebPush: info" || 0 || ua_public || as_public, 32): extract, then one expand block
    prk_key = _hmac(auth_secret, ecdh)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    salt = salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)          # 0x02: the last (only) record, no padding
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public + ciphertext


# ---------------------------------------------------------------------------------------------------------------- RFC 8292
def vapid_token(endpoint: str, subject: str, priv_b64: str, now: float | None = None, ttl_s: int = 12 * 3600) -> str:
    """ES256 JWT for the push service of this endpoint: aud = its origin, exp at most 24 h ahead, sub = how to reach us."""
    u = urlsplit(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int((now or time.time()) + ttl_s), "sub": subject or "mailto:admin@localhost"}
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    der = _private(priv_b64).sign(f"{header}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return f"{header}.{body}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def request(sub: dict, message: dict, *, subject: str, pub_b64: str, priv_b64: str, ttl: int = 86400,
            urgency: str = "normal", topic: str | None = None) -> tuple[str, dict, bytes]:
    """-> (url, headers, body) of the POST to the push service. topic: a newer message with the same topic replaces one
    still waiting on the service (e.g. today's reminders replace yesterday's undelivered ones)."""
    check_subscription(sub["endpoint"], sub["p256dh"], sub["auth"])
    body = encrypt(json.dumps(message, separators=(",", ":")).encode(), sub["p256dh"], sub["auth"])
    headers = {"TTL": str(ttl), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
               "Urgency": urgency, "Authorization": f"vapid t={vapid_token(sub['endpoint'], subject, priv_b64)}, k={pub_b64}"}
    if topic:
        headers["Topic"] = "".join(ch for ch in topic if ch.isalnum() or ch in "-_")[:32]
    return sub["endpoint"], headers, body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):                                # a push service never redirects: don't follow
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def post(url: str, headers: dict, body: bytes, timeout: float = 10) -> int:
    """POST to the push service -> HTTP status (201 accepted; 404/410: the subscription is gone; 0: not reachable)."""
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:                            # host checked against PUSH_HOSTS
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (OSError, ValueError):
        return 0
