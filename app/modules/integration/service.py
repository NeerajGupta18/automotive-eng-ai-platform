"""Enterprise integration hub: signed webhooks fed from the audit log, Jira tickets, ReqIF requirement exchange.

  webhooks     project-scoped subscriptions (event patterns); a dispatcher turns new audit-log rows into
               deliveries, signs them and sends them with retries (1 min, 5 min, 30 min, 2 h) before giving up.
               INTEGRATIONS_LIVE=0 (default, and on the public demo): deliveries are prepared and signed but not sent.
  tickets      Jira issues for blocking items, findings and agent recommendations; idempotent per source and
               summary, dry run unless Jira is configured and integrations are live
  ReqIF        export of the project's requirements; import of a partner's ReqIF with change detection:
               new / modified / removed requirements are reported, and platform items linked to a changed
               customer requirement become suspect (released ADAS evidence too)"""
from __future__ import annotations

import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.core.db import now
from app.core.errors import Conflict, Invalid, NotFound
from app.core.workflow import Workflow
from app.modules.integration import jira, reqif, webhooks as wh

REF_RE_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class IntegrationService(Workflow):
    def __init__(self, container):
        super().__init__(container, "integration")

    @property
    def s(self):
        return self.c.settings

    def config(self) -> dict:
        s = self.s
        return {"live": s.integrations_live,
                "webhooks": {"allow_hosts": list(s.webhook_allow_hosts), "allow_private": s.webhook_allow_private,
                             "dispatch_interval_s": s.integration_dispatch_s, "default_events": wh.DEFAULT_EVENTS},
                "jira": {"configured": bool(s.jira_base_url and s.jira_project_key and s.jira_api_token),
                         "base_url": s.jira_base_url, "project_key": s.jira_project_key, "issue_type": s.jira_issue_type,
                         "auth": "basic (e-mail + API token)" if s.jira_user else "bearer token" if s.jira_api_token else "none"},
                "mode": "live" if s.integrations_live else "dry run: payloads are prepared and signed, nothing is sent"}

    # ================================================================ webhooks
    def _check_url(self, url: str) -> str:
        try:
            return wh.check_url(url, self.s.webhook_allow_hosts, self.s.webhook_allow_private, resolve=self.s.integrations_live)
        except wh.UnsafeURL as e:
            raise Invalid(str(e)) from e

    def add_webhook(self, project: str, url: str, events: list[str] | None, description: str, actor: str) -> dict:
        url = self._check_url(url)
        try:
            patterns = wh.validate_patterns(events or [])
        except ValueError as e:
            raise Invalid(str(e)) from e
        if len(self.webhooks(project)) >= 10:
            raise Conflict("At most 10 webhooks per project")
        wid, secret = f"WH-{uuid.uuid4().hex[:8]}", "whsec_" + secrets.token_urlsafe(32)
        cursor = self.db._q("SELECT COALESCE(MAX(id), 0) AS m FROM audit")[0]["m"]           # only future events
        self.db._q("INSERT INTO webhooks(id,project,url,events,secret,description,active,cursor,created_by,created_at) "
                   "VALUES(?,?,?,?,?,?,1,?,?,?)", (wid, project, url, json.dumps(patterns), secret,
                                                   " ".join((description or "").split())[:200], cursor, actor, now()))
        self.db.audit(actor, "integration.webhook.add", project, wid, {"url": url, "events": patterns})
        return {**self._wh(project, wid), "secret": secret,
                "note": "Store the secret now: it is shown only once and used to verify the X-AEAI-Signature-256 header."}

    def _wh(self, project: str, wid: str, with_secret: bool = False) -> dict:
        rows = self.db._q("SELECT * FROM webhooks WHERE project=? AND id=?", (project, wid))
        if not rows:
            raise NotFound(f"Webhook {wid} not found")
        r = rows[0]
        out = {k: r[k] for k in ("id", "url", "description", "cursor", "created_by", "created_at")}
        out.update(events=json.loads(r["events"]), active=bool(r["active"]))
        stats = self.db._q("SELECT status, COUNT(*) AS n FROM deliveries WHERE webhook_id=? GROUP BY status", (wid,))
        out["deliveries"] = {s["status"]: s["n"] for s in stats}
        if with_secret:
            out["secret"] = r["secret"]
        return out

    def webhooks(self, project: str) -> list[dict]:
        return [self._wh(project, r["id"]) for r in self.db._q("SELECT id FROM webhooks WHERE project=? ORDER BY created_at", (project,))]

    def remove_webhook(self, project: str, wid: str, actor: str) -> dict:
        self._wh(project, wid)
        self.db._q("DELETE FROM webhooks WHERE project=? AND id=?", (project, wid))
        self.db._q("UPDATE deliveries SET status='cancelled' WHERE webhook_id=? AND status IN ('pending','retry')", (wid,))
        self.db.audit(actor, "integration.webhook.remove", project, wid)
        return {"removed": wid}

    def deliveries(self, project: str, limit: int = 100) -> list[dict]:
        rows = self.db._q("SELECT id,webhook_id,audit_id,event,status,attempts,next_attempt_at,last_attempt_at,last_status,last_error,"
                          "signature,created_at,body FROM deliveries WHERE project=? ORDER BY id DESC LIMIT ?", (project, min(limit, 500)))
        for r in rows:
            r["body"] = json.loads(r["body"])
        return rows

    def _queue(self, project: str | None) -> int:
        """Turn new audit rows into deliveries for every matching active webhook."""
        hooks = self.db._q("SELECT * FROM webhooks WHERE active=1" + (" AND project=?" if project else ""), (project,) if project else ())
        n = 0
        for h in hooks:
            patterns = json.loads(h["events"])
            rows = self.db._q("SELECT * FROM audit WHERE project=? AND id>? ORDER BY id LIMIT 500", (h["project"], h["cursor"]))
            for row in rows:
                if wh.matches(row["action"], patterns):
                    body = json.dumps(wh.event_body(row), separators=(",", ":"))
                    self.db._q("INSERT OR IGNORE INTO deliveries(webhook_id,project,audit_id,event,body,status,next_attempt_at,created_at) "
                               "VALUES(?,?,?,?,?,'pending',?,?)", (h["id"], h["project"], row["id"], row["action"], body, now(), now()))
                    n += 1
            if rows:
                self.db._q("UPDATE webhooks SET cursor=? WHERE id=?", (rows[-1]["id"], h["id"]))
        return n

    def _attempt(self, d: dict, hook: dict) -> str:
        ts = str(int(time.time()))
        body = d["body"].encode()
        sig = wh.sign(hook["secret"], ts, body)
        attempts = d["attempts"] + 1
        if not self.s.integrations_live:
            self.db._q("UPDATE deliveries SET status='dry_run', attempts=?, last_attempt_at=?, signature=?, last_error=? WHERE id=?",
                       (attempts, now(), sig, "INTEGRATIONS_LIVE=0: not sent", d["id"]))
            return "dry_run"
        try:
            url = wh.check_url(hook["url"], self.s.webhook_allow_hosts, self.s.webhook_allow_private)   # again: DNS may have changed
            code, err = wh.post(url, body, wh.headers(d["event"], f"dlv_{d['id']}", ts, sig))
        except wh.UnsafeURL as e:
            code, err = None, f"blocked: {e}"
        if code is not None and 200 <= code < 300:
            status, nxt = "delivered", None
        elif attempts >= wh.MAX_ATTEMPTS or (err or "").startswith("blocked"):
            status, nxt = "failed", None
        else:
            status, nxt = "retry", _future(wh.BACKOFF_S[attempts - 1])
        self.db._q("UPDATE deliveries SET status=?, attempts=?, last_attempt_at=?, next_attempt_at=?, last_status=?, last_error=?, "
                   "signature=? WHERE id=?", (status, attempts, now(), nxt, code, err[:300] if err else None, sig, d["id"]))
        return status

    def dispatch(self, project: str | None = None, actor: str = "dispatcher") -> dict:
        queued = self._queue(project)
        due = self.db._q("SELECT * FROM deliveries WHERE status IN ('pending','retry') AND next_attempt_at<=?"
                         + (" AND project=?" if project else "") + " ORDER BY id LIMIT 100", (now(), project) if project else (now(),))
        hooks = {h["id"]: h for h in self.db._q("SELECT * FROM webhooks")}
        out = {"queued": queued, "delivered": 0, "retry": 0, "failed": 0, "dry_run": 0}
        for d in due:
            hook = hooks.get(d["webhook_id"])
            if hook is None:
                self.db._q("UPDATE deliveries SET status='cancelled' WHERE id=?", (d["id"],))
                continue
            out[self._attempt(d, hook)] += 1
        return out

    def ping(self, project: str, wid: str, actor: str) -> dict:
        hook = self.db._q("SELECT * FROM webhooks WHERE project=? AND id=?", (project, wid))
        if not hook:
            raise NotFound(f"Webhook {wid} not found")
        body = json.dumps({"id": f"evt_ping_{uuid.uuid4().hex[:8]}", "type": "ping", "project": project, "ref": wid,
                           "actor": actor, "occurred_at": now(), "data": {"message": "Test delivery from automotive-eng-ai"}},
                          separators=(",", ":"))
        self.db._q("INSERT INTO deliveries(webhook_id,project,audit_id,event,body,status,next_attempt_at,created_at) "
                   "VALUES(?,?,NULL,'ping',?,'pending',?,?)", (wid, project, body, now(), now()))
        d = self.db._q("SELECT * FROM deliveries WHERE webhook_id=? ORDER BY id DESC LIMIT 1", (wid,))[0]
        status = self._attempt(d, hook[0])
        self.db.audit(actor, "integration.webhook.ping", project, wid, {"status": status})
        return self.db._q("SELECT id,event,status,attempts,last_status,last_error,signature FROM deliveries WHERE id=?", (d["id"],))[0]

    # ================================================================ Jira tickets
    def create_ticket(self, project: str, summary: str, description: str, source_ref: str, actor: str,
                      priority: str = "Medium", labels: list[str] | None = None) -> dict:
        summary = " ".join((summary or "").split())
        if not 5 <= len(summary) <= 250:
            raise Invalid("summary: 5 to 250 characters")
        if source_ref and (len(source_ref) > 40 or not set(source_ref) <= REF_RE_CHARS | set("abcdefghijklmnopqrstuvwxyz_.:")):
            raise Invalid("source_ref must be a platform reference such as F-012 or RR04")
        labels = ["automotive-eng-ai", project] + list(labels or [])
        body = jira.payload(self.s.jira_project_key, self.s.jira_issue_type, summary, (description or "")[:30000], labels, priority)
        key = f"{source_ref}:{summary.lower()}"[:200]
        rec, created = self.create(project, "ticket", "TKT", {
            "title": summary, "summary": summary, "description": (description or "")[:30000], "source_ref": source_ref,
            "priority": priority, "target": "jira", "request": body, "external_key": "", "external_url": "",
            "attempts": 0, "last_error": ""}, actor, status="queued", key=key, machine=False)
        if source_ref and created and self.db.rec_get(project, source_ref):
            self.db.link_add(project, source_ref, rec["ref"], "tracked_in", actor)
        if created:
            self.audit(actor, "ticket.create", project, rec["ref"], {"source": source_ref, "summary": summary})
            rec = self.send_ticket(project, rec["ref"], actor)
        return {**rec, "created": created}

    def send_ticket(self, project: str, ref: str, actor: str) -> dict:
        rec = self.get(project, ref, "ticket")
        p = dict(rec["payload"])
        if rec["status"] == "sent":
            return rec
        cfg = self.config()["jira"]
        if not (self.s.integrations_live and cfg["configured"]):
            p["last_error"] = "dry run: " + ("INTEGRATIONS_LIVE=0" if not self.s.integrations_live else "Jira is not configured")
            return self.db.rec_update(project, ref, actor, p, "dry_run", event="ticket:dry_run")
        p["attempts"] = p.get("attempts", 0) + 1
        try:
            res = jira.create_issue(self.s.jira_base_url, self.s.jira_user, self.s.jira_api_token, p["request"])
            p.update(external_key=res["key"], external_url=res["url"], last_error="")
            out = self.db.rec_update(project, ref, actor, p, "sent", event="ticket:sent")
            self.audit(actor, "ticket.sent", project, ref, {"jira": res["key"]})
            return out
        except jira.JiraError as e:
            p["last_error"] = str(e)[:300]
            return self.db.rec_update(project, ref, actor, p, "failed", event="ticket:failed")

    def tickets(self, project: str) -> list[dict]:
        return list(reversed(self.list(project, "ticket")))

    # ================================================================ ReqIF
    def export_reqif(self, project: str, actor: str) -> tuple[str, str]:
        recs = [r for m, k, _ in reqif.EXPORT_KINDS for r in self.db.rec_list(project, m, k) if r["status"] != "rejected"]
        if not recs:
            raise Conflict("Nothing to export: no safety, cybersecurity or ADAS requirements in this project")
        body = reqif.export(project, recs, self.db.links(project))
        self.audit(actor, "reqif.export", project, None, {"requirements": len(recs)})
        return body, f"{project}_requirements.reqif"

    def external(self, project: str, status: str | None = None) -> list[dict]:
        out = []
        for r in self.list(project, "extreq", status):
            out.append({**r, "links": self.trace.children(project, r["ref"], "satisfied_by")})
        return sorted(out, key=lambda r: r["payload"].get("position", 0))

    def _impact(self, project: str, xr: dict, reason: str, actor: str) -> list[str]:
        """Platform items linked to a changed customer requirement need re-review (one hop)."""
        hit = []
        for target in self.trace.children(project, xr["ref"], "satisfied_by"):
            rec = self.db.rec_get(project, target)
            if not rec or rec["status"] not in ("approved", "assessed", "proposed"):
                continue
            self.db.rec_update(project, target, actor, {**rec["payload"], "suspect_reason": reason}, "suspect", event="suspect")
            hit.append(target)
            if rec["module"] == "adas" and rec["kind"] == "requirement":
                hit += self.c.adas._invalidate(project, actor, reason, req_id=rec["payload"]["id"])
        return hit

    def import_reqif(self, project: str, filename: str, content: bytes, actor: str) -> dict:
        try:
            parsed = reqif.parse(content, filename)
        except reqif.ReqIFError as e:
            raise Invalid(f"ReqIF import: {e}") from e
        if not parsed["requirements"]:
            raise Invalid("ReqIF import: the file contains no spec objects")
        imports = self.list(project, "reqifimport")
        if any(i["payload"]["sha256"] == parsed["sha256"] for i in imports):
            raise Conflict("This exact file was already imported")
        existing = {r["payload"]["identifier"]: r for r in self.list(project, "extreq")}
        source = parsed["header"].get("title") or filename
        imp_ref = f"IMP-{len(imports) + 1:03d}"
        changes, impacted = [], []
        seen = set()
        for req in parsed["requirements"]:
            seen.add(req["identifier"])
            payload = {"title": req["title"], "identifier": req["identifier"], "key": req["key"], "text": req["text"],
                       "attributes": req["attributes"], "position": req["position"], "hash": req["hash"], "source": source,
                       "last_import": imp_ref}
            old = existing.get(req["identifier"])
            if old is None:
                rec, _ = self.create(project, "extreq", "XR", {**payload, "change": "new", "previous": None}, actor,
                                     key=req["identifier"])
                changes.append({"change": "new", "ref": rec["ref"], "key": req["key"], "title": req["title"]})
            elif old["payload"]["hash"] != req["hash"] or old["status"] == "rejected" and old["payload"].get("change") == "removed":
                prev = {"title": old["payload"]["title"], "text": old["payload"]["text"]}
                was = old["status"]
                status = "suspect" if was in ("approved", "suspect") else "proposed"
                self.db.rec_update(project, old["ref"], actor, {**old["payload"], **payload, "change": "modified", "previous": prev},
                                   status, event="reqif:modified")
                changes.append({"change": "modified", "ref": old["ref"], "key": req["key"], "title": req["title"],
                                "before": prev["text"], "after": req["text"]})
                if was in ("approved", "suspect"):
                    impacted += self._impact(project, old, f"Customer requirement {req['key'] or req['identifier']} changed ({imp_ref})", actor)
        for ident, old in existing.items():
            if ident not in seen and old["status"] != "rejected" and old["payload"].get("change") != "removed":
                was = old["status"]
                self.db.rec_update(project, old["ref"], actor, {**old["payload"], "change": "removed", "last_import": imp_ref},
                                   "suspect" if was == "approved" else "rejected", event="reqif:removed")
                changes.append({"change": "removed", "ref": old["ref"], "key": old["payload"]["key"], "title": old["payload"]["title"]})
                if was == "approved":
                    impacted += self._impact(project, old, f"Customer requirement {old['payload']['key'] or ident} removed ({imp_ref})", actor)
        summary = {c: sum(x["change"] == c for x in changes) for c in ("new", "modified", "removed")}
        summary["unchanged"] = len(parsed["requirements"]) - summary["new"] - summary["modified"]
        rec, _ = self.create(project, "reqifimport", "IMP", {
            "title": f"ReqIF import of '{source}'", "filename": filename[:120], "sha256": parsed["sha256"], "header": parsed["header"],
            "summary": summary, "changes": changes, "impacted": sorted(set(impacted))}, actor, status="approved", machine=False)
        self.audit(actor, "reqif.import", project, rec["ref"], {"source": source, **summary, "impacted": len(set(impacted))})
        return rec

    def imports(self, project: str) -> list[dict]:
        return list(reversed(self.list(project, "reqifimport")))

    def decide_external(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        rec = self.get(project, ref, "extreq")
        if rec["payload"].get("change") == "removed" and decision == "approved":
            # accepting a removal retires the requirement
            out = self.db.rec_update(project, ref, actor, {**rec["payload"], "decision_comment": comment}, "rejected",
                                     event="decision:removal-accepted")
            self.audit(actor, "decision", project, ref, {"decision": "removal accepted", "comment": comment})
            return out
        return self.decide(project, ref, actor, decision, comment)

    def link_external(self, project: str, xr_ref: str, target_ref: str, actor: str) -> dict:
        self.get(project, xr_ref, "extreq")
        target = self.db.rec_get(project, target_ref)
        if not target or target["module"] == "integration":
            raise Invalid(f"{target_ref} is not a platform record that can satisfy a customer requirement")
        self.db.link_add(project, xr_ref, target_ref, "satisfied_by", actor)
        self.audit(actor, "reqif.link", project, xr_ref, {"target": target_ref})
        return {"xr": xr_ref, "target": target_ref, "links": self.trace.children(project, xr_ref, "satisfied_by")}
