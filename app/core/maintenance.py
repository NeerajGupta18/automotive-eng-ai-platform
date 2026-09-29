"""Operations: consistent backup, verified restore, and vector reindexing.

Backup uses SQLite's online backup API (a consistent snapshot even while the service runs) plus a copy of
uploads/parsed text/vector files with a SHA-256 manifest. Vector files can be inconsistent if written during the
copy: they are derived data and can always be rebuilt with reindex()."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from app.core.errors import Conflict, Invalid

DB_NAME = "platform.db"
SKIP = (DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm", DB_NAME + "-journal")
MAX_RESTORE_BYTES = 20 * 1024 ** 3


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(data_dir: Path, out: Path) -> dict:
    data_dir, out = data_dir.resolve(), out.resolve()
    db = data_dir / DB_NAME
    if not db.exists():
        raise Invalid(f"No database at {db}")
    if data_dir in out.parents:
        raise Invalid("The backup file must be written outside the data directory")
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / DB_NAME
        src, dst = sqlite3.connect(db), sqlite3.connect(snap)
        try:
            src.backup(dst)
            version = dst.execute("SELECT max(version) FROM schema_version").fetchone()[0]
            head = dst.execute("SELECT hash FROM audit WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            dst.close(); src.close()
        files = {DB_NAME: _sha(snap)}
        entries = [(snap, DB_NAME)]
        for p in sorted(data_dir.rglob("*")):
            rel = p.relative_to(data_dir).as_posix()
            if p.is_file() and not p.is_symlink() and p.name not in SKIP and not p.name.startswith(".ready-") and not p.name.endswith(".tmp"):
                files[rel] = _sha(p)
                entries.append((p, rel))
        manifest = {"format": 1, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "schema_version": version,
                    "audit_head": head[0] if head else None, "files": files}
        mpath = Path(tmp) / "manifest.json"
        mpath.write_text(json.dumps(manifest, indent=1))
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_suffix(out.suffix + ".part")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            tar.add(mpath, arcname="manifest.json")
            for p, rel in entries:
                tar.add(p, arcname=f"data/{rel}", recursive=False)
        part.replace(out)
    return {"archive": str(out), "files": len(files), "bytes": out.stat().st_size, "schema_version": version, "audit_head": manifest["audit_head"]}


def restore(archive: Path, data_dir: Path, force: bool = False) -> dict:
    data_dir = data_dir.resolve()
    if (data_dir / DB_NAME).exists() and not force:
        raise Conflict(f"{data_dir} already contains a database; pass force=True to replace it (the old data is kept as a copy)")
    stage = data_dir.parent / f".restore-{uuid.uuid4().hex[:8]}"
    stage.mkdir(parents=True)
    try:
        total = 0
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for m in members:
                name = Path(m.name)
                if name.is_absolute() or ".." in name.parts or not (m.isfile() or m.isdir()) or m.issym() or m.islnk():
                    raise Invalid(f"Unsafe archive member rejected: {m.name!r}")
                total += m.size
                if total > MAX_RESTORE_BYTES:
                    raise Invalid("Archive is larger than the restore limit")
            try:
                tar.extractall(stage, members=members, filter="data")               # Python >= 3.12 hardening
            except TypeError:
                tar.extractall(stage, members=members)
        mf = stage / "manifest.json"
        if not mf.exists():
            raise Invalid("Not a platform backup (manifest.json missing)")
        manifest = json.loads(mf.read_text())
        for rel, digest in manifest["files"].items():
            p = stage / "data" / rel
            if not p.is_file() or _sha(p) != digest:
                raise Invalid(f"Integrity check failed for {rel}: the backup is corrupt or has been modified")
        extra = [p for p in (stage / "data").rglob("*") if p.is_file() and p.relative_to(stage / "data").as_posix() not in manifest["files"]]
        if extra:
            raise Invalid(f"Backup contains files that are not in its manifest: {extra[0].name}")
        kept = None
        if data_dir.exists():
            kept = data_dir.with_name(f"{data_dir.name}.pre-restore-{time.strftime('%Y%m%d%H%M%S')}")
            data_dir.rename(kept)
        (stage / "data").rename(data_dir)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    from app.core.db import Database
    db = Database(data_dir / DB_NAME)                                               # runs migrations on older backups
    audit = db.verify_audit()
    return {"restored_files": len(manifest["files"]), "schema_version": manifest["schema_version"], "migrations_applied": db.migrations_applied,
            "audit_ok": audit["ok"], "audit_head_matches_manifest": audit["head"] == manifest["audit_head"] or bool(db.migrations_applied),
            "previous_data_kept_at": str(kept) if kept else None}


def reindex(container, project: str | None = None) -> dict:
    """Rebuilds vector collections from the stored parsed text (after changing the embedding model, or after
    losing/restoring vector files without a consistent copy)."""
    from app.core.chunking import chunk_sections
    from app.core.parsing import split_sections
    cfg, report = container.settings, {"documents": 0, "chunks": 0, "skipped": [], "hara_libraries": 0}
    projects = [project] if project else [p["name"] for p in container.db.projects()]
    for proj in projects:
        store = container.store(proj)
        for d in container.db.documents(proj):
            try:
                pages = container.documents.load_pages(d["id"])
            except Exception:                                                        # noqa: BLE001
                report["skipped"].append(d["id"])
                continue
            store.delete({"doc_id": d["id"]})
            meta = {"doc_id": d["id"], "doc_name": d["name"], "version": d["version"], "project": proj}
            chunks = chunk_sections(split_sections(pages), meta, cfg.chunk_chars, cfg.chunk_overlap_chars)
            if chunks:
                store.add(chunks, container.embedder.embed([c.text for c in chunks]))
            container.db._q("UPDATE documents SET chunks=? WHERE id=?", (len(chunks), d["id"]))
            report["documents"] += 1
            report["chunks"] += len(chunks)
        lib = container.hara.library(proj)
        if lib:
            container.hara.load_library(proj, "reindex", {"name": lib["name"], "version": lib["version"], "entries": lib["entries"], "situations": lib["situations"]})
            report["hara_libraries"] += 1
    container.db.audit("reindex", "maintenance.reindex", project, None, report)
    return report
