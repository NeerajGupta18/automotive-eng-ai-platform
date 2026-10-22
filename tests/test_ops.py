import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from app.api.main import create_app
from app.config import get_settings
from app.container import Container
from app.core import maintenance
from app.core.auth import AuthService
from app.core.db import SCHEMA, SCHEMA_VERSION, Database
from app.core.errors import Conflict, Invalid

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"
TABLES = ("projects", "documents", "records", "record_history", "links", "audit")
A, B = {"X-User": "alice"}, {"X-User": "bob"}


@pytest.fixture
def make(tmp_path, monkeypatch):
    def build(name="data", **env):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / name))
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return Container(get_settings())
    return build


def dump(db):
    return {t: db._q(f"SELECT * FROM {t} ORDER BY 1, 2") for t in TABLES}


# ------------------------------------------------------------------ migrations
def stage4_database(path: Path) -> None:
    """A database exactly as Stage 4 left it: baseline schema, no schema_version, audit rows without hashes."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO projects VALUES('legacy','2026-09-01T00:00:00+00:00')")
    for i in range(4):
        conn.execute("INSERT INTO audit(ts,actor,project,action,entity,detail) VALUES(?,?,?,?,?,?)", (f"2026-09-0{i + 1}T00:00:00+00:00", "old-user", "legacy", "old.action", str(i), None))
    conn.execute("INSERT INTO records(project,module,kind,ref,payload,status,created_at,updated_at) VALUES('legacy','hara','hazard','H-001','{}','proposed','t','t')")
    conn.commit(); conn.close()


def test_stage4_database_is_upgraded_in_place(tmp_path):
    p = tmp_path / "old.db"
    stage4_database(p)
    db = Database(p)
    assert db.migrations_applied == [1, 2, 3, 4, 5] and db._q("SELECT max(version) AS v FROM schema_version")[0]["v"] == SCHEMA_VERSION
    assert db.project_exists("legacy") and db.rec_get("legacy", "H-001")
    assert {r["name"] for r in db._q("SELECT name FROM sqlite_master WHERE type='table'")} >= {"users", "memberships", "schema_version", "webhooks", "deliveries",
                                                                                          "field_logs"}
    v = db.verify_audit()
    assert v["ok"] and v["legacy_rows"] == 4 and v["chained_rows"] == 0 and v["head"] is None          # legacy rows cannot be verified, and are reported as such
    db.audit("new-user", "new.action", "legacy")
    db.audit("new-user", "new.action2", "legacy")
    v = db.verify_audit()
    assert v["ok"] and v["legacy_rows"] == 4 and v["chained_rows"] == 2 and len(v["head"]) == 64
    db._q("INSERT INTO audit(ts,actor,action) VALUES('t','forger','fake.entry')")                         # an unchained row after the chain started
    assert not db.verify_audit()["ok"]
    again = Database(p)
    assert again.migrations_applied == [] and len(again._q("SELECT * FROM schema_version")) == SCHEMA_VERSION           # idempotent


# ------------------------------------------------------------------ backup and restore
def populate(c):
    c.db.create_project("proj1")
    doc = c.documents.ingest("proj1", "hld_bcm_v1.md", (SAMPLES / "hld_bcm_v1.md").read_bytes(), actor="alice")
    c.hara.create_item("proj1", "BCM", "door locking", [{"name": "Central door locking", "guidewords": ["unintended"]}], None, "alice")
    c.db.audit("alice", "custom.event", "proj1", "x", {"k": 1})
    return doc


def test_backup_restore_round_trip(make, tmp_path):
    c1 = make("src")
    doc = populate(c1)
    out = tmp_path / "backups" / "b.tar.gz"
    info = maintenance.backup(c1.settings.data_dir, out)
    assert info["schema_version"] == SCHEMA_VERSION and info["files"] >= 3 and oct(out.stat().st_mode & 0o777) == "0o600"
    with tarfile.open(out) as t:
        names = t.getnames()
    assert "manifest.json" in names and "data/platform.db" in names and any(n.startswith("data/parsed/") for n in names) and any(n.startswith("data/vectors/") for n in names)
    dest = tmp_path / "restored"
    rep = maintenance.restore(out, dest)
    assert rep["audit_ok"] and rep["audit_head_matches_manifest"] and rep["schema_version"] == SCHEMA_VERSION and rep["previous_data_kept_at"] is None
    c2 = make("restored")
    assert dump(c2.db) == dump(c1.db)                                                                       # every table identical
    ans = c2.hld.ask("proj1", "Which component provides the vehicle speed?")
    assert not ans["refused"] and ans["citations"]                                                          # vectors came back too
    assert c2.db.verify_audit()["ok"]
    with pytest.raises(Conflict):
        maintenance.restore(out, dest)                                                                      # never overwrite silently
    rep = maintenance.restore(out, dest, force=True)
    assert Path(rep["previous_data_kept_at"]).exists()                                                      # the replaced data is kept
    with pytest.raises(Invalid):
        maintenance.backup(c1.settings.data_dir, c1.settings.data_dir / "inside.tar.gz")


def test_restore_rejects_hostile_or_damaged_archives(make, tmp_path):
    c = make("src")
    populate(c)
    good = tmp_path / "good.tar.gz"
    maintenance.backup(c.settings.data_dir, good)

    def craft(name, edit):
        out = tmp_path / name
        with tarfile.open(good) as src, tarfile.open(out, "w:gz") as dst:
            for m in src.getmembers():
                data = src.extractfile(m).read() if m.isfile() else None
                m2, data2 = edit(m, data)
                if m2 is not None:
                    dst.addfile(m2, io.BytesIO(data2) if data2 is not None else None)
            extra = edit(None, None)
            if extra:
                info, payload = extra
                dst.addfile(info, io.BytesIO(payload) if payload is not None else None)
        return out

    def traversal(m, d):
        if m is None:
            ti = tarfile.TarInfo("../evil.txt"); ti.size = 4
            return ti, b"evil"
        return m, d

    def absolute(m, d):
        if m is None:
            ti = tarfile.TarInfo("/tmp/evil.txt"); ti.size = 4
            return ti, b"evil"
        return m, d

    def symlink(m, d):
        if m is None:
            ti = tarfile.TarInfo("data/link"); ti.type = tarfile.SYMTYPE; ti.linkname = "/etc/passwd"
            return ti, None
        return m, d

    def nomanifest(m, d):
        return (None, None) if m is not None and m.name == "manifest.json" else (m, d) if m else None

    def tampered(m, d):
        if m is not None and m.name == "data/platform.db":
            d = d[:-1] + bytes([d[-1] ^ 1]); m.size = len(d)
        return (m, d) if m else None

    def extra(m, d):
        if m is None:
            ti = tarfile.TarInfo("data/planted.txt"); ti.size = 3
            return ti, b"new"
        return m, d
    for name, fn, expect in (("t.tgz", traversal, "Unsafe"), ("a.tgz", absolute, "Unsafe"), ("s.tgz", symlink, "Unsafe"), ("n.tgz", nomanifest, "manifest"),
                             ("x.tgz", tampered, "Integrity"), ("e.tgz", extra, "not in its manifest")):
        arc = craft(name, fn)
        dest = tmp_path / f"dest-{name}"
        with pytest.raises(Invalid, match=expect):
            maintenance.restore(arc, dest)
        assert not dest.exists() and not (tmp_path / "evil.txt").exists() and not list(tmp_path.glob(".restore-*"))       # nothing leaks, staging is cleaned up
    assert not Path("/tmp/evil.txt").exists()


def test_reindex_rebuilds_vectors_from_stored_text(make, tmp_path):
    c1 = make("ri")
    doc = populate(c1)
    chunks = c1.db.document(doc["id"])["chunks"]
    import shutil
    shutil.rmtree(c1.settings.data_dir / "vectors")
    c2 = make("ri")
    assert c2.hld.ask("proj1", "Which component provides the vehicle speed?")["refused"]                      # vectors are gone
    rep = maintenance.reindex(c2, "proj1")
    assert rep["documents"] == 1 and rep["chunks"] == chunks and rep["skipped"] == []
    assert not c2.hld.ask("proj1", "Which component provides the vehicle speed?")["refused"]
    (c2.settings.data_dir / "parsed" / f"{doc['id']}.json").unlink()
    assert maintenance.reindex(c2)["skipped"] == [doc["id"]]                                                  # missing text is reported, not fatal
    assert c2.db._q("SELECT action FROM audit WHERE action='maintenance.reindex'")


# ------------------------------------------------------------------ readiness
def test_readiness_reflects_real_dependencies(client, container):
    r = client.get("/ready")
    assert r.status_code == 200 and r.json() == {"ready": True, "checks": {"database": True, "storage_writable": True, "embedder": True}}
    container.settings = replace(container.settings, data_dir=Path("/nonexistent-dir/none"))
    bad = client.get("/ready")
    assert bad.status_code == 503 and bad.json()["checks"]["storage_writable"] is False and bad.json()["ready"] is False
    container.settings = replace(container.settings, data_dir=container.settings.data_dir)
    container.db._q = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
    assert client.get("/ready").json()["checks"]["database"] is False


def test_system_info(client):
    i = client.get("/system/info").json()
    assert i["schema_version"] == SCHEMA_VERSION and i["auth_mode"] == "off" and any("AUTH_MODE=off" in w for w in i["warnings"])
    assert i["limits"]["max_upload_mb"] == 50


# ------------------------------------------------------------------ management CLI
def cli(tmp_path, *args, **env):
    e = {**os.environ, "DATA_DIR": str(tmp_path / "clidata"), **env}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "manage.py"), *args], capture_output=True, text=True, env=e)


def test_management_cli(tmp_path, monkeypatch):
    assert cli(tmp_path, "check-config").returncode == 0
    r = cli(tmp_path, "check-config", AUTH_MODE="enforced", ADMIN_TOKEN="tiny")
    assert r.returncode == 1 and "ERROR: ADMIN_TOKEN" in r.stdout
    assert "WARNING" in cli(tmp_path, "check-config").stdout
    r = cli(tmp_path, "create-user", "carol", "--role", "user")
    assert r.returncode == 0
    token = r.stdout.split("Token (shown once): ")[1].strip()
    db = Database(tmp_path / "clidata" / "platform.db")
    assert AuthService(db, get_settings()).authenticate(token) == {"name": "carol", "role": "user"}
    assert cli(tmp_path, "create-user", "carol").returncode == 2                                            # duplicate
    assert "carol" in cli(tmp_path, "list-users").stdout
    db.create_project("cliproj")
    assert cli(tmp_path, "grant", "cliproj", "carol", "reviewer").returncode == 0
    assert cli(tmp_path, "grant", "ghostproj", "carol", "reviewer").returncode == 2
    assert cli(tmp_path, "disable", "carol").returncode == 0 and AuthService(db, get_settings()).authenticate(token) is None
    v = cli(tmp_path, "verify-audit")
    assert v.returncode == 0 and json.loads(v.stdout)["ok"] is True
    db._q("UPDATE audit SET actor='mallory' WHERE id=(SELECT min(id) FROM audit)")
    assert cli(tmp_path, "verify-audit").returncode == 1
    db._q("UPDATE audit SET actor='cli' WHERE id=(SELECT min(id) FROM audit)")
    out = tmp_path / "cli-backup.tar.gz"
    assert cli(tmp_path, "backup", str(out)).returncode == 0 and out.exists()
    restore = subprocess.run([sys.executable, str(ROOT / "scripts" / "manage.py"), "restore", str(out)], capture_output=True, text=True,
                             env={**os.environ, "DATA_DIR": str(tmp_path / "cli-restored")})
    assert restore.returncode == 0 and json.loads(restore.stdout)["audit_ok"] is True
    assert cli(tmp_path, "restore", str(out)).returncode == 2                                                # refuses to overwrite


# ------------------------------------------------------------------ cross-module traceability
def build_chain(c):
    """hazard -> event -> goal -> FSR, with a UDS test and a code-review finding attached to the FSR."""
    P, H_ = "/projects/tr1", "/projects/tr1/hara"
    c.post("/projects", json={"name": "tr1"})
    good = {"rationale_S": "Occupant risk at speed", "rationale_E": "Driving is frequent", "rationale_C": "Driver cannot react"}
    c.post(H_ + "/library/load", json={})
    item = c.post(H_ + "/items", json={"name": "BCM", "functions": [{"name": "Central door locking", "guidewords": ["unintended"]}]}).json()["ref"]
    c.post(f"{H_}/items/{item}/suggest-hazards"); c.post(H_ + "/hazards/H-001/decision", json={"decision": "accepted"})
    events = c.post(H_ + "/hazards/H-001/propose-events").json()["events"]
    ev = events[0]["ref"]
    for e in events[1:]:
        c.post(f"{H_}/events/{e['ref']}/decision", json={"decision": "rejected"}, headers=B)
    c.put(f"{H_}/events/{ev}/assessment", headers=A, json={"S": 3, "E": 4, "C": 3, **good})
    c.post(f"{H_}/events/{ev}/decision", headers=B, json={"decision": "approved"})
    goal = c.post(f"{H_}/items/{item}/derive-goals").json()["goals"][0]["ref"]
    fsr = c.post(f"{H_}/goals/{goal}/draft-requirements").json()["requirements"][0]["ref"]
    c.put(f"{H_}/requirements/{fsr}", headers=A, json={"allocated_to": "SWC_DoorLockCtrl", "verification": ["MANUAL-1"]})
    doc = c.post(P + "/documents", files={"file": ("uds_bcm_spec.md", (SAMPLES / "uds_bcm_spec.md").read_bytes())}).json()["id"]
    spec = c.post(P + "/uds/specs/from-document", headers=A, json={"doc_id": doc}).json()["ref"]
    c.post(f"{P}/uds/specs/{spec}/generate", headers=A)
    c.post(f"{P}/uds/specs/{spec}/decision", headers=B, json={"decision": "approved"})
    tc = c.get(P + "/uds/tests").json()[0]["ref"]
    c.post(f"{P}/uds/tests/{tc}/decision", headers=B, json={"decision": "approved"})
    c.post(f"{P}/uds/tests/{tc}/link", headers=A, json={"requirement": fsr})
    c.post(P + "/codereview/sources", files={"file": ("d.c", b"void f(char *n) {\n    gets(n);\n}\n")}, headers=A)
    c.post(P + "/codereview/analyze", json={}, headers=A)
    fnd = c.get(P + "/codereview/findings").json()[0]["ref"]
    c.post(f"{P}/codereview/findings/{fnd}/link", json={"requirement": fsr}, headers=A)
    return {"ev": ev, "goal": goal, "fsr": fsr, "tc": tc, "fnd": fnd, "H": H_, "P": P}


def test_traceability_spans_all_modules(client):
    x = build_chain(client)
    T = x["P"] + "/trace"
    n = client.get(f"{T}/nodes/{x['ev']}").json()
    down = {d["ref"]: d for d in n["descendants"]}
    assert n["node"]["module"] == "hara" and n["node"]["kind"] == "event" and n["node"]["asil"] == "D"
    assert {x["goal"], x["fsr"], x["tc"], x["fnd"], "COMP:SWC_DoorLockCtrl", f"TEST:{x['tc']}", "TEST:MANUAL-1"} <= set(down)
    assert down[x["tc"]]["module"] == "uds" and down[x["fnd"]]["module"] == "codereview" and down["COMP:SWC_DoorLockCtrl"]["external"] is True
    assert down[x["goal"]]["hops"] == 1 and down[x["fsr"]]["hops"] == 2 and down[x["tc"]]["hops"] > down[x["fsr"]]["hops"]
    up = {a["ref"] for a in client.get(f"{T}/nodes/{x['tc']}").json()["ancestors"]}
    assert {x["fsr"], x["goal"], x["ev"], "H-001"} <= up                                                   # a test knows which hazard it ultimately evidences
    ext = client.get(f"{T}/nodes/COMP:SWC_DoorLockCtrl").json()
    assert ext["node"]["external"] and x["fsr"] in {a["ref"] for a in ext["ancestors"]}
    assert client.get(f"{T}/nodes/NOPE-999").status_code == 404
    s = client.get(f"{T}/summary").json()
    assert s["records"]["hara/goal"] == 1 and s["links"]["verified_by"] >= 2 and s["requirements_with_open_findings"] == [x["fsr"]]
    assert s["requirements_without_verification"] == ["TSR-001"] and s["suspect"] == 0                 # the drafted TSR still has no evidence
    dot = client.get(f"{T}/graph", params={"around": x["fsr"]}).text
    assert dot.startswith("digraph") and f'"{x["goal"]}" -> "{x["fsr"]}"' in dot and '"H-001"' in dot
    assert "digraph" in client.get(f"{T}/graph").text
    client.post("/projects", json={"name": "other1"})
    assert client.get(f"/projects/other1/trace/nodes/{x['fsr']}").status_code == 404 and client.get("/projects/other1/trace/summary").json()["nodes"] == 0


def test_a_change_puts_every_dependent_artifact_on_the_rereview_worklist(client):
    x = build_chain(client)
    T = x["P"] + "/trace"
    assert client.get(f"{T}/suspect").json() == []
    good = {"rationale_S": "Reassessed after design change", "rationale_E": "Driving is infrequent", "rationale_C": "Driver can react"}
    client.put(f"{x['H']}/events/{x['ev']}/assessment", headers=A, json={"S": 2, "E": 2, "C": 2, **good})           # ASIL D -> QM
    sus = {n["ref"]: n for n in client.get(f"{T}/suspect").json()}
    assert {x["goal"], x["fsr"], x["tc"], x["fnd"]} <= set(sus)                                                     # safety goal, requirement, test evidence and finding
    assert sus[x["fsr"]]["module"] == "hara" and sus[x["tc"]]["module"] == "uds" and "changed D -> QM" in sus[x["goal"]]["suspect_reason"]
    assert client.get(f"{T}/summary").json()["suspect"] == len(sus)
    assert "d9480f" in client.get(f"{T}/graph", params={"around": x["ev"]}).text                                     # suspect nodes are highlighted
