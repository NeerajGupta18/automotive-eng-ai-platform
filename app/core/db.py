"""SQLite structured store: projects, documents, review queue, audit trail.
(PostgreSQL is the production option; the schema is deliberately plain SQL.)"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(name TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
    status TEXT NOT NULL, sha256 TEXT NOT NULL, pages INTEGER, chunks INTEGER,
    uploaded_by TEXT, uploaded_at TEXT NOT NULL, UNIQUE(project, sha256));
CREATE TABLE IF NOT EXISTS reviews(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, module TEXT NOT NULL,
    kind TEXT NOT NULL, ref TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', reviewer TEXT, comment TEXT,
    created_at TEXT NOT NULL, decided_at TEXT, UNIQUE(project, module, ref));
CREATE TABLE IF NOT EXISTS extractions(doc_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, module TEXT NOT NULL, kind TEXT NOT NULL,
    ref TEXT NOT NULL, key TEXT, payload TEXT NOT NULL, status TEXT NOT NULL, rev INTEGER NOT NULL DEFAULT 1,
    created_by TEXT, created_at TEXT NOT NULL, updated_by TEXT, updated_at TEXT NOT NULL,
    approved_by TEXT, approved_at TEXT, UNIQUE(project, ref), UNIQUE(project, module, kind, key));
CREATE TABLE IF NOT EXISTS record_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, ref TEXT NOT NULL, rev INTEGER NOT NULL,
    ts TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, status TEXT NOT NULL, snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS links(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,
    rel TEXT NOT NULL, created_by TEXT, created_at TEXT NOT NULL, UNIQUE(project, src, dst, rel));
CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
    project TEXT, action TEXT NOT NULL, entity TEXT, detail TEXT);
"""


# ------------------------------------------------------------------ migrations
# SCHEMA above is the Stage 1-4 baseline. Every later change is a numbered, idempotent migration, so an existing
# database is upgraded in place and the version is recorded in schema_version.
def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _m_audit_chain(conn) -> None:
    for col in ("prev_hash", "hash"):
        if col not in _cols(conn, "audit"):
            conn.execute(f"ALTER TABLE audit ADD COLUMN {col} TEXT")


