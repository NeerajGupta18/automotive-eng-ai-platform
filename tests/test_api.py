import pytest


def setup_project(client, sample, project="bcm"):
    assert client.post("/projects", json={"name": project}).status_code == 201
    r1 = client.post(f"/projects/{project}/documents", files={"file": ("hld_bcm_v1.md", sample("hld_bcm_v1.md"))},
                     data={"version": "1.0", "status": "approved"}, headers={"X-User": "alice"})
    assert r1.status_code == 201, r1.text
    return r1.json()


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_project_name_validation(client):
    assert client.post("/projects", json={"name": "../evil"}).status_code == 422


def test_upload_dedupe_and_validation(client, sample):
    setup_project(client, sample)
    dup = client.post("/projects/bcm/documents", files={"file": ("copy.md", sample("hld_bcm_v1.md"))})
    assert dup.status_code == 400 and "already ingested" in dup.json()["detail"]
    bad = client.post("/projects/bcm/documents", files={"file": ("x.exe", b"MZ....")})
    assert bad.status_code == 400 and "not allowed" in bad.json()["detail"]
    empty = client.post("/projects/bcm/documents", files={"file": ("e.md", b"")})
    assert empty.status_code == 400
    corrupt = client.post("/projects/bcm/documents", files={"file": ("c.pdf", b"not a pdf")})
    assert corrupt.status_code == 400
    assert client.post("/projects/nope/documents", files={"file": ("a.md", b"# x\n\ntext body here")}).status_code == 404
    weird = client.post("/projects/bcm/documents", files={"file": ("../../etc/passwd.md", b"# T\n\nsome text here")})
    assert weird.status_code == 201 and "/" not in weird.json()["name"]


def test_pdf_upload_and_query(client, sample):
    client.post("/projects", json={"name": "pdfp"})
    r = client.post("/projects/pdfp/documents", files={"file": ("hld_bcm_v1.pdf", sample("hld_bcm_v1.pdf"))})
    assert r.status_code == 201 and r.json()["pages"] == 3
    ans = client.post("/projects/pdfp/hld/query", json={"question": "Which component provides the vehicle speed?"}).json()
    assert not ans["refused"] and ans["citations"] and ans["grounded"]
    assert ans["citations"][0]["page"] >= 1


def test_cited_qa(client, sample):
    setup_project(client, sample)
    ans = client.post("/projects/bcm/hld/query", json={"question": "When are the doors locked automatically?"}).json()
    assert not ans["refused"] and ans["grounded"] and ans["status"] == "needs_review"
    assert "10 km/h" in ans["answer"]
    assert all(c["doc_name"] == "hld_bcm_v1.md" and c["section"] for c in ans["citations"])


def test_refuses_out_of_scope_question(client, sample):
    setup_project(client, sample)
    ans = client.post("/projects/bcm/hld/query", json={"question": "What is the best recipe for chocolate cake?"}).json()
    assert ans["refused"] and not ans["citations"]


def test_draft_documents_excluded_until_approved(client, sample):
    client.post("/projects", json={"name": "draftp"})
    doc = client.post("/projects/draftp/documents", files={"file": ("hld_bcm_v1.md", sample("hld_bcm_v1.md"))},
                      data={"status": "draft"}).json()
    q = {"question": "When are the doors locked automatically?"}
    assert client.post("/projects/draftp/hld/query", json=q).json()["refused"]              # not approved
    assert not client.post("/projects/draftp/hld/query", json={**q, "include_draft": True}).json()["refused"]
    client.post(f"/projects/draftp/documents/{doc['id']}/status", params={"status": "approved"})
    assert not client.post("/projects/draftp/hld/query", json=q).json()["refused"]


def test_project_isolation(client, sample):
    setup_project(client, sample, "proja")
    client.post("/projects", json={"name": "projb"})
    doc_a = client.get("/projects/proja/documents").json()[0]
    assert client.get(f"/projects/projb/hld/{doc_a['id']}/entities").status_code == 404
    assert client.post("/projects/projb/hld/query", json={"question": "door lock speed"}).json()["refused"]


def test_entities_issues_and_review_queue(client, sample):
    doc = setup_project(client, sample)
    ex = client.get(f"/projects/bcm/hld/{doc['id']}/entities").json()
    assert ex["summary"]["components"] == 6 and ex["review_items_queued"] >= 5
    pending = client.get("/projects/bcm/reviews", params={"status": "pending"}).json()
    assert pending and all(r["module"] == "hld" for r in pending)
    # re-running extraction must not duplicate review items
    client.get(f"/projects/bcm/hld/{doc['id']}/entities", params={"refresh": True})
    assert len(client.get("/projects/bcm/reviews").json()) == len(pending)
    # decide one
    rid = pending[0]["id"]
    d = client.post(f"/projects/bcm/reviews/{rid}/decision", json={"decision": "approved", "comment": "confirmed"},
                    headers={"X-User": "bob"}).json()
    assert d["status"] == "approved" and d["reviewer"] == "bob"
    assert client.post(f"/projects/bcm/reviews/{rid}/decision", json={"decision": "rejected"}).status_code == 409
    assert client.post(f"/projects/bcm/reviews/{rid}/decision", json={"decision": "maybe"}).status_code == 422
    assert client.post(f"/projects/other/reviews/{rid}/decision", json={"decision": "approved"}).status_code == 404


def test_compare_impact_graph_export(client, sample):
    d1 = setup_project(client, sample)
    d2 = client.post("/projects/bcm/documents", files={"file": ("hld_bcm_v2.md", sample("hld_bcm_v2.md"))},
                     data={"version": "1.1"}).json()
    cmp = client.get("/projects/bcm/hld/compare", params={"a": d1["id"], "b": d2["id"]}).json()
    assert cmp["components_added"] == ["SWC_CrashEventMgr"] and cmp["to"]["version"] == "1.1"
    imp = client.get(f"/projects/bcm/hld/{d1['id']}/impact/SWC_WheelSensorIf").json()
    assert "SWC_DoorLockCtrl" in imp["downstream_impacted"]
    assert client.get(f"/projects/bcm/hld/{d1['id']}/impact/SWC_Nope").status_code == 404
    assert client.get(f"/projects/bcm/hld/{d1['id']}/graph").text.startswith("digraph")
    csv = client.get(f"/projects/bcm/hld/{d1['id']}/export", params={"format": "csv"})
    assert csv.headers["content-type"].startswith("text/csv") and csv.text.count("\n") >= 15
    assert client.get(f"/projects/bcm/hld/{d1['id']}/export", params={"format": "json"}).json()["summary"]["ports"] == 14


def test_audit_trail_records_actions(client, sample):
    doc = setup_project(client, sample)
    client.post("/projects/bcm/hld/query", json={"question": "door lock speed"}, headers={"X-User": "carol"})
    client.get(f"/projects/bcm/hld/{doc['id']}/entities", headers={"X-User": "carol"})
    log = client.get("/projects/bcm/audit").json()
    actions = {(a["actor"], a["action"]) for a in log}
    assert ("alice", "document.ingest") in actions
    assert ("carol", "hld.ask") in actions and ("carol", "hld.extract") in actions


def test_delete_document_removes_vectors(client, container, sample):
    doc = setup_project(client, sample)
    assert container.store("bcm").count() > 0
    assert client.delete(f"/projects/bcm/documents/{doc['id']}").status_code == 200
    assert container.store("bcm").count() == 0
    assert client.get("/projects/bcm/documents").json() == []
