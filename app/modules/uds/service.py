"""UDS diagnostics: spec management, request validation, test generation/review, coverage, simulation, export."""
from __future__ import annotations

import json

from app.core.errors import Conflict, Invalid
from app.core.workflow import Workflow
from app.modules.uds import export, harness_template as H
from app.modules.uds.engine import State, parse_request, respond
from app.modules.uds.simulator import MUTATIONS, SimEcu
from app.modules.uds.spec import check_spec, from_document, normalize_spec
from app.modules.uds.testgen import coverage as coverage_of, generate


class UDSService(Workflow):
    def __init__(self, container):
        super().__init__(container, "uds")

    # ================================================================ specifications
    def _payload(self, name: str, raw: dict, source_doc: str | None, origin: str, warnings: list[str] | None = None) -> dict:
        spec = normalize_spec(raw)
        if name.strip():
            spec["name"] = name.strip()
        return {"name": spec["name"] or spec["ecu"] or "diagnostic spec", "spec": spec, "issues": check_spec(spec),
                "source_doc": source_doc, "origin": origin, "warnings": warnings or []}

    def create_spec(self, project: str, name: str, raw: dict, actor: str) -> dict:
        rec, _ = self.create(project, "spec", "SPEC", self._payload(name, raw, None, "manual"), actor, machine=False)
        self.audit(actor, "spec.create", project, rec["ref"])
        return rec

    def spec_from_document(self, project: str, doc_id: str, name: str, actor: str) -> dict:
        d = self.c.db.document(doc_id)
        if not d or d["project"] != project:
            raise Invalid("doc_id does not belong to this project")
        spec, warnings = from_document(self.c.documents.load_pages(doc_id))
        payload = self._payload(name or d["name"], spec, doc_id, "rules", warnings)
        rec, _ = self.create(project, "spec", "SPEC", payload, actor)
        self.audit(actor, "spec.extract", project, rec["ref"], {"doc": doc_id, "issues": len(payload["issues"])})
        return rec

    def specs(self, project: str) -> list[dict]:
        return self.list(project, "spec")

    def _spec(self, project: str, ref: str) -> dict:
        return self.get(project, ref, "spec")

    def update_spec(self, project: str, ref: str, raw: dict, actor: str) -> dict:
        rec = self._spec(project, ref)
        p = {**rec["payload"], **self._payload(rec["payload"]["name"], raw, rec["payload"]["source_doc"], rec["payload"]["origin"])}
        return self.edit(project, ref, actor, p, status="suspect" if rec["status"] == "approved" else "assessed", event="spec.edit")

    def decide_spec(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        def gate(rec):
            errs = [i["message"] for i in rec["payload"]["issues"] if i["severity"] == "error"]
            if errs:
                raise Invalid("The specification has consistency errors: " + "; ".join(errs[:5]))
        self._spec(project, ref)
        return self.decide(project, ref, actor, decision, comment, gate)

    # ================================================================ validation (oracle)
    def validate(self, project: str, ref: str, requests: list[str], session: int | str | None, unlock: int | None) -> dict:
        spec = self._spec(project, ref)["payload"]["spec"]
        if not requests or len(requests) > 50:
            raise Invalid("Provide 1 to 50 requests")
        ids = {s["name"]: s["id"] for s in spec["sessions"]}
        sid = spec["sessions"][0]["id"] if session in (None, "") else ids.get(str(session).lower()) or _int(session, "session")
        if sid not in ids.values():
            raise Invalid(f"Unknown session '{session}'")
        if unlock is not None and unlock not in {l["level"] for l in spec["security_levels"]}:
            raise Invalid(f"Unknown security level {unlock}")
        st, out = State(session=sid, unlocked=unlock), []
        for text in requests:
            try:
                req = parse_request(text)
            except ValueError as e:
                raise Invalid(str(e))
            o, st = respond(spec, st, req)
            out.append({"request": text.upper(), **o.to_dict(), "state_after": {"session": st.session, "unlocked": st.unlocked}})
        return {"spec": ref, "results": out}

    # ================================================================ test generation and review
    def generate(self, project: str, ref: str, actor: str) -> dict:
        rec = self._spec(project, ref)
        spec = rec["payload"]["spec"]
        cases = generate(spec)
        created, changed, unchanged, keys = [], [], 0, set()
        for c in cases:
            keys.add(c["key"])
            payload = {**c, "spec": ref, "spec_rev": rec["rev"], "origin": "rules", "orphan": False}
            t, new = self.create(project, "test", "TC", payload, actor, key=f"{ref}:{c['key']}")
            if new:
                created.append(t["ref"])
                for el, _ in c["covers"]:
                    self.db.link_add(project, f"ELM:{ref}:{el}", t["ref"], "tested_by", actor)
                continue
            old = t["payload"]
            if any(old.get(k) != c[k] for k in ("steps", "pre", "title", "category", "covers")) or old.get("orphan"):
                merged = {**old, **c, "spec_rev": rec["rev"], "orphan": False,
                          "changed_reason": f"Expected behaviour changed with specification revision {rec['rev']}"}
                self.db.rec_update(project, t["ref"], actor, merged,
                                   "suspect" if t["status"] == "approved" else t["status"], event="regenerate")
                changed.append(t["ref"])
            else:
                unchanged += 1
        orphaned = []
        for t in self.tests(project, ref):
            if t["payload"]["key"] not in keys and t["status"] != "rejected" and not t["payload"].get("orphan"):
                self.db.rec_update(project, t["ref"], actor, {**t["payload"], "orphan": True,
                                   "changed_reason": "No longer derived from the current specification"}, "suspect", event="orphan")
                orphaned.append(t["ref"])
        self.audit(actor, "tests.generate", project, ref, {"created": len(created), "changed": len(changed), "orphaned": len(orphaned)})
        return {"created": len(created), "changed": changed, "orphaned": orphaned, "unchanged": unchanged,
                "spec_approved": rec["status"] == "approved", "total": len(cases)}

    def tests(self, project: str, spec: str | None = None, category: str | None = None, status: str | None = None) -> list[dict]:
        rows = self.list(project, "test", status=status, spec=spec)
        return [r for r in rows if category is None or r["payload"]["category"] == category]

    def decide_test(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        t = self.get(project, ref, "test")

        def gate(rec):
            if rec["payload"].get("orphan"):
                raise Conflict("Test is no longer derived from the current specification")
            if self._spec(project, rec["payload"]["spec"])["status"] != "approved":
                raise Conflict("Approve the specification before approving tests derived from it")
        return self.decide(project, t["ref"], actor, decision, comment, gate)

    def link_requirement(self, project: str, test_ref: str, req_ref: str, actor: str) -> dict:
        """Attach a test as verification evidence of a HARA (FSR/TSR) or TARA (CSR) requirement."""
        self.get(project, test_ref, "test")
        req = self.db.rec_get(project, req_ref)
        if not req or (req["module"], req["kind"]) not in (("hara", "fsr"), ("hara", "tsr"), ("tara", "csr")):
            raise Invalid(f"{req_ref} is not a safety or cybersecurity requirement of this project")
        ver = list(req["payload"]["verification"])
        if test_ref not in ver:
            ver.append(test_ref)
            svc = self.c.hara if req["module"] == "hara" else self.c.tara
            (svc.update_requirement if req["module"] == "hara" else svc.update_csr)(project, req_ref, actor, verification=ver)
        self.db.link_add(project, f"TEST:{test_ref}", test_ref, "realized_by", actor)
        self.audit(actor, "test.link", project, test_ref, {"requirement": req_ref})
        return self.db.rec_get(project, req_ref)

    # ================================================================ coverage, completeness, run
    def coverage(self, project: str, ref: str) -> dict:
        spec = self._spec(project, ref)["payload"]["spec"]
        tests = self.tests(project, ref)
        cov = coverage_of(spec, tests)
        cov["approved_only"] = coverage_of(spec, [t for t in tests if t["status"] == "approved"])["coverage"]
        cov["by_category"] = {}
        for t in tests:
            if t["status"] != "rejected":
                cov["by_category"][t["payload"]["category"]] = cov["by_category"].get(t["payload"]["category"], 0) + 1
        return cov

    def completeness(self, project: str, ref: str) -> dict:
        rec, cov = self._spec(project, ref), self.coverage(project, ref)
        tests = [t for t in self.tests(project, ref) if t["status"] != "rejected"]
        errs = [i["message"] for i in rec["payload"]["issues"] if i["severity"] == "error"]
        checks = [("spec_consistent", "Specification has no consistency errors", errs),
                  ("spec_approved", "Specification approved", [] if rec["status"] == "approved" else [ref]),
                  ("tests_generated", "Tests generated", [] if tests else [ref]),
                  ("tests_decided", "All tests approved or rejected", [t["ref"] for t in tests if t["status"] in ("proposed", "assessed")]),
                  ("no_suspect", "No test is suspect", [t["ref"] for t in tests if t["status"] == "suspect"]),
                  ("elements_covered", "Every spec element has a positive and a negative test", [r["element"] for r in cov["elements"] if not r["covered"]]),
                  ("nrcs_exercised", "Every applicable NRC is exercised", cov["nrc_missing"])]
        return {"spec": ref, "checks": [{"id": i, "description": d, "passed": not f, "failing": f} for i, d, f in checks],
                "complete": all(not f for _, _, f in checks), "coverage": cov["coverage"]}

    def run(self, project: str, ref: str, mutation: str, only_approved: bool, actor: str) -> dict:
        spec = self._spec(project, ref)["payload"]["spec"]
        if mutation not in MUTATIONS:
            raise Invalid(f"mutation must be one of {list(MUTATIONS)}")
        tests = [t for t in self.tests(project, ref) if t["status"] != "rejected" and (t["status"] == "approved" or not only_approved)]
        if not tests:
            raise Conflict("No tests to run (generate and approve tests first)")
        res = []
        for t in tests:
            p = t["payload"]
            case = {"id": t["ref"], "pre": {**p["pre"], "default_session": spec["sessions"][0]["id"]}, "steps": p["steps"]}
            fails = H.run_case(SimEcu(spec, mutation), case, spec["timing"], check_p2=False)
            res.append({"test": t["ref"], "title": p["title"], "element": p["element"], "passed": not fails, "failures": fails})
        failed = [r for r in res if not r["passed"]]
        self.audit(actor, "tests.run", project, ref, {"mutation": mutation, "failed": len(failed)})
        return {"mutation": mutation, "description": MUTATIONS[mutation], "total": len(res), "passed": len(res) - len(failed),
                "failed": len(failed), "defect_detected": bool(failed) if mutation != "none" else None, "results": res}

    def detection_matrix(self, project: str, ref: str, actor: str) -> dict:
        """Run every injected defect: shows what the generated suite can and cannot detect."""
        out = {}
        for m in MUTATIONS:
            r = self.run(project, ref, m, False, actor)
            out[m] = {"description": r["description"], "failed": r["failed"], "detected": r["failed"] > 0 if m != "none" else None}
        return out

    # ================================================================ export / ask / metrics
    def export(self, project: str, ref: str, fmt: str, include_unapproved: bool, actor: str) -> tuple[bytes | str, str, str]:
        rec = self._spec(project, ref)
        spec = rec["payload"]["spec"]
        tests = [t for t in self.tests(project, ref) if t["status"] == "approved" or (include_unapproved and t["status"] in ("proposed", "assessed"))]
        if not tests and fmt not in ("harness", "conftest"):
            raise Conflict("No approved tests to export (approve tests, or pass include_unapproved=true for a draft export)")
        self.audit(actor, "export", project, ref, {"format": fmt, "tests": len(tests), "include_unapproved": include_unapproved})
        if fmt == "python":
            return export.python_tests(ref, rec["rev"], spec, tests), "text/x-python", f"test_uds_{ref}.py"
        if fmt == "harness":
            return export.harness_text(), "text/x-python", "uds_harness.py"
        if fmt == "conftest":
            return export.conftest_text(), "text/x-python", "conftest.py"
        if fmt == "capl":
            return export.capl_text(ref, rec["rev"], spec, tests), "text/plain", f"uds_{ref}.can"
        if fmt == "csv":
            return export.csv_text(tests), "text/csv", f"uds_{ref}.csv"
        if fmt == "json":
            return json.dumps({"spec": spec, "tests": [{**t["payload"], "id": t["ref"], "status": t["status"]} for t in tests]}, indent=2), "application/json", f"uds_{ref}.json"
        if fmt == "bundle":
            return export.bundle(ref, rec["rev"], spec, tests), "application/zip", f"uds_bundle_{ref}.zip"
        raise Invalid("format must be python, harness, conftest, capl, csv, json or bundle")

    def ask(self, project: str, ref: str, question: str, actor: str) -> dict:
        doc = self._spec(project, ref)["payload"]["source_doc"]
        if not doc:
            raise Conflict("This specification was not extracted from a document; there is no source text to search")
        return self.c.hld.ask(project, question, include_draft=True, doc_ids=[doc], actor=actor)

    def metrics(self, project: str, ref: str) -> dict:
        cov = self.coverage(project, ref)
        tests = self.tests(project, ref)
        return {"recommendation_acceptance": self.acceptance(project, ["test", "spec"]),
                "coverage": cov["coverage"], "approved_only_coverage": cov["approved_only"],
                "tests": {s: sum(t["status"] == s for t in tests) for s in ("proposed", "approved", "rejected", "suspect")},
                "by_category": cov["by_category"], "nrc_missing": cov["nrc_missing"]}

    def report(self, project: str, ref: str, actor: str) -> str:
        rec = self._spec(project, ref)
        spec, cov, comp = rec["payload"]["spec"], self.coverage(project, ref), self.completeness(project, ref)
        self.audit(actor, "report", project, ref)
        L = [f"# UDS test specification: {rec['payload']['name']} ({ref}, rev {rec['rev']})", "",
             "Expected results are derived from the specification by a deterministic oracle; AI output is advisory.", "",
             f"Spec status: **{rec['status']}** · approved by {rec['approved_by'] or 'n/a'}", "", "## Coverage",
             f"{cov['covered']}/{cov['total']} spec elements have positive and negative tests ({cov['coverage']:.0%}); approved only: {cov['approved_only']:.0%}.", "",
             "| Element | Tests | Approved | Positive | Negative |", "|---|---|---|---|---|"]
        L += [f"| {r['element']} | {r['tests']} | {r['approved']} | {'yes' if r['positive'] else 'no'} | {'yes' if r['negative'] else 'no'} |" for r in cov["elements"]]
        L += ["", "NRCs not exercised: " + (", ".join(cov["nrc_missing"]) or "none"), "", "## Completeness", ""]
        L += [f"- [{'x' if c['passed'] else ' '}] {c['description']}" + (f" (open: {', '.join(map(str, c['failing'][:8]))})" if c["failing"] else "") for c in comp["checks"]]
        L += ["", "## Test cases", "", "| ID | Category | Title | Status |", "|---|---|---|---|"]
        L += [f"| {t['ref']} | {t['payload']['category']} | {t['payload']['title']} | {t['status']} |" for t in self.tests(project, ref)]
        return "\n".join(L)


def _int(v, what):
    try:
        return int(str(v), 0)
    except ValueError:
        raise Invalid(f"{what} must be a session name or number")
