"""Shared review workflow for HARA / TARA (and later modules).

States: proposed -> assessed -> approved | rejected ;  approved -> suspect (upstream changed).
Rules enforced here, not in the UI:
  * four-eyes: the approver must differ from the last human editor of that record
  * machine-generated proposals carry updated_by='rules' so anyone may review them
  * every state change is stored in the record history and in the audit log
  * changing an upstream record marks everything downstream 'suspect' (change-impact)"""
from __future__ import annotations

from typing import Callable

from app.core.errors import Conflict, Invalid, NotFound
from app.core.trace import Trace

MACHINE = "rules"


class Workflow:
    def __init__(self, container, module: str):
        self.c, self.module = container, module
        self.db, self.trace = container.db, Trace(container.db)

    # -------------------------------------------------------------- basics
    def get(self, project: str, ref: str, kind: str | tuple[str, ...] | None = None) -> dict:
        rec = self.db.rec_get(project, ref)
        kinds = (kind,) if isinstance(kind, str) else kind
        if not rec or rec["module"] != self.module or (kinds and rec["kind"] not in kinds):
            raise NotFound(f"{ref} not found")
        return rec

    def audit(self, actor: str, action: str, project: str, ref: str | None = None, detail=None) -> None:
        self.db.audit(actor, f"{self.module}.{action}", project, ref, detail)

    def create(self, project, kind, prefix, payload, actor, *, status="proposed", key=None, machine=True):
        who = MACHINE if machine else actor
        return self.db.rec_create(project, self.module, kind, prefix, payload, status, who, key)

    def list(self, project, kind=None, status=None, **payload_filters) -> list[dict]:
        rows = self.db.rec_list(project, self.module, kind, status)
        for k, v in payload_filters.items():
            if v is not None:
                rows = [r for r in rows if r["payload"].get(k) == v]
        return rows

    def history(self, project: str, ref: str) -> list[dict]:
        self.get(project, ref)
        return self.db.rec_history(project, ref)

    # -------------------------------------------------------------- decisions
    def decide(self, project: str, ref: str, actor: str, decision: str, comment: str = "",
               gate: Callable[[dict], None] | None = None) -> dict:
        """decision: 'approved' | 'rejected'. `gate` raises Invalid/Conflict when content is not approvable."""
        if decision not in ("approved", "rejected"):
            raise Invalid("decision must be 'approved' or 'rejected'")
        rec = self.get(project, ref)
        if rec["status"] in ("approved", "rejected"):
            raise Conflict(f"{ref} is already {rec['status']}")
        if decision == "approved":
            if rec["updated_by"] == actor:
                raise Conflict(f"Four-eyes rule: {actor} last edited {ref} and cannot approve it")
            if gate:
                gate(rec)
        payload = {**rec["payload"], "decision_comment": comment}
        out = self.db.rec_update(project, ref, actor, payload, decision, event=f"decision:{decision}",
                                 approve=(decision == "approved"))
        self.audit(actor, "decision", project, ref, {"decision": decision, "comment": comment})
        return out

    def edit(self, project: str, ref: str, actor: str, payload: dict, *, status: str | None = None,
             event: str = "edit") -> dict:
        """Human edit. Editing an approved record makes it 'suspect' until re-approved."""
        rec = self.get(project, ref)
        if rec["status"] == "rejected":
            raise Conflict(f"{ref} was rejected and cannot be edited")
        if status is None:
            status = "suspect" if rec["status"] == "approved" else rec["status"]
        out = self.db.rec_update(project, ref, actor, payload, status, event=event)
        self.audit(actor, event, project, ref)
        return out

    def suspect_downstream(self, project: str, ref: str, actor: str, reason: str) -> list[str]:
        marked = []
        for d in self.trace.descendants(project, ref):
            rec = self.db.rec_get(project, d)
            if rec and rec["status"] in ("approved", "assessed", "proposed"):
                self.db.rec_update(project, d, actor, {**rec["payload"], "suspect_reason": reason}, "suspect",
                                   event="suspect")
                marked.append(d)
        if marked:
            self.audit(actor, "suspect", project, ref, {"reason": reason, "affected": marked})
        return marked

    # -------------------------------------------------------------- metrics
    def acceptance(self, project: str, kinds: list[str]) -> dict:
        """Recommendation acceptance: among machine-originated records that a human decided on."""
        acc = rej = 0
        for r in self.db.rec_list(project, self.module):
            if r["kind"] in kinds and r["payload"].get("origin") in ("rules", "library", "template"):
                if r["status"] in ("approved", "accepted"):
                    acc += 1
                elif r["status"] == "rejected":
                    rej += 1
        n = acc + rej
        return {"decided": n, "accepted": acc, "rejected": rej, "rate": round(acc / n, 3) if n else None}
