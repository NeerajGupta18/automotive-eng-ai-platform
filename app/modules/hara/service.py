"""HARA & safety requirement management.

Pipeline: item definition -> hazard suggestions -> hazardous events -> guided S/E/C -> deterministic ASIL
-> safety goals -> FSR/TSR drafts -> allocation + verification -> traceability / completeness / report.

Design rules: the ASIL is computed by rules (never by the LLM); every S/E/C needs a written rationale;
approval is four-eyes; approved items become 'suspect' when an upstream item changes."""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from app.core.errors import Conflict, Invalid, NotFound
from app.core.models import Chunk
from app.core.reqquality import check_requirement, has_errors
from app.core.workflow import Workflow
from app.modules.hara import rules, templates

BUNDLED_LIBRARY = Path(__file__).resolve().parents[3] / "data" / "library" / "hara_library.json"
STATUS_OK_FOR_EDIT = ("proposed", "assessed", "approved", "suspect", "accepted")


class HARAService(Workflow):
    def __init__(self, container):
        super().__init__(container, "hara")

    # ================================================================ library (approved knowledge)
    def load_library(self, project: str, actor: str, data: dict | None = None) -> dict:
        if data is None:
            data = json.loads(BUNDLED_LIBRARY.read_text())
        entries = data.get("entries")
        if not isinstance(entries, list) or not entries:
            raise Invalid("Library needs a non-empty 'entries' list")
        need = {"id", "function", "malfunction", "hazard", "situation", "S", "E", "C", "rationale"}
        for e in entries:
            missing = need - set(e)
            if missing:
                raise Invalid(f"Library entry {e.get('id', '?')} lacks fields: {sorted(missing)}")
            if e["malfunction"] not in rules.GUIDEWORDS:
                raise Invalid(f"{e['id']}: unknown malfunction '{e['malfunction']}'")
            rules.validate_sec(e["S"], e["E"], e["C"])
        store = self.c.store(f"{project}__hara_lib")
        store.delete({"kind": "hara_lib"})
        chunks = [Chunk(f"lib::{e['id']}", f"{e['function']} {rules.GUIDEWORDS[e['malfunction']]['label']} {e['hazard']} {e['situation']}",
                        {"kind": "hara_lib", "doc_id": "hara_lib", "lib_id": e["id"]}) for e in entries]
        store.add(chunks, self.c.embedder.embed([c.text for c in chunks]))
        payload = {"name": data.get("name", "custom"), "version": data.get("version", ""), "entries": entries,
                   "situations": data.get("situations", [])}
        rec, created = self.create(project, "library", "LIB", payload, actor, status="approved", key="hara-library")
        if not created:
            self.db.rec_update(project, rec["ref"], actor, payload, "approved", event="reload")
        self.audit(actor, "library.load", project, rec["ref"], {"entries": len(entries)})
        return {"ref": rec["ref"], "entries": len(entries), "name": payload["name"]}

    def library(self, project: str) -> dict | None:
        recs = self.list(project, "library")
        return recs[0]["payload"] if recs else None

    def _similar(self, project: str, text: str, guideword: str | None = None, k: int = 3) -> list[dict]:
        lib = self.library(project)
        if not lib:
            return []
        store = self.c.store(f"{project}__hara_lib")
        by_id = {e["id"]: e for e in lib["entries"]}
        out = []
        for h in store.query(self.c.embedder.embed_query(text), 10):
            e = by_id.get(h.meta.get("lib_id"))
            if e and h.score >= self.c.settings.min_score and (guideword is None or e["malfunction"] == guideword):
                out.append({"id": e["id"], "score": round(h.score, 3), "function": e["function"], "hazard": e["hazard"],
                            "situation": e["situation"], "S": e["S"], "E": e["E"], "C": e["C"],
                            "rationale": e["rationale"], "review_date": e.get("review_date", "")})
        return out[:k]

    # ================================================================ item definition
    def create_item(self, project: str, name: str, description: str, functions: list[dict],
                    hld_doc_id: str | None, actor: str) -> dict:
        if not name.strip():
            raise Invalid("Item name required")
        if not functions:
            raise Invalid("At least one function is required")
        names, funcs = set(), []
        for i, f in enumerate(functions, start=1):
            n = (f.get("name") or "").strip()
            if not n:
                raise Invalid("Function names must not be empty")
            if n.lower() in names:
                raise Invalid(f"Duplicate function '{n}'")
            names.add(n.lower())
            gws = f.get("guidewords") or list(rules.GUIDEWORDS)
            bad = [g for g in gws if g not in rules.GUIDEWORDS]
            if bad:
                raise Invalid(f"Unknown guidewords {bad}; allowed: {list(rules.GUIDEWORDS)}")
            funcs.append({"id": f"F{i}", "name": n, "description": f.get("description", ""), "guidewords": gws})
        if hld_doc_id:
            d = self.db.document(hld_doc_id)
            if not d or d["project"] != project:
                raise Invalid("hld_doc_id does not belong to this project")
        rec, _ = self.create(project, "item", "ITEM",
                             {"name": name.strip(), "description": description, "functions": funcs,
                              "hld_doc_id": hld_doc_id, "origin": "manual"},
                             actor, status="approved", machine=False)
        self.audit(actor, "item.create", project, rec["ref"], {"functions": len(funcs)})
        return rec

    def items(self, project: str) -> list[dict]:
        return self.list(project, "item")

    def _item(self, project: str, ref: str) -> dict:
        return self.get(project, ref, "item")

    # ================================================================ hazards
    def suggest_hazards(self, project: str, item_ref: str, actor: str) -> dict:
        item = self._item(project, item_ref)["payload"]
        created = 0
        for f in item["functions"]:
            for gw in f["guidewords"]:
                similar = self._similar(project, f"{f['name']} {rules.GUIDEWORDS[gw]['label']} {f['description']}", gw)
                label = rules.GUIDEWORDS[gw]["label"]
                desc = (f"{label} of '{f['name']}' may lead to a hazardous event in relevant operating situations."
                        + (f" Similar approved hazard: {similar[0]['hazard']}." if similar else ""))
                _, new = self.create(project, "hazard", "H", {
                    "item": item_ref, "function_id": f["id"], "function": f["name"], "guideword": gw,
                    "malfunction": label, "description": desc, "similar": similar, "origin": "rules"},
                    actor, key=f"{item_ref}:{f['id']}:{gw}")
                created += new
        self.audit(actor, "hazards.suggest", project, item_ref, {"created": created})
        return {"created": created, "hazards": self.list(project, "hazard", item=item_ref)}

    def add_hazard(self, project: str, item_ref: str, function_id: str, guideword: str, description: str,
                   actor: str) -> dict:
        item = self._item(project, item_ref)["payload"]
        f = next((x for x in item["functions"] if x["id"] == function_id), None)
        if not f:
            raise Invalid(f"Unknown function id '{function_id}' in {item_ref}")
        if guideword not in rules.GUIDEWORDS:
            raise Invalid(f"Unknown guideword '{guideword}'")
        if len(description.strip()) < 10:
            raise Invalid("Describe the hazard (at least 10 characters)")
        rec, new = self.create(project, "hazard", "H", {
            "item": item_ref, "function_id": f["id"], "function": f["name"], "guideword": guideword,
            "malfunction": rules.GUIDEWORDS[guideword]["label"], "description": description.strip(),
            "similar": [], "origin": "manual"}, actor, status="accepted", key=f"{item_ref}:{f['id']}:{guideword}",
            machine=False)
        if not new:
            raise Conflict(f"Hazard for this function/guideword already exists: {rec['ref']}")
        self.audit(actor, "hazard.add", project, rec["ref"])
        return rec

    def decide_hazard(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        rec = self.get(project, ref, "hazard")
        if decision not in ("accepted", "rejected"):
            raise Invalid("decision must be 'accepted' or 'rejected'")
        if rec["status"] != "proposed":
            raise Conflict(f"{ref} is already {rec['status']}")
        out = self.db.rec_update(project, ref, actor, {**rec["payload"], "decision_comment": comment}, decision,
                                 event=f"triage:{decision}")
        self.audit(actor, "hazard.triage", project, ref, {"decision": decision})
        return out

    # ================================================================ hazardous events
    def propose_events(self, project: str, hazard_ref: str, actor: str) -> dict:
        h = self.get(project, hazard_ref, "hazard")
        if h["status"] != "accepted":
            raise Conflict("Accept the hazard before proposing hazardous events")
        hp, lib = h["payload"], self.library(project) or {}
        similar = self._similar(project, f"{hp['function']} {hp['malfunction']} {hp['description']}", hp["guideword"])
        situations = [s["name"] for s in lib.get("situations", [])] or ["Highway driving", "Urban driving", "Vehicle stationary"]
        for s in similar:
            if s["situation"] not in situations:
                situations.append(s["situation"])
        created = 0
        for sit in situations:
            match = next((s for s in similar if s["situation"] == sit), None)
            suggested = ({"S": match["S"], "E": match["E"], "C": match["C"], "rationale": match["rationale"],
                          "source": match["id"], "score": match["score"]} if match else None)
            rec, new = self.create(project, "event", "HE", {
                "hazard": hazard_ref, "item": hp["item"], "function_id": hp["function_id"], "function": hp["function"],
                "guideword": hp["guideword"], "situation": sit, "S": None, "E": None, "C": None,
                "rationale": {"S": "", "E": "", "C": ""}, "asil": None, "asil_rationale": "",
                "suggested": suggested, "origin": "library" if suggested else "rules"},
                actor, key=f"{hazard_ref}:{sit.lower()}")
            if new:
                self.db.link_add(project, hazard_ref, rec["ref"], "has_event", actor)
            created += new
        self.audit(actor, "events.propose", project, hazard_ref, {"created": created})
        return {"created": created, "events": self.list(project, "event", hazard=hazard_ref)}

    def add_event(self, project: str, hazard_ref: str, situation: str, actor: str) -> dict:
        h = self.get(project, hazard_ref, "hazard")
        if h["status"] != "accepted":
            raise Conflict("Accept the hazard first")
        if len(situation.strip()) < 4:
            raise Invalid("Describe the operational situation")
        hp = h["payload"]
        rec, new = self.create(project, "event", "HE", {
            "hazard": hazard_ref, "item": hp["item"], "function_id": hp["function_id"], "function": hp["function"],
            "guideword": hp["guideword"], "situation": situation.strip(), "S": None, "E": None, "C": None,
            "rationale": {"S": "", "E": "", "C": ""}, "asil": None, "asil_rationale": "", "suggested": None,
            "origin": "manual"}, actor, key=f"{hazard_ref}:{situation.strip().lower()}", machine=False)
        if not new:
            raise Conflict(f"Event already exists: {rec['ref']}")
        self.db.link_add(project, hazard_ref, rec["ref"], "has_event", actor)
        return rec

    def assess_event(self, project: str, ref: str, S: int, E: int, C: int, rationale: dict, actor: str) -> dict:
        rec = self.get(project, ref, "event")
        if rec["status"] == "rejected":
            raise Conflict(f"{ref} was rejected")
        for k in ("S", "E", "C"):
            if len((rationale.get(k) or "").strip()) < 10:
                raise Invalid(f"A written rationale (min 10 characters) is required for {k}")
        new_asil = rules.asil(S, E, C)
        p = rec["payload"]
        sug = p.get("suggested")
        adopted = bool(sug and (sug["S"], sug["E"], sug["C"]) == (S, E, C))
        old_asil = p.get("asil")
        payload = {**p, "S": S, "E": E, "C": C, "rationale": {k: rationale[k].strip() for k in ("S", "E", "C")},
                   "asil": new_asil, "suggestion_adopted": adopted,
                   "asil_rationale": f"S{S} ({rules.SEVERITY[S]}), E{E} ({rules.EXPOSURE[E]}), C{C} ({rules.CONTROLLABILITY[C]}) "
                                     f"-> ASIL {new_asil} [rule set {rules.RULESET_VERSION}]"}
        out = self.db.rec_update(project, ref, actor, payload, "assessed", event="assess")
        self.audit(actor, "event.assess", project, ref, {"asil": new_asil, "adopted": adopted})
        if old_asil and old_asil != new_asil:
            self.suspect_downstream(project, ref, actor, f"ASIL of {ref} changed {old_asil} -> {new_asil}")
        return out

    def decide_event(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        def gate(rec):
            if rec["payload"].get("asil") is None:
                raise Invalid("Event has not been assessed (S/E/C) yet")
        self.get(project, ref, "event")
        return self.decide(project, ref, actor, decision, comment, gate)

    # ================================================================ safety goals
    def derive_goals(self, project: str, item_ref: str, actor: str) -> dict:
        self._item(project, item_ref)
        events = [e for e in self.list(project, "event", item=item_ref) if e["status"] == "approved"]
        groups: dict[tuple, list[dict]] = {}
        qm = []
        for e in events:
            if e["payload"]["asil"] == "QM":
                qm.append(e["ref"])
                continue
            groups.setdefault((e["payload"]["function_id"], e["payload"]["guideword"]), []).append(e)
        created, updated, keys = [], [], set()
        for (fid, gw), evs in groups.items():
            p0 = evs[0]["payload"]
            key = f"{item_ref}:{fid}:{gw}"
            keys.add(key)
            asil = rules.max_asil([e["payload"]["asil"] for e in evs])
            refs = sorted(e["ref"] for e in evs)
            payload = {"item": item_ref, "function_id": fid, "function": p0["function"], "guideword": gw,
                       "text": templates.fill_goal_text(p0["function"], gw), "asil": asil, "events": refs,
                       "safe_state": "", "ftti_ms": None, "origin": "rules", "orphan": False}
            rec, new = self.create(project, "goal", "SG", payload, actor, key=key)
            if new:
                created.append(rec["ref"])
            else:
                old = rec["payload"]
                if old["asil"] != asil or old["events"] != refs or old.get("orphan"):
                    merged = {**old, "asil": asil, "events": refs, "orphan": False}
                    self.db.rec_update(project, rec["ref"], actor, merged,
                                       "suspect" if rec["status"] == "approved" else rec["status"], event="rederive")
                    updated.append(rec["ref"])
                    if old["asil"] != asil:
                        self.suspect_downstream(project, rec["ref"], actor, f"ASIL of {rec['ref']} changed {old['asil']} -> {asil}")
            for r in refs:
                self.db.link_add(project, r, rec["ref"], "derives", actor)
        orphans = []
        for g in self.list(project, "goal", item=item_ref):     # goals no longer supported by approved events
            if g["payload"]["item"] == item_ref and self._goal_key(g) not in keys and g["status"] != "rejected":
                self.db.rec_update(project, g["ref"], actor, {**g["payload"], "orphan": True}, "suspect", event="orphan")
                orphans.append(g["ref"])
        self.audit(actor, "goals.derive", project, item_ref, {"created": created, "updated": updated, "orphans": orphans})
        return {"created": created, "updated": updated, "orphaned": orphans, "qm_events": qm,
                "goals": self.list(project, "goal", item=item_ref)}

    @staticmethod
    def _goal_key(g: dict) -> str:
        p = g["payload"]
        return f"{p['item']}:{p['function_id']}:{p['guideword']}"

    def update_goal(self, project: str, ref: str, actor: str, text: str | None = None,
                    safe_state: str | None = None, ftti_ms: int | None = None) -> dict:
        rec = self.get(project, ref, "goal")
        p = dict(rec["payload"])
        if text is not None:
            if len(text.strip()) < 10:
                raise Invalid("Goal text too short")
            p["text"] = text.strip()
        if safe_state is not None:
            p["safe_state"] = safe_state.strip()
        if ftti_ms is not None:
            if ftti_ms <= 0 or ftti_ms > 60000:
                raise Invalid("FTTI must be between 1 and 60000 ms")
            p["ftti_ms"] = ftti_ms
        status = "suspect" if rec["status"] == "approved" else "assessed"
        out = self.edit(project, ref, actor, p, status=status, event="goal.edit")
        return out

    def decide_goal(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        def gate(rec):
            p = rec["payload"]
            if p.get("orphan"):
                raise Conflict("Goal is no longer supported by approved hazardous events")
            if not p.get("safe_state") or not p.get("ftti_ms"):
                raise Invalid("A safe state and FTTI must be defined before a safety goal can be approved")
        self.get(project, ref, "goal")
        return self.decide(project, ref, actor, decision, comment, gate)

    # ================================================================ requirements (FSR / TSR)
    def draft_requirements(self, project: str, goal_ref: str, actor: str) -> dict:
        g = self.get(project, goal_ref, "goal")
        if g["status"] == "rejected":
            raise Conflict("Goal was rejected")
        gp = g["payload"]
        item = self._item(project, gp["item"])["payload"]
        out = []
        specs = [("fsr", "FSR", templates.draft_fsr(item["name"], gp["function"], gp["guideword"], gp["safe_state"], gp["ftti_ms"])),
                 ("tsr", "TSR", templates.draft_tsr(gp["function"], gp["guideword"], gp["safe_state"], gp["ftti_ms"]))]
        parent = goal_ref
        for kind, prefix, (text, source) in specs:
            rec, new = self.create(project, kind, prefix, {
                "level": prefix, "item": gp["item"], "goal": goal_ref, "parent": parent, "text": text, "asil": gp["asil"],
                "allocated_to": "", "verification": [], "allocation_check": "n/a",
                "quality": check_requirement(text, require_quantity=True), "origin": "template", "source": source},
                actor, key=f"{goal_ref}:{kind}")
            if new:
                self.db.link_add(project, parent, rec["ref"], "refined_by", actor)
            out.append(rec)
            parent = rec["ref"]
        self.audit(actor, "requirements.draft", project, goal_ref, {"refs": [r["ref"] for r in out]})
        return {"requirements": out}

    def _hld_components(self, project: str, item_payload: dict) -> set[str] | None:
        doc = item_payload.get("hld_doc_id")
        if not doc:
            return None
        return {c["name"] for c in self.c.hld.extract(doc)["components"]}

    def update_requirement(self, project: str, ref: str, actor: str, text: str | None = None,
                           allocated_to: str | None = None, verification: list[str] | None = None) -> dict:
        rec = self.get(project, ref, ("fsr", "tsr"))
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
        comps = self._hld_components(project, self._item(project, p["item"])["payload"])
        p["allocation_check"] = ("n/a" if comps is None or not p["allocated_to"]
                                 else "ok" if p["allocated_to"] in comps else "not_in_architecture")
        p["quality"] = check_requirement(p["text"], require_quantity=True)
        status = "suspect" if rec["status"] == "approved" else "assessed"
        out = self.edit(project, ref, actor, p, status=status, event="requirement.edit")
        self.trace.replace_out(project, ref, "allocated_to", [f"COMP:{p['allocated_to']}"] if p["allocated_to"] else [], actor)
        self.trace.replace_out(project, ref, "verified_by", [f"TEST:{v}" for v in p["verification"]], actor)
        return out

    def decide_requirement(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        def gate(rec):
            p = rec["payload"]
            if has_errors(p["quality"]):
                raise Invalid("Requirement has quality errors: " + "; ".join(q["message"] for q in p["quality"] if q["severity"] == "error"))
            if not p["allocated_to"]:
                raise Invalid("Allocate the requirement to an architecture element before approval")
            if p.get("allocation_check") == "not_in_architecture":
                raise Invalid(f"'{p['allocated_to']}' is not a component in the linked HLD")
            if not p["verification"]:
                raise Invalid("Add at least one verification reference (test / review ID) before approval")
        self.get(project, ref, ("fsr", "tsr"))
        return self.decide(project, ref, actor, decision, comment, gate)

    # ================================================================ views
    def hazards(self, project: str, item: str | None = None, status: str | None = None) -> list[dict]:
        return self.list(project, "hazard", status=status, item=item)

    def events(self, project: str, item: str | None = None, hazard: str | None = None, status: str | None = None):
        return self.list(project, "event", status=status, item=item, hazard=hazard)

    def goals(self, project: str, item: str | None = None) -> list[dict]:
        return self.list(project, "goal", item=item)

    def requirements(self, project: str, item: str | None = None) -> list[dict]:
        return self.list(project, "fsr", item=item) + self.list(project, "tsr", item=item)

    def completeness(self, project: str, item_ref: str) -> dict:
        self._item(project, item_ref)
        haz = self.hazards(project, item_ref)
        ev = [e for e in self.events(project, item_ref) if e["status"] != "rejected"]
        goals = [g for g in self.goals(project, item_ref) if g["status"] != "rejected"]
        fsrs = [r for r in self.list(project, "fsr", item=item_ref) if r["status"] != "rejected"]
        tsrs = [r for r in self.list(project, "tsr", item=item_ref) if r["status"] != "rejected"]
        reqs = fsrs + tsrs
        covered = {e for g in goals for e in g["payload"]["events"]}
        ev_by_haz = {e["payload"]["hazard"] for e in ev}
        has_child = lambda ref, kind: any(self.db.rec_get(project, c) and self.db.rec_get(project, c)["kind"] == kind
                                          for c in self.trace.children(project, ref, "refined_by"))
        checks = [
            ("hazards_triaged", "All suggested hazards accepted or rejected", [h["ref"] for h in haz if h["status"] == "proposed"]),
            ("hazards_have_events", "Every accepted hazard has hazardous events", [h["ref"] for h in haz if h["status"] == "accepted" and h["ref"] not in ev_by_haz]),
            ("events_approved", "All hazardous events assessed and approved", [e["ref"] for e in ev if e["status"] != "approved"]),
            ("goals_cover_events", "Every approved ASIL-rated event is covered by a safety goal",
             [e["ref"] for e in ev if e["status"] == "approved" and e["payload"]["asil"] not in (None, "QM") and e["ref"] not in covered]),
            ("goals_approved", "All safety goals approved", [g["ref"] for g in goals if g["status"] != "approved"]),
            ("goals_have_fsr", "Every safety goal has a functional safety requirement", [g["ref"] for g in goals if not has_child(g["ref"], "fsr")]),
            ("fsr_have_tsr", "Every FSR has a technical safety requirement", [r["ref"] for r in fsrs if not has_child(r["ref"], "tsr")]),
            ("reqs_allocated", "Every requirement is allocated to an architecture element", [r["ref"] for r in reqs if not r["payload"]["allocated_to"]]),
            ("reqs_verified", "Every requirement has a verification reference", [r["ref"] for r in reqs if not r["payload"]["verification"]]),
            ("reqs_approved", "All requirements approved", [r["ref"] for r in reqs if r["status"] != "approved"]),
            ("no_suspect", "No item is suspect after upstream changes",
             [r["ref"] for r in ev + goals + reqs if r["status"] == "suspect"]),
        ]
        # mandatory links: goal->FSR ; FSR->TSR ; every requirement -> allocation and verification
        total = len(goals) + len(fsrs) + 2 * len(reqs)
        missing = (sum(1 for g in goals if not has_child(g["ref"], "fsr")) + sum(1 for r in fsrs if not has_child(r["ref"], "tsr"))
                   + sum(1 for r in reqs if not r["payload"]["allocated_to"]) + sum(1 for r in reqs if not r["payload"]["verification"]))
        return {"item": item_ref,
                "checks": [{"id": i, "description": d, "passed": not f, "failing": f} for i, d, f in checks],
                "mandatory_links": total, "satisfied_links": total - missing,
                "traceability_completeness": round((total - missing) / total, 3) if total else 0.0,
                "complete": all(not f for _, _, f in checks) and total > 0}

    def trace_matrix(self, project: str, item_ref: str) -> list[dict]:
        rows = []
        for g in [g for g in self.goals(project, item_ref) if g["status"] != "rejected"]:
            gp = g["payload"]
            fsrs = [self.db.rec_get(project, c) for c in self.trace.children(project, g["ref"], "refined_by")] or [None]
            for f in fsrs:
                tsrs = [self.db.rec_get(project, c) for c in self.trace.children(project, f["ref"], "refined_by")] if f else []
                for t in tsrs or [None]:
                    leaf = t or f
                    lp = leaf["payload"] if leaf else {}
                    rows.append({"goal": g["ref"], "goal_text": gp["text"], "asil": gp["asil"],
                                 "events": ";".join(gp["events"]),
                                 "fsr": f["ref"] if f else "", "fsr_text": f["payload"]["text"] if f else "",
                                 "fsr_status": f["status"] if f else "", "tsr": t["ref"] if t else "",
                                 "tsr_text": t["payload"]["text"] if t else "", "tsr_status": t["status"] if t else "",
                                 "allocated_to": lp.get("allocated_to", ""), "verification": ";".join(lp.get("verification", []))})
        return rows

    def metrics(self, project: str, item_ref: str) -> dict:
        comp = self.completeness(project, item_ref)
        ev = [e for e in self.events(project, item_ref) if e["payload"].get("asil")]
        with_sug = [e for e in ev if e["payload"].get("suggested")]
        dist: dict[str, int] = {}
        for e in ev:
            if e["status"] == "approved":
                dist[e["payload"]["asil"]] = dist.get(e["payload"]["asil"], 0) + 1
        return {"recommendation_acceptance": self.acceptance(project, ["hazard", "event", "goal", "fsr", "tsr"]),
                "suggestion_adoption": {"assessed_with_suggestion": len(with_sug),
                                        "adopted": sum(1 for e in with_sug if e["payload"].get("suggestion_adopted"))},
                "traceability_completeness": comp["traceability_completeness"], "asil_distribution": dist,
                "counts": {k: len(self.list(project, k, item=item_ref)) for k in ("hazard", "event", "goal", "fsr", "tsr")}}

    def report(self, project: str, item_ref: str, fmt: str, actor: str) -> tuple[str, str]:
        item = self._item(project, item_ref)
        comp, rows = self.completeness(project, item_ref), self.trace_matrix(project, item_ref)
        self.audit(actor, "report", project, item_ref, {"format": fmt})
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(rows[0]) if rows else ["goal"])
            w.writeheader(); w.writerows(rows)
            return buf.getvalue(), "text/csv"
        evs = self.events(project, item_ref)
        goals = self.goals(project, item_ref)
        decisions = [r for r in self.db.rec_list(project, self.module) if r["approved_by"] and r["payload"].get("item") == item_ref]
        if fmt == "json":
            return json.dumps({"item": item, "hazards": self.hazards(project, item_ref), "events": evs, "goals": goals,
                               "requirements": self.requirements(project, item_ref), "trace": rows,
                               "completeness": comp, "ruleset": rules.RULESET_VERSION}, indent=2), "application/json"
        md = [f"# HARA report: {item['payload']['name']} ({item_ref})", "",
              f"ASIL rule set: `{rules.RULESET_VERSION}`. AI-generated content is advisory and requires qualified review.", "",
              "## Hazardous events", "", "| Ref | Function | Malfunction | Situation | S | E | C | ASIL | Status |", "|---|---|---|---|---|---|---|---|---|"]
        for e in evs:
            p = e["payload"]
            md.append(f"| {e['ref']} | {p['function']} | {p['guideword']} | {p['situation']} | {p['S']} | {p['E']} | {p['C']} | {p['asil']} | {e['status']} |")
        md += ["", "## Safety goals", "", "| Ref | ASIL | Goal | Safe state | FTTI ms | Status |", "|---|---|---|---|---|---|"]
        for g in goals:
            p = g["payload"]
            md.append(f"| {g['ref']} | {p['asil']} | {p['text']} | {p['safe_state']} | {p['ftti_ms']} | {g['status']} |")
        md += ["", "## Traceability", "", "| Goal | ASIL | FSR | TSR | Allocated to | Verification |", "|---|---|---|---|---|---|"]
        for r in rows:
            md.append(f"| {r['goal']} | {r['asil']} | {r['fsr']} | {r['tsr']} | {r['allocated_to']} | {r['verification']} |")
        md += ["", f"## Completeness ({comp['traceability_completeness']:.0%} of mandatory links)", ""]
        md += [f"- [{'x' if c['passed'] else ' '}] {c['description']}" + (f" (open: {', '.join(c['failing'])})" if c["failing"] else "") for c in comp["checks"]]
        md += ["", "## Approval evidence", ""]
        md += [f"- {r['ref']} ({r['kind']}) approved by {r['approved_by']} at {r['approved_at']}" for r in decisions] or ["- none yet"]
        return "\n".join(md), "text/markdown"
