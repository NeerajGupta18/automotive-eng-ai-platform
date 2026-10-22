"""ADAS virtual validation of the AEB function: requirements, scenarios, MIL/SIL campaigns, release.

  requirements   machine-checkable (scenario selector + check). Loaded from the library or written by an
                 engineer; a second person approves new or changed ones
  scenarios      Euro NCAP-style catalog and SOTIF edge cases (approved when the library is loaded). New ones
                 from boundary-value analysis, plain-language descriptions or manual entry are proposals that
                 a second person approves before they count
  campaigns      every approved scenario on the compiled C code (SIL; the model when no compiler is
                 available), every approved requirement judged, back-to-back against the model, optional
                 mutation testing. Stored with the firmware hash, the calibration and the exact versions of
                 the scenarios and requirements used (evidence)
  release        four-eyes, and impossible when a release-blocking requirement is not passed, back-to-back
                 failed, the evidence came from the model only, or anything it was based on has changed since
  traceability   requirement -> scenario (verified_by) and requirement -> campaign (verified_in). Changing an
                 approved requirement or scenario makes released campaigns that used it suspect"""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import zipfile

from app.core.db import now
from app.core.errors import Conflict, Invalid
from app.core.workflow import Workflow
from app.modules.adas import authoring, campaign, sil
from app.modules.adas import requirements as rq
from app.modules.adas.model import CAL, CAL_INFO, STATE_NAMES, AebModel, calibration_changes, make_calibration
from app.modules.adas.report import markdown
from app.modules.adas.scenarios import ROAD_FILE, catalog, road_xodr, to_openscenario
from app.modules.adas.sim import simulate

OPEN = ("proposed", "assessed", "approved", "suspect")
REQ_FIELDS = ("id", "title", "text", "category", "release_blocking", "check", "params", "applies")


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


