"""SQLite structured store: projects, documents, review queue, audit trail.
(PostgreSQL is the production option; the schema is deliberately plain SQL.)"""
from __future__ import annotations

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
CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
    project TEXT, action TEXT NOT NULL, entity TEXT, detail TEXT);
"""


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
    def audit(self, actor: str, action: str, project: str | None = None, entity: str | None = None,
              detail: dict | str | None = None) -> None:
        d = detail if isinstance(detail, str) or detail is None else json.dumps(detail)
        self._q("INSERT INTO audit(ts,actor,project,action,entity,detail) VALUES(?,?,?,?,?,?)",
                (now(), actor, project, action, entity, d))

    def audit_log(self, project: str | None = None, limit: int = 200) -> list[dict]:
        if project:
            return self._q("SELECT * FROM audit WHERE project=? ORDER BY id DESC LIMIT ?", (project, limit))
        return self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
