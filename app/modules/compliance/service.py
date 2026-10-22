"""CSMS / SUMS evidence pack: pulls TARA, vulnerability monitoring, OTA update management and
traceability into one report organised by the UN R155 Annex 5 threat groups.

It shows what the records in this project demonstrate and where the gaps are. It does not decide
compliance: type approval is granted by an approval authority after assessing the manufacturer's
CSMS/SUMS and the vehicle type."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.core import tracequery
from app.modules.compliance.r155 import CATEGORIES, map_threats

DONE = ("approved",)
ASSESSED = ("assessed", "approved", "suspect")


class ComplianceService:
    def __init__(self, container):
        self.c, self.db = container, container.db

    # ------------------------------------------------------------------ R155 coverage
    def r155(self, project: str, selftest: dict | None = None) -> dict:
        threats = self.db.rec_list(project, "tara", "threat")
        assets = {a["ref"]: a["payload"] for a in self.db.rec_list(project, "tara", "asset")}
        by_cat = map_threats(threats, assets)
        vm = self.c.vuln.metrics(project)
        keys = self.c.ota.keys(project)
        pkgs = self.c.ota.list(project, "package")
        code_open = [f for f in self.db.rec_list(project, "codereview", "finding") if f["status"] in ("proposed", "assessed", "approved")]
        rows = []
        for cat in CATEGORIES:
            ts = by_cat[cat["id"]]
            n, assessed = len(ts), sum(t["status"] in ASSESSED for t in ts)
            approved, suspect = sum(t["status"] in DONE for t in ts), sum(t["status"] == "suspect" for t in ts)
            evidence, gaps = [], []
            if n:
                evidence.append(f"{n} TARA threat scenarios ({assessed} assessed, {approved} approved)")
            if suspect:
                gaps.append(f"{suspect} threat assessments are suspect after an upstream change")
            if cat["id"] == "R155-3":
                active = [k for k in keys if k["status"] == "approved"]
                evidence.append(f"{len(active)} active OTA signing key(s); {len(pkgs)} package(s) verified, "
                                f"{sum(p['status'] == 'approved' for p in pkgs)} released")
                if selftest:
                    evidence.append(f"OTA verifier self-test: {selftest['detected']}/{selftest['attacks']} attack scenarios "
                                    f"detected, genuine package {'passes' if selftest['genuine_passes'] else 'FAILS'}")
                if not active:
                    gaps.append("No active signing key: update authenticity cannot be verified")
            if cat["id"] == "R155-7":
                feed = vm["feed"]
                evidence.append(f"SBOM for {vm['ecus_with_sbom']} ECU(s); advisory feed "
                                f"{'loaded (' + str(feed.get('advisories')) + ' advisories, ' + str(feed.get('age_days')) + ' days old)' if feed.get('loaded') else 'NOT loaded'}")
                evidence.append(f"{vm['findings']} vulnerability findings: {vm['open']} open "
                                f"({vm['open_by_severity']['critical']} critical, {vm['open_by_severity']['high']} high), "
                                f"{vm['resolved']} resolved")
                evidence.append(f"{len(code_open)} open secure-code-review findings")
                if not feed.get("loaded") or feed.get("stale"):
                    gaps.append("Advisory feed missing or older than 7 days")
                if vm["untriaged"]:
                    gaps.append(f"{vm['untriaged']} vulnerability findings not triaged yet")
            if cat["id"] == "R155-4":
                status = "organisational"
                gaps.append("Mainly addressed by organisational measures (training, procedures, access control); "
                            "not assessable from engineering records")
            elif cat["id"] == "R155-7":
                status = "monitored" if vm["ecus_with_sbom"] and vm["feed"].get("loaded") and not vm["feed"].get("stale") else "gap"
            elif not n:
                status = "gap"
                gaps.append("No TARA threat scenario maps to this group: extend the architecture or the threat library")
            elif approved == n:
                status = "addressed"
            elif assessed:
                status = "in_progress"
            else:
                status = "identified"
            if cat["id"] == "R155-3" and status == "addressed" and not any(k["status"] == "approved" for k in keys):
                status = "in_progress"
            rows.append({**cat, "status": status, "threats": [t["ref"] for t in ts], "evidence": evidence, "gaps": gaps})
        return {"categories": rows, "summary": {s: sum(r["status"] == s for r in rows) for s in
                                                ("addressed", "in_progress", "identified", "monitored", "gap", "organisational")}}

    # ------------------------------------------------------------------ evidence pack
    def evidence(self, project: str, actor: str) -> dict:
        st = self.c.ota.self_test(project, actor)
        cov = self.r155(project, st)
        vm = self.c.vuln.metrics(project)
        ecus = self.c.vuln.ecus(project)
        exploitable = [f for f in self.c.vuln.findings(project) if f["status"] == "approved"]
        untriaged = [f for f in self.c.vuln.findings(project) if f["status"] == "proposed"
                     and f["payload"]["severity"] in ("critical", "high")]
        keys = self.c.ota.keys(project)
        pkgs = self.c.ota.list(project, "package")
        suspects = tracequery.suspects(self.db, project)
        actions = []
        actions += [f"Triage {f['ref']}: {f['payload']['title']} ({f['payload']['severity']})" for f in untriaged]
        actions += [f"Remediate {f['ref']}: {f['payload']['title']} (fixed in {', '.join(f['payload']['fixed_in']) or 'n/a'})" for f in exploitable]
        actions += [f"Fix or reject package {p['ref']} ({p['payload']['title']}): failed {', '.join(p['payload']['failed'])}"
                    for p in pkgs if p["status"] == "proposed" and p["payload"]["verdict"] == "fail"]
        actions += [f"Re-review {s['ref']} ({s['module']} {s['kind']}): {s.get('suspect_reason') or 'upstream change'}" for s in suspects[:30]]
        actions += [f"{c['id']} {c['title']}: {g}" for c in cov["categories"] for g in c["gaps"] if c["status"] != "organisational"]
        out = {
            "project": project, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "generated_by": actor,
            "r155": cov, "vulnerability_monitoring": {"metrics": vm, "ecus": ecus,
                                                      "exploitable": [{"ref": f["ref"], **{k: f["payload"][k] for k in ("title", "severity", "score", "fixed_in")}} for f in exploitable]},
            "software_updates": {
                "keys": [{"ref": k["ref"], "key_id": k["payload"]["key_id"], "owner": k["payload"]["owner"], "status": k["status"],
                          "fingerprint": k["payload"]["fingerprint"], "valid_until": k["payload"]["valid_until"],
                          "approved_by": k.get("approved_by"), "revoked_reason": k["payload"].get("revoked_reason")} for k in keys],
                "packages": [{"ref": p["ref"], "title": p["payload"]["title"], "package_id": p["payload"]["package_id"],
                              "verdict": p["payload"]["verdict"], "failed": p["payload"]["failed"], "status": p["status"],
                              "uploaded_by": p["payload"].get("uploaded_by"), "released_by": p["payload"].get("released_by"),
                              "released_at": p["payload"].get("released_at")} for p in pkgs],
                "rxswin": {e["ecu"]: e["rxswin"] for e in ecus},
                "self_test": {k: st[k] for k in ("genuine_passes", "attacks", "detected", "all_ok")} | {"rows": st["rows"]},
            },
            "suspect_items": len(suspects), "actions": actions,
        }
        self.db.audit(actor, "compliance.evidence", project, None, {"actions": len(actions), "summary": cov["summary"]})
        return out

    def render(self, ev: dict, fmt: str) -> tuple[str, str]:
        if fmt == "json":
            return json.dumps(ev, indent=1, default=str), "application/json"
        vm, su = ev["vulnerability_monitoring"]["metrics"], ev["software_updates"]
        L = [f"# Cybersecurity and software-update evidence pack: {ev['project']}", "",
             f"Generated {ev['generated_at']} by {ev['generated_by']}. Organised by the threat groups of UN R155 Annex 5 "
             "(India's draft AIS-189 follows the same UN framework; software updates: UN R156 / draft AIS-190).", "",
             "> This pack summarises what the engineering records in this project show. It supports the preparation of "
             "CSMS/SUMS evidence; it is not a compliance statement. Type approval requires assessment by an approval "
             "authority or technical service.", "", "## 1. Coverage by UN R155 Annex 5 threat group", "",
             "| Group | Status | Evidence | Gaps |", "|---|---|---|---|"]
        for c in ev["r155"]["categories"]:
            L.append(f"| {c['id']} {c['title']} | **{c['status']}** | {'<br>'.join(c['evidence']) or '-'} | {'<br>'.join(c['gaps']) or '-'} |")
        L += ["", "## 2. Vulnerability monitoring", "",
              f"- Advisory feed: {vm['feed'].get('name', 'not loaded')} · {vm['feed'].get('advisories', 0)} advisories · "
              f"loaded {vm['feed'].get('loaded_at', '-')}",
              f"- Findings: {vm['findings']} total, {vm['open']} open, {vm['exploitable']} exploitable, {vm['resolved']} resolved, "
              f"{vm['awaiting_second_person']} awaiting a second reviewer · mean time to triage: {vm['mean_hours_to_triage'] or '-'} h", "",
              "| ECU | Software | Hardware | RXSWIN | Components | Open findings |", "|---|---|---|---|---|---|"]
        for e in ev["vulnerability_monitoring"]["ecus"]:
            sev = ", ".join(f"{v} {k}" for k, v in e["by_severity"].items() if v) or "none"
            L.append(f"| {e['ecu']} | {e['ecu_version']} | {e['hardware_id'] or '-'} | {e['rxswin'] or '-'} | {e['components']} | {sev} |")
        L += ["", "## 3. Software update management", "", "| Key | Owner | Status | Fingerprint (SHA-256) | Valid until |", "|---|---|---|---|---|"]
        for k in su["keys"]:
            L.append(f"| {k['key_id']} | {k['owner']} | {k['status']}{' (' + k['revoked_reason'] + ')' if k.get('revoked_reason') else ''} | "
                     f"`{k['fingerprint'][:32]}...` | {k['valid_until'] or '-'} |")
        L += ["", "| Package | Update | Verdict | Status | Uploaded by | Released by |", "|---|---|---|---|---|---|"]
        for p in su["packages"]:
            L.append(f"| {p['ref']} | {p['title']} | {p['verdict']}{' (' + ', '.join(p['failed']) + ')' if p['failed'] else ''} | "
                     f"{p['status']} | {p['uploaded_by'] or '-'} | {p['released_by'] or '-'} |")
        st = su["self_test"]
        L += ["", f"### OTA verifier self-test: {st['detected']}/{st['attacks']} attacks detected; genuine package "
              f"{'passes' if st['genuine_passes'] else 'FAILS'}", "", "| Attack | Caught by | Result |", "|---|---|---|"]
        for r in st["rows"][1:]:
            L.append(f"| {r['attack']} | {r['expected_check']} | {'detected' if r['detected'] else 'NOT DETECTED'} |")
        L += ["", f"## 4. Open actions ({len(ev['actions'])})", ""] + [f"- {a}" for a in ev["actions"]] + [""]
        return "\n".join(L), "text/markdown"
