"""Program release readiness: one GO / NO-GO view across every discipline in the project.

Each check reuses the module's own rules (HARA/TARA/UDS completeness, the code-review gate, vulnerability
triage, the ADAS release, the traceability re-review worklist, the R155 mapping, the audit chain and imported
customer requirements). Nothing is decided here that a module does not already decide; this view only
collects it, names an owner and an action for every open item, and gives a verdict:

  NO-GO               at least one blocking item
  GO WITH CONDITIONS  only warnings (documented, accepted by the release board)
  GO                  every applicable check passed
  NOT ASSESSED        no engineering check applies yet (an empty project is not releasable)

The verdict supports a release board; it does not replace its decision."""
from __future__ import annotations

from app.core import tracequery
from app.core.db import now

BLOCK, WARN, PASS, NA = "block", "warn", "pass", "n/a"
MAX_ITEMS = 25


def _check(cid: str, title: str, module: str, owner: str) -> dict:
    return {"id": cid, "title": title, "module": module, "owner": owner, "status": PASS, "summary": "",
            "items": [], "count": 0, "action": ""}


def _add(chk: dict, status: str, items: list[dict], summary: str, action: str) -> None:
    order = {NA: 0, PASS: 1, WARN: 2, BLOCK: 3}
    if order[status] > order[chk["status"]]:
        chk["status"] = status
    chk["items"] = (chk["items"] + items)[:MAX_ITEMS]
    chk["count"] += len(items)
    chk["summary"] = (chk["summary"] + "; " if chk["summary"] else "") + summary
    if action and action not in chk["action"]:
        chk["action"] = (chk["action"] + " " if chk["action"] else "") + action


def _completeness(chk: dict, scope: str, comp: dict, warn_ids: set[str], action: str) -> None:
    failing = [c for c in comp["checks"] if not c["passed"]]
    if not failing:
        chk["summary"] = (chk["summary"] + "; " if chk["summary"] else "") + f"{scope}: complete"
        return
    for c in failing:
        items = [{"ref": str(r), "text": c["description"]} for r in c["failing"]]
        _add(chk, WARN if c["id"] in warn_ids else BLOCK, items,
             f"{scope}: {len(c['failing'])} open for '{c['description']}'", action)


