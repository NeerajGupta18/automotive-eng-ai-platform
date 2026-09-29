"""Cybersecurity TARA assistant (ISO/SAE 21434 style).

Pipeline: architecture -> assets -> attack surface / trust boundaries -> threat scenarios (approved library)
-> guided impact + attack-potential assessment -> deterministic risk -> treatment -> cybersecurity goals
-> controls / requirements (with source, assumptions, alternatives) -> coverage checklist -> traceability.

Design rules: feasibility and risk are computed by rules, never by the LLM; suggestions come from a
versioned library with owner and review date (stale knowledge is flagged); approval is four-eyes;
approved items become 'suspect' when an upstream item changes; coverage gaps are reported for expert review."""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from pathlib import Path

from app.core.errors import Conflict, Invalid
from app.core.reqquality import check_requirement, has_errors
from app.core.workflow import Workflow
from app.modules.tara import architecture as arch_mod
from app.modules.tara import rules

BUNDLED_LIBRARY = Path(__file__).resolve().parents[3] / "data" / "library" / "tara_library.json"


class TARAService(Workflow):
    def __init__(self, container):
        super().__init__(container, "tara")
        self.today = date.today          # injectable for tests

    # ================================================================ library
    def load_library(self, project: str, actor: str, data: dict | None = None) -> dict:
        data = data or json.loads(BUNDLED_LIBRARY.read_text())
        pats, ctrls = data.get("patterns"), data.get("controls")
        if not pats or not ctrls:
            raise Invalid("Library needs non-empty 'patterns' and 'controls'")
        cids = {c.get("id") for c in ctrls}
        need = {"id", "name", "stride", "property", "asset_kind", "interface", "damage", "impact_hint", "ap_hint", "controls"}
        for p in pats:
            miss = need - set(p)
            if miss:
                raise Invalid(f"Pattern {p.get('id', '?')} lacks fields: {sorted(miss)}")
            if p["stride"] not in rules.STRIDE:
                raise Invalid(f"{p['id']}: stride must be one of {list(rules.STRIDE)}")
            rules.impact_rating(p["impact_hint"]); rules.attack_potential(p["ap_hint"])
            if set(p["controls"]) - cids:
                raise Invalid(f"{p['id']} references unknown controls {sorted(set(p['controls']) - cids)}")
        for c in ctrls:
            if not c.get("requirement_template") or not c.get("id"):
                raise Invalid("Every control needs 'id' and 'requirement_template'")
        rec, created = self.create(project, "library", "TLIB", data, actor, status="approved", key="tara-library")
        if not created:
            self.db.rec_update(project, rec["ref"], actor, data, "approved", event="reload")
        self.audit(actor, "library.load", project, rec["ref"], {"patterns": len(pats), "controls": len(ctrls)})
        return {"ref": rec["ref"], "patterns": len(pats), "controls": len(ctrls)}

    def _lib(self, project: str) -> dict:
        recs = self.list(project, "library")
        if not recs:
            raise Conflict("Load the threat library first (POST /tara/library/load)")
        return recs[0]["payload"]

    def _stale(self, entry: dict, lib: dict) -> bool:
        try:
            d = datetime.strptime(entry.get("review_date", ""), "%Y-%m-%d").date()
        except ValueError:
            return True                       # no valid review date = treat as stale
        return (self.today() - d).days > lib.get("stale_after_days", 365)

    def library_status(self, project: str) -> dict:
        if not self.list(project, "library"):
            return {"loaded": False, "patterns": [], "controls": []}
        lib = self._lib(project)
        return {"loaded": True, "name": lib.get("name"), "version": lib.get("version"),
                "patterns": [{**p, "stale": self._stale(p, lib)} for p in lib["patterns"]],
                "controls": [{**c, "stale": self._stale(c, lib)} for c in lib["controls"]]}

    # ================================================================ systems / architecture
    def create_system(self, project: str, name: str, description: str, architecture: dict | None, actor: str) -> dict:
        if not name.strip():
            raise Invalid("System name required")
        arch = arch_mod.normalize(architecture or {"components": [], "data_flows": []})
        rec, _ = self.create(project, "system", "SYS", {"name": name.strip(), "description": description,
                             "architecture": arch, "hld_doc_id": None, "origin": "manual"},
                             actor, status="approved", machine=False)
        self.audit(actor, "system.create", project, rec["ref"], {"components": len(arch["components"])})
        return rec

    def systems(self, project: str) -> list[dict]:
        return self.list(project, "system")

    def _sys(self, project: str, ref: str) -> dict:
        return self.get(project, ref, "system")

    def set_architecture(self, project: str, ref: str, architecture: dict, actor: str, *, merge: bool = False,
                         hld_doc_id: str | None = None) -> dict:
        rec = self._sys(project, ref)
        new = arch_mod.normalize(architecture) if not merge else arch_mod.merge(rec["payload"]["architecture"], architecture)
        payload = {**rec["payload"], "architecture": new}
        if hld_doc_id:
            payload["hld_doc_id"] = hld_doc_id
        out = self.db.rec_update(project, ref, actor, payload, "approved", event="architecture")
        self.audit(actor, "architecture.set", project, ref, {"components": len(new["components"]), "flows": len(new["data_flows"])})
        return out

    def import_comm_matrix(self, project: str, ref: str, csv_text: str, actor: str) -> dict:
        return self.set_architecture(project, ref, arch_mod.parse_comm_matrix(csv_text), actor, merge=True)

    def import_hld(self, project: str, ref: str, doc_id: str, actor: str) -> dict:
        d = self.db.document(doc_id)
        if not d or d["project"] != project:
            raise Invalid("doc_id does not belong to this project")
        return self.set_architecture(project, ref, arch_mod.from_hld(self.c.hld.extract(doc_id)), actor,
                                     merge=True, hld_doc_id=doc_id)

    def attack_surface(self, project: str, ref: str) -> dict:
        arch = self._sys(project, ref)["payload"]["architecture"]
        return {"surface": arch_mod.attack_surface(arch), "boundary_crossings": arch_mod.boundary_crossings(arch)}

    # ================================================================ analysis: assets + threats
    @staticmethod
    def _assets_from(arch: dict) -> list[dict]:
        out = []

        def add(kind, name, comp, comps, ifs, props, safety):
            out.append({"kind": kind, "name": name, "component": comp, "components": comps, "interfaces": ifs,
                        "properties": props, "safety_relevant": safety})

        for c in arch["components"]:
            n, ifs = c["name"], c["interfaces"]
            if c["type"] in ("ecu", "gateway", "telematics", "infotainment", "hsm") and ifs:
                add("firmware", f"{n} software", n, [n], ifs, ["integrity", "authenticity"], c["safety_relevant"])
            if c["security_relevant"] or c["type"] in ("gateway", "telematics", "hsm"):
                add("key_material", f"{n} cryptographic keys", n, [n], ifs, ["confidentiality", "integrity"], False)
            if "OBD" in ifs:
                add("diagnostic_access", f"{n} diagnostic access", n, [n], ["OBD"],
                    ["authenticity", "confidentiality", "non_repudiation"], c["safety_relevant"])
            if c["safety_relevant"] or c["security_relevant"]:
                add("configuration", f"{n} configuration data", n, [n], ifs, ["integrity"], c["safety_relevant"])
            if {"OTA", "Cellular"} & set(ifs):
                add("ota_update_package", f"{n} update package", n, [n], sorted({"OTA"} | ({"Cellular"} & set(ifs))),
                    ["integrity", "authenticity", "confidentiality"], c["safety_relevant"])
            if c["handles_personal_data"]:
                add("personal_data", f"{n} personal data", n, [n], ifs, ["confidentiality"], False)
        for f in arch["data_flows"]:
            props = ["authenticity", "integrity", "availability"] + (["confidentiality"] if f["confidential"] else [])
            add("message", f["name"], f["to"], [f["from"], f["to"]], [f["channel"]], props, f["safety_relevant"])
        return out

    def analyze(self, project: str, sys_ref: str, actor: str) -> dict:
        sysrec = self._sys(project, sys_ref)
        arch, lib = sysrec["payload"]["architecture"], self._lib(project)
        if not arch["components"]:
            raise Conflict("Add components to the architecture first")
        warnings, a_new, t_new = [], 0, 0
        for c in arch["components"]:
            if not c["interfaces"]:
                warnings.append(f"Component {c['name']} declares no interfaces; no attack surface derived")
        for a in self._assets_from(arch):
            arec, new = self.create(project, "asset", "AST", {"system": sys_ref, **a, "origin": "rules"}, actor,
                                    key=f"{sys_ref}:{a['kind']}:{a['name']}:{','.join(a['components'])}")
            a_new += new
            for p in lib["patterns"]:
                if p["asset_kind"] != a["kind"] or not (p["interface"] == "any" or p["interface"] in a["interfaces"]):
                    continue
                stale = self._stale(p, lib)
                impact = dict(p["impact_hint"])
                if not a["safety_relevant"]:
                    impact["safety"] = "negligible"
                trec, tnew = self.create(project, "threat", "TS", {
                    "system": sys_ref, "asset": arec["ref"], "asset_name": a["name"], "component": a["component"],
                    "components": a["components"], "pattern": p["id"], "name": p["name"], "stride": p["stride"],
                    "property": p["property"], "interface": p["interface"],
                    "exposure": arch_mod.INTERFACE_EXPOSURE.get(p["interface"], "internal"),
                    "description": p["description"], "attack_summary": p.get("attack_summary", ""), "damage": p["damage"],
                    "suggested": {"impact": impact, "ap": p["ap_hint"], "note": None if a["safety_relevant"] else
                                  "Safety impact set to negligible because the asset is not marked safety relevant"},
                    "source": {"library": p["id"], "review_date": p.get("review_date", ""), "stale": stale},
                    "impact": None, "ap": None, "feasibility": None, "risk": None, "treatment": None,
                    "safety_link": None, "origin": "library"}, actor, key=f"{arec['ref']}:{p['id']}")
                if tnew:
                    t_new += 1
                    self.db.link_add(project, arec["ref"], trec["ref"], "threatened_by", actor)
                if stale:
                    warnings.append(f"Threat library entry {p['id']} is past its review date; results need expert scrutiny")
        threats = self.threats(project, sys_ref)
        with_threat = {t["payload"]["asset"] for t in threats}
        assets = self.assets(project, sys_ref)
        self.audit(actor, "analyze", project, sys_ref, {"assets_created": a_new, "threats_created": t_new})
        return {"assets_created": a_new, "threats_created": t_new, "assets": len(assets), "threats": len(threats),
                "assets_without_threats": [a["ref"] for a in assets if a["ref"] not in with_threat],
                "warnings": sorted(set(warnings))}

    def assets(self, project: str, system: str | None = None) -> list[dict]:
        return self.list(project, "asset", system=system)

    def threats(self, project: str, system: str | None = None, status: str | None = None) -> list[dict]:
        return self.list(project, "threat", status=status, system=system)

    def add_threat(self, project: str, asset_ref: str, name: str, stride: str, prop: str, interface: str,
                   description: str, damage: str, actor: str) -> dict:
        a = self.get(project, asset_ref, "asset")
        if stride not in rules.STRIDE:
            raise Invalid(f"stride must be one of {list(rules.STRIDE)}")
        if len(description.strip()) < 15 or len(damage.strip()) < 5:
            raise Invalid("Describe the threat (min 15 chars) and the damage scenario")
        ap = a["payload"]
        rec, new = self.create(project, "threat", "TS", {
            "system": ap["system"], "asset": asset_ref, "asset_name": ap["name"], "component": ap["component"],
            "components": ap["components"], "pattern": None, "name": name.strip() or "Manual threat", "stride": stride,
            "property": prop, "interface": interface, "exposure": arch_mod.INTERFACE_EXPOSURE.get(interface, "internal"),
            "description": description.strip(), "attack_summary": "", "damage": damage.strip(), "suggested": None,
            "source": {"library": None, "review_date": "", "stale": False}, "impact": None, "ap": None,
            "feasibility": None, "risk": None, "treatment": None, "safety_link": None, "origin": "manual"},
            actor, key=f"{asset_ref}:manual:{name.strip().lower()}", machine=False)
        if not new:
            raise Conflict(f"Threat already exists: {rec['ref']}")
        self.db.link_add(project, asset_ref, rec["ref"], "threatened_by", actor)
        self.audit(actor, "threat.add", project, rec["ref"])
        return rec

    # ================================================================ assessment
    def assess_threat(self, project: str, ref: str, impact: dict, ap: dict, rationale: str,
                      safety_link: str | None, actor: str) -> dict:
        rec = self.get(project, ref, "threat")
        if rec["status"] == "rejected":
            raise Conflict(f"{ref} was rejected")
        if len((rationale or "").strip()) < 10:
            raise Invalid("A written rationale (min 10 characters) is required")
        level = rules.impact_rating(impact)
        total, feas = rules.attack_potential(ap)
        risk = rules.risk_value(level, feas)
        p, warning = rec["payload"], None
        if safety_link:
            ev = self.db.rec_get(project, safety_link)
            if not ev or ev["module"] != "hara" or ev["kind"] != "event" or ev["status"] != "approved":
                raise Invalid("safety_link must reference an approved HARA hazardous event of this project")
            need = rules.SAFETY_IMPACT_FROM_S[ev["payload"]["S"]]
            if rules.IMPACT_LEVELS.index(impact["safety"]) < rules.IMPACT_LEVELS.index(need):
                warning = f"Safety impact '{impact['safety']}' is lower than the HARA severity S{ev['payload']['S']} of {safety_link} ('{need}' expected)"
        sug = p.get("suggested")
        payload = {**p, "impact": impact, "impact_rating": level, "ap": ap, "ap_sum": total, "feasibility": feas,
                   "risk": risk, "rationale": rationale.strip(), "safety_link": safety_link, "safety_warning": warning,
                   "suggestion_adopted": bool(sug and sug["impact"] == impact and sug["ap"] == ap),
                   "mitigation_required": risk >= rules.MITIGATION_THRESHOLD, "ruleset": rules.RULESET_VERSION}
        out = self.db.rec_update(project, ref, actor, payload, "assessed", event="assess")
        self.audit(actor, "threat.assess", project, ref, {"risk": risk, "feasibility": feas})
        if p.get("risk") is not None and p["risk"] != risk:
            self.suspect_downstream(project, ref, actor, f"Risk of {ref} changed {p['risk']} -> {risk}")
        return out

    def decide_threat(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        def gate(rec):
            if rec["payload"].get("risk") is None:
                raise Invalid("Threat has not been assessed (impact and attack potential) yet")
        self.get(project, ref, "threat")
        return self.decide(project, ref, actor, decision, comment, gate)

    def treat_threat(self, project: str, ref: str, decision: str, justification: str, actor: str) -> dict:
        rec = self.get(project, ref, "threat")
        p = rec["payload"]
        if p.get("risk") is None:
            raise Conflict("Assess the threat before choosing a treatment")
        if decision not in rules.TREATMENTS:
            raise Invalid(f"treatment must be one of {list(rules.TREATMENTS)}")
        just = (justification or "").strip()
        if p["risk"] >= rules.MITIGATION_THRESHOLD and decision == "retain" and len(just) < 20:
            raise Invalid("Retaining a risk at or above the mitigation threshold needs a justification (min 20 characters)")
        payload = {**p, "treatment": decision, "treatment_justification": just,
                   "risk_acceptance": "pending" if (decision == "retain" and p["risk"] >= rules.MITIGATION_THRESHOLD) else "n/a"}
        status = "suspect" if rec["status"] == "approved" else rec["status"]
        return self.edit(project, ref, actor, payload, status=status, event="treat")

    def accept_risk(self, project: str, ref: str, comment: str, actor: str) -> dict:
        rec = self.get(project, ref, "threat")
        p = rec["payload"]
        if p.get("risk_acceptance") != "pending":
            raise Conflict("No risk acceptance is pending for this threat")
        if rec["updated_by"] == actor:
            raise Conflict(f"Four-eyes rule: {actor} chose the treatment and cannot accept the risk")
        out = self.db.rec_update(project, ref, actor, {**p, "risk_acceptance": "accepted", "acceptance_comment": comment},
                                 rec["status"], event="risk-accepted")
        self.audit(actor, "risk.accept", project, ref)
        return out

    # ================================================================ goals & requirements
    def derive_goals(self, project: str, sys_ref: str, actor: str) -> dict:
        self._sys(project, sys_ref)
        groups: dict[tuple, list[dict]] = {}
        for t in self.threats(project, sys_ref, "approved"):
            p = t["payload"]
            if p["risk"] >= rules.MITIGATION_THRESHOLD and p.get("treatment") in ("reduce", "avoid", "share"):
                groups.setdefault((p["asset"], p["property"]), []).append(t)
        created, updated, keys = [], [], set()
        for (asset, prop), ts in groups.items():
            key = f"{sys_ref}:{asset}:{prop}"
            keys.add(key)
            risk, refs = max(t["payload"]["risk"] for t in ts), sorted(t["ref"] for t in ts)
            payload = {"system": sys_ref, "asset": asset, "asset_name": ts[0]["payload"]["asset_name"], "property": prop,
                       "text": f"Protect the {prop.replace('_', ' ')} of '{ts[0]['payload']['asset_name']}'",
                       "risk": risk, "threats": refs, "origin": "rules", "orphan": False}
            rec, new = self.create(project, "cgoal", "CG", payload, actor, key=key)
            if new:
                created.append(rec["ref"])
            else:
                old = rec["payload"]
                if old["risk"] != risk or old["threats"] != refs or old.get("orphan"):
                    self.db.rec_update(project, rec["ref"], actor, {**old, "risk": risk, "threats": refs, "orphan": False},
                                       "suspect" if rec["status"] == "approved" else rec["status"], event="rederive")
                    updated.append(rec["ref"])
                    if old["risk"] != risk:
                        self.suspect_downstream(project, rec["ref"], actor, f"Risk of {rec['ref']} changed {old['risk']} -> {risk}")
            for r in refs:
                self.db.link_add(project, r, rec["ref"], "addressed_by", actor)
        orphans = []
        for g in self.list(project, "cgoal", system=sys_ref):
            k = f"{sys_ref}:{g['payload']['asset']}:{g['payload']['property']}"
            if k not in keys and g["status"] != "rejected":
                self.db.rec_update(project, g["ref"], actor, {**g["payload"], "orphan": True}, "suspect", event="orphan")
                orphans.append(g["ref"])
        self.audit(actor, "goals.derive", project, sys_ref, {"created": created, "updated": updated, "orphans": orphans})
        return {"created": created, "updated": updated, "orphaned": orphans, "goals": self.goals(project, sys_ref)}

    def goals(self, project: str, system: str | None = None) -> list[dict]:
        return self.list(project, "cgoal", system=system)

    def decide_goal(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        def gate(rec):
            if rec["payload"].get("orphan"):
                raise Conflict("Goal is no longer supported by approved, treated threats")
        self.get(project, ref, "cgoal")
        return self.decide(project, ref, actor, decision, comment, gate)

    def recommend_controls(self, project: str, goal_ref: str, actor: str) -> dict:
        g = self.get(project, goal_ref, "cgoal")
        if g["status"] == "rejected":
            raise Conflict("Goal was rejected")
        lib = self._lib(project)
        ctrls = {c["id"]: c for c in lib["controls"]}
        pats = {p["id"]: p for p in lib["patterns"]}
        by_ctrl: dict[str, list[dict]] = {}
        for tref in g["payload"]["threats"]:
            t = self.get(project, tref, "threat")
            for cid in (pats.get(t["payload"]["pattern"]) or {}).get("controls", []):
                by_ctrl.setdefault(cid, []).append(t)
        if not by_ctrl:
            raise Conflict("No library controls apply (manual threats need manually authored requirements)")
        out = []
        for cid, ts in by_ctrl.items():
            c = ctrls[cid]
            t0 = ts[0]["payload"]
            text = c["requirement_template"].format(component=t0["component"], asset=t0["asset_name"])
            rec, new = self.create(project, "csr", "CSR", {
                "system": g["payload"]["system"], "goal": goal_ref, "control_id": cid, "control_name": c["name"],
                "text": text, "risk": g["payload"]["risk"], "threats": sorted(t["ref"] for t in ts),
                "suggested_allocation": t0["component"], "allocated_to": "", "verification": [], "allocation_check": "n/a",
                "quality": check_requirement(text, require_quantity=False), "assumptions": c.get("assumptions", ""),
                "alternatives": c.get("alternatives", ""),
                "source": {"library": cid, "review_date": c.get("review_date", ""), "stale": self._stale(c, lib)},
                "origin": "library"}, actor, key=f"{goal_ref}:{cid}")
            if new:
                self.db.link_add(project, goal_ref, rec["ref"], "refined_by", actor)
            out.append(rec)
        self.audit(actor, "controls.recommend", project, goal_ref, {"csr": [r["ref"] for r in out]})
        return {"requirements": out}

    def csrs(self, project: str, system: str | None = None) -> list[dict]:
        return self.list(project, "csr", system=system)

    def update_csr(self, project: str, ref: str, actor: str, text: str | None = None, allocated_to: str | None = None,
                   verification: list[str] | None = None) -> dict:
        rec = self.get(project, ref, "csr")
        p = dict(rec["payload"])
        if text is not None:
            p["text"] = text.strip()
        if allocated_to is not None:
            p["allocated_to"] = allocated_to.strip()[:120]
        if verification is not None:
            v = [x.strip() for x in verification if x and x.strip()]
            if any(len(x) > 80 for x in v):
                raise Invalid("Verification references must be at most 80 characters")
            p["verification"] = v
        sysrec = self._sys(project, p["system"])["payload"]
        names = {c["name"] for c in sysrec["architecture"]["components"]}
        p["allocation_check"] = "n/a" if not p["allocated_to"] else "ok" if p["allocated_to"] in names else "not_in_architecture"
        p["quality"] = check_requirement(p["text"])
        out = self.edit(project, ref, actor, p, status="suspect" if rec["status"] == "approved" else "assessed",
                        event="csr.edit")
        self.trace.replace_out(project, ref, "allocated_to", [f"COMP:{p['allocated_to']}"] if p["allocated_to"] else [], actor)
        self.trace.replace_out(project, ref, "verified_by", [f"TEST:{v}" for v in p["verification"]], actor)
        return out

    def decide_csr(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        def gate(rec):
            p = rec["payload"]
            if has_errors(p["quality"]):
                raise Invalid("Requirement has quality errors: " + "; ".join(q["message"] for q in p["quality"] if q["severity"] == "error"))
            if not p["allocated_to"]:
                raise Invalid("Allocate the requirement to an architecture element before approval")
            if p["allocation_check"] == "not_in_architecture":
                raise Invalid(f"'{p['allocated_to']}' is not a component of this system architecture")
            if not p["verification"]:
                raise Invalid("Add at least one verification reference before approval")
        self.get(project, ref, "csr")
        return self.decide(project, ref, actor, decision, comment, gate)

    # ================================================================ coverage checklist
    def coverage(self, project: str, sys_ref: str) -> dict:
        arch = self._sys(project, sys_ref)["payload"]["architecture"]
        checklist = self._lib(project).get("coverage_checklist", {})
        threats = [t for t in self.threats(project, sys_ref) if t["status"] != "rejected"]
        nas = {(n["payload"]["component"], n["payload"]["interface"], n["payload"]["stride"]): n
               for n in self.list(project, "na", system=sys_ref)}
        rows, missing, total = [], 0, 0
        for c in arch["components"]:
            for iface in c["interfaces"]:
                for stride in checklist.get(iface, []):
                    total += 1
                    hit = [t["ref"] for t in threats if c["name"] in t["payload"]["components"]
                           and t["payload"]["interface"] == iface and t["payload"]["stride"] == stride]
                    na = nas.get((c["name"], iface, stride))
                    status = "covered" if hit else "not_applicable" if na and na["status"] == "approved" else \
                             "na_pending" if na and na["status"] in ("proposed", "assessed") else "missing"
                    missing += status in ("missing", "na_pending")
                    rows.append({"component": c["name"], "interface": iface, "stride": stride, "status": status,
                                 "threats": hit, "na_ref": na["ref"] if na else None})
        return {"rows": rows, "total": total, "gaps": missing,
                "coverage": round((total - missing) / total, 3) if total else 1.0,
                "expert_review_required": True,
                "note": "Checklist coverage is not proof of completeness; a qualified expert (red-team) review is required for novel threats."}

    def add_na(self, project: str, sys_ref: str, component: str, interface: str, stride: str, justification: str,
               actor: str) -> dict:
        self._sys(project, sys_ref)
        if stride not in rules.STRIDE:
            raise Invalid(f"stride must be one of {list(rules.STRIDE)}")
        if len((justification or "").strip()) < 20:
            raise Invalid("A justification of at least 20 characters is required")
        rec, new = self.create(project, "na", "NA", {
            "system": sys_ref, "component": component, "interface": interface, "stride": stride,
            "justification": justification.strip(), "origin": "manual"}, actor, status="assessed",
            key=f"{sys_ref}:{component}:{interface}:{stride}", machine=False)
        if not new:
            raise Conflict(f"Already declared: {rec['ref']}")
        return rec

    def decide_na(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        self.get(project, ref, "na")
        return self.decide(project, ref, actor, decision, comment)

    # ================================================================ views: completeness / trace / metrics / report
    def completeness(self, project: str, sys_ref: str) -> dict:
        self._sys(project, sys_ref)
        ts = [t for t in self.threats(project, sys_ref) if t["status"] != "rejected"]
        goals = [g for g in self.goals(project, sys_ref) if g["status"] != "rejected"]
        csrs = [c for c in self.csrs(project, sys_ref) if c["status"] != "rejected"]
        high = [t for t in ts if (t["payload"].get("risk") or 0) >= rules.MITIGATION_THRESHOLD]
        in_goal = {r for g in goals for r in g["payload"]["threats"]}
        has_csr = lambda g: any(True for _ in self.trace.children(project, g["ref"], "refined_by"))
        cov = self.coverage(project, sys_ref)
        stale_used = [t["ref"] for t in ts if t["payload"]["source"].get("stale")]
        checks = [
            ("threats_approved", "All threat scenarios assessed and approved", [t["ref"] for t in ts if t["status"] != "approved"]),
            ("treatment_defined", "Every threat at/above the mitigation threshold has a treatment", [t["ref"] for t in high if not t["payload"].get("treatment")]),
            ("risks_accepted", "Retained high risks have been accepted", [t["ref"] for t in high if t["payload"].get("risk_acceptance") == "pending"]),
            ("goals_cover_threats", "Every treated high-risk threat is covered by a cybersecurity goal",
             [t["ref"] for t in high if t["status"] == "approved" and t["payload"].get("treatment") in ("reduce", "avoid", "share") and t["ref"] not in in_goal]),
            ("goals_approved", "All cybersecurity goals approved", [g["ref"] for g in goals if g["status"] != "approved"]),
            ("goals_have_csr", "Every goal has cybersecurity requirements", [g["ref"] for g in goals if not has_csr(g)]),
            ("csr_allocated", "Every requirement is allocated", [c["ref"] for c in csrs if not c["payload"]["allocated_to"]]),
            ("csr_verified", "Every requirement has a verification reference", [c["ref"] for c in csrs if not c["payload"]["verification"]]),
            ("csr_approved", "All requirements approved", [c["ref"] for c in csrs if c["status"] != "approved"]),
            ("coverage_complete", "Interface x STRIDE checklist has no open gaps", [f"{r['component']}/{r['interface']}/{r['stride']}" for r in cov["rows"] if r["status"] in ("missing", "na_pending")]),
            ("library_current", "No threat relies on a library entry past its review date", stale_used),
            ("no_suspect", "No item is suspect after upstream changes", [r["ref"] for r in ts + goals + csrs if r["status"] == "suspect"]),
        ]
        total = len(goals) + 2 * len(csrs) + len([t for t in high if t["payload"].get("treatment") in ("reduce", "avoid", "share")])
        missing = (sum(1 for g in goals if not has_csr(g)) + sum(1 for c in csrs if not c["payload"]["allocated_to"])
                   + sum(1 for c in csrs if not c["payload"]["verification"])
                   + sum(1 for t in high if t["payload"].get("treatment") in ("reduce", "avoid", "share") and t["ref"] not in in_goal))
        return {"system": sys_ref, "checks": [{"id": i, "description": d, "passed": not f, "failing": f} for i, d, f in checks],
                "mandatory_links": total, "satisfied_links": total - missing,
                "traceability_completeness": round((total - missing) / total, 3) if total else 0.0,
                "complete": all(not f for _, _, f in checks) and total > 0, "coverage": cov["coverage"]}

    def trace_matrix(self, project: str, sys_ref: str) -> list[dict]:
        rows = []
        for t in self.threats(project, sys_ref):
            if t["status"] == "rejected":
                continue
            tp = t["payload"]
            goals = [self.db.rec_get(project, g) for g in self.trace.children(project, t["ref"], "addressed_by")] or [None]
            for g in goals:
                csrs = [self.db.rec_get(project, c) for c in self.trace.children(project, g["ref"], "refined_by")] if g else []
                for c in csrs or [None]:
                    rows.append({"asset": tp["asset"], "asset_name": tp["asset_name"], "threat": t["ref"], "threat_name": tp["name"],
                                 "stride": tp["stride"], "risk": tp.get("risk"), "treatment": tp.get("treatment"),
                                 "goal": g["ref"] if g else "", "csr": c["ref"] if c else "",
                                 "csr_text": c["payload"]["text"] if c else "",
                                 "allocated_to": c["payload"]["allocated_to"] if c else "",
                                 "verification": ";".join(c["payload"]["verification"]) if c else ""})
        return rows

    def metrics(self, project: str, sys_ref: str) -> dict:
        sysrec = self._sys(project, sys_ref)
        ts = self.threats(project, sys_ref)
        lib = [t for t in ts if t["payload"]["origin"] == "library"]
        approved = [r["approved_at"] for r in self.db.rec_list(project, self.module) if r["approved_at"] and r["payload"].get("system") == sys_ref]
        hours = None
        if approved:
            hours = round((datetime.fromisoformat(max(approved)) - datetime.fromisoformat(sysrec["created_at"])).total_seconds() / 3600, 2)
        comp = self.completeness(project, sys_ref)
        dist: dict[int, int] = {}
        for t in ts:
            if t["payload"].get("risk"):
                dist[t["payload"]["risk"]] = dist.get(t["payload"]["risk"], 0) + 1
        return {"knowledge_reuse": round(len(lib) / len(ts), 3) if ts else None,
                "recommendation_acceptance": self.acceptance(project, ["threat", "cgoal", "csr"]),
                "traceability_completeness": comp["traceability_completeness"], "checklist_coverage": comp["coverage"],
                "cycle_time_hours": hours, "risk_distribution": dict(sorted(dist.items())),
                "counts": {"assets": len(self.assets(project, sys_ref)), "threats": len(ts),
                           "goals": len(self.goals(project, sys_ref)), "requirements": len(self.csrs(project, sys_ref))}}

    def report(self, project: str, sys_ref: str, fmt: str, actor: str) -> tuple[str, str]:
        sysrec = self._sys(project, sys_ref)
        rows, comp, cov = self.trace_matrix(project, sys_ref), self.completeness(project, sys_ref), self.coverage(project, sys_ref)
        self.audit(actor, "report", project, sys_ref, {"format": fmt})
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(rows[0]) if rows else ["asset"])
            w.writeheader(); w.writerows(rows)
            return buf.getvalue(), "text/csv"
        if fmt == "json":
            return json.dumps({"system": sysrec, "assets": self.assets(project, sys_ref), "threats": self.threats(project, sys_ref),
                               "goals": self.goals(project, sys_ref), "requirements": self.csrs(project, sys_ref),
                               "trace": rows, "completeness": comp, "coverage": cov, "ruleset": rules.RULESET_VERSION}, indent=2), "application/json"
        s = self.attack_surface(project, sys_ref)
        md = [f"# TARA report: {sysrec['payload']['name']} ({sys_ref})", "",
              f"Assessment rule set: `{rules.RULESET_VERSION}`. AI-generated content is advisory; qualified cybersecurity review is required.", "",
              "## Attack surface", "", "| Component | Interface | Exposure | Zone |", "|---|---|---|---|"]
        md += [f"| {r['component']} | {r['interface']} | {r['exposure']} | {r['zone']} |" for r in s["surface"]]
        md += ["", "## Threat scenarios", "", "| Ref | Asset | Threat | STRIDE | Impact | Feasibility | Risk | Treatment | Status |", "|---|---|---|---|---|---|---|---|---|"]
        for t in self.threats(project, sys_ref):
            p = t["payload"]
            md.append(f"| {t['ref']} | {p['asset_name']} | {p['name']} | {p['stride']} | {p.get('impact_rating', '')} | {p.get('feasibility', '')} | {p.get('risk', '')} | {p.get('treatment') or ''} | {t['status']} |")
        md += ["", "## Goals and requirements", "", "| Goal | Requirement | Allocated to | Verification | Status |", "|---|---|---|---|---|"]
        for r in rows:
            if r["csr"]:
                md.append(f"| {r['goal']} | {r['csr']}: {r['csr_text']} | {r['allocated_to']} | {r['verification']} | |")
        md += ["", f"## Coverage checklist ({cov['coverage']:.0%}, {cov['gaps']} open gaps)", "", cov["note"], ""]
        md += [f"- {r['component']} / {r['interface']} / {r['stride']}: **{r['status']}**" for r in cov["rows"] if r["status"] in ("missing", "na_pending")]
        md += ["", f"## Completeness ({comp['traceability_completeness']:.0%} of mandatory links)", ""]
        md += [f"- [{'x' if c['passed'] else ' '}] {c['description']}" + (f" (open: {', '.join(c['failing'][:8])})" if c["failing"] else "") for c in comp["checks"]]
        appr = [r for r in self.db.rec_list(project, self.module) if r["approved_by"] and r["payload"].get("system") == sys_ref]
        md += ["", "## Approval evidence", ""] + ([f"- {r['ref']} ({r['kind']}) approved by {r['approved_by']} at {r['approved_at']}" for r in appr] or ["- none yet"])
        return "\n".join(md), "text/markdown"
