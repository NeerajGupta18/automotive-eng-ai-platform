"""Credentials of the owner service.

  sign-in codes  6 digits, valid 10 minutes, at most 5 tries, stored only as HMAC-SHA256(secret, email:code)
  sessions       opaque random bearer tokens ("cs_..."), stored only as SHA-256, revocable (sign out, sign out everywhere,
                 account deletion), sliding last-use time, fixed expiry
  rate limits    in-memory sliding windows per IP and per e-mail for sign-in, per IP for the whole API"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def code_hash(secret: str, email: str, code: str) -> str:
    return hmac.new(secret.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def new_token() -> str:
    return "cs_" + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    """Sliding window: allow(key, limit, seconds) -> (allowed, retry_after_s)."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, seconds: float) -> tuple[bool, int]:
        t = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= t - seconds:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(q[0] + seconds - t) + 1)
            q.append(t)
            if len(self._hits) > 50_000:                                      # bounded memory under abuse
                for k in [k for k, v in self._hits.items() if not v][:10_000]:
                    del self._hits[k]
            return True, 0