class ReadinessService:
    def __init__(self, container):
        self.c = container

    # ---------------------------------------------------------------- checks
    def _hara(self, p: str) -> dict:
        chk = _check("RR01", "Functional safety (HARA, ISO 26262)", "hara", "Functional safety engineer")
        items = self.c.hara.items(p)
        if not items:
            chk.update(status=NA, summary="No HARA item in this project")
        for it in items:
            _completeness(chk, it["ref"], self.c.hara.completeness(p, it["ref"]), set(),
                          "Complete and approve the HARA chain (hazards, events, goals, requirements) on the HARA page.")
        return chk

    def _tara(self, p: str) -> dict:
        chk = _check("RR02", "Cybersecurity (TARA, ISO/SAE 21434)", "tara", "Cybersecurity engineer")
        systems = self.c.tara.systems(p)
        if not systems:
            chk.update(status=NA, summary="No TARA system in this project")
        for s in systems:
            _completeness(chk, s["ref"], self.c.tara.completeness(p, s["ref"]), {"library_current", "coverage_complete"},
                          "Assess, treat and approve the threat scenarios and goals on the TARA page.")
        return chk

    def _uds(self, p: str) -> dict:
        chk = _check("RR03", "Diagnostics (UDS)", "uds", "Diagnostics engineer")
        specs = self.c.uds.specs(p)
        if not specs:
            chk.update(status=NA, summary="No diagnostic specification in this project")
        for s in specs:
            _completeness(chk, s["ref"], self.c.uds.completeness(p, s["ref"]), {"elements_covered", "nrcs_exercised"},
                          "Approve the specification and review the generated tests on the UDS page.")
        return chk

    def _code(self, p: str) -> dict:
        chk = _check("RR04", "Secure code review gate", "codereview", "Software engineer")
        if not self.c.codereview.sources(p):
            chk.update(status=NA, summary="No source code analysed in this project")
            return chk
        g = self.c.codereview.gate(p, "high")
        if g["passed"]:
            chk["summary"] = "No open high or critical findings"
        else:
            _add(chk, BLOCK, [{"ref": b["ref"], "text": f"{b['severity']}: {b['message']} ({b['file']}:{b['line']})"} for b in g["blockers"]],
                 f"{len(g['blockers'])} open high/critical findings", "Fix or triage them on the Secure Code Review page.")
        return chk

    def _vuln(self, p: str) -> dict:
        chk = _check("RR05", "Vulnerability management (SBOM monitoring)", "vuln", "Product security engineer")
        m = self.c.vuln.metrics(p)
        if not m["sboms"]:
            chk.update(status=NA, summary="No ECU SBOM in this project")
            return chk
        if not m["feed"].get("loaded"):
            _add(chk, BLOCK, [], "no advisory feed loaded, so nothing is monitored", "Load a current OSV feed.")
        elif m["feed"].get("stale"):
            _add(chk, WARN, [], f"advisory feed is {m['feed'].get('age_days')} days old", "Load a current OSV feed.")
        open_ = [f for f in self.c.vuln.findings(p) if f["status"] in ("proposed", "assessed", "approved")]
        hi = [f for f in open_ if f["payload"]["severity"] in ("critical", "high")]
        lo = [f for f in open_ if f not in hi]
        state = {"proposed": "not triaged", "assessed": "awaiting second person", "approved": "exploitable, not fixed"}
        if hi:
            _add(chk, BLOCK, [{"ref": f["ref"], "text": f"{f['payload']['severity']}: {f['payload']['title']}, {state[f['status']]}"} for f in hi],
                 f"{len(hi)} open critical/high vulnerabilities", "Triage them; ship a fix by OTA for exploitable ones.")
        if lo:
            _add(chk, WARN, [{"ref": f["ref"], "text": f"{f['payload']['severity']}: {f['payload']['title']}, {state[f['status']]}"} for f in lo],
                 f"{len(lo)} open medium/low vulnerabilities", "Triage the medium/low findings.")
        if not chk["summary"]:
            chk["summary"] = f"{m['findings']} findings, none open"
        return chk

    def _ota(self, p: str) -> dict:
        chk = _check("RR06", "Software updates (OTA, UN R156)", "ota", "Release manager")
        keys, pkgs = self.c.ota.keys(p), self.c.ota.packages(p)
        if not keys and not pkgs:
            chk.update(status=NA, summary="No OTA keys or packages in this project")
            return chk
        if not any(k["status"] == "approved" for k in keys):
            _add(chk, WARN, [], "no active signing key", "Register a key and have a second person approve it.")
        failing = [k for k in pkgs if k["status"] == "proposed" and k["payload"].get("verdict") == "fail"]
        waiting = [k for k in pkgs if k["status"] == "proposed" and k["payload"].get("verdict") != "fail"]
        if waiting:
            _add(chk, WARN, [{"ref": k["ref"], "text": f"{k['payload']['title']}: verified, not released"} for k in waiting],
                 f"{len(waiting)} verified update package(s) not yet released", "Release or reject them (second person).")
        if failing:
            _add(chk, WARN, [{"ref": k["ref"], "text": f"{k['payload']['title']}: failed verification"} for k in failing],
                 f"{len(failing)} package(s) failed verification", "Reject them.")
        if not chk["summary"]:
            chk["summary"] = f"{sum(k['status'] == 'approved' for k in keys)} active key(s), {sum(k['status'] == 'approved' for k in pkgs)} released package(s)"
        return chk

    def _adas(self, p: str) -> dict:
        chk = _check("RR07", "ADAS virtual validation (AEB)", "adas", "ADAS validation engineer")
        st = self.c.adas.status(p)
        if not st["library_loaded"]:
            chk.update(status=NA, summary="ADAS validation not used in this project")
            return chk
        rel = st["current_release"]
        suspect = [c for c in self.c.adas.campaigns(p) if c["status"] == "suspect"]
        if rel and not rel["stale"]:
            chk["summary"] = f"{rel['ref']} released by {rel['approved_by']} (firmware {rel['firmware']})"
        elif rel:
            _add(chk, BLOCK, [{"ref": rel["ref"], "text": "; ".join(rel["stale"])}], f"released evidence {rel['ref']} is out of date",
                 "Run a new SIL campaign and release it (second person).")
        else:
            _add(chk, BLOCK, [{"ref": c["ref"], "text": c["suspect_reason"]} for c in suspect],
                 "no valid ADAS release" + (f" ({len(suspect)} released campaign(s) became suspect)" if suspect else ""),
                 "Run a SIL campaign that passes and have a second person release it.")
        waiting = [f"{n} {what}" for n, what in ((st["requirements"]["proposed"], "requirement(s)"), (st["scenarios"]["proposed"], "scenario(s)")) if n]
        if waiting:
            _add(chk, WARN, [], f"{' and '.join(waiting)} await review", "Review the proposals on the ADAS page.")
        return chk

    def _trace(self, p: str) -> dict:
        chk = _check("RR08", "Traceability and change impact", "trace", "Project lead")
        sus = tracequery.suspects(self.c.db, p)
        if sus:
            _add(chk, BLOCK, [{"ref": n["ref"], "text": f"{n['module']}/{n['kind']}: {n.get('suspect_reason') or 'upstream changed'}"} for n in sus],
                 f"{len(sus)} item(s) need re-review after upstream changes", "Work through the Traceability re-review worklist.")
        else:
            chk["summary"] = "Nothing waiting for re-review"
        return chk

    def _r155(self, p: str) -> dict:
        chk = _check("RR09", "UN R155 evidence (CSMS)", "compliance", "Cybersecurity manager")
        if not self.c.tara.systems(p):
            chk.update(status=NA, summary="No TARA system, so no R155 mapping")
            return chk
        gaps = [g for g in self.c.compliance.r155(p)["categories"] if g["status"] == "gap"]
        if gaps:
            _add(chk, WARN, [{"ref": g["id"], "text": g["title"]} for g in gaps], f"{len(gaps)} Annex 5 group(s) without evidence",
                 "Add threats/controls or document why the group does not apply.")
        else:
            chk["summary"] = "Every Annex 5 group has some evidence or is organisational"
        return chk

    def _audit(self, p: str) -> dict:
        chk = _check("RR10", "Audit trail integrity", "audit", "Platform administrator")
        v = self.c.db.verify_audit()
        if v["ok"]:
            chk["summary"] = f"Hash chain verified ({v['chained_rows']} entries)"
        else:
            _add(chk, BLOCK, [{"ref": f"audit#{v['first_bad_id'] or (v['missing_ids'] or ['?'])[0]}", "text": "chain broken"}],
                 "audit chain broken: records may have been altered", "Investigate before any release (Administration page).")
        return chk

    def _external(self, p: str) -> dict:
        chk = _check("RR11", "Customer requirements (ReqIF exchange)", "integration", "Requirements engineer")
        xr = [r for r in self.c.db.rec_list(p, "integration", "extreq") if r["status"] != "rejected"]
        if not xr:
            chk.update(status=NA, summary="No imported customer requirements")
            return chk
        changed = [r for r in xr if r["status"] == "suspect"]
        new = [r for r in xr if r["status"] == "proposed"]
        unlinked = [r for r in xr if r["status"] == "approved" and not self.c.adas.trace.children(p, r["ref"])]
        if changed:
            _add(chk, BLOCK, [{"ref": r["ref"], "text": f"{r['payload']['identifier']}: {r['payload'].get('change', 'changed')} by the customer"} for r in changed],
                 f"{len(changed)} customer requirement(s) changed since they were accepted", "Review the change and its impact, then accept it again.")
        if new:
            _add(chk, WARN, [{"ref": r["ref"], "text": r["payload"]["title"]} for r in new], f"{len(new)} imported requirement(s) not yet reviewed",
                 "Accept or reject them.")
        if unlinked:
            _add(chk, WARN, [{"ref": r["ref"], "text": r["payload"]["title"]} for r in unlinked], f"{len(unlinked)} accepted requirement(s) not linked to platform items",
                 "Link them to the requirements or tests that implement them.")
        if not chk["summary"]:
            chk["summary"] = f"{len(xr)} customer requirement(s) accepted and linked"
        return chk

    def _field(self, p: str) -> dict:
        from app.modules.field.investigate import MALFUNCTION
        chk = _check("RR12", "Field issues (in-service monitoring)", "field", "Field quality engineer")
        iss = self.c.field.issues(p)
        if not iss:
            chk.update(status=NA, summary="No field complaints in this project")
            return chk
        open_ = [i for i in iss if i["status"] not in ("approved", "rejected")]
        malf = [i for i in open_ if i["class"] in MALFUNCTION or i.get("escalate")]
        pending = [i for i in open_ if i not in malf and not i["class"]]
        other = [i for i in open_ if i not in malf and i["class"]]
        item = lambda i: {"ref": i["ref"], "text": f"{i['title']}: {i['class_title'] or 'not investigated'}"  # noqa: E731
                                                  + (f" (ASIL {i['asil']})" if i.get("asil") else "")}
        if malf:
            _add(chk, BLOCK, [item(i) for i in malf], f"{len(malf)} open field issue(s) with a malfunction in vehicles",
                 "Finish the investigation, verify and release the fix, close the issue (second person).")
        if pending:
            _add(chk, WARN, [item(i) for i in pending], f"{len(pending)} field issue(s) not yet investigated", "Run the field investigation.")
        if other:
            _add(chk, WARN, [item(i) for i in other], f"{len(other)} field issue(s) without a defect awaiting closure",
                 "Answer the customer and close the issue.")
        if not chk["summary"]:
            chk["summary"] = f"{len(iss)} field issue(s), all closed"
        return chk

    # ---------------------------------------------------------------- verdict
    def evaluate(self, project: str) -> dict:
        checks = [fn(project) for fn in (self._hara, self._tara, self._uds, self._code, self._vuln, self._ota, self._adas,
                                         self._trace, self._r155, self._audit, self._external, self._field)]
        blocks = [c for c in checks if c["status"] == BLOCK]
        warns = [c for c in checks if c["status"] == WARN]
        engineering = [c for c in checks if c["id"] not in ("RR08", "RR10")]
        if all(c["status"] == NA for c in engineering):
            verdict = "NOT ASSESSED"                       # nothing to judge is not the same as nothing wrong
        else:
            verdict = "NO-GO" if blocks else "GO WITH CONDITIONS" if warns else "GO"
        return {"project": project, "generated_at": now(), "verdict": verdict,
                "counts": {s: sum(c["status"] == s for c in checks) for s in (BLOCK, WARN, PASS, NA)},
                "blocking": [c["id"] for c in blocks], "warnings": [c["id"] for c in warns], "checks": checks,
                "note": "Supports the release board; it does not replace its decision."}

    def report(self, project: str, fmt: str = "md") -> tuple[str, str]:
        import json
        r = self.evaluate(project)
        if fmt == "json":
            return json.dumps(r, indent=2), "application/json"
        icon = {BLOCK: "❌ block", WARN: "⚠️ warn", PASS: "✅ pass", NA: "– n/a"}
        L = [f"# Release readiness: {project}", "", f"Generated {r['generated_at'][:16]} UTC. **Verdict: {r['verdict']}** "
             f"({r['counts']['block']} blocking, {r['counts']['warn']} with warnings, {r['counts']['pass']} passed, "
             f"{r['counts']['n/a']} not applicable).", "", "| Check | Status | Summary | Owner | Action |", "|---|---|---|---|---|"]
        cell = lambda t: str(t).replace("|", "\\|").replace("\n", " ")  # noqa: E731
        for c in r["checks"]:
            L.append(f"| {c['id']} {cell(c['title'])} | {icon[c['status']]} | {cell(c['summary'])} | {cell(c['owner'])} | {cell(c['action'])} |")
        for c in r["checks"]:
            if c["items"]:
                L += ["", f"## {c['id']} {c['title']}: open items ({c['count']})", ""]
                L += [f"- `{i['ref']}` {i['text']}" for i in c["items"]]
                if c["count"] > len(c["items"]):
                    L.append(f"- … and {c['count'] - len(c['items'])} more")
        L += ["", r["note"], ""]
        return "\n".join(L), "text/markdown"
