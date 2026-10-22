"""Field issues: customer complaints with vehicle recordings, grouped into issues and investigated by the
engineering agent (playbook `field_investigation`).

  builds       registry of AEB software builds in the field (firmware, calibration, fleet size, validation evidence)
  complaints   dealer / service complaint text + optional event recording (validated, stored once per content hash)
  issues       complaints with the same symptom on the same build; a new issue starts an investigation automatically
               (FIELD_AUTO_INVESTIGATE). A complaint that joins an issue whose investigation has not changed anything
               yet restarts it with the new evidence
  gates        the agent writes nothing on its own: the safety rating and the corrective actions are agent steps a
               person confirms. Scenario proposals and the verification campaign then go through the normal ADAS
               four-eyes rules, and an issue is closed by a second person only when the verification evidence is
               released (or, without a technical fix, with a documented decision)

The analysis functions used by the agent's tools are deterministic and only read; the full results are recomputed
on demand for the UI (and cached per process)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.core.db import now
from app.core.errors import Conflict, Invalid, NotFound
from app.core.workflow import Workflow
from app.modules.adas import requirements as rq
from app.modules.adas.model import calibration_changes, make_calibration
from app.modules.adas.scenarios import catalog
from app.modules.field import builds as B
from app.modules.field import investigate as I
from app.modules.field import logs as L
from app.modules.field import report as R
from app.modules.field import triage as T

SAMPLES = Path(__file__).resolve().parents[3] / "data" / "samples" / "field"
MAX_COMPLAINTS_PER_RUN = 8
CLOSED = ("approved", "rejected")


def _short(text: str, n: int = 220) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


class FieldService(Workflow):
    def __init__(self, container):
        super().__init__(container, "field")
        self._cache: dict[tuple, dict] = {}

    # ================================================================ builds
    def builds(self, project: str) -> list[dict]:
        return self.list(project, "build")

    def registry(self, project: str) -> list[dict]:
        return [b["payload"]["build"] for b in self.builds(project)]

    def build(self, project: str, version: str) -> dict:
        b = next((x for x in self.registry(project) if x["version"] == version), None)
        if b is None:
            raise NotFound(f"Build {version} is not in the build registry of this project")
        return b

    def add_build(self, project: str, data: dict, actor: str) -> dict:
        try:
            b = B.validate(data)
        except ValueError as e:
            raise Invalid(str(e)) from e
        if any(x["version"] == b["version"] for x in self.registry(project)):
            raise Conflict(f"Build {b['version']} is already registered")
        rec, _ = self.create(project, "build", "SWB", {"title": B.describe(b), "build": b}, actor, status="active", key=b["version"],
                             machine=False)
        self.audit(actor, "build.add", project, rec["ref"], {"version": b["version"], "firmware": b["firmware"]})
        return rec

    def load_sample_builds(self, project: str, actor: str) -> dict:
        data = json.loads((SAMPLES / "builds.json").read_text())
        added = 0
        for b in data["builds"]:
            if not any(x["version"] == b["version"] for x in self.registry(project)):
                self.add_build(project, b, actor)
                added += 1
        return {"added": added, "builds": len(self.registry(project))}

    def baseline(self, project: str, build: dict) -> dict | None:
        """The last validated build this one derives from (the reference for 'what changed')."""
        reg = {b["version"]: b for b in self.registry(project)}
        seen, v = set(), build.get("previous")
        while v and v in reg and v not in seen:
            seen.add(v)
            if reg[v].get("validated"):
                return reg[v]
            v = reg[v].get("previous")
        return None

    # ================================================================ logs
    def _store_log(self, project: str, name: str, content: bytes, actor: str) -> dict:
        try:
            log = L.parse(content)
        except L.LogError as e:
            raise Invalid(f"Recording rejected: {e}") from e
        sha = hashlib.sha256(content).hexdigest()
        self.db._q("INSERT OR IGNORE INTO field_logs(project,sha256,filename,content,rows,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                   (project, sha, _short(name, 120), content.decode("utf-8-sig"), log["rows"], actor, now()))
        row = self.db._q("SELECT id FROM field_logs WHERE project=? AND sha256=?", (project, sha))[0]
        return {"id": row["id"], "sha256": sha, "filename": _short(name, 120), "rows": log["rows"],
                "duration_s": round(log["rows"] * L.DT, 2), "header": log["header"]}

    def _log(self, project: str, info: dict | None) -> dict | None:
        if not info:
            return None
        rows = self.db._q("SELECT content FROM field_logs WHERE project=? AND id=?", (project, info["id"]))
        return L.parse(rows[0]["content"]) if rows else None

    def log_csv(self, project: str, ref: str) -> tuple[str, str]:
        c = self.get(project, ref, "complaint")
        info = c["payload"].get("log")
        if not info:
            raise NotFound(f"{ref} has no recording")
        rows = self.db._q("SELECT content, filename FROM field_logs WHERE project=? AND id=?", (project, info["id"]))
        if not rows:
            raise NotFound("recording not found")
        return rows[0]["content"], rows[0]["filename"]

    # ================================================================ complaints and issues
    def _understand(self, text: str) -> dict:
        if self.c.settings.field_triage_llm and self.c.settings.llm_backend != "mock":
            return T.with_llm(text, self.c.llm)
        return T.rules(text)

    def submit(self, project: str, text: str, actor: str, *, vin: str = "", build: str = "", reported_at: str = "", dealer: str = "",
               log_name: str | None = None, log_content: bytes | None = None, investigate: bool = True) -> dict:
        out = self._add_complaint(project, text, actor, vin=vin, build=build, reported_at=reported_at, dealer=dealer,
                                  log_name=log_name, log_content=log_content)
        run = self._trigger(project, out["issue"], actor) if investigate else None
        return {**out, "run": run}

    def import_batch(self, project: str, items: list[dict], actor: str, investigate: bool = True) -> dict:
        """Several complaints at once (e.g. the daily export of the dealer system); investigations start after all are filed."""
        if not 1 <= len(items) <= 200:
            raise Invalid("1 to 200 complaints per import")
        added, touched = [], []
        for it in items:
            o = self._add_complaint(project, it.get("text", ""), actor, vin=it.get("vin", ""), build=it.get("build", ""),
                                    reported_at=it.get("reported_at", ""), dealer=it.get("dealer", ""), log_name=it.get("log_name"),
                                    log_content=it.get("log_content"))
            added.append(o["complaint"]["ref"])
            if o["issue"]["ref"] not in touched:
                touched.append(o["issue"]["ref"])
        runs = []
        if investigate:
            for ref in touched:
                r = self._trigger(project, self.get(project, ref, "issue"), actor)
                if r:
                    runs.append({"issue": ref, "run": r["ref"], "status": r["status"]})
        return {"complaints": added, "issues": touched, "runs": runs}

    def _add_complaint(self, project: str, text: str, actor: str, *, vin: str, build: str, reported_at: str, dealer: str,
                       log_name: str | None, log_content: bytes | None) -> dict:
        text = " ".join(str(text or "").split())
        if not 10 <= len(text) <= 4000:
            raise Invalid("complaint text: 10 to 4000 characters")
        info = self._store_log(project, log_name or "recording.csv", log_content, actor) if log_content else None
        build = (build or "").strip() or ((info or {}).get("header") or {}).get("sw_version", "") or "unknown"
        if build != "unknown" and not B.VERSION_RE.match(build):
            raise Invalid("build must look like 2.4.0")
        u = self._understand(text)
        payload = {"title": _short(text, 100), "text": text, "vin": _short(vin, 20), "build": build, "reported_at": _short(reported_at, 40),
                   "dealer": _short(dealer, 80), "understood": u, "symptom": u["symptom"], "crash": u["crash"], "injury": u["injury"],
                   "log": None if not info else {k: info[k] for k in ("id", "sha256", "filename", "rows", "duration_s")},
                   "log_header": None if not info else info["header"], "received_at": now(), "received_by": actor}
        rec, _ = self.create(project, "complaint", "FC", payload, actor, status="open", machine=False)
        issue, created = self._assign(project, rec, actor)
        self.db.rec_update(project, rec["ref"], actor, {**rec["payload"], "issue": issue["ref"]}, "open", event="assigned")
        self.audit(actor, "complaint.submit", project, rec["ref"], {"issue": issue["ref"], "symptom": u["symptom"], "build": build,
                                                                   "log": bool(info), "understood_by": u["by"]})
        return {"complaint": self.get(project, rec["ref"]), "issue": issue, "new_issue": created}

    def _assign(self, project: str, complaint: dict, actor: str) -> tuple[dict, bool]:
        p = complaint["payload"]
        for i in self.list(project, "issue"):
            if i["status"] not in CLOSED and i["payload"]["symptom"] == p["symptom"] and i["payload"]["build"] == p["build"]:
                ip = i["payload"]
                upd = self.db.rec_update(project, i["ref"], actor, {**ip, "complaints": ip["complaints"] + [complaint["ref"]],
                                                                    "new_evidence": True}, i["status"], event="complaint")
                self.db.link_add(project, i["ref"], complaint["ref"], "evidenced_by", actor)
                return upd, False
        title = f"{T.SYMPTOMS[p['symptom']]} on AEB {p['build']}"
        rec, _ = self.create(project, "issue", "FI", {"title": title, "symptom": p["symptom"], "build": p["build"],
                                                      "complaints": [complaint["ref"]], "runs": [], "safety": None, "actions": None,
                                                      "verification": None, "new_evidence": False, "opened_at": now()},
                             actor, status="open", machine=False)
        self.db.link_add(project, rec["ref"], complaint["ref"], "evidenced_by", actor)
        self.audit(actor, "issue.open", project, rec["ref"], {"symptom": p["symptom"], "build": p["build"]})
        return rec, True

    def _trigger(self, project: str, issue: dict, actor: str) -> dict | None:
        """Automatic investigation of new evidence; restarts an investigation that has not written anything yet."""
        if not self.c.settings.field_auto_investigate or issue["status"] in CLOSED or issue["status"] == "actions_taken":
            return None
        runs = issue["payload"].get("runs") or []
        if runs:
            last = self.c.agent.run(project, runs[-1])
            wrote = any(s.get("kind") == "write" and s["status"] in ("done", "error") for s in last["payload"]["steps"])
            if last["status"] in ("running", "waiting") and not issue["payload"].get("new_evidence"):
                return last
            if last["status"] in ("running", "waiting") and not wrote:
                self.c.agent.cancel(project, last["ref"], actor)
            elif last["status"] in ("running", "waiting"):
                return last
        return self.investigate(project, issue["ref"], actor)

    def investigate(self, project: str, ref: str, actor: str) -> dict:
        issue = self.get(project, ref, "issue")
        if issue["status"] in CLOSED:
            raise Conflict(f"{ref} is closed")
        ip = issue["payload"]
        for r in ip.get("runs") or []:
            run = self.c.agent.run(project, r)
            if run["status"] in ("running", "waiting"):
                raise Conflict(f"{r} is still {run['status']}; confirm, skip or cancel it first")
        self.db.rec_update(project, ref, actor, {**ip, "new_evidence": False}, issue["status"], event="investigate")
        run = self.c.agent.start(project, f"Investigate field issue {ref}: {ip['title']}", actor, "field_investigation", {"issue": ref})
        issue = self.get(project, ref, "issue")
        self.db.rec_update(project, ref, actor, {**issue["payload"], "runs": (issue["payload"].get("runs") or []) + [run["ref"]]},
                           issue["status"], event="run")
        self.db.link_add(project, ref, run["ref"], "investigated_in", actor)
        return run

    def complaints(self, project: str) -> list[dict]:
        return list(reversed(self.list(project, "complaint")))

    def complaint(self, project: str, ref: str) -> dict:
        return self.get(project, ref, "complaint")

    # ================================================================ analysis (cached, deterministic)
    def _library(self, project: str) -> tuple[list[dict], list[dict], str]:
        reqs = [self.c.adas.req_def(r) for r in self.c.adas.requirements(project, "approved")]
        scns = [self.c.adas.scn_def(s) for s in self.c.adas.scenarios(project, "approved")]
        if reqs and scns:
            return scns, reqs, "the project's approved ADAS library"
        return catalog(), rq.load_library()["requirements"], "the built-in AEB library (no ADAS library loaded in this project)"

    def _cases(self, project: str, issue: dict) -> list[dict]:
        out = []
        for cref in issue["payload"]["complaints"][-MAX_COMPLAINTS_PER_RUN:]:
            c = self.get(project, cref, "complaint")
            out.append({"ref": cref, "complaint": c, "symptom": c["payload"]["symptom"], "log": self._log(project, c["payload"].get("log"))})
        return out

    def _diag(self, project: str, case: dict, build: dict | None, baseline: dict | None, registry: list[dict]) -> dict:
        if build is None:
            return I._final({"class": "insufficient_data", "confidence": "high", "culprits": [], "first_difference": None,
                             "counterfactual": None, "reproduced": None, "reconstruction": None, "matching_build": None, "notes": [],
                             "evidence": [f"Build '{case['complaint']['payload']['build']}' is not in the build registry: register it (firmware, "
                                          f"calibration) so that the recording can be replayed."]})
        key = (project, case["ref"], (case["complaint"]["payload"].get("log") or {}).get("sha256"), B.signature(build),
               B.signature(baseline) if baseline else None, tuple(sorted(B.signature(b) for b in registry)), case["symptom"])
        if key not in self._cache:
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[key] = I.diagnose(case["log"], case["symptom"], build, baseline, registry, f"FIELD-{case['ref']}")
        return self._cache[key]

    def analysis(self, project: str, ref: str) -> dict:
        """Full per-complaint diagnosis of an issue (what the agent's tools summarise)."""
        issue = self.get(project, ref, "issue")
        registry = self.registry(project)
        try:
            build = self.build(project, issue["payload"]["build"])
        except NotFound:
            build = None
        baseline = self.baseline(project, build) if build else None
        rows = []
        for case in self._cases(project, issue):
            d = self._diag(project, case, build, baseline, registry)
            an = L.analyze(case["log"], case["symptom"]) if case["log"] else None
            rows.append({"ref": case["ref"], "symptom": case["symptom"], "text": case["complaint"]["payload"]["text"],
                         "understood": case["complaint"]["payload"]["understood"], "diagnosis": d, "log_analysis": an})
        agg = I.aggregate([r["diagnosis"] for r in rows])
        return {"issue": ref, "build": build, "baseline": baseline, "aggregate": agg, "complaints": rows}

    def _fix(self, project: str, ref: str) -> dict:
        a = self.analysis(project, ref)
        if not a["build"]:
            return {"kind": "none", "summary": "Unknown build: nothing to search.", "candidates": [], "recommended": None}
        key = ("fix", project, ref, json.dumps([[r["ref"], r["diagnosis"]["class"]] for r in a["complaints"]]), B.signature(a["build"]),
               len(self.c.adas.scenarios(project, "approved")), len(self.c.adas.requirements(project, "approved")))
        if key not in self._cache:
            scns, reqs, _ = self._library(project)
            cls = a["aggregate"]["class"]
            cases = [{"ref": r["ref"], "symptom": r["symptom"], "scenario": (r["diagnosis"].get("reconstruction") or {}).get("scenario"),
                      "log": self._log(project, self.get(project, r["ref"])["payload"].get("log")),
                      "justified": bool((r["diagnosis"].get("counterfactual") or {}).get("collision"))}
                     for r in a["complaints"] if r["diagnosis"]["class"] == cls]
            self._cache[key] = I.fix_search(cls, a["build"], a["baseline"], cases, scns, reqs, self.registry(project), a["aggregate"]["culprits"])
        return self._cache[key]

    # ================================================================ agent tools (read; compact results)
    def tool_issue(self, project: str, ref: str) -> dict:
        i = self.get(project, ref, "issue")
        p = i["payload"]
        try:
            b = self.build(project, p["build"])
        except NotFound:
            b = None
        cs = [self.get(project, r, "complaint") for r in p["complaints"]]
        return {"ref": ref, "title": p["title"], "status": i["status"], "symptom": p["symptom"], "build": p["build"],
                "build_known": b is not None, "build_description": B.describe(b) if b else None,
                "vehicles_on_build": b["vehicles"] if b else None, "validated": b["validated"] if b else None,
                "baseline": (self.baseline(project, b) or {}).get("version") if b else None,
                "complaints": [{"ref": c["ref"], "text": _short(c["payload"]["text"], 160), "location": c["payload"]["understood"]["location"],
                                "speed_kmh": c["payload"]["understood"]["speed_kmh"], "recording": bool(c["payload"].get("log")),
                                "crash_reported": c["payload"]["crash"], "injury_reported": c["payload"]["injury"]} for c in cs[-MAX_COMPLAINTS_PER_RUN:]],
                "complaints_total": len(cs)}

    def tool_analyze(self, project: str, ref: str) -> dict:
        out = []
        for case in self._cases(project, self.get(project, ref, "issue")):
            if not case["log"]:
                out.append({"ref": case["ref"], "recording": False})
                continue
            an = L.analyze(case["log"], case["symptom"])
            tr = an["tracks"][an["event"]["track"]] if an["event"].get("track") is not None else None
            out.append({"ref": case["ref"], "recording": True, "duration_s": an["duration_s"], "sw_version": an["sw_version"],
                        "event": {k: an["event"].get(k) for k in ("kind", "t", "kmh", "max_state", "max_decel")},
                        "object": None if not tr else {k: tr[k] for k in ("d_first_m", "d_last_m", "lifetime_s", "stationary", "vanished_ahead",
                                                                          "phantom_like", "target_kmh_first")},
                        "activations": len(an["activations"]), "warnings": len(an["warnings"]), "driver_braked": bool(an["driver_brake"]),
                        "crash": bool(an["crash"]), "moved_after_stop_m": (an["stop"] or {}).get("moved_after_m")})
        return {"issue": ref, "recordings": sum(1 for o in out if o["recording"]), "complaints": out}

    def tool_replay(self, project: str, ref: str) -> dict:
        a = self.analysis(project, ref)
        rows = []
        for r in a["complaints"]:
            rp = r["diagnosis"].get("reproduced")
            rows.append({"ref": r["ref"], "replayed": bool(rp), "reproduced": (rp or {}).get("reproduced"),
                         "agreement": (rp or {}).get("state_agreement"), "target": (rp or {}).get("target"),
                         "matching_build": r["diagnosis"].get("matching_build")})
        return {"issue": ref, "build": (a["build"] or {}).get("version"), "reproduced": sum(1 for x in rows if x["reproduced"]),
                "of": len(rows), "complaints": rows}

    def tool_reconstruct(self, project: str, ref: str) -> dict:
        a = self.analysis(project, ref)
        rows = []
        for r in a["complaints"]:
            rc = r["diagnosis"].get("reconstruction")
            if not rc:
                rows.append({"ref": r["ref"], "reconstructed": False})
                continue
            s = rc["scenario"] or {}
            rows.append({"ref": r["ref"], "reconstructed": bool(rc["scenario"]), "matched": rc["matched"], "attempts": len(rc["attempts"]),
                         "basis": _short(rc["basis"], 200), "scenario_id": s.get("id"), "family": s.get("family"),
                         "parameters": {k: s[k] for k in ("ego_kmh", "target", "target_kmh", "gap_m", "ghost_distance_m", "ghost_duration_s", "mu",
                                                          "sensor_range_m", "latency_s") if k in s}})
        return {"issue": ref, "matched": sum(1 for x in rows if x.get("matched")), "of": len(rows), "complaints": rows}

    def tool_diagnose(self, project: str, ref: str) -> dict:
        a = self.analysis(project, ref)
        agg = a["aggregate"]
        rows = []
        for n, r in enumerate(a["complaints"]):
            d = r["diagnosis"]
            ev = d["evidence"] if n < 3 else d["evidence"][-1:]
            rows.append({"ref": r["ref"], "class": d["class"], "confidence": d["confidence"],
                         "evidence": [_short(e, 260 if n < 3 else 160) for e in ev[-3:]],
                         "culprits": [c["parameter"] for c in d["culprits"]],
                         "intervention_prevented_collision": (d.get("counterfactual") or {}).get("collision")})
        return {"issue": ref, "class": agg["class"], "title": agg["title"], "consistent": agg["consistent"], "counts": agg["counts"],
                "culprits": agg["culprits"], "build": (a["build"] or {}).get("version"), "baseline": (a["baseline"] or {}).get("version"),
                "complaints": rows}

    def tool_escape(self, project: str, ref: str) -> dict:
        a = self.analysis(project, ref)
        if not a["build"] or a["aggregate"]["class"] not in I.MALFUNCTION:
            return {"issue": ref, "applicable": False}
        scns, reqs, lib_name = self._library(project)
        released = [x for x in self.c.adas.campaigns(project) if x["status"] == "approved"]
        e = I.escape(a["build"], a["baseline"], a["aggregate"]["class"], scns, reqs, released)
        return {"issue": ref, "applicable": True, "library": lib_name, **e}

    def tool_fix(self, project: str, ref: str) -> dict:
        f = self._fix(project, ref)
        cands = [{k: v for k, v in c.items() if k in ("parameter", "value", "version", "fixes_complaints", "fixes_margin_cases", "regression",
                                                       "brake_onset_change_ms", "passes", "free_of_problem", "validated", "is_validated_value",
                                                       "is_field_value")} for c in f["candidates"]]
        return {"issue": ref, "kind": f["kind"], "summary": _short(f["summary"], 600), "recommended": f["recommended"], "candidates": cands[:16]}

    def tool_safety(self, project: str, ref: str) -> dict:
        a = self.analysis(project, ref)
        issue = self.get(project, ref, "issue")
        try:
            fleet = self.build(project, issue["payload"]["build"])["vehicles"]
        except NotFound:
            fleet = 0
        rows = [r for r in a["complaints"] if r["log_analysis"]]
        comps = [self.get(project, r["ref"])["payload"] for r in a["complaints"]]
        sp = I.safety_proposal(issue["payload"]["symptom"], a["aggregate"]["class"], [r["log_analysis"] for r in rows], comps, fleet)
        return {"issue": ref, **sp}

    # ================================================================ agent tools (write: run only after a person confirms)
    def record_safety(self, project: str, ref: str, s: int, e: int, c: int, rationale: str, escalate: bool, actor: str) -> dict:
        from app.modules.hara import rules as hr
        issue = self.get(project, ref, "issue")
        hr.validate_sec(s, e, c)
        safety = {"severity": s, "exposure": e, "controllability": c, "asil": hr.asil(s, e, c), "rationale": _short(rationale, 1200),
                  "escalate": bool(escalate), "confirmed_by": actor, "confirmed_at": now(), "ruleset": hr.RULESET_VERSION}
        self.db.rec_update(project, ref, actor, {**issue["payload"], "safety": safety}, issue["status"], event="safety")
        self.audit(actor, "safety.confirm", project, ref, {"asil": safety["asil"], "escalate": safety["escalate"]})
        return safety

    def apply_actions(self, project: str, ref: str, actor: str, *, propose_scenarios: bool, calibration: dict | None, rollback_to: str | None,
                      tickets: list[dict], summary: str) -> dict:
        issue = self.get(project, ref, "issue")
        if issue["status"] in CLOSED or issue["status"] == "actions_taken":
            raise Conflict(f"{ref} is {issue['status']}; actions were already taken")
        a = self.analysis(project, ref)
        cls = a["aggregate"]["class"]
        created_scn, notes = [], []
        if propose_scenarios and cls in I.MALFUNCTION:
            if not self.c.adas.requirements(project, "approved"):
                notes.append("No ADAS library in this project: the field scenarios were not proposed (load the AEB library first).")
            else:
                have = {s["payload"]["scenario"]["id"] for s in self.c.adas.scenarios(project)}
                for r in a["complaints"]:
                    s = (r["diagnosis"].get("reconstruction") or {}).get("scenario")
                    if not s or r["diagnosis"]["class"] != cls or not r["diagnosis"]["reconstruction"]["matched"] or s["id"] in have:
                        continue
                    rec = self.c.adas._add_scenario(project, s, actor, "field recording (field-issue agent)", machine=False,
                                                    extra={"field_issue": ref, "complaint": r["ref"]})
                    self.db.link_add(project, ref, rec["ref"], "verified_by", actor)
                    created_scn.append({"ref": rec["ref"], "id": s["id"]})
        if calibration:
            try:
                cal = make_calibration(calibration)
            except ValueError as e:
                raise Invalid(f"calibration: {e}") from e
            calibration = {k: calibration[k] for k in sorted(calibration)}
            notes.append("Next: a reviewer approves the field scenarios on the ADAS page; then run the verification campaign with this "
                         "calibration (Field issues page) and have it released by a second person.")
        made = []
        for t in tickets:
            rec = self.c.integration.create_ticket(project, t["summary"], t["description"], t["source_ref"] or ref, actor, t["priority"],
                                                   ["field-issue", "agent"])
            self.db.link_add(project, ref, rec["ref"], "tracked_in", actor)
            made.append({"ref": rec["ref"], "summary": t["summary"], "status": rec["status"]})
        snapshot = {"class": cls, "title": a["aggregate"]["title"], "counts": a["aggregate"]["counts"], "culprits": a["aggregate"]["culprits"],
                    "build": (a["build"] or {}).get("version"), "baseline": (a["baseline"] or {}).get("version"),
                    "evidence": {r["ref"]: r["diagnosis"]["evidence"][:6] for r in a["complaints"]}}
        fix = self._fix(project, ref) if cls in I.MALFUNCTION else None
        actions = {"by": actor, "at": now(), "summary": _short(summary, 600), "scenarios": created_scn, "tickets": made,
                   "calibration": calibration, "calibration_changes": calibration_changes(make_calibration(calibration)) if calibration else None,
                   "rollback_to": rollback_to, "notes": notes, "investigation": snapshot,
                   "fix": None if not fix else {k: fix.get(k) for k in ("kind", "summary", "recommended", "candidates")}, "escape": None}
        if cls in I.MALFUNCTION and a["build"]:
            scns, reqs, _ = self._library(project)
            released = [x for x in self.c.adas.campaigns(project) if x["status"] == "approved"]
            actions["escape"] = I.escape(a["build"], a["baseline"], cls, scns, reqs, released)["findings"]
        self.db.rec_update(project, ref, actor, {**issue["payload"], "actions": actions}, "actions_taken", event="actions")
        self.audit(actor, "actions", project, ref, {"class": cls, "scenarios": len(created_scn), "tickets": len(made),
                                                     "calibration": bool(calibration), "rollback_to": rollback_to})
        return {"issue": ref, "status": "actions_taken", "scenarios_proposed": created_scn, "tickets": made, "calibration": calibration,
                "rollback_to": rollback_to, "notes": notes}

    # ================================================================ after the actions: verification and closure
    def verify(self, project: str, ref: str, actor: str) -> dict:
        issue = self.get(project, ref, "issue")
        act = issue["payload"].get("actions") or {}
        if issue["status"] != "actions_taken" or not act.get("calibration"):
            raise Conflict(f"{ref} has no calibration fix to verify")
        pending = [s for s in act["scenarios"] if self.db.rec_get(project, s["ref"])["status"] != "approved"]
        if pending:
            raise Conflict(f"Approve the field scenario(s) {', '.join(s['ref'] for s in pending)} on the ADAS page first "
                           f"(a reviewer other than the person who proposed them)")
        camp = self.c.adas.run_campaign(project, actor, "auto", act["calibration"], True, False, f"verification of the fix for field issue {ref}")
        self.db.link_add(project, ref, camp["ref"], "verified_in", actor)
        ver = {"campaign": camp["ref"], "passed": camp["payload"]["verdict"]["passed"], "target": camp["payload"]["target"], "by": actor, "at": now()}
        self.db.rec_update(project, ref, actor, {**issue["payload"], "verification": ver}, issue["status"], event="verify")
        self.audit(actor, "verify", project, ref, ver)
        return camp

    def decide_issue(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        """'approved' closes the issue as resolved, 'rejected' closes it without a fix (e.g. duplicate, no defect)."""
        issue = self.get(project, ref, "issue")
        if decision == "rejected" and len((comment or "").strip()) < 10:
            raise Invalid("Give a reason (at least 10 characters)")

        def gate(r: dict) -> None:
            p = r["payload"]
            if r["status"] != "actions_taken":
                raise Conflict("Close it after the corrective actions were confirmed (agent checkpoint)")
            act = p.get("actions") or {}
            if act.get("calibration"):
                ver = p.get("verification")
                if not ver:
                    raise Conflict("The calibration fix has no verification campaign yet")
                camp = self.db.rec_get(project, ver["campaign"])
                if not camp or camp["status"] != "approved":
                    raise Conflict(f"Verification campaign {ver['campaign']} is not released (a second person releases it on the ADAS page)")
                if camp["payload"]["calibration_changes"] != act["calibration_changes"]:
                    raise Conflict(f"{ver['campaign']} did not test the fix calibration")
                used = {x["id"] for x in camp["payload"]["scenario_set"]}
                missing = [s["id"] for s in act["scenarios"] if s["id"] not in used]
                if missing:
                    raise Conflict(f"{ver['campaign']} does not include the field scenario(s) {', '.join(missing)}")
            if len((comment or "").strip()) < 10:
                raise Invalid("Closing a field issue needs a comment of at least 10 characters (what was checked)")
        out = self.decide(project, ref, actor, decision, comment, gate=gate if decision == "approved" else None)
        for cref in issue["payload"]["complaints"]:
            c = self.db.rec_get(project, cref)
            if c and c["status"] == "open":
                self.db.rec_update(project, cref, actor, c["payload"], "closed", event="closed")
        return out

    # ================================================================ views
    def _latest_run(self, project: str, issue: dict) -> dict | None:
        runs = issue["payload"].get("runs") or []
        return self.c.agent.run(project, runs[-1]) if runs else None

    @staticmethod
    def _step(run: dict | None, tool: str) -> dict | None:
        if not run:
            return None
        st = next((s for s in run["payload"]["steps"] if s["tool"] == tool and s["status"] == "done"), None)
        return st["result"] if st else None

    def issue_summary(self, project: str, issue: dict) -> dict:
        p = issue["payload"]
        run = self._latest_run(project, issue)
        diag = self._step(run, "diagnose_field_issue")
        waiting = run["payload"]["steps"][-1] if run and run["status"] == "waiting" else None
        cls = (p.get("actions") or {}).get("investigation", {}).get("class") or (diag or {}).get("class")
        return {"ref": issue["ref"], "status": issue["status"], "title": p["title"], "symptom": p["symptom"], "build": p["build"],
                "complaints": len(p["complaints"]), "class": cls, "class_title": I.CLASSES.get(cls) if cls else None,
                "run": run["ref"] if run else None, "run_status": run["status"] if run else None,
                "waiting_for": None if not waiting else {"n": waiting["n"], "tool": waiting["tool"]},
                "asil": (p.get("safety") or {}).get("asil"), "escalate": (p.get("safety") or {}).get("escalate"),
                "new_evidence": p.get("new_evidence", False), "verification": p.get("verification"),
                "opened_at": p.get("opened_at"), "updated_by": issue["updated_by"], "approved_by": issue["approved_by"]}

    def issues(self, project: str) -> list[dict]:
        return [self.issue_summary(project, i) for i in reversed(self.list(project, "issue"))]

    def issue(self, project: str, ref: str) -> dict:
        i = self.get(project, ref, "issue")
        run = self._latest_run(project, i)
        return {**i, "summary": self.issue_summary(project, i), "complaint_records": [self.get(project, r) for r in i["payload"]["complaints"]],
                "run": run}

    def signals(self, project: str, ref: str) -> dict:
        """Recorded signals and their closed-loop reconstruction (field build, and the recommended fix) for plots."""
        c = self.get(project, ref, "complaint")
        log = self._log(project, c["payload"].get("log"))
        if not log:
            raise NotFound(f"{ref} has no recording")
        issue = self.get(project, c["payload"]["issue"], "issue")
        out = {"complaint": ref, "recorded": L.series(log), "reconstructed": None, "with_fix": None, "fix": None}
        try:
            build = self.build(project, c["payload"]["build"])
        except NotFound:
            return out
        d = self._diag(project, {"ref": ref, "complaint": c, "symptom": c["payload"]["symptom"], "log": log}, build,
                       self.baseline(project, build), self.registry(project))
        scn = (d.get("reconstruction") or {}).get("scenario")
        if scn:
            ctrl, _ = B.controller(build)
            out["reconstructed"] = L.series(I.sim_log(scn, ctrl)[0])
            act = issue["payload"].get("actions") or {}
            fix = act.get("calibration") or ((self._fix(project, issue["ref"]).get("recommended") or {}).get("calibration")
                                             if d["class"] in ("calibration_regression", "sensor_false_target") else None)
            if fix:
                ctrl, _ = B.controller(build, cal=make_calibration(fix))
                out["with_fix"], out["fix"] = L.series(I.sim_log(scn, ctrl)[0]), fix
        return out

    def report(self, project: str, ref: str, fmt: str = "md") -> tuple[str, str]:
        data = self.issue(project, ref)
        a = self.analysis(project, ref)
        act = data["payload"].get("actions") or {}
        fix = act["fix"] if act.get("fix") else (self._fix(project, ref) if a["aggregate"]["class"] in I.MALFUNCTION else None)
        if fmt == "json":
            return json.dumps({"issue": {k: data[k] for k in ("ref", "status", "payload")}, "analysis": a, "fix": fix}, indent=2, default=str), "application/json"
        if fmt != "md":
            raise Invalid("format must be md or json")
        camp = None
        ver = data["payload"].get("verification")
        if ver:
            camp = self.db.rec_get(project, ver["campaign"])
        return R.eight_d(project, data, a, fix, camp), "text/markdown"

    def open_issues(self, project: str) -> list[dict]:
        return [s for s in self.issues(project) if s["status"] not in CLOSED]

    def metrics(self, project: str) -> dict:
        iss = self.issues(project)
        return {"complaints": len(self.list(project, "complaint")), "issues": len(iss), "open": sum(1 for i in iss if i["status"] not in CLOSED),
                "waiting_for_people": sum(1 for i in iss if i["run_status"] == "waiting"),
                "by_class": {c: sum(1 for i in iss if i["class"] == c) for c in I.CLASSES if any(i["class"] == c for i in iss)},
                "builds": len(self.registry(project))}