class AdasService(Workflow):
    def __init__(self, container):
        super().__init__(container, "adas")

    # ================================================================ helpers
    @staticmethod
    def req_def(rec: dict) -> dict:
        return {k: rec["payload"][k] for k in REQ_FIELDS}

    @staticmethod
    def scn_def(rec: dict) -> dict:
        return rec["payload"]["scenario"]

    def requirements(self, project: str, status: str | None = None) -> list[dict]:
        return self.list(project, "requirement", status)

    def scenarios(self, project: str, status: str | None = None, family: str | None = None) -> list[dict]:
        return [s for s in self.list(project, "scenario", status) if not family or s["payload"]["family"] == family]

    def _approved(self, project: str) -> tuple[list[dict], list[dict]]:
        return self.requirements(project, "approved"), self.scenarios(project, "approved")

    def _relink(self, project: str, actor: str) -> None:
        """requirement -> scenario links follow the selectors (rejected records are left out)."""
        scns = [s for s in self.scenarios(project) if s["status"] != "rejected"]
        current: dict[str, set] = {}
        for link in self.db.links(project):
            if link["rel"] == "verified_by":
                current.setdefault(link["src"], set()).add(link["dst"])
        for r in self.requirements(project):
            dsts = [] if r["status"] == "rejected" else [s["ref"] for s in scns if rq.applies(self.req_def(r), self.scn_def(s))]
            if set(dsts) != current.get(r["ref"], set()):                    # only touch what changed
                self.trace.replace_out(project, r["ref"], "verified_by", dsts, actor)

    def _invalidate(self, project: str, actor: str, reason: str, *, req_id: str | None = None, scn_id: str | None = None) -> list[str]:
        """Released campaigns that used a changed requirement or scenario become suspect."""
        hit = []
        for c in self.list(project, "campaign", "approved"):
            p = c["payload"]
            used = (req_id and any(x["id"] == req_id for x in p["requirement_set"])) or \
                   (scn_id and any(x["id"] == scn_id for x in p["scenario_set"]))
            if used:
                self.db.rec_update(project, c["ref"], actor, {**p, "suspect_reason": reason}, "suspect", event="suspect")
                hit.append(c["ref"])
        if hit:
            self.audit(actor, "campaign.suspect", project, None, {"reason": reason, "campaigns": hit})
        return hit

    # ================================================================ library
    def load_library(self, project: str, actor: str) -> dict:
        try:
            lib = rq.load_library()
            scns = [authoring.validate_scenario(s) for s in catalog()]
        except ValueError as e:
            raise Invalid(f"Library is invalid: {e}") from e
        new_r = new_s = 0
        for r in lib["requirements"]:
            payload = {"title": f"{r['id']}: {r['title']}", **r, "origin": "library",
                       "library": f"{lib['name']} v{lib['version']}"}
            _, created = self.create(project, "requirement", "AR", payload, actor, status="approved", key=r["id"], machine=False)
            new_r += created
        for s in scns:
            payload = {"title": f"{s['id']}: {s['title']}", "scenario": s, "family": s["family"], "origin": "catalog",
                       "source": s["source"]}
            _, created = self.create(project, "scenario", "SC", payload, actor, status="approved", key=s["id"], machine=False)
            new_s += created
        self._relink(project, actor)
        self.audit(actor, "library.load", project, None, {"requirements": new_r, "scenarios": new_s})
        return {"library": lib["name"], "version": lib["version"], "note": lib["note"],
                "requirements_added": new_r, "scenarios_added": new_s}

    # ================================================================ requirements
    def _check_req(self, data: dict) -> dict:
        try:
            return rq.validate(data)
        except ValueError as e:
            raise Invalid(str(e)) from e

    def create_requirement(self, project: str, data: dict, actor: str) -> dict:
        r = self._check_req(data)
        if any(x["payload"]["id"] == r["id"] for x in self.requirements(project)):
            raise Conflict(f"Requirement id {r['id']} already exists")
        rec, _ = self.create(project, "requirement", "AR", {"title": f"{r['id']}: {r['title']}", **r, "origin": "manual"},
                             actor, key=r["id"], machine=False)
        self._relink(project, actor)
        self.audit(actor, "requirement.create", project, rec["ref"], {"id": r["id"]})
        return rec

    def edit_requirement(self, project: str, ref: str, data: dict, actor: str) -> dict:
        rec = self.get(project, ref, "requirement")
        r = self._check_req({**data, "id": rec["payload"]["id"]})            # the id never changes
        was = rec["status"]
        out = self.edit(project, ref, actor, {**rec["payload"], **r, "title": f"{r['id']}: {r['title']}"})
        self._relink(project, actor)
        if was == "approved":
            self._invalidate(project, actor, f"requirement {r['id']} changed", req_id=r["id"])
        return out

    def decide_requirement(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        self.get(project, ref, "requirement")
        out = self.decide(project, ref, actor, decision, comment)
        self._relink(project, actor)
        return out

    # ================================================================ scenarios
    def _check_scn(self, data: dict) -> dict:
        try:
            s = authoring.validate_scenario(data)
        except ValueError as e:
            raise Invalid(str(e)) from e
        return s

    def _add_scenario(self, project: str, s: dict, actor: str, origin: str, *, machine: bool, extra: dict | None = None) -> dict:
        if any(x["payload"]["scenario"]["id"] == s["id"] for x in self.scenarios(project)):
            raise Conflict(f"Scenario id {s['id']} already exists")
        payload = {"title": f"{s['id']}: {s['title']}", "scenario": s, "family": s["family"], "origin": origin,
                   "source": s["source"], **(extra or {})}
        rec, _ = self.create(project, "scenario", "SC", payload, actor, key=s["id"], machine=machine)
        self._relink(project, actor)
        self.audit(actor, "scenario.propose", project, rec["ref"], {"id": s["id"], "origin": origin})
        return rec

    def create_scenario(self, project: str, data: dict, actor: str) -> dict:
        return self._add_scenario(project, self._check_scn(data), actor, "manual", machine=False)

    def propose_scenarios(self, project: str, actor: str) -> dict:
        """Boundary-value analysis for the approved requirements; proposals need approval."""
        reqs = [self.req_def(r) for r in self.requirements(project, "approved")]
        if not reqs:
            raise Conflict("No approved requirements: load the library first")
        existing = [self.scn_def(s) for s in self.scenarios(project) if s["status"] != "rejected"]
        out = []
        for p in authoring.propose_boundary(reqs, existing):
            rec = self._add_scenario(project, p["scenario"], actor, "boundary-value analysis", machine=True,
                                     extra={"proposal_reason": p["reason"], "for_requirement": p["requirement"]})
            out.append({"ref": rec["ref"], "id": p["scenario"]["id"], "reason": p["reason"]})
        return {"created": len(out), "proposals": out,
                "message": "No gaps: every requirement boundary already has a scenario" if not out else
                           f"{len(out)} scenario(s) proposed; a reviewer approves them before they count"}

    def scenario_from_text(self, project: str, text: str, actor: str, use_llm: bool = False) -> dict:
        ids = {s["payload"]["scenario"]["id"] for s in self.scenarios(project)}
        n = 1
        while f"TXT-{n:02d}" in ids:
            n += 1
        try:
            if use_llm and self.c.settings.llm_backend != "mock":
                parsed = authoring.from_text_llm(text, self.c.llm, f"TXT-{n:02d}")
            else:
                parsed = authoring.from_text(text, f"TXT-{n:02d}")
                if use_llm:
                    parsed["llm"] = "not used: the LLM backend is the offline mock"
        except ValueError as e:
            raise Invalid(str(e)) from e
        extra = {k: parsed.get(k) for k in ("understood", "assumptions", "not_modelled", "llm") if parsed.get(k) is not None}
        rec = self._add_scenario(project, parsed["scenario"], actor, "plain language" + (" + LLM" if parsed["origin"] == "llm" else ""),
                                 machine=False, extra={"description": " ".join(text.split())[:500], **extra})
        return {"scenario": rec, **extra}

    def edit_scenario(self, project: str, ref: str, data: dict, actor: str) -> dict:
        rec = self.get(project, ref, "scenario")
        s = self._check_scn({**data, "id": rec["payload"]["scenario"]["id"]})
        was = rec["status"]
        out = self.edit(project, ref, actor, {**rec["payload"], "scenario": s, "family": s["family"],
                                              "title": f"{s['id']}: {s['title']}", "source": s["source"]})
        self._relink(project, actor)
        if was == "approved":
            self._invalidate(project, actor, f"scenario {s['id']} changed", scn_id=s["id"])
        return out

    def decide_scenario(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        self.get(project, ref, "scenario")
        out = self.decide(project, ref, actor, decision, comment)
        self._relink(project, actor)
        return out

    def scenario(self, project: str, ref: str) -> dict:
        rec = self.get(project, ref, "scenario")
        reqs = [self.req_def(r) for r in self.requirements(project) if r["status"] != "rejected"]
        return {**rec, "requirements": [r["id"] for r in reqs if rq.applies(r, self.scn_def(rec))]}

    def _controller(self, target: str, overrides: dict | None):
        try:
            cal = make_calibration(overrides)
        except ValueError as e:
            raise Invalid(str(e)) from e
        if target == "model":
            return AebModel(cal), cal, None
        if target != "sil":
            raise Invalid("target must be 'model' or 'sil'")
        try:
            b = sil.build(None)
            return sil.SilController(b["path"], None if cal == CAL else cal), cal, b["hash"]
        except sil.SilUnavailable as e:
            raise Conflict(f"SIL is not available on this server: {e}") from e

    def run_scenario(self, project: str, ref: str, target: str = "model", overrides: dict | None = None) -> dict:
        """One simulation with time series for plots; nothing is stored."""
        rec = self.get(project, ref, "scenario")
        scn = self.scn_def(rec)
        ctrl, cal, fw = self._controller(target, overrides)
        res = simulate(scn, ctrl, trace_every=5)
        verdicts = []
        for r in self.requirements(project, "approved"):
            d = self.req_def(r)
            if rq.applies(d, scn):
                verdicts.append({"requirement": d["id"], "title": d["title"], "release_blocking": d["release_blocking"],
                                 **rq.judge(d, res["metrics"])})
        return {"scenario": scn, "target": target, "firmware_hash": fw, "calibration_changes": calibration_changes(cal),
                "metrics": res["metrics"], "trace": res["trace"], "verdicts": verdicts, "states": STATE_NAMES}

    # ================================================================ OpenSCENARIO export
    def openscenario(self, project: str, ref: str) -> tuple[str, str]:
        s = self.scn_def(self.get(project, ref, "scenario"))
        return to_openscenario(s), f"{s['id']}.xosc"

    def openscenario_zip(self, project: str) -> bytes:
        scns = [self.scn_def(s) for s in self.scenarios(project, "approved")]
        if not scns:
            raise Conflict("No approved scenarios to export")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(ROAD_FILE, road_xodr())
            for s in scns:
                z.writestr(f"{s['id']}.xosc", to_openscenario(s))
            z.writestr("scenarios.json", json.dumps(scns, indent=2))
            z.writestr("README.txt", (
                "ASAM OpenSCENARIO 1.1 files (+ OpenDRIVE 1.7 road) exported by automotive-eng-ai.\n"
                "Each .xosc places the ego and target vehicles with their speeds; the AEB under test is supplied by the\n"
                "tool that plays the file (e.g. esmini with a controller). Sensor range, latency, phantom objects and the\n"
                "driver model are parameters of the platform's own simulation; range and friction are also expressed\n"
                "as fog and road condition. scenarios.json holds every parameter.\n"))
        return buf.getvalue()

    # ================================================================ campaigns
    def _sets(self, reqs: list[dict], scns: list[dict]) -> tuple[list[dict], list[dict]]:
        return ([{"ref": r["ref"], "id": r["payload"]["id"], "hash": _digest(self.req_def(r))} for r in reqs],
                [{"ref": s["ref"], "id": s["payload"]["scenario"]["id"], "hash": _digest(self.scn_def(s))} for s in scns])

    def run_campaign(self, project: str, actor: str, target: str = "auto", overrides: dict | None = None,
                     back_to_back: bool = True, mutation: bool = False, note: str = "") -> dict:
        if target not in ("auto", "sil", "model"):
            raise Invalid("target must be auto, sil or model")
        reqs, scns = self._approved(project)
        if not reqs or not scns:
            raise Conflict("Needs approved requirements and scenarios: load the library first")
        try:
            cal = make_calibration(overrides)
        except ValueError as e:
            raise Invalid(str(e)) from e
        try:
            res = campaign.run_campaign([self.scn_def(s) for s in scns], [self.req_def(r) for r in reqs], target=target,
                                        cal=cal, do_b2b=back_to_back, do_mutation=mutation)
        except sil.SilUnavailable as e:
            raise Conflict(f"SIL is not available on this server: {e}") from e
        req_set, scn_set = self._sets(reqs, scns)
        ref_of = {s["payload"]["scenario"]["id"]: s["ref"] for s in scns}
        v = res["verdict"]
        payload = {
            "title": f"{res['target'].upper()} campaign: {'PASSED' if v['passed'] else 'FAILED'}"
                     + (" (calibration change)" if calibration_changes(cal) else ""),
            "target": res["target"], "verdict": v, "requirements": res["requirements"],
            "scenarios": [{"ref": ref_of[x["id"]], **x} for x in res["scenarios"]],
            "back_to_back": res.get("back_to_back"), "mutation": res.get("mutation"), "firmware": res["firmware"],
            "calibration": dataclasses.asdict(cal), "calibration_changes": calibration_changes(cal),
            "requirement_set": req_set, "scenario_set": scn_set, "notes": res["notes"], "seconds": res["seconds"],
            "note": " ".join((note or "").split())[:500], "run_by": actor, "run_at": now()}
        rec, _ = self.create(project, "campaign", "CMP", payload, actor, status="proposed", machine=False)
        for r in reqs:
            self.db.link_add(project, r["ref"], rec["ref"], "verified_in", actor)
        self.audit(actor, "campaign.run", project, rec["ref"], {"target": res["target"], "passed": v["passed"],
                                                                 "blocking_not_passed": v["blocking_not_passed"]})
        return self._with_status(project, rec)

    def _current(self, project: str) -> tuple[list[dict], list[dict], str]:
        req_set, scn_set = self._sets(*self._approved(project))
        return req_set, scn_set, sil.status()["firmware_hash"]

    def _stale(self, project: str, p: dict, current: tuple | None = None) -> list[str]:
        req_set, scn_set, fw = current or self._current(project)
        reasons = []
        if req_set != p["requirement_set"]:
            reasons.append("the approved requirements changed")
        if scn_set != p["scenario_set"]:
            reasons.append("the approved scenarios changed")
        if p.get("firmware") and fw != p["firmware"]["hash"]:
            reasons.append("the firmware source changed")
        return reasons

    def _with_status(self, project: str, rec: dict) -> dict:
        return {**rec, "stale": self._stale(project, rec["payload"]) if rec["status"] in ("proposed", "approved") else []}

    def campaigns(self, project: str) -> list[dict]:
        out, current = [], self._current(project)
        for c in reversed(self.list(project, "campaign")):
            p = c["payload"]
            mut = p.get("mutation") or {}
            out.append({"ref": c["ref"], "status": c["status"], "title": p["title"], "target": p["target"],
                        "passed": p["verdict"]["passed"], "blocking_not_passed": p["verdict"]["blocking_not_passed"],
                        "exploratory_findings": p["verdict"]["exploratory_findings"],
                        "requirements": p["verdict"]["requirements"], "scenarios": len(p["scenario_set"]),
                        "back_to_back": None if not p.get("back_to_back") else p["back_to_back"]["passed"],
                        "mutation_score": mut.get("score"), "calibration_changes": p["calibration_changes"],
                        "firmware": (p.get("firmware") or {}).get("version"), "run_by": p["run_by"], "run_at": p["run_at"],
                        "approved_by": c["approved_by"], "stale": self._stale(project, p, current) if c["status"] in ("proposed", "approved") else [],
                        "suspect_reason": p.get("suspect_reason", ""), "seconds": p["seconds"]})
        return out

    def get_campaign(self, project: str, ref: str) -> dict:
        return self._with_status(project, self.get(project, ref, "campaign"))

    def decide_campaign(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        """'approved' releases the AEB software + calibration on this evidence."""
        rec = self.get(project, ref, "campaign")

        def gate(r: dict) -> None:
            p = r["payload"]
            if p["target"] != "sil":
                raise Conflict("Release needs evidence from the compiled C code (SIL); this campaign ran on the model only")
            if not p["verdict"]["passed"]:
                why = p["verdict"]["blocking_not_passed"] + (["back-to-back"] if p["verdict"].get("back_to_back_failed") else [])
                raise Conflict(f"Release blocked: not passed: {', '.join(why)}")
            if not p.get("back_to_back") or not p["back_to_back"]["passed"]:
                raise Conflict("Release blocked: back-to-back comparison with the model was not run or failed")
            stale = self._stale(project, p)
            if stale:
                raise Conflict(f"Release blocked: the evidence is out of date ({'; '.join(stale)}). Run a new campaign.")
            if decision == "approved" and len((comment or "").strip()) < 5:
                raise Invalid("A release needs a comment (what was reviewed)")
        out = self.decide(project, ref, actor, decision, comment, gate=gate if decision == "approved" else None)
        if decision == "approved":
            self.audit(actor, "release", project, ref, {"firmware": (rec["payload"].get("firmware") or {}).get("hash"),
                                                        "calibration_changes": rec["payload"]["calibration_changes"]})
        return self._with_status(project, out)

    # ================================================================ views
    def coverage(self, project: str) -> dict:
        reqs = [r for r in self.requirements(project) if r["status"] != "rejected"]
        scns = [s for s in self.scenarios(project) if s["status"] != "rejected"]
        rows = []
        for r in reqs:
            d = self.req_def(r)
            hits = [s for s in scns if rq.applies(d, self.scn_def(s))]
            ok = [s["ref"] for s in hits if s["status"] == "approved"]
            rows.append({"ref": r["ref"], "id": d["id"], "title": d["title"], "status": r["status"],
                         "release_blocking": d["release_blocking"], "check": rq.CHECK_TEXT[d["check"]],
                         "approved_scenarios": len(ok), "proposed_scenarios": len(hits) - len(ok),
                         "scenario_refs": [s["ref"] for s in hits]})
        families = {}
        for s in scns:
            families.setdefault(s["payload"]["family"], {"approved": 0, "proposed": 0, "other": 0})
            families[s["payload"]["family"]][s["status"] if s["status"] in ("approved", "proposed") else "other"] += 1
        gaps = [r["id"] for r in rows if r["status"] == "approved" and r["approved_scenarios"] == 0]
        return {"requirements": rows, "families": families, "gaps": gaps,
                "covered": sum(1 for r in rows if r["approved_scenarios"]), "total": len(rows)}

    def status(self, project: str) -> dict:
        reqs, scns = self.requirements(project), self.scenarios(project)
        camps = self.campaigns(project)
        released = next((c for c in camps if c["status"] == "approved"), None)
        count = lambda rows: {k: sum(1 for r in rows if r["status"] == k) for k in ("proposed", "approved", "suspect", "rejected")}  # noqa: E731
        return {"library_loaded": bool(reqs), "requirements": count(reqs), "scenarios": count(scns),
                "sil": sil.status(), "latest_campaign": camps[0] if camps else None, "current_release": released,
                "campaigns": len(camps)}

    @staticmethod
    def reference() -> dict:
        return {"calibration": [{"name": k, "default": getattr(CAL, k), "min": v[0], "max": v[1], "unit": v[2], "meaning": v[3]}
                                for k, v in CAL_INFO.items()],
                "scenario_bounds": {k: {"min": lo, "max": hi} for k, (lo, hi) in authoring.BOUNDS.items()},
                "families": list(authoring.FAMILIES), "targets": list(authoring.TARGETS),
                "checks": rq.CHECK_TEXT, "states": STATE_NAMES,
                "mutants": [{"id": m["id"], "description": m["description"]} for m in campaign.MUTANTS]}

    def firmware(self) -> dict:
        from app.modules.codereview.analyzer import analyze
        c_text, h_text = sil.sources()
        files, findings = [], []
        for name, text in ((sil.C_FILE, c_text), (sil.H_FILE, h_text)):
            files.append({"name": name, "lines": text.count("\n") + 1, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                          "text": text})
            findings += [{"file": name, **{k: f[k] for k in ("rule", "title", "severity", "line", "message")}} for f in analyze(name, text)]
        return {"files": files, "review_findings": findings, "sil": sil.status(),
                "mutants": [{"id": m["id"], "description": m["description"]} for m in campaign.MUTANTS]}

    # ================================================================ report
    def report(self, project: str, ref: str, fmt: str = "md") -> tuple[str, str]:
        rec = self.get_campaign(project, ref)
        if fmt == "json":
            return json.dumps(rec, indent=2, default=str), "application/json"
        if fmt != "md":
            raise Invalid("format must be md or json")
        return markdown(project, rec), "text/markdown"
