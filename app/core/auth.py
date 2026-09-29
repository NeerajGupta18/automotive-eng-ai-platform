"""Authentication (API tokens) and role-based access control.

Model
  * a user has a GLOBAL role: 'admin' (platform administrator) or 'user'
  * a user has a PROJECT role per project: viewer < engineer < reviewer < admin
  * permissions are ranked: read < write < approve < admin; a project role grants everything up to its rank
Tokens are 256-bit random values shown once; only their SHA-256 is stored (a slow KDF adds nothing for
high-entropy random tokens). Identity is bound to the token, so the four-eyes rule in the workflow layer is
only as strong as this layer: with AUTH_MODE=enforced the X-User header is ignored."""
from __future__ import annotations

import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from app.core.db import Database, now
from app.core.errors import Conflict, Invalid, NotFound

ROLES = ("viewer", "engineer", "reviewer", "admin")
RANK = {"read": 1, "write": 2, "approve": 3, "admin": 4}
ROLE_RANK = {"viewer": 1, "engineer": 2, "reviewer": 3, "admin": 4}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{1,63}$")
TOKEN_PREFIX = "aeai_"


def hash_token(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


class SlidingWindow:
    """In-memory sliding-window limiter (single process)."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float = 60.0, count: bool = True) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        if limit <= 0:
            return True, 0
        t = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and t - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(window - (t - q[0])) + 1)
            if count:
                q.append(t)
            if len(self._hits) > 50000:                      # bound memory under key churn
                for k in [k for k, v in self._hits.items() if not v][:10000]:
                    self._hits.pop(k, None)
            return True, 0


class AuthService:
    def __init__(self, db: Database, settings):
        self.db, self.s = db, settings
        self._touch: dict[str, float] = {}

    # ---------------------------------------------------------------- users
    def create_user(self, name: str, role: str, actor: str, ttl_days: int | None = None, token: str | None = None) -> tuple[dict, str]:
        if not NAME_RE.match(name or ""):
            raise Invalid("User name: 2-64 characters, letters, digits and _ . @ - only")
        if role not in ("admin", "user"):
            raise Invalid("Global role must be 'admin' or 'user'")
        if self.db._q("SELECT 1 FROM users WHERE name=?", (name,)):
            raise Conflict(f"User '{name}' already exists")
        token = token or new_token()
        ttl = self.s.token_ttl_days if ttl_days is None else ttl_days
        exp = (datetime.now(timezone.utc) + timedelta(days=ttl)).isoformat(timespec="seconds") if ttl else None
        self.db._q("INSERT INTO users(name,role,key_hash,key_prefix,created_at,created_by,expires_at) VALUES(?,?,?,?,?,?,?)",
                   (name, role, hash_token(token), token[:9], now(), actor, exp))
        self.db.audit(actor, "user.create", None, name, {"role": role, "expires_at": exp})
        return self.user(name), token

    def user(self, name: str) -> dict:
        r = self.db._q("SELECT name,role,key_prefix,created_at,created_by,expires_at,disabled,last_used FROM users WHERE name=?", (name,))
        if not r:
            raise NotFound(f"User '{name}' not found")
        return r[0]

    def users(self) -> list[dict]:
        return self.db._q("SELECT name,role,key_prefix,created_at,created_by,expires_at,disabled,last_used FROM users ORDER BY name")

    def rotate(self, name: str, actor: str) -> tuple[dict, str]:
        self.user(name)
        token = new_token()
        ttl = self.s.token_ttl_days
        exp = (datetime.now(timezone.utc) + timedelta(days=ttl)).isoformat(timespec="seconds") if ttl else None
        self.db._q("UPDATE users SET key_hash=?, key_prefix=?, expires_at=? WHERE name=?", (hash_token(token), token[:9], exp, name))
        self.db.audit(actor, "user.rotate", None, name)
        return self.user(name), token

    def set_disabled(self, name: str, disabled: bool, actor: str) -> dict:
        u = self.user(name)
        if disabled and u["role"] == "admin" and u["name"] == actor:
            raise Conflict("You cannot disable your own administrator account")
        if disabled and u["role"] == "admin" and sum(1 for x in self.users() if x["role"] == "admin" and not x["disabled"]) <= 1:
            raise Conflict("At least one enabled administrator must remain")
        self.db._q("UPDATE users SET disabled=? WHERE name=?", (int(disabled), name))
        self.db.audit(actor, "user.disable" if disabled else "user.enable", None, name)
        return self.user(name)

    def authenticate(self, token: str | None) -> dict | None:
        if not token or not 20 <= len(token) <= 200:
            return None
        r = self.db._q("SELECT name,role,expires_at,disabled FROM users WHERE key_hash=?", (hash_token(token),))
        if not r or r[0]["disabled"]:
            return None
        u = r[0]
        if u["expires_at"] and u["expires_at"] < datetime.now(timezone.utc).isoformat(timespec="seconds"):
            return None
        t = time.monotonic()
        if t - self._touch.get(u["name"], -1e9) > 60:                      # last_used at most once a minute
            self._touch[u["name"]] = t
            self.db._q("UPDATE users SET last_used=? WHERE name=?", (now(), u["name"]))
        return {"name": u["name"], "role": u["role"]}

    # ---------------------------------------------------------------- memberships
    def set_member(self, project: str, user: str, role: str, actor: str) -> dict:
        if role not in ROLES:
            raise Invalid(f"Project role must be one of {list(ROLES)}")
        self.user(user)
        if not self.db.project_exists(project):
            raise NotFound(f"Project '{project}' not found")
        cur = self.db._q("SELECT role FROM memberships WHERE user=? AND project=?", (user, project))
        if cur and cur[0]["role"] == "admin" and role != "admin" and self._admins(project) <= 1:
            raise Conflict("A project must keep at least one project administrator")
        self.db._q("INSERT INTO memberships(user,project,role,granted_by,granted_at) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(user,project) DO UPDATE SET role=excluded.role, granted_by=excluded.granted_by, granted_at=excluded.granted_at",
                   (user, project, role, actor, now()))
        self.db.audit(actor, "member.set", project, user, {"role": role})
        return {"user": user, "project": project, "role": role}

    def _admins(self, project: str) -> int:
        return self.db._q("SELECT count(*) AS n FROM memberships WHERE project=? AND role='admin'", (project,))[0]["n"]

    def remove_member(self, project: str, user: str, actor: str) -> None:
        cur = self.db._q("SELECT role FROM memberships WHERE user=? AND project=?", (user, project))
        if not cur:
            raise NotFound(f"{user} is not a member of {project}")
        if cur[0]["role"] == "admin" and self._admins(project) <= 1:
            raise Conflict("A project must keep at least one project administrator")
        self.db._q("DELETE FROM memberships WHERE user=? AND project=?", (user, project))
        self.db.audit(actor, "member.remove", project, user)

    def members(self, project: str) -> list[dict]:
        return self.db._q("SELECT user,role,granted_by,granted_at FROM memberships WHERE project=? ORDER BY user", (project,))

    def memberships(self, user: str) -> list[dict]:
        return self.db._q("SELECT project,role FROM memberships WHERE user=? ORDER BY project", (user,))

    def project_role(self, user: dict, project: str) -> str | None:
        if user["role"] == "admin":
            return "admin"
        r = self.db._q("SELECT role FROM memberships WHERE user=? AND project=?", (user["name"], project))
        return r[0]["role"] if r else None

    def visible_projects(self, user: dict) -> set[str] | None:
        """None = all projects (platform administrator)."""
        return None if user["role"] == "admin" else {m["project"] for m in self.memberships(user["name"])}

    # ---------------------------------------------------------------- bootstrap
    def bootstrap(self) -> str | None:
        """Creates the first administrator when enforcement is on and no user exists. Returns a generated token (or None)."""
        if self.s.auth_mode != "enforced" or self.users():
            return None
        provided = self.s.admin_token or None
        _, token = self.create_user("admin", "admin", "bootstrap", token=provided)
        if provided:
            return None
        print(f"\n*** INITIAL ADMIN TOKEN (shown once, store it safely): {token}\n", file=sys.stderr, flush=True)
        return token
