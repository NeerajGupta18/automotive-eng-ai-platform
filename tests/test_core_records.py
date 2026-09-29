"""Unit tests for the shared record store, traceability graph and workflow base."""
import pytest

from app.core.errors import Conflict, Invalid, NotFound
from app.core.trace import Trace
from app.core.workflow import Workflow


@pytest.fixture
def wf(container):
    container.db.create_project("p")
    return container, Workflow(container, "hara")


def test_records_are_idempotent_by_key_and_sequential(wf):
    c, w = wf
    a, new_a = w.create("p", "hazard", "H", {"x": 1}, "u", key="k")
    b, new_b = w.create("p", "hazard", "H", {"x": 2}, "u", key="k")
    d, _ = w.create("p", "hazard", "H", {"x": 3}, "u")
    assert (a["ref"], new_a, b["ref"], new_b, d["ref"]) == ("H-001", True, "H-001", False, "H-002")
    assert b["payload"] == {"x": 1}
    with pytest.raises(NotFound):
        w.get("p", "H-999")
    with pytest.raises(NotFound):
        Workflow(c, "tara").get("p", "H-001")          # modules cannot read each other's records


def test_history_records_every_revision(wf):
    c, w = wf
    w.create("p", "hazard", "H", {"v": 0}, "u")
    w.edit("p", "H-001", "alice", {"v": 1})
    w.decide("p", "H-001", "bob", "approved", "ok")
    hist = w.history("p", "H-001")
    assert [(h["rev"], h["actor"], h["event"], h["status"]) for h in hist] == [
        (1, "rules", "create", "proposed"), (2, "alice", "edit", "proposed"), (3, "bob", "decision:approved", "approved")]
    w.edit("p", "H-001", "alice", {"v": 2})                # editing an approved record makes it suspect and clears approval
    rec = w.get("p", "H-001")
    assert rec["status"] == "suspect" and rec["approved_by"] is None


def test_decision_rules(wf):
    c, w = wf
    w.create("p", "hazard", "H", {}, "u")
    w.edit("p", "H-001", "alice", {"v": 1})
    with pytest.raises(Conflict):
        w.decide("p", "H-001", "alice", "approved")         # four-eyes
    with pytest.raises(Invalid):
        w.decide("p", "H-001", "bob", "maybe")
    with pytest.raises(Invalid):
        w.decide("p", "H-001", "bob", "approved", gate=lambda r: (_ for _ in ()).throw(Invalid("blocked")))
    assert w.decide("p", "H-001", "bob", "rejected")["status"] == "rejected"
    with pytest.raises(Conflict):
        w.decide("p", "H-001", "carol", "approved")         # already decided
    with pytest.raises(Conflict):
        w.edit("p", "H-001", "carol", {"v": 3})              # rejected records are frozen


def test_trace_graph_and_suspect_propagation(wf):
    c, w = wf
    for i in range(4):
        w.create("p", "hazard", "H", {"i": i}, "u", status="approved")
    tr = Trace(c.db)
    for a, b in (("H-001", "H-002"), ("H-002", "H-003"), ("H-001", "COMP:X"), ("H-003", "H-004")):
        c.db.link_add("p", a, b, "rel", "u")
    assert tr.children("p", "H-001") == ["H-002", "COMP:X"] and tr.parents("p", "H-003") == ["H-002"]
    assert tr.descendants("p", "H-001") == {"H-002": 1, "COMP:X": 1, "H-003": 2, "H-004": 3}
    assert tr.ancestors("p", "H-004") == {"H-003": 1, "H-002": 2, "H-001": 3}
    marked = w.suspect_downstream("p", "H-001", "alice", "upstream changed")
    assert set(marked) == {"H-002", "H-003", "H-004"} and w.get("p", "H-003")["status"] == "suspect"
    assert w.get("p", "H-001")["status"] == "approved"      # the changed item itself is not touched
    tr.replace_out("p", "H-001", "rel", ["H-004"], "u")
    assert tr.children("p", "H-001") == ["H-004"]
    assert "digraph" in tr.dot("p")
