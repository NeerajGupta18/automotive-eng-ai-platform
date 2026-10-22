"""Vulnerability monitoring for ECU software (ISO/SAE 21434 continual cybersecurity activities;
UN R155 / AIS-189 expect vulnerabilities to be monitored, analysed and handled after release).

Pipeline: CycloneDX SBOM per ECU release -> ECU baseline -> match against an OSV advisory feed ->
findings -> triage with VEX states (four-eyes for dismissing high/critical) -> exploitable findings
flag the TARA threats of that ECU for re-assessment -> a newer SBOM resolves what it fixed ->
CycloneDX VEX export.

Finding states (CycloneDX analysis.state) and record status:
  in_triage       proposed      new, nobody has looked at it yet
  exploitable     approved      confirmed: remediation needed, TARA threats of the ECU become suspect
  not_affected    rejected      needs a VEX justification; for high/critical a second person must agree
  false_positive  rejected      the match is wrong (e.g. different product with the same name)
  resolved        resolved      a newer SBOM of the ECU no longer contains the vulnerable version"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core.db import now
from app.core.errors import Conflict, Invalid
from app.core.workflow import Workflow
from app.modules.vuln import cvss
from app.modules.vuln.feed import parse_feed
from app.modules.vuln.sbom import parse_cyclonedx, purl_key
from app.modules.vuln.versions import fixed_versions, in_ranges, version_key

BUNDLED_FEED = Path(__file__).resolve().parents[3] / "data" / "library" / "vuln_feed_demo.json"
STATES = ("exploitable", "not_affected", "false_positive")
JUSTIFICATIONS = ("code_not_present", "code_not_reachable", "requires_configuration", "requires_dependency",
                  "requires_environment", "protected_by_compiler", "protected_at_runtime", "protected_at_perimeter",
                  "protected_by_mitigating_control")
RESPONSES = ("can_not_fix", "will_not_fix", "update", "rollback", "workaround_available")
OPEN = ("proposed", "assessed", "approved")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def match_components(components: list[dict], advisories: list[dict]) -> list[tuple[dict, dict]]:
    """(component, advisory) pairs where the component's version is inside an affected range.
    A component matches an affected package by purl (type/namespace/name) or, failing that, by name."""
    by_name: dict[str, list[tuple[dict, dict]]] = {}
    by_purl: dict[str, list[tuple[dict, dict]]] = {}
    for adv in advisories:
        for aff in adv["affected"]:
            if aff.get("purl_key"):
                by_purl.setdefault(aff["purl_key"], []).append((adv, aff))
            if aff.get("name"):
                by_name.setdefault(aff["name"], []).append((adv, aff))
    out, seen = [], set()
    for comp in components:
        if not comp.get("version"):
            continue
        pk = purl_key(comp["purl"]) if comp.get("purl") else None
        cands = by_purl.get(pk, []) if pk else []
        cands = cands + by_name.get(comp["name"].lower(), [])
        for adv, aff in cands:
            # a purl-qualified advisory entry only matches components with the same purl (no cross-product name clashes)
            if aff.get("purl_key") and pk and aff["purl_key"] != pk:
                continue
            key = (comp["bom_ref"], adv["id"])
            if key in seen:
                continue
            if in_ranges(comp["version"], aff["ranges"], aff["versions"]):
                seen.add(key)
                out.append((comp, adv))
    return out


class VulnService(Workflow):
    def __init__(self, container):
        super().__init__(container, "vuln")
        self._feed_cache: dict[tuple[str, str], list[dict]] = {}

    # ================================================================ advisory feed
    def _feed_path(self, project: str) -> Path:
        d = self.c.settings.data_dir / "vuln" / _safe(project)
        d.mkdir(parents=True, exist_ok=True)
        return d / "feed.json"

    def load_feed(self, project: str, actor: str, content: bytes | None = None, filename: str = "") -> dict:
        if content is None:
            content, filename = BUNDLED_FEED.read_bytes(), BUNDLED_FEED.name
        meta, advisories = parse_feed(content, filename or "feed.json")
        body = json.dumps({"meta": meta, "advisories": advisories}).encode()
        sha = hashlib.sha256(body).hexdigest()
        path = self._feed_path(project)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)
        info = {"name": meta.get("name") or filename, "version": meta.get("version", ""), "note": meta.get("note", ""),
                "advisories": len(advisories), "synthetic": sum(a["synthetic"] for a in advisories),
                "sha256": sha, "loaded_at": now(), "loaded_by": actor, "filename": filename,
                "newest_modified": max((a["modified"] for a in advisories if a["modified"]), default="")}
        rec, created = self.create(project, "feed", "FEED", info, actor, status="approved", key="current", machine=False)
        if not created:
            rec = self.db.rec_update(project, rec["ref"], actor, info, "approved", event="feed:reload")
        self.audit(actor, "feed.load", project, rec["ref"], {"advisories": len(advisories), "name": info["name"]})
        scan = self.scan(project, actor)
        return {"feed": info, "scan": scan}

    def advisories(self, project: str) -> list[dict]:
        path = self._feed_path(project)
        if not path.exists():
            return []
        body = path.read_bytes()
        key = (project, hashlib.sha256(body).hexdigest())
        if key not in self._feed_cache:
            self._feed_cache = {k: v for k, v in self._feed_cache.items() if k[0] != project}
            self._feed_cache[key] = json.loads(body)["advisories"]
        return self._feed_cache[key]

    def feed_status(self, project: str) -> dict:
        recs = self.list(project, "feed")
        if not recs:
            return {"loaded": False}
        p = recs[0]["payload"]
        age = None
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(p["loaded_at"])).days
        except (KeyError, ValueError):
            pass
        return {"loaded": True, **p, "age_days": age, "stale": age is not None and age > 7}

    # ================================================================ SBOMs and the ECU baseline
    def add_sbom(self, project: str, filename: str, content: bytes, actor: str, source: str = "upload") -> dict:
        s = parse_cyclonedx(content)
        payload = {**s, "title": f"{s['ecu']} {s['ecu_version']} SBOM", "filename": filename, "source": source}
        rec, created = self.create(project, "sbom", "SBOM", payload, actor, status="approved",
                                   key=f"{s['ecu']}@{s['ecu_version']}:{s['sha256'][:16]}", machine=False)
        if created:
            self.db.link_add(project, f"COMP:{s['ecu']}", rec["ref"], "described_by", actor)
            self.audit(actor, "sbom.add", project, rec["ref"], {"ecu": s["ecu"], "version": s["ecu_version"],
                                                                "components": s["component_count"], "source": source})
        scan = self.scan(project, actor, ecu=s["ecu"])
        return {"sbom": self._brief(rec), "created": created, "warnings": s["warnings"], "scan": scan}

    @staticmethod
    def _brief(rec: dict) -> dict:
        p = rec["payload"]
        return {"ref": rec["ref"], "ecu": p["ecu"], "ecu_version": p["ecu_version"], "hardware_id": p["hardware_id"],
                "rxswin": p["rxswin"], "components": p["component_count"], "filename": p["filename"],
                "source": p.get("source", "upload"), "created_at": rec["created_at"], "created_by": rec["created_by"]}

    def sboms(self, project: str) -> list[dict]:
        return [self._brief(r) for r in self.list(project, "sbom")]

    def sbom(self, project: str, ref: str) -> dict:
        return self.get(project, ref, "sbom")

    def baseline(self, project: str) -> dict[str, dict]:
        """Current software of each ECU = the SBOM with the highest version (latest upload breaks ties)."""
        cur: dict[str, dict] = {}
        for r in self.list(project, "sbom"):
            p = r["payload"]
            old = cur.get(p["ecu"])
            if old is None or version_key(p["ecu_version"]) >= version_key(old["payload"]["ecu_version"]):
                cur[p["ecu"]] = r
        return cur

    def ecus(self, project: str) -> list[dict]:
        out = []
        findings = self.list(project, "vfinding")
        for ecu, r in sorted(self.baseline(project).items()):
            mine = [f for f in findings if f["payload"]["ecu"] == ecu and f["status"] in OPEN]
            sev = {b: sum(f["payload"]["severity"] == b for f in mine) for b in ("critical", "high", "medium", "low", "unknown")}
            out.append({**self._brief(r), "open_findings": len(mine), "by_severity": sev})
        return out

    # ================================================================ scanning
    def scan(self, project: str, actor: str, ecu: str | None = None) -> dict:
        advisories = self.advisories(project)
        base = self.baseline(project)
        new = updated = resolved = 0
        if not advisories:
            return {"new": 0, "resolved": 0, "updated": 0, "note": "No advisory feed loaded"}
        for name, srec in base.items():
            if ecu and name != ecu:
                continue
            p = srec["payload"]
            current = set()
            for comp, adv in match_components(p["components"], advisories):
                key = f"{name}:{comp['name'].lower()}@{comp['version']}:{adv['id']}"
                current.add(key)
                sev = adv["severity"]
                payload = {
                    "title": f"{adv['id']} in {comp['name']} {comp['version']} ({name})",
                    "ecu": name, "ecu_version": p["ecu_version"], "sbom": srec["ref"], "component": comp["name"],
                    "component_version": comp["version"], "purl": comp.get("purl", ""), "bom_ref": comp["bom_ref"],
                    "advisory": adv["id"], "aliases": adv["aliases"], "summary": adv["summary"],
                    "severity": sev["band"], "score": sev["score"], "vector": sev["vector"],
                    "fixed_in": fixed_versions([r for a in adv["affected"] for r in a["ranges"]]),
                    "references": adv["references"], "synthetic": adv["synthetic"],
                    "state": "in_triage", "justification": None, "response": [], "detail": "", "pending": None,
                    "found_at": now(),
                }
                rec, created = self.create(project, "vfinding", "VF", payload, actor, key=key)
                if created:
                    new += 1
                    self.db.link_add(project, srec["ref"], rec["ref"], "has_vulnerability", actor)
                    self.db.link_add(project, f"COMP:{name}", rec["ref"], "has_vulnerability", actor)
                elif rec["status"] == "resolved":                 # vulnerable version came back
                    self.db.rec_update(project, rec["ref"], actor, {**rec["payload"], "state": "in_triage",
                                       "resolved_by": None}, "proposed", event="reintroduced")
                    updated += 1
                elif (rec["payload"].get("score"), rec["payload"].get("severity")) != (sev["score"], sev["band"]):
                    self.db.rec_update(project, rec["ref"], actor, {**rec["payload"], "severity": sev["band"],
                                       "score": sev["score"], "vector": sev["vector"]}, None, event="rescored")
                    updated += 1
            for f in self.list(project, "vfinding"):
                fp = f["payload"]
                if fp["ecu"] != name or f["status"] == "resolved" or f["key"] in current:
                    continue
                note = (f"No longer present in {name} {p['ecu_version']} ({srec['ref']})")
                self.db.rec_update(project, f["ref"], actor, {**fp, "state": "resolved", "resolved_by": srec["ref"],
                                   "resolution": note}, "resolved", event="resolved")
                resolved += 1
        self.audit(actor, "scan", project, None, {"ecu": ecu, "new": new, "resolved": resolved, "updated": updated})
        return {"new": new, "resolved": resolved, "updated": updated, "advisories": len(advisories)}

    def preview(self, project: str, parsed_sbom: dict) -> list[dict]:
        """Findings an SBOM WOULD produce, without storing anything (used by the OTA SBOM-delta check)."""
        return [{"advisory": adv["id"], "component": comp["name"], "version": comp["version"],
                 "severity": adv["severity"]["band"], "score": adv["severity"]["score"]}
                for comp, adv in match_components(parsed_sbom["components"], self.advisories(project))]

    # ================================================================ findings and triage
    def findings(self, project: str, status: str | None = None, ecu: str | None = None,
                 severity: str | None = None) -> list[dict]:
        rows = self.list(project, "vfinding", status, ecu=ecu, severity=severity)
        return sorted(rows, key=lambda r: (-cvss.SEVERITY_RANK.get(r["payload"]["severity"], 0),
                                           -(r["payload"]["score"] or 0), r["ref"]))

    def triage(self, project: str, ref: str, state: str, actor: str, justification: str | None = None,
               response: list[str] | None = None, detail: str = "") -> dict:
        rec = self.get(project, ref, "vfinding")
        p = rec["payload"]
        if state not in STATES:
            raise Invalid(f"state must be one of {list(STATES)}")
        if rec["status"] != "proposed":
            raise Conflict(f"{ref} is {rec['status']}; reopen it first to change the analysis")
        detail = (detail or "").strip()
        response = [r for r in (response or []) if r]
        bad = [r for r in response if r not in RESPONSES]
        if bad:
            raise Invalid(f"Unknown response {bad}; allowed: {list(RESPONSES)}")
        if state == "not_affected":
            if justification not in JUSTIFICATIONS:
                raise Invalid(f"not_affected needs a VEX justification: one of {list(JUSTIFICATIONS)}")
            if len(detail) < 20:
                raise Invalid("Explain why the ECU is not affected (at least 20 characters)")
        elif state == "false_positive" and len(detail) < 10:
            raise Invalid("Explain why the match is wrong (at least 10 characters)")
        else:
            justification = None if state != "not_affected" else justification
        payload = {**p, "state": state, "justification": justification, "response": response, "detail": detail,
                   "triaged_by": actor, "triaged_at": now(), "pending": None}
        if state == "exploitable":
            status = "approved"
        elif cvss.SEVERITY_RANK.get(p["severity"], 0) >= cvss.SEVERITY_RANK["high"] or p["severity"] == "unknown":
            status, payload["pending"] = "assessed", state      # dismissing a high/critical needs a second person
        else:
            status = "rejected"
        out = self.db.rec_update(project, ref, actor, payload, status, event=f"triage:{state}",
                                 approve=status == "approved")
        self.audit(actor, "triage", project, ref, {"state": state, "status": status})
        if state == "exploitable":
            out = {**out, "tara_suspect": self._flag_tara(project, out, actor)}
        return out

    def approve_pending(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        rec = self.get(project, ref, "vfinding")
        p = rec["payload"]
        if rec["status"] != "assessed" or not p.get("pending"):
            raise Conflict("No decision is waiting for a second person")
        if rec["updated_by"] == actor:
            raise Conflict(f"Four-eyes rule: {actor} proposed this analysis and cannot approve it")
        if decision == "approved":
            out = self.db.rec_update(project, ref, actor, {**p, "pending": None, "second_reviewer": actor,
                                     "second_comment": comment}, "rejected", event=f"decision:{p['pending']}", approve=True)
        elif decision == "rejected":
            out = self.db.rec_update(project, ref, actor, {**p, "pending": None, "state": "in_triage",
                                     "justification": None, "second_comment": comment}, "proposed", event="decision:returned")
        else:
            raise Invalid("decision must be 'approved' or 'rejected'")
        self.audit(actor, "triage.second", project, ref, {"decision": decision})
        return out

    def reopen(self, project: str, ref: str, comment: str, actor: str) -> dict:
        rec = self.get(project, ref, "vfinding")
        if rec["status"] not in ("approved", "rejected", "assessed"):
            raise Conflict(f"{ref} is {rec['status']} and cannot be reopened")
        if len((comment or "").strip()) < 10:
            raise Invalid("Explain why the finding is reopened (min 10 characters)")
        out = self.db.rec_update(project, ref, actor, {**rec["payload"], "state": "in_triage", "pending": None,
                                 "justification": None, "detail": comment.strip()}, "proposed", event="reopened")
        self.audit(actor, "reopen", project, ref)
        return out

    def _flag_tara(self, project: str, finding: dict, actor: str) -> list[str]:
        """An exploitable vulnerability changes the attack feasibility of the ECU's threats:
        link it to them and mark assessed/approved TARA items (and their goals/requirements) suspect."""
        p = finding["payload"]
        threats = [t for t in self.db.rec_list(project, "tara", "threat") if t["payload"].get("component") == p["ecu"]]
        reason = (f"{finding['ref']}: {p['advisory']} ({p['severity']}, CVSS {p['score']}) is exploitable in "
                  f"{p['component']} {p['component_version']} on {p['ecu']}; re-assess attack feasibility")
        marked = []
        for t in threats:
            self.db.link_add(project, finding["ref"], t["ref"], "raises_feasibility_of", actor)
            if t["status"] in ("assessed", "approved"):          # only existing assessments become invalid
                self.db.rec_update(project, t["ref"], actor, {**t["payload"], "suspect_reason": reason}, "suspect",
                                   event="suspect")
                marked.append(t["ref"])
                marked += [d for d in self.suspect_downstream(project, t["ref"], actor, reason) if d not in marked]
        if marked:
            self.audit(actor, "tara.flagged", project, finding["ref"], {"affected": marked})
        return marked

    # ================================================================ VEX and metrics
    def vex(self, project: str, ecu: str | None = None) -> dict:
        state_map = {"proposed": "in_triage", "assessed": "in_triage", "approved": "exploitable", "resolved": "resolved"}
        vulns = []
        sboms = {r["ref"]: r["payload"] for r in self.list(project, "sbom")}
        for f in self.findings(project, ecu=ecu):
            p = f["payload"]
            state = p["state"] if f["status"] == "rejected" else state_map.get(f["status"], "in_triage")
            serial = sboms.get(p["sbom"], {}).get("serial", "")
            ref = (f"urn:cdx:{serial.removeprefix('urn:uuid:')}/1#{p['bom_ref']}" if serial else p["bom_ref"])
            analysis = {"state": state, "detail": p.get("detail") or p.get("resolution", "")}
            if state == "not_affected" and p.get("justification"):
                analysis["justification"] = p["justification"]
            if p.get("response"):
                analysis["response"] = p["response"]
            rating = {"severity": p["severity"] if p["severity"] in ("critical", "high", "medium", "low", "none") else "unknown"}
            if p.get("score") is not None:
                rating.update({"score": p["score"], "method": "CVSSv31" if p["vector"].startswith("CVSS:3.1") else "CVSSv3",
                               "vector": p["vector"]})
            vulns.append({"bom-ref": f["ref"], "id": p["advisory"], "source": {"name": "OSV"},
                          "references": [{"id": a, "source": {"name": "alias"}} for a in p["aliases"]],
                          "ratings": [rating], "description": p["summary"], "affects": [{"ref": ref}],
                          "analysis": analysis,
                          "properties": [{"name": "aeai:ecu", "value": p["ecu"]},
                                         {"name": "aeai:component", "value": f"{p['component']} {p['component_version']}"}]})
        self.audit("system", "vex.export", project, None, {"ecu": ecu, "vulnerabilities": len(vulns)})
        return {"bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{uuid.uuid4()}", "version": 1,
                "metadata": {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "tools": {"components": [{"type": "application", "name": "automotive-eng-ai"}]},
                             "properties": [{"name": "aeai:project", "value": project}] +
                                           ([{"name": "aeai:ecu", "value": ecu}] if ecu else [])},
                "vulnerabilities": vulns}

    def metrics(self, project: str) -> dict:
        fs = self.list(project, "vfinding")
        open_ = [f for f in fs if f["status"] in OPEN]
        triage_hours = []
        for f in fs:
            t = f["payload"].get("triaged_at")
            if t:
                try:
                    triage_hours.append((datetime.fromisoformat(t) - datetime.fromisoformat(f["created_at"])).total_seconds() / 3600)
                except ValueError:
                    pass
        return {
            "ecus_with_sbom": len(self.baseline(project)), "sboms": len(self.list(project, "sbom")),
            "findings": len(fs), "open": len(open_),
            "open_by_severity": {b: sum(f["payload"]["severity"] == b for f in open_) for b in ("critical", "high", "medium", "low", "unknown")},
            "untriaged": sum(f["status"] == "proposed" for f in fs),
            "exploitable": sum(f["status"] == "approved" for f in fs),
            "not_affected_or_fp": sum(f["status"] == "rejected" for f in fs),
            "awaiting_second_person": sum(f["status"] == "assessed" for f in fs),
            "resolved": sum(f["status"] == "resolved" for f in fs),
            "mean_hours_to_triage": round(sum(triage_hours) / len(triage_hours), 2) if triage_hours else None,
            "feed": self.feed_status(project),
        }
