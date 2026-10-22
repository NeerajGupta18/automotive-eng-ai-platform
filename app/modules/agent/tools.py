"""The agent's tool registry: the only actions an agent (rule playbook or LLM) can take.

  read   look at the project; runs immediately
  write  creates something (tickets, scenario proposals, a validation campaign); the run stops at a human
         checkpoint and the tool runs only when a person confirms it, under that person's name, so the
         platform's four-eyes rules still apply to everything the agent produced

There are deliberately no tools that approve, release, reject, delete, revoke or change users. Arguments are
validated here, results are compacted (an LLM sees summaries, never whole documents), and every tool result is
treated as untrusted data by the planner."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from app.core import tracequery
from app.core.errors import Conflict, Invalid, NotFound
from app.modules.adas.model import make_calibration

REF_RE = re.compile(r"^[A-Z]{1,8}-[0-9]{1,6}$")
VERSION_RE = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(-[a-z0-9]{1,12})?$")
CHECK_RE = re.compile(r"^RR[0-9]{2}$")
SEVERITIES = ("critical", "high", "medium", "low")


class ToolError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    type: str                     # str | bool | int | ref | tickets | calibration | version
    required: bool = False
    description: str = ""
    max_len: int = 200
    choices: tuple = ()


@dataclass(frozen=True)
class Tool:
    name: str
    kind: str                     # read | write
    description: str
    params: dict = field(default_factory=dict)
    fn: Callable | None = None


def _validate(tool: Tool, args: dict) -> dict:
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    unknown = set(args) - set(tool.params)
    if unknown:
        raise ToolError(f"unknown argument(s) for {tool.name}: {sorted(unknown)}")
    out = {}
    for name, p in tool.params.items():
        if name not in args or args[name] in (None, ""):
            if p.required:
                raise ToolError(f"{tool.name}: '{name}' is required")
            continue
        v = args[name]
        if p.type == "bool":
            if not isinstance(v, bool):
                raise ToolError(f"{name} must be true or false")
        elif p.type == "int":
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 1000:
                raise ToolError(f"{name} must be an integer 0-1000")
        elif p.type == "ref":
            if not isinstance(v, str) or not REF_RE.match(v):
                raise ToolError(f"{name} must be a record reference like AR-005 or F-012")
        elif p.type == "version":
            if not isinstance(v, str) or not VERSION_RE.match(v):
                raise ToolError(f"{name} must be a software version like 2.3.0")
        elif p.type == "calibration":
            if not isinstance(v, dict) or len(v) > 20:
                raise ToolError(f"{name} must map calibration parameters to numbers")
            try:
                make_calibration(v)
            except ValueError as e:
                raise ToolError(f"{name}: {e}") from e
            v = {k: v[k] for k in sorted(v)}
        elif p.type == "tickets":
            if not isinstance(v, list) or not 1 <= len(v) <= 10:
                raise ToolError("tickets must be a list of 1-10 items")
            clean = []
            for t in v:
                if not isinstance(t, dict) or set(t) - {"summary", "description", "source_ref", "priority"}:
                    raise ToolError("each ticket has summary, description, source_ref, priority")
                s = " ".join(str(t.get("summary", "")).split())
                if not 5 <= len(s) <= 250:
                    raise ToolError("ticket summary: 5-250 characters")
                src = str(t.get("source_ref", ""))
                if src and not (REF_RE.match(src) or CHECK_RE.match(src)):
                    raise ToolError("ticket source_ref must be a record reference or a readiness check id")
                pr = t.get("priority", "Medium")
                clean.append({"summary": s, "description": str(t.get("description", ""))[:4000], "source_ref": src,
                              "priority": pr if pr in ("Highest", "High", "Medium", "Low", "Lowest") else "Medium"})
            v = clean
        else:
            if not isinstance(v, str) or len(v) > p.max_len:
                raise ToolError(f"{name} must be text of at most {p.max_len} characters")
            v = " ".join(v.split())
            if p.choices and v not in p.choices:
                raise ToolError(f"{name} must be one of {list(p.choices)}")
        out[name] = v
    return out


# ------------------------------------------------------------------------------------------- read tools
def _readiness(c, p, a, actor):
    r = c.readiness.evaluate(p)
    brief = lambda ch: {"id": ch["id"], "title": ch["title"], "summary": ch["summary"][:300], "owner": ch["owner"],  # noqa: E731
                        "action": ch["action"], "open_items": ch["count"], "examples": ch["items"][:5]}
    return {"verdict": r["verdict"], "blocking": [brief(ch) for ch in r["checks"] if ch["status"] == "block"],
            "warnings": [brief(ch) for ch in r["checks"] if ch["status"] == "warn"],
            "passed": [ch["id"] for ch in r["checks"] if ch["status"] == "pass"],
            "not_applicable": [ch["id"] for ch in r["checks"] if ch["status"] == "n/a"]}


def _trace(c, p, a, actor):
    try:
        n = tracequery.neighborhood(c.db, p, a["ref"])
    except NotFound as e:
        raise ToolError(str(e)) from e
    short = lambda x: {k: x.get(k) for k in ("ref", "module", "kind", "status", "label", "hops")}  # noqa: E731
    return {"node": short(n["node"]), "upstream": [short(x) for x in n["ancestors"][:15]],
            "downstream": [short(x) for x in n["descendants"][:30]], "upstream_total": len(n["ancestors"]),
            "downstream_total": len(n["descendants"])}


def _suspects(c, p, a, actor):
    s = tracequery.suspects(c.db, p)
    return {"total": len(s), "items": [{k: x.get(k) for k in ("ref", "module", "kind", "label", "suspect_reason")} for x in s[:30]]}


def _record(c, p, a, actor):
    rec = c.db.rec_get(p, a["ref"])
    if not rec:
        raise ToolError(f"{a['ref']} not found in this project")
    pl = rec["payload"]
    keep = {k: pl[k] for k in ("title", "text", "asil", "risk", "severity", "check", "params", "release_blocking", "family",
                               "suspect_reason", "change", "key", "identifier", "verdict", "allocated_to") if k in pl}
    for k, v in list(keep.items()):
        if isinstance(v, str) and len(v) > 600:
            keep[k] = v[:600] + "…"
    return {"ref": rec["ref"], "module": rec["module"], "kind": rec["kind"], "status": rec["status"], "rev": rec["rev"],
            "label": tracequery.label(rec), "updated_by": rec["updated_by"], "approved_by": rec["approved_by"], "fields": keep}


def _documents(c, p, a, actor):
    ans = c.hld.ask(p, a["question"], k=5, actor=actor)
    return {"answer": ans["answer"][:1500], "refused": ans["refused"], "refusal_reason": ans["refusal_reason"],
            "confidence": ans["confidence_label"],
            "citations": [{"label": x["label"], "document": x["doc_name"], "version": x["version"], "page": x["page"],
                           "section": x["section"]} for x in ans["citations"][:5]]}


def _vulns(c, p, a, actor):
    rows = [f for f in c.vuln.findings(p) if f["status"] in ("proposed", "assessed", "approved")]
    if a.get("ecu"):
        rows = [f for f in rows if f["payload"]["ecu"].lower() == a["ecu"].lower()]
    if a.get("query"):
        q = a["query"].lower()
        rows = [f for f in rows if q in f["payload"]["title"].lower() or q in " ".join(f["payload"].get("aliases") or []).lower()
                or q in (f["payload"].get("advisory") or "").lower()]
    if a.get("min_severity"):
        rank = SEVERITIES.index(a["min_severity"])
        rows = [f for f in rows if f["payload"]["severity"] in SEVERITIES[:rank + 1]]
    state = {"proposed": "not triaged", "assessed": "awaiting second person", "approved": "exploitable"}
    m = c.vuln.metrics(p)
    return {"open_total": m["open"], "open_by_severity": m["open_by_severity"], "matching": len(rows),
            "findings": [{"ref": f["ref"], "title": f["payload"]["title"], "ecu": f["payload"]["ecu"], "severity": f["payload"]["severity"],
                          "score": f["payload"]["score"], "state": state[f["status"]], "fixed_in": f["payload"].get("fixed_in")}
                         for f in rows[:20]]}


def _code_gate(c, p, a, actor):
    g = c.codereview.gate(p, "high")
    return {"passed": g["passed"], "blockers_total": len(g["blockers"]),
            "blockers": [{k: b[k] for k in ("ref", "severity", "file", "line", "message")} for b in g["blockers"][:15]]}


def _adas(c, p, a, actor):
    st = c.adas.status(p)
    if not st["library_loaded"]:
        return {"used": False}
    cov = c.adas.coverage(p)
    camp = lambda x: None if not x else {k: x.get(k) for k in ("ref", "status", "passed", "blocking_not_passed", "exploratory_findings",  # noqa: E731
                                                                 "back_to_back", "mutation_score", "stale", "suspect_reason", "approved_by")}
    suspect = [x["ref"] for x in c.adas.campaigns(p) if x["status"] == "suspect"]
    return {"used": True, "requirements": st["requirements"], "scenarios": st["scenarios"], "sil_available": st["sil"]["available"],
            "firmware": st["sil"]["firmware_version"], "coverage_gaps": cov["gaps"], "latest_campaign": camp(st["latest_campaign"]),
            "current_release": camp(st["current_release"]), "suspect_campaigns": suspect}


def _ota(c, p, a, actor):
    return {"active_keys": sum(k["status"] == "approved" for k in c.ota.keys(p)),
            "packages": [{"ref": k["ref"], "title": k["payload"]["title"], "ecu": k["payload"].get("ecu"), "verdict": k["payload"].get("verdict"),
                          "status": {"approved": "released"}.get(k["status"], k["status"])} for k in c.ota.packages(p)[:20]]}


def _r155(c, p, a, actor):
    if not c.tara.systems(p):
        return {"used": False}
    return {"used": True, "groups": [{"id": g["id"], "title": g["title"], "status": g["status"]} for g in c.compliance.r155(p)["categories"]]}


def _customer(c, p, a, actor):
    rows = c.integration.external(p)
    return {"total": len(rows), "requirements": [{"ref": r["ref"], "key": r["payload"]["key"], "title": r["payload"]["title"],
                                                   "status": r["status"], "change": r["payload"].get("change"), "linked_to": r["links"]}
                                                  for r in rows[:30]]}


# ------------------------------------------------------------------------------------------- write tools
def _tickets(c, p, a, actor):
    out = []
    for t in a["tickets"]:
        rec = c.integration.create_ticket(p, t["summary"], t["description"], t["source_ref"], actor, t["priority"], ["agent"])
        out.append({"ref": rec["ref"], "summary": t["summary"], "status": rec["status"], "new": rec["created"],
                    "jira": rec["payload"].get("external_key") or None})
    return {"tickets": out, "created": sum(t["new"] for t in out)}


def _propose(c, p, a, actor):
    try:
        r = c.adas.propose_scenarios(p, actor)
    except Conflict as e:
        raise ToolError(str(e)) from e
    return {"created": r["created"], "proposals": r["proposals"], "note": "A reviewer must approve them before they count."}


def _campaign(c, p, a, actor):
    try:
        rec = c.adas.run_campaign(p, actor, "auto", None, True, bool(a.get("mutation")), "started by the engineering agent")
    except (Conflict, Invalid) as e:
        raise ToolError(str(e)) from e
    pl = rec["payload"]
    mut = pl.get("mutation") or {}
    return {"ref": rec["ref"], "target": pl["target"], "passed": pl["verdict"]["passed"],
            "blocking_not_passed": pl["verdict"]["blocking_not_passed"], "exploratory_findings": pl["verdict"]["exploratory_findings"],
            "back_to_back": None if not pl.get("back_to_back") else pl["back_to_back"]["passed"],
            "mutation_score": mut.get("score"), "survivors": [x["id"] for x in mut.get("rows", []) if x["status"] == "survived"],
            "note": "The campaign is not released: a second person decides on the ADAS page."}


# ------------------------------------------------------------------------------------------- field issues (Phase 6)
def _field(method: str):
    def fn(c, p, a, actor):
        return getattr(c.field, method)(p, a["ref"])
    return fn


def _record_safety(c, p, a, actor):
    return c.field.record_safety(p, a["ref"], a.get("severity", 0), a.get("exposure", 0), a.get("controllability", 0),
                                 a.get("rationale", ""), bool(a.get("escalate")), actor)


def _field_actions(c, p, a, actor):
    return c.field.apply_actions(p, a["ref"], actor, propose_scenarios=bool(a.get("propose_scenarios")), calibration=a.get("calibration"),
                                 rollback_to=a.get("rollback_to"), tickets=a.get("tickets") or [], summary=a.get("summary", ""))


FI = {"ref": Param("ref", True, "field issue reference, e.g. FI-001")}
TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("release_readiness", "read", "GO / NO-GO across all disciplines with blocking items, owners and actions.", {}, _readiness),
    Tool("trace_impact", "read", "Everything upstream and downstream of a record in the traceability graph.",
         {"ref": Param("ref", True, "record reference, e.g. AR-005")}, _trace),
    Tool("suspect_items", "read", "Items in any module waiting for re-review after an upstream change.", {}, _suspects),
    Tool("get_record", "read", "One record's status and main fields.", {"ref": Param("ref", True, "record reference")}, _record),
    Tool("search_documents", "read", "Answer a question from the project's approved documents, with citations; refuses without evidence.",
         {"question": Param("str", True, "the question", 500)}, _documents),
    Tool("vulnerabilities", "read", "Open vulnerability findings, optionally filtered by ECU, text (e.g. a CVE id) and minimum severity.",
         {"ecu": Param("str", False, "ECU name", 60), "query": Param("str", False, "text such as CVE-2022-37434", 60),
          "min_severity": Param("str", False, "critical | high | medium | low", 10, SEVERITIES)}, _vulns),
    Tool("code_review_gate", "read", "Open high/critical code-review findings that block the gate.", {}, _code_gate),
    Tool("adas_status", "read", "ADAS validation: coverage gaps, latest campaign, current release, suspect campaigns.", {}, _adas),
    Tool("ota_status", "read", "Signing keys and update packages with their verification verdicts.", {}, _ota),
    Tool("r155_status", "read", "UN R155 Annex 5 groups and their evidence status.", {}, _r155),
    Tool("customer_requirements", "read", "Imported customer requirements (ReqIF) with change status and links.", {}, _customer),
    Tool("create_tickets", "write", "Create Jira tickets (dry run unless Jira is configured). Needs human confirmation.",
         {"tickets": Param("tickets", True, "list of {summary, description, source_ref, priority}")}, _tickets),
    Tool("propose_adas_scenarios", "write", "Boundary-value scenario proposals for uncovered requirements. Needs human confirmation.",
         {}, _propose),
    Tool("run_adas_campaign", "write", "Run the ADAS SIL validation campaign (does not release it). Needs human confirmation.",
         {"mutation": Param("bool", False, "also run mutation testing")}, _campaign),
    # field issues (Phase 6)
    Tool("field_issue", "read", "A field issue: its complaints, the software build and fleet size.", FI, _field("tool_issue")),
    Tool("analyze_field_logs", "read", "Events in the issue's vehicle recordings: interventions, warnings, radar objects (real or phantom), crashes.",
         FI, _field("tool_analyze")),
    Tool("replay_field_logs", "read", "Replay each recording's inputs on the field build's compiled C code and compare with the recorded outputs.",
         FI, _field("tool_replay")),
    Tool("reconstruct_field_scenarios", "read", "Rebuild each recorded situation as a closed-loop scenario and check that it reproduces.",
         FI, _field("tool_reconstruct")),
    Tool("diagnose_field_issue", "read", "Root cause per complaint and for the issue (code vs model, field vs validated calibration, counterfactual).",
         FI, _field("tool_diagnose")),
    Tool("field_escape_analysis", "read", "Why the problem reached the field: validation evidence, regression results, back-to-back.", FI, _field("tool_escape")),
    Tool("search_field_fix", "read", "Calibration candidates (or a rollback build) checked against every complaint and the regression library.",
         FI, _field("tool_fix")),
    Tool("assess_field_safety", "read", "Proposed hazard rating (S/E/C, ASIL from the HARA rule table) for the issue.", FI, _field("tool_safety")),
    Tool("record_safety_assessment", "write", "Record the hazard rating of a field issue. Needs confirmation by a person (safety manager).",
         {**FI, "severity": Param("int", True, "S 0-3"), "exposure": Param("int", True, "E 0-4"), "controllability": Param("int", True, "C 0-3"),
          "rationale": Param("str", False, "why", 1200), "escalate": Param("bool", False, "escalate to the product safety committee")},
         _record_safety),
    Tool("apply_field_actions", "write", "Corrective actions for a field issue: regression scenario proposals, the fix to verify, tickets, "
         "8D record. Needs human confirmation.",
         {**FI, "propose_scenarios": Param("bool", False, "add the reconstructed situations as scenario proposals"),
          "calibration": Param("calibration", False, "calibration to verify (full set of overrides)"),
          "rollback_to": Param("version", False, "containment: build to roll back to"),
          "tickets": Param("tickets", False, "list of {summary, description, source_ref, priority}"),
          "summary": Param("str", False, "what is being done", 600)}, _field_actions),
]}


def describe() -> list[dict]:
    return [{"name": t.name, "kind": t.kind, "description": t.description,
             "params": {k: {"type": p.type, "required": p.required, "description": p.description,
                            **({"choices": list(p.choices)} if p.choices else {})} for k, p in t.params.items()}}
            for t in TOOLS.values()]


def prepare(name: str, args: dict) -> tuple[Tool, dict]:
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"'{name}' is not an available tool. Approving, releasing, deleting and user administration are "
                        f"never available to agents.")
    return tool, _validate(tool, args or {})


def run(container, project: str, name: str, args: dict, actor: str) -> dict:
    tool, clean = prepare(name, args)
    try:
        return tool.fn(container, project, clean, actor)
    except (Invalid, Conflict, NotFound) as e:
        raise ToolError(str(e)) from e