def _m_auth(conn) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        name TEXT PRIMARY KEY, role TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, key_prefix TEXT,
        created_at TEXT NOT NULL, created_by TEXT, expires_at TEXT, disabled INTEGER NOT NULL DEFAULT 0, last_used TEXT);
    CREATE TABLE IF NOT EXISTS memberships(
        user TEXT NOT NULL, project TEXT NOT NULL, role TEXT NOT NULL, granted_by TEXT, granted_at TEXT NOT NULL,
        PRIMARY KEY(user, project));
    """)


MIGRATIONS = [(1, "baseline (stages 1-4)", lambda conn: None),
              (2, "hash-chained audit log", _m_audit_chain),
              (3, "users and project memberships", _m_auth)]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def migrate(conn) -> list[int]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    applied = []
    for v, desc, fn in MIGRATIONS:
        if v not in done:
            fn(conn)
            conn.execute("INSERT INTO schema_version VALUES(?,?,?)", (v, desc, now()))
            applied.append(v)
    conn.commit()
    return applied


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self.migrations_applied = migrate(self._conn)

    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return [dict(r) for r in cur.fetchall()]

    # ---- projects
    def create_project(self, name: str) -> None:
        self._q("INSERT OR IGNORE INTO projects VALUES(?,?)", (name, now()))

    def projects(self) -> list[dict]:
        return self._q("SELECT * FROM projects ORDER BY name")

    def project_exists(self, name: str) -> bool:
        return bool(self._q("SELECT 1 FROM projects WHERE name=?", (name,)))

    # ---- documents
    def add_document(self, d: dict) -> None:
        self._q(
            "INSERT INTO documents VALUES(:id,:project,:name,:version,:status,:sha256,:pages,:chunks,:uploaded_by,:uploaded_at)",
            d,
        )

    def documents(self, project: str, status: str | None = None) -> list[dict]:
        if status:
            return self._q("SELECT * FROM documents WHERE project=? AND status=? ORDER BY uploaded_at", (project, status))
        return self._q("SELECT * FROM documents WHERE project=? ORDER BY uploaded_at", (project,))

    def document(self, doc_id: str) -> dict | None:
        r = self._q("SELECT * FROM documents WHERE id=?", (doc_id,))
        return r[0] if r else None

    def document_by_hash(self, project: str, sha: str) -> dict | None:
        r = self._q("SELECT * FROM documents WHERE project=? AND sha256=?", (project, sha))
        return r[0] if r else None

    def set_document_status(self, doc_id: str, status: str) -> None:
        self._q("UPDATE documents SET status=? WHERE id=?", (status, doc_id))

    # ---- extraction cache
    def save_extraction(self, doc_id: str, payload: dict) -> None:
        self._q("INSERT OR REPLACE INTO extractions VALUES(?,?,?)", (doc_id, json.dumps(payload), now()))

    def get_extraction(self, doc_id: str) -> dict | None:
        r = self._q("SELECT payload FROM extractions WHERE doc_id=?", (doc_id,))
        return json.loads(r[0]["payload"]) if r else None

    # ---- review queue
    def add_review(self, project: str, module: str, kind: str, ref: str, payload: dict) -> bool:
        """Returns True if a new item was queued (dedupes on project/module/ref)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO reviews(project,module,kind,ref,payload,created_at) VALUES(?,?,?,?,?,?)",
                (project, module, kind, ref, json.dumps(payload), now()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def reviews(self, project: str, status: str | None = None, module: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM reviews WHERE project=?", [project]
        if status:
            sql += " AND status=?"; args.append(status)
        if module:
            sql += " AND module=?"; args.append(module)
        rows = self._q(sql + " ORDER BY id", tuple(args))
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def review(self, review_id: int) -> dict | None:
        r = self._q("SELECT * FROM reviews WHERE id=?", (review_id,))
        if not r:
            return None
        r[0]["payload"] = json.loads(r[0]["payload"])
        return r[0]

    def decide_review(self, review_id: int, status: str, reviewer: str, comment: str) -> None:
        self._q("UPDATE reviews SET status=?,reviewer=?,comment=?,decided_at=? WHERE id=?",
                (status, reviewer, comment, now(), review_id))

    # ---- audit
    @staticmethod
    def _audit_hash(prev: str, row: list) -> str:
        return hashlib.sha256((prev + json.dumps(row, separators=(",", ":"), ensure_ascii=False)).encode()).hexdigest()

    def audit(self, actor: str, action: str, project: str | None = None, entity: str | None = None,
              detail: dict | str | None = None) -> None:
        """Append-only, hash-chained: each row commits to the previous row, so edits, deletions and reordering are
        detectable by verify_audit(). BEGIN IMMEDIATE keeps the chain linear even with several processes."""
        d = detail if isinstance(detail, str) or detail is None else json.dumps(detail)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                last = self._conn.execute("SELECT hash FROM audit WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
                prev, ts = (last[0] if last else "GENESIS"), now()
                h = self._audit_hash(prev, [ts, actor, project, action, entity, d])
                self._conn.execute("INSERT INTO audit(ts,actor,project,action,entity,detail,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?)",
                                   (ts, actor, project, action, entity, d, prev, h))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def verify_audit(self) -> dict:
        rows = self._q("SELECT * FROM audit ORDER BY id")
        prev, legacy, hashed, bad = "GENESIS", 0, 0, None
        for r in rows:
            if r["hash"] is None:
                legacy += 1                       # rows written before the chain existed cannot be verified
                if hashed:                        # an unchained row after chained rows means somebody inserted it
                    bad = bad or r["id"]
                continue
            hashed += 1
            expect = self._audit_hash(prev, [r["ts"], r["actor"], r["project"], r["action"], r["entity"], r["detail"]])
            if r["prev_hash"] != prev or r["hash"] != expect:
                bad = bad or r["id"]
            prev = r["hash"]
        first, last = (rows[0]["id"] if rows else 0), (rows[-1]["id"] if rows else 0)
        ids = [r["id"] for r in rows]
        gaps = [i for i in range(first, last + 1) if i not in set(ids)] if rows and last - first < 200000 else []
        return {"ok": bad is None and not gaps, "rows": len(rows), "chained_rows": hashed, "legacy_rows": legacy,
                "first_bad_id": bad, "missing_ids": gaps[:20], "head": prev if hashed else None}

    def audit_log(self, project: str | None = None, limit: int = 200, actor: str | None = None,
                  action: str | None = None) -> list[dict]:
        sql, args = "SELECT id,ts,actor,project,action,entity,detail,hash FROM audit WHERE 1=1", []
        for col, val in (("project", project), ("actor", actor)):
            if val:
                sql += f" AND {col}=?"; args.append(val)
        if action:
            sql += " AND action LIKE ?"; args.append(action.replace("%", "") + "%")
        return self._q(sql + " ORDER BY id DESC LIMIT ?", tuple(args) + (limit,))


    # ================================================================ generic records (HARA, TARA, ...)
    def _rec(self, row) -> dict:
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    def _next_ref(self, project: str, prefix: str) -> str:
        rows = self._conn.execute("SELECT ref FROM records WHERE project=? AND ref LIKE ?",
                                  (project, f"{prefix}-%")).fetchall()
        nums = [int(r["ref"].split("-")[-1]) for r in rows if r["ref"].split("-")[-1].isdigit()]
        return f"{prefix}-{(max(nums) if nums else 0) + 1:03d}"

    def _history(self, rec: dict, actor: str, event: str) -> None:
        self._conn.execute(
            "INSERT INTO record_history(project,ref,rev,ts,actor,event,status,snapshot) VALUES(?,?,?,?,?,?,?,?)",
            (rec["project"], rec["ref"], rec["rev"], now(), actor, event, rec["status"], json.dumps(rec["payload"])))

    def rec_create(self, project: str, module: str, kind: str, prefix: str, payload: dict, status: str,
                   actor: str, key: str | None = None) -> tuple[dict, bool]:
        """Create a record with a sequential reference (H-001, TS-014, ...). If `key` already exists the
        existing record is returned unchanged, which makes generation steps idempotent."""
        with self._lock:
            if key is not None:
                r = self._conn.execute("SELECT * FROM records WHERE project=? AND module=? AND kind=? AND key=?",
                                       (project, module, kind, key)).fetchone()
                if r:
                    return self._rec(r), False
            ref, ts = self._next_ref(project, prefix), now()
            self._conn.execute(
                "INSERT INTO records(project,module,kind,ref,key,payload,status,rev,created_by,created_at,updated_by,updated_at)"
                " VALUES(?,?,?,?,?,?,?,1,?,?,?,?)",
                (project, module, kind, ref, key, json.dumps(payload), status, actor, ts, actor, ts))
            rec = self.rec_get(project, ref)
            self._history(rec, actor, "create")
            self._conn.commit()
            return rec, True

    def rec_get(self, project: str, ref: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM records WHERE project=? AND ref=?", (project, ref)).fetchone()
            return self._rec(r) if r else None

    def rec_list(self, project: str, module: str | None = None, kind: str | None = None,
                 status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM records WHERE project=?", [project]
        for col, val in (("module", module), ("kind", kind), ("status", status)):
            if val:
                sql += f" AND {col}=?"; args.append(val)
        with self._lock:
            return [self._rec(r) for r in self._conn.execute(sql + " ORDER BY id", args).fetchall()]

    def rec_update(self, project: str, ref: str, actor: str, payload: dict | None = None,
                   status: str | None = None, event: str = "update", approve: bool = False) -> dict:
        """Every change bumps the revision and stores a snapshot (decision history / audit evidence)."""
        with self._lock:
            rec = self.rec_get(project, ref)
            if rec is None:
                raise KeyError(ref)
            new_payload = rec["payload"] if payload is None else payload
            new_status = status or rec["status"]
            approved_by, approved_at = rec["approved_by"], rec["approved_at"]
            if approve:
                approved_by, approved_at = actor, now()
            elif new_status != "approved":
                approved_by = approved_at = None
            self._conn.execute(
                "UPDATE records SET payload=?,status=?,rev=rev+1,updated_by=?,updated_at=?,approved_by=?,approved_at=?"
                " WHERE project=? AND ref=?",
                (json.dumps(new_payload), new_status, actor, now(), approved_by, approved_at, project, ref))
            rec = self.rec_get(project, ref)
            self._history(rec, actor, event)
            self._conn.commit()
            return rec

    def rec_history(self, project: str, ref: str) -> list[dict]:
        rows = self._q("SELECT * FROM record_history WHERE project=? AND ref=? ORDER BY id", (project, ref))
        for r in rows:
            r["snapshot"] = json.loads(r["snapshot"])
        return rows

    # ================================================================ traceability links
    def link_add(self, project: str, src: str, dst: str, rel: str, actor: str) -> None:
        self._q("INSERT OR IGNORE INTO links(project,src,dst,rel,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (project, src, dst, rel, actor, now()))

    def link_remove(self, project: str, src: str, rel: str | None = None) -> None:
        if rel:
            self._q("DELETE FROM links WHERE project=? AND src=? AND rel=?", (project, src, rel))
        else:
            self._q("DELETE FROM links WHERE project=? AND src=?", (project, src))

    def links(self, project: str) -> list[dict]:
        return self._q("SELECT src,dst,rel FROM links WHERE project=? ORDER BY id", (project,))
