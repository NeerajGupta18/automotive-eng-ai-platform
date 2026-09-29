import json
import logging
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.security import PUBLIC, classify
from app.config import ConfigError, get_settings
from app.container import Container
from app.core.observability import InstrumentedLLM, JsonFormatter, clean_id, configure_logging, new_metrics

ADMIN = "aeai_" + "a" * 40


def H(tok, **extra):
    return {"Authorization": f"Bearer {tok}", **extra}


@pytest.fixture
def secure(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("AUTH_MODE", "enforced")
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    container = Container(get_settings())
    client = TestClient(create_app(container))

    def mk(name, role="user"):
        r = client.post("/admin/users", json={"name": name, "role": role}, headers=H(ADMIN))
        assert r.status_code == 201, r.text
        return r.json()["token"]
    return SimpleNamespace(c=client, container=container, mk=mk, app=client.app)


def project_with_team(s):
    """alice administers p1; bob and erin review; dave engineers; carol views; frank is a stranger."""
    t = {n: s.mk(n) for n in ("alice", "bob", "carol", "dave", "erin", "frank")}
    assert s.c.post("/projects", json={"name": "p1"}, headers=H(t["alice"])).status_code == 201
    for user, role in (("bob", "reviewer"), ("erin", "reviewer"), ("carol", "viewer"), ("dave", "engineer")):
        assert s.c.put(f"/projects/p1/members/{user}", json={"role": role}, headers=H(t["alice"])).status_code == 200
    return t


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize("method,path,expect", [
    ("GET", "/health", ("public", None)), ("GET", "/docs", ("public", None)), ("GET", "/openapi.json", ("public", None)),
    ("GET", "/projects", ("authenticated", None)), ("POST", "/projects", ("create_project", None)),
    ("GET", "/projects/p/documents", ("read", "p")), ("POST", "/projects/p/documents", ("write", "p")),
    ("POST", "/projects/p/documents/abc/status", ("approve", "p")), ("DELETE", "/projects/p/documents/abc", ("admin", "p")),
    ("POST", "/projects/p/hld/query", ("read", "p")), ("POST", "/projects/p/hara/hazards/H-001/decision", ("approve", "p")),
    ("POST", "/projects/p/tara/threats/TS-001/accept-risk", ("approve", "p")), ("POST", "/projects/p/hara/library/load", ("approve", "p")),
    ("POST", "/projects/p/uds/specs/SPEC-001/validate", ("read", "p")), ("POST", "/projects/p/uds/specs/SPEC-001/generate", ("write", "p")),
    ("POST", "/projects/p/codereview/findings/F-1/triage", ("write", "p")), ("POST", "/projects/p/codereview/findings/F-1/decision", ("approve", "p")),
    ("GET", "/projects/p/members", ("admin", "p")), ("PUT", "/projects/p/members/bob", ("admin", "p")),
    ("GET", "/projects/p/audit", ("approve", "p")), ("GET", "/admin/users", ("global_admin", None)), ("GET", "/metrics", ("global_admin", None)),
    ("GET", "/system/info", ("global_admin", None)), ("GET", "/me", ("authenticated", None)), ("GET", "/tara/rules", ("authenticated", None)),
    ("GET", "/some/unknown/path", ("authenticated", None)), ("PATCH", "/projects/p/anything/new", ("write", "p")),
])
def test_classification_table(method, path, expect):
    assert classify(method, path) == expect


def test_every_route_is_classified_and_new_endpoints_default_to_write(secure):
    import re
    read_posts, seen = set(), 0
    for path, ops in secure.app.openapi()["paths"].items():
        sample = re.sub(r"\{[^}]+\}", "x", path.replace("{project}", "p"))
        for method in (m.upper() for m in ops if m in ("get", "post", "put", "delete", "patch")):
            perm, project = classify(method, sample)
            seen += 1
            if path in PUBLIC:
                assert perm == "public", path
            elif path.startswith("/projects/{project}/"):
                assert project == "p" and perm in ("read", "write", "approve", "admin"), (method, path, perm)
                if method != "GET" and perm == "read":
                    read_posts.add(path)
            else:
                assert perm in ("authenticated", "create_project", "global_admin"), (method, path, perm)
    assert seen > 120, seen
    assert read_posts == {"/projects/{project}/hld/query", "/projects/{project}/uds/specs/{ref}/validate", "/projects/{project}/uds/specs/{ref}/ask",
                          "/projects/{project}/uds/specs/{ref}/run", "/projects/{project}/uds/specs/{ref}/detection-matrix"}


# ------------------------------------------------------------------ authentication
def test_unauthenticated_and_malformed_credentials_are_rejected(secure):
    c = secure.c
    for headers in ({}, H("aeai_" + "z" * 40), {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}, {"Authorization": "Bearer " + "x" * 500}):
        r = c.get("/projects", headers=headers)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer" and "request_id" in r.json()
    assert c.get(f"/projects?token={ADMIN}").status_code == 401                # tokens in the query string are never accepted
    assert c.get("/projects", headers=H(ADMIN)).status_code == 200
    assert c.get("/health").json() == {"status": "ok", "auth": "enforced"}   # no configuration details for anonymous callers
    assert "llm" in c.get("/health", headers=H(ADMIN)).json()
    assert c.get("/docs").status_code == 200 and c.get("/ready").status_code == 200


def test_bootstrap_generates_a_token_once_and_config_is_validated(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("AUTH_MODE", "enforced")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    c1 = Container(get_settings())
    err = capsys.readouterr().err
    token = err.split("INITIAL ADMIN TOKEN (shown once, store it safely): ")[1].split()[0]
    assert c1.auth.authenticate(token) == {"name": "admin", "role": "admin"}
    Container(get_settings())
    assert "INITIAL ADMIN TOKEN" not in capsys.readouterr().err and len(c1.auth.users()) == 1      # not repeated on restart
    for env, val in (("ADMIN_TOKEN", "short"), ("AUTH_MODE", "maybe"), ("LOG_FORMAT", "xml")):
        with monkeypatch.context() as m:
            m.setenv("DATA_DIR", str(tmp_path / f"s-{env}"))
            m.setenv("AUTH_MODE", "enforced")
            m.setenv(env, val)
            with pytest.raises(ConfigError):
                Container(get_settings())


def test_user_and_token_lifecycle(secure):
    c, db = secure.c, secure.container.db
    tok = secure.mk("alice")
    assert secure.mk("bob") != tok
    assert c.post("/admin/users", json={"name": "alice"}, headers=H(ADMIN)).status_code == 409
    assert c.post("/admin/users", json={"name": "x"}, headers=H(ADMIN)).status_code == 422
    assert c.post("/admin/users", json={"name": "ok-name", "role": "root"}, headers=H(ADMIN)).status_code == 422
    listing = c.get("/admin/users", headers=H(ADMIN)).json()
    assert {"alice", "bob", "admin"} <= {u["name"] for u in listing} and not any("key_hash" in u or "token" in u for u in listing)
    assert db._q("SELECT key_hash FROM users WHERE name='alice'")[0]["key_hash"] not in (tok, "") and tok not in json.dumps(db._q("SELECT * FROM users"))   # only a hash is stored
    assert c.get("/admin/users", headers=H(tok)).status_code == 403                # ordinary users cannot administer
    assert c.post("/admin/users/alice/disable", headers=H(ADMIN)).json()["disabled"] == 1
    assert c.get("/projects", headers=H(tok)).status_code == 401
    c.post("/admin/users/alice/enable", headers=H(ADMIN))
    assert c.get("/projects", headers=H(tok)).status_code == 200
    new = c.post("/admin/users/alice/rotate", headers=H(ADMIN)).json()["token"]
    assert c.get("/projects", headers=H(tok)).status_code == 401 and c.get("/projects", headers=H(new)).status_code == 200
    db._q("UPDATE users SET expires_at='2020-01-01T00:00:00+00:00' WHERE name='alice'")                   # expired token
    assert c.get("/projects", headers=H(new)).status_code == 401
    assert c.post("/admin/users/admin/disable", headers=H(ADMIN)).status_code == 409                       # cannot lock yourself out
    assert c.post("/admin/users/ghost/rotate", headers=H(ADMIN)).status_code == 404
    forever = c.post("/admin/users", json={"name": "svc", "ttl_days": 0}, headers=H(ADMIN)).json()
    assert forever["user"]["expires_at"] is None


# ------------------------------------------------------------------ RBAC
def test_role_matrix_on_a_project(secure, sample):
    c, t = secure.c, None
    t = project_with_team(secure)
    doc = lambda who: c.post("/projects/p1/documents", files={"file": ("hld_bcm_v1.md", sample("hld_bcm_v1.md"))}, headers=H(t[who]))
    r = doc("carol")
    assert r.status_code == 403 and "engineer" in r.json()["detail"] and "viewer" in r.json()["detail"]                 # viewer cannot write
    up = doc("dave")
    assert up.status_code == 201
    did = up.json()["id"]
    assert c.get("/projects/p1/documents", headers=H(t["carol"])).status_code == 200                                     # viewer reads
    st = lambda who: c.post(f"/projects/p1/documents/{did}/status", params={"status": "draft"}, headers=H(t[who]))
    assert st("dave").status_code == 403 and st("bob").status_code == 200                                              # approve needs reviewer
    lib = lambda who: c.post("/projects/p1/hara/library/load", json={}, headers=H(t[who]))
    assert lib("dave").status_code == 403 and lib("bob").status_code == 201                                             # knowledge base changes need reviewer
    assert c.delete(f"/projects/p1/documents/{did}", headers=H(t["bob"])).status_code == 403                             # deleting needs admin
    assert c.get("/projects/p1/audit", headers=H(t["dave"])).status_code == 403 and c.get("/projects/p1/audit", headers=H(t["bob"])).status_code == 200
    assert c.get("/projects/p1/members", headers=H(t["bob"])).status_code == 403 and c.get("/projects/p1/members", headers=H(t["alice"])).status_code == 200
    assert c.put("/projects/p1/members/carol", json={"role": "reviewer"}, headers=H(t["bob"])).status_code == 403
    assert c.delete(f"/projects/p1/documents/{did}", headers=H(t["alice"])).status_code == 200
    # the read-style POSTs work for a viewer
    assert c.post("/projects/p1/hld/query", json={"question": "Which components exist?"}, headers=H(t["carol"])).status_code == 200
    assert c.put("/projects/p1/members/carol", json={"role": "boss"}, headers=H(t["alice"])).status_code == 422


def test_project_visibility_and_existence_are_not_leaked(secure):
    c = secure.c
    t = project_with_team(secure)
    assert c.post("/projects", json={"name": "p2"}, headers=H(t["frank"])).status_code == 201
    assert [p["name"] for p in c.get("/projects", headers=H(t["alice"])).json()] == ["p1"]
    assert [p["name"] for p in c.get("/projects", headers=H(t["frank"])).json()] == ["p2"]
    assert {p["name"] for p in c.get("/projects", headers=H(ADMIN)).json()} == {"p1", "p2"}
    real = c.get("/projects/p1/documents", headers=H(t["frank"]))
    ghost = c.get("/projects/nonexistent/documents", headers=H(t["frank"]))
    assert real.status_code == ghost.status_code == 404                              # a stranger cannot tell them apart
    assert c.get("/projects/p1/hara/items", headers=H(t["frank"])).status_code == 404
    assert c.post("/projects/p1/codereview/analyze", json={}, headers=H(t["frank"])).status_code == 404
    assert c.get("/projects/p1/documents", headers=H(ADMIN)).status_code == 200          # platform admin sees everything
    assert c.post("/projects", json={"name": "p1"}, headers=H(t["frank"])).status_code == 409


def test_project_keeps_an_administrator(secure):
    c = secure.c
    t = project_with_team(secure)
    assert c.put("/projects/p1/members/alice", json={"role": "viewer"}, headers=H(t["alice"])).status_code == 409
    assert c.delete("/projects/p1/members/alice", headers=H(t["alice"])).status_code == 409
    assert c.put("/projects/p1/members/bob", json={"role": "admin"}, headers=H(t["alice"])).status_code == 200
    assert c.delete("/projects/p1/members/alice", headers=H(t["alice"])).status_code == 200
    assert c.get("/projects/p1/documents", headers=H(t["alice"])).status_code == 404               # removed member loses access at once
    assert c.delete("/projects/p1/members/ghost", headers=H(t["bob"])).status_code == 404


# ------------------------------------------------------------------ identity is bound to the token
def test_four_eyes_cannot_be_bypassed_by_claiming_another_identity(secure):
    c = secure.c
    t = project_with_team(secure)
    P = "/projects/p1/hara"
    good = {"rationale_S": "Occupant risk at speed", "rationale_E": "Driving is frequent", "rationale_C": "Driver cannot react"}
    c.post(P + "/library/load", json={}, headers=H(t["erin"]))
    item = c.post(P + "/items", json={"name": "BCM", "functions": [{"name": "Central door locking", "guidewords": ["unintended"]}]}, headers=H(t["erin"])).json()["ref"]
    c.post(f"{P}/items/{item}/suggest-hazards", headers=H(t["erin"]))
    c.post(P + "/hazards/H-001/decision", json={"decision": "accepted"}, headers=H(t["erin"]))
    ev = c.post(P + "/hazards/H-001/propose-events", headers=H(t["erin"])).json()["events"][0]["ref"]
    assert c.put(f"{P}/events/{ev}/assessment", json={"S": 3, "E": 4, "C": 3, **good}, headers=H(t["erin"])).status_code == 200
    dec = lambda tok, **extra: c.post(f"{P}/events/{ev}/decision", json={"decision": "approved"}, headers=H(tok, **extra))
    spoof = dec(t["erin"], **{"X-User": "bob"})                                       # erin tries to pose as bob
    assert spoof.status_code == 409 and "erin" in spoof.json()["detail"] and "Four-eyes" in spoof.json()["detail"]
    assert dec(t["dave"]).status_code == 403                                           # an engineer cannot approve at all
    ok = dec(t["bob"], **{"X-User": "erin"})                                           # and bob cannot be blamed on erin either
    assert ok.status_code == 200 and ok.json()["approved_by"] == "bob"
    actors = {h["actor"] for h in c.get(f"{P}/records/{ev}/history", headers=H(ADMIN)).json()}
    assert actors == {"rules", "erin", "bob"}


def test_denials_and_failed_logins_are_audited(secure):
    c = secure.c
    t = project_with_team(secure)
    c.post("/projects/p1/documents", files={"file": ("x.md", b"# t\nbody text here")}, headers=H(t["carol"]))
    c.get("/projects", headers=H("aeai_" + "q" * 40))
    rows = c.get("/admin/audit", params={"action": "auth."}, headers=H(ADMIN)).json()
    denied = next(r for r in rows if r["action"] == "auth.denied")
    d = json.loads(denied["detail"])
    assert denied["actor"] == "carol" and d["needed"] == "write" and d["why"] == "forbidden"
    failed = next(r for r in rows if r["action"] == "auth.failed")
    assert failed["actor"] == "anonymous" and "aeai_qqqq" not in json.dumps(rows)                  # the presented token is never stored


def test_development_mode_is_unchanged_and_sanitised(client):
    r = client.post("/projects", json={"name": "devproj"}, headers={"X-User": "bob<script>"})
    assert r.status_code == 201
    assert client.get("/projects/devproj/audit").json()[0]["actor"] == "bobscript"
    assert client.get("/me", headers={"X-User": "alice"}).json() == {"name": "alice", "role": "admin", "auth": "off", "memberships": []}
    assert "llm" in client.get("/health").json() and client.get("/admin/users").status_code == 200
    assert clean_id("ab c\nd<e>") == "abcde" and clean_id("") is None


def test_me_reports_identity_and_memberships(secure):
    t = project_with_team(secure)
    me = secure.c.get("/me", headers=H(t["bob"])).json()
    assert me == {"name": "bob", "role": "user", "auth": "enforced", "memberships": [{"project": "p1", "role": "reviewer"}]}
    assert secure.c.get("/me", headers=H(ADMIN)).json()["role"] == "admin"


# ------------------------------------------------------------------ limits and hardening
def test_failed_authentication_lockout(secure):
    secure.container.settings = replace(secure.container.settings, auth_fail_limit_per_min=3)
    c = secure.c
    codes = [c.get("/projects", headers=H("aeai_" + "b" * 40)).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    r = c.get("/projects", headers=H(ADMIN))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1                 # lockout is per client address, even for a valid token
    assert c.get("/health").status_code == 200                                           # health checks still work


def test_per_user_rate_limit(secure):
    t = {n: secure.mk(n) for n in ("alice", "bob")}
    secure.container.settings = replace(secure.container.settings, rate_limit_per_min=5)
    c = secure.c
    codes = [c.get("/projects", headers=H(t["alice"])).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    assert c.get("/projects", headers=H(t["bob"])).status_code == 200                    # another user is unaffected
    secure.container.settings = replace(secure.container.settings, rate_limit_per_min=0)
    assert 'aeai_rate_limited_total{kind="requests"} 2' in c.get("/metrics", headers=H(ADMIN)).text


def test_oversized_bodies_are_rejected_before_processing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    monkeypatch.setenv("MAX_REQUEST_MB", "1")
    c = TestClient(create_app(Container(get_settings())))
    c.post("/projects", json={"name": "big1"})
    big = b"x" * (1024 * 1024 + 10)
    assert c.post("/projects/big1/documents", files={"file": ("a.md", big)}).status_code == 413
    def chunks():                                                                          # streamed multipart upload, no Content-Length
        yield b'--b0undary\r\nContent-Disposition: form-data; name="file"; filename="a.md"\r\nContent-Type: text/markdown\r\n\r\n'
        for _ in range(4):
            yield b"y" * 400_000
        yield b"\r\n--b0undary--\r\n"
    r = c.post("/projects/big1/documents", content=chunks(), headers={"content-type": "multipart/form-data; boundary=b0undary"})
    assert r.status_code == 413 and r.json()["detail"] == "Request body too large"
    assert c.post("/projects/big1/documents", files={"file": ("a.md", b"# ok\nsmall document text")}).status_code == 201


def test_security_headers_and_request_ids(client):
    r = client.get("/projects", headers={"X-Request-ID": "abc<def>ghi"})
    h = r.headers
    assert h["x-request-id"] == "abcdefghi" and h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["cache-control"] == "no-store" and "frame-ancestors 'none'" in h["content-security-policy"] and h["referrer-policy"] == "no-referrer"
    assert client.get("/projects").headers["x-request-id"] != client.get("/projects").headers["x-request-id"]
    assert "content-security-policy" not in client.get("/docs").headers                        # the docs UI needs scripts


def test_unhandled_errors_are_generic_and_logged(client, container, caplog):
    def boom(project):
        raise RuntimeError("secret internal detail /etc/passwd")
    container.hara.items = boom
    client.post("/projects", json={"name": "errp1"})
    with caplog.at_level(logging.ERROR, logger="aeai"):
        r = client.get("/projects/errp1/hara/items")
    assert r.status_code == 500 and r.json()["detail"] == "Internal server error" and r.json()["request_id"] == r.headers["x-request-id"]
    assert "secret internal detail" not in r.text and any("unhandled error" in x.getMessage() for x in caplog.records)


# ------------------------------------------------------------------ audit chain
def test_audit_chain_is_verifiable_and_tampering_is_detected(secure):
    c, db = secure.c, secure.container.db
    t = project_with_team(secure)
    v = c.get("/admin/audit/verify", headers=H(ADMIN)).json()
    assert v["ok"] and v["chained_rows"] > 10 and v["legacy_rows"] == 0 and len(v["head"]) == 64 and v["first_bad_id"] is None
    lines = c.get("/admin/audit/export", headers=H(ADMIN)).text.strip().split("\n")
    assert len(lines) > 10 and all("hash" in json.loads(l) for l in lines)
    ids = [r["id"] for r in db._q("SELECT id FROM audit ORDER BY id")]
    victim = ids[len(ids) // 2]
    db._q("UPDATE audit SET actor='mallory' WHERE id=?", (victim,))
    bad = c.get("/admin/audit/verify", headers=H(ADMIN)).json()
    assert not bad["ok"] and bad["first_bad_id"] == victim
    db._q("UPDATE audit SET actor=(SELECT actor FROM audit WHERE id=?) WHERE id=?", (ids[0], victim))
    original = db._q("SELECT * FROM audit WHERE id=?", (ids[3],))[0]
    db._q("DELETE FROM audit WHERE id=?", (ids[3],))
    gone = c.get("/admin/audit/verify", headers=H(ADMIN)).json()
    assert not gone["ok"] and ids[3] in gone["missing_ids"]
    db._q("INSERT INTO audit(id,ts,actor,project,action,entity,detail,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?)",
          tuple(original[k] for k in ("id", "ts", "actor", "project", "action", "entity", "detail", "prev_hash", "hash")))
    assert c.get("/projects/p1/audit", headers=H(t["bob"])).status_code == 200
    filt = c.get("/admin/audit", params={"actor": "alice", "action": "member."}, headers=H(ADMIN)).json()
    assert filt and all(r["actor"] == "alice" and r["action"].startswith("member.") for r in filt)


def test_audit_entries_are_serialised_under_concurrency(container):
    import threading
    def worker(n):
        for i in range(25):
            container.db.audit(f"u{n}", "load.test", None, str(i))
    ts = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    v = container.db.verify_audit()
    assert v["ok"] and v["chained_rows"] == 150


# ------------------------------------------------------------------ observability
def test_metrics_are_admin_only_low_cardinality_and_cover_llm_and_auth(secure):
    c = secure.c
    t = project_with_team(secure)
    c.get("/projects/p1/documents", headers=H(t["carol"]))
    c.get("/projects/p1/hara/records/H-001/history", headers=H(t["carol"]))
    c.get("/projects", headers=H("aeai_" + "n" * 40))
    secure.container.llm.generate("system", "prompt")
    assert c.get("/metrics", headers=H(t["carol"])).status_code == 403
    m = c.get("/metrics", headers=H(ADMIN))
    assert m.headers["content-type"].startswith("text/plain")
    body = m.text
    assert 'aeai_http_requests_total{method="GET",route="/projects/{project}/documents",status="200"}' in body
    assert "H-001" not in body and "/projects/p1" not in body                            # route templates, never raw ids
    assert 'aeai_http_request_duration_seconds_bucket{route="/projects",le="+Inf"}' in body and "_sum" in body and "_count" in body
    assert 'aeai_auth_events_total{outcome="unauthenticated"} 1' in body
    assert 'aeai_llm_calls_total{backend="mock",outcome="ok"} 1' in body and "aeai_users" in body and "aeai_audit_rows" in body
    assert "# TYPE aeai_http_requests_total counter" in body and "aeai_uptime_seconds" in body


def test_llm_wrapper_counts_failures_and_reraises():
    m = new_metrics()

    class Boom:
        name = "boom"
        def generate(self, *a, **k):
            raise RuntimeError("down")
    with pytest.raises(RuntimeError):
        InstrumentedLLM(Boom(), m).generate("s", "p")
    assert 'aeai_llm_calls_total{backend="boom",outcome="error"} 1' in m.render()


def test_json_logs_are_structured_and_secrets_never_reach_them(secure, caplog):
    rec = logging.LogRecord("aeai.access", logging.INFO, __file__, 1, "GET /projects 200", None, None)
    rec.x_request_id, rec.x_user, rec.x_status = "r1", "alice", 200
    d = json.loads(JsonFormatter().format(rec))
    assert d["request_id"] == "r1" and d["user"] == "alice" and d["status"] == 200 and d["level"] == "INFO" and d["ts"].endswith("Z")
    assert len([h for h in configure_logging().handlers if getattr(h, "_aeai", False)]) == 1
    configure_logging(); configure_logging("INFO", "json")
    assert len([h for h in logging.getLogger("aeai").handlers if getattr(h, "_aeai", False)]) == 1     # idempotent
    tok = secure.mk("alice")
    with caplog.at_level(logging.DEBUG):
        secure.c.get("/projects", headers=H(tok))
        secure.c.get("/projects", headers=H("aeai_" + "w" * 40))
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "route" in text and tok not in text and ("w" * 40) not in text and ADMIN not in text
    access = [r for r in caplog.records if r.name == "aeai.access"]
    assert access and all(hasattr(r, "x_request_id") and hasattr(r, "x_status") for r in access)
