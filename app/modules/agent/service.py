"""Engineering agent runs: a goal is routed to a playbook (or planned by an LLM), executed with tools, paused at
human checkpoints for every write, and reported.

Guardrails (enforced here, not in the UI)
  * tools come only from the registry; there are no approval, release, delete or user tools
  * write tools run only after a person confirms the step, under that person's name (four-eyes intact)
  * step budget (AGENT_MAX_STEPS), argument validation, compact results, untrusted-data framing for the LLM
  * every step, checkpoint and confirmation is in the tamper-evident audit log; the run record keeps the full trace
  * durable: a waiting run is resumed by replaying its plan with the recorded results (nothing re-executes)"""
from __future__ import annotations

import json
import time

from app.core.db import now
from app.core.errors import Conflict, Invalid
from app.core.workflow import Workflow
from app.modules.agent import playbooks as PB
from app.modules.agent import tools as T

MAX_RESULT_CHARS = 6000


def _compact(result: dict) -> dict:
    s = json.dumps(result, default=str)
    if len(s) <= MAX_RESULT_CHARS:
        return result
    return {"truncated": True, "preview": s[:MAX_RESULT_CHARS]}


class ReplayError(RuntimeError):
    pass


class AgentService(Workflow):
    def __init__(self, container):
        super().__init__(container, "agent")

    def llm_available(self) -> bool:
        return self.c.settings.llm_backend != "mock" and getattr(self.c.llm, "name", "mock") != "mock"

    # ---------------------------------------------------------------- start
    def start(self, project: str, goal: str, actor: str, playbook: str = "auto", params: dict | None = None,
              planner: str = "rules") -> dict:
        goal = " ".join((goal or "").split())
        if not 3 <= len(goal) <= 1000:
            raise Invalid("goal: 3 to 1000 characters")
        if planner not in ("rules", "llm"):
            raise Invalid("planner must be 'rules' or 'llm'")
        notes = []
        if planner == "llm" and not self.llm_available():
            planner = "rules"
            notes.append("The LLM planner needs a real LLM (LLM_BACKEND); the rule-based playbooks were used instead.")
        if planner == "llm":
            mode, params, why = "llm", {}, "planned step by step by the LLM"
        elif playbook == "auto":
            mode, params, why = PB.route(goal, [e["ecu"] for e in self.c.vuln.ecus(project)])
        else:
            if playbook not in PB.PLAYBOOKS:
                raise Invalid(f"playbook must be 'auto' or one of {list(PB.PLAYBOOKS)}")
            mode, why = playbook, "chosen by the user"
            params = self._params(playbook, params or {}, goal)
        payload = {"title": goal[:120], "goal": goal, "mode": mode, "params": params, "why": why, "planner": planner,
                   "steps": [], "llm_decisions": [], "notes": notes, "outcome": None, "started_by": actor, "started_at": now()}
        rec, _ = self.create(project, "run", "RUN", payload, actor, status="running", machine=False)
        self.audit(actor, "run.start", project, rec["ref"], {"mode": mode, "params": params})
        return self._advance(project, rec["ref"], actor)

    @staticmethod
    def _params(playbook: str, params: dict, goal: str) -> dict:
        if playbook == "change_impact":
            ref = str(params.get("ref") or (PB.REF_IN_TEXT.findall(goal) or [""])[0])
            if not T.REF_RE.match(ref):
                raise Invalid("change_impact needs a record reference (params.ref or in the goal), e.g. AR-005")
            return {"ref": ref}
        if playbook == "vulnerability_response":
            out = {}
            cve = PB.CVE_IN_TEXT.search(str(params.get("cve") or goal))
            if cve:
                out["cve"] = cve.group(1).upper()
            if params.get("ecu"):
                out["ecu"] = str(params["ecu"])[:60]
            return out
        if playbook == "documents":
            return {"question": str(params.get("question") or goal)[:500]}
        if playbook == "field_investigation":
            ref = str(params.get("issue") or next((r for r in PB.REF_IN_TEXT.findall(goal) if r.startswith("FI-")), ""))
            if not T.REF_RE.match(ref):
                raise Invalid("field_investigation needs a field issue reference (params.issue or in the goal), e.g. FI-001")
            return {"issue": ref}
        return {}

    # ---------------------------------------------------------------- execution (replay + continue)
    def _generator(self, project: str, payload: dict, decisions: list):
        if payload["mode"] != "llm":
            return PB.GENERATORS[payload["mode"]](payload["params"])
        state = {"i": 0}

        def decide(history):
            i = state["i"]
            state["i"] += 1
            if i < len(decisions):
                return decisions[i]
            if i >= 30:
                return {"final": "Stopped: too many planner decisions."}
            try:
                raw = self.c.llm.generate(PB.LLM_SYSTEM, PB._llm_prompt(payload["goal"], history))
            except Exception as e:                                   # noqa: BLE001 - an unreachable model ends the run cleanly
                d = {"final": f"The LLM planner is unavailable ({type(e).__name__}); no further steps were taken."}
            else:
                d = PB.parse_decision(raw)
            decisions.append(d)
            return d
        return PB.llm_plan({}, decide, max_tool_steps=min(8, self.c.settings.agent_max_steps))

    def _advance(self, project: str, ref: str, actor: str) -> dict:
        rec = self.get(project, ref, "run")
        p = dict(rec["payload"])
        steps, decisions = list(p["steps"]), list(p.get("llm_decisions", []))
        gen = self._generator(project, p, decisions)
        i, status, outcome, error = 0, "completed", None, None
        try:
            call = gen.send(None)
            while True:
                if i >= self.c.settings.agent_max_steps:
                    raise ReplayError(f"step budget of {self.c.settings.agent_max_steps} reached")
                if i < len(steps):                                   # replay a recorded step
                    st = steps[i]
                    if st["tool"] != call.tool or st["args_in"] != call.args:
                        raise ReplayError(f"step {i + 1} does not replay identically ({call.tool})")
                    if st["status"] == "awaiting_confirmation":
                        status = "waiting"
                        break
                    result = st["result"]
                else:
                    st = {"n": i + 1, "tool": call.tool, "args_in": call.args, "why": call.why[:300], "kind": None,
                          "status": None, "result": None, "at": now(), "by": None, "ms": None}
                    try:
                        tool, clean = T.prepare(call.tool, call.args)
                        st["kind"], st["args"] = tool.kind, clean
                    except T.ToolError as e:
                        st.update(status="blocked", result={"error": f"guardrail: {e}"})
                        steps.append(st)
                        self.audit(actor, "step", project, ref, {"n": st["n"], "tool": call.tool[:60], "status": "blocked"})
                        result = st["result"]
                        i += 1
                        call = gen.send(result)
                        continue
                    if tool.kind == "write":
                        st["status"] = "awaiting_confirmation"
                        steps.append(st)
                        status = "waiting"
                        self.audit(actor, "checkpoint", project, ref, {"n": st["n"], "tool": tool.name})
                        break
                    result = self._exec(project, ref, st, actor)
                    steps.append(st)
                i += 1
                call = gen.send(result)
        except StopIteration as stop:
            outcome = stop.value
        except ReplayError as e:
            status, error = "failed", str(e)
        p.update(steps=steps, llm_decisions=decisions)
        if status == "completed":
            o = outcome or PB.Outcome("Finished.")
            p["outcome"] = {"summary": o.summary, "findings": o.findings, "recommendations": o.recommendations}
            p["finished_at"] = now()
        elif status == "failed":
            p["outcome"] = {"summary": f"The run stopped: {error}", "findings": [], "recommendations": []}
            p["finished_at"] = now()
        out = self.db.rec_update(project, ref, actor, p, status, event=f"run:{status}")
        if status in ("completed", "failed"):
            self.audit(actor, f"run.{status}", project, ref, {"mode": p["mode"], "steps": len(steps),
                                                               "summary": p["outcome"]["summary"][:300]})
        return out

    def _exec(self, project: str, ref: str, st: dict, actor: str) -> dict:
        t0 = time.perf_counter()
        try:
            result = _compact(T.run(self.c, project, st["tool"], st["args"], actor))
            st["status"] = "done"
        except T.ToolError as e:
            result, st["status"] = {"error": str(e)[:500]}, "error"
        st.update(result=result, by=actor, ms=round((time.perf_counter() - t0) * 1000))
        self.audit(actor, "step", project, ref, {"n": st["n"], "tool": st["tool"], "kind": st["kind"], "status": st["status"]})
        return result

    # ---------------------------------------------------------------- human checkpoint
    def confirm(self, project: str, ref: str, n: int, decision: str, actor: str, comment: str = "") -> dict:
        rec = self.get(project, ref, "run")
        if rec["status"] != "waiting":
            raise Conflict(f"{ref} is {rec['status']}, not waiting for a confirmation")
        p = dict(rec["payload"])
        steps = list(p["steps"])
        st = steps[-1] if steps else None
        if not st or st["n"] != n or st["status"] != "awaiting_confirmation":
            raise Conflict(f"Step {n} is not the step waiting for confirmation")
        if decision not in ("run", "skip"):
            raise Invalid("decision must be 'run' or 'skip'")
        st = dict(st)
        st["comment"] = " ".join((comment or "").split())[:300]
        if decision == "skip":
            st.update(status="skipped", result={"skipped": True, "by": actor}, by=actor)
            self.audit(actor, "confirm", project, ref, {"n": n, "tool": st["tool"], "decision": "skip"})
        else:
            self.audit(actor, "confirm", project, ref, {"n": n, "tool": st["tool"], "decision": "run"})
            self._exec(project, ref, st, actor)
            st["confirmed_by"] = actor
        steps[-1] = st
        p["steps"] = steps
        self.db.rec_update(project, ref, actor, p, "running", event=f"step:{decision}")
        return self._advance(project, ref, actor)

    def cancel(self, project: str, ref: str, actor: str) -> dict:
        rec = self.get(project, ref, "run")
        if rec["status"] not in ("waiting", "running"):
            raise Conflict(f"{ref} is already {rec['status']}")
        p = {**rec["payload"], "finished_at": now(),
             "outcome": {"summary": f"Cancelled by {actor}.", "findings": [], "recommendations": []}}
        self.audit(actor, "run.cancelled", project, ref)
        return self.db.rec_update(project, ref, actor, p, "cancelled", event="run:cancelled")

    # ---------------------------------------------------------------- views
    def runs(self, project: str) -> list[dict]:
        out = []
        for r in reversed(self.list(project, "run")):
            p = r["payload"]
            out.append({"ref": r["ref"], "status": r["status"], "goal": p["goal"], "mode": p["mode"], "planner": p["planner"],
                        "steps": len(p["steps"]), "waiting_step": p["steps"][-1]["n"] if r["status"] == "waiting" else None,
                        "summary": (p.get("outcome") or {}).get("summary", ""), "started_by": p["started_by"],
                        "started_at": p["started_at"]})
        return out

    def run(self, project: str, ref: str) -> dict:
        return self.get(project, ref, "run")

    def report(self, project: str, ref: str) -> str:
        rec = self.get(project, ref, "run")
        p = rec["payload"]
        o = p.get("outcome") or {}
        L = [f"# Engineering agent run {ref}", "", f"Goal: **{p['goal']}**", "",
             f"Plan: `{p['mode']}` ({p['why']}); planner: {p['planner']}; started by {p['started_by']} at {p['started_at'][:16]}; "
             f"status: **{rec['status']}**", ""]
        L += [f"> {n}" for n in p.get("notes", [])]
        if o:
            L += ["## Result", "", o.get("summary", ""), ""]
            if o.get("findings"):
                L += ["## Findings", ""] + [f"- {f}" for f in o["findings"]] + [""]
            if o.get("recommendations"):
                L += ["## Recommended actions", ""] + [f"- {r}" for r in o["recommendations"]] + [""]
        L += ["## Steps", "", "| # | Tool | Kind | Status | Why | Run by |", "|---|---|---|---|---|---|"]
        cell = lambda t: str(t or "").replace("|", "\\|").replace("\n", " ")  # noqa: E731
        for s in p["steps"]:
            L.append(f"| {s['n']} | {s['tool']} | {s.get('kind') or '-'} | {s['status']} | {cell(s['why'])} | {cell(s.get('by'))} |")
        L += ["", "Write steps ran only after a person confirmed them, under that person's name. The agent cannot approve, "
              "release or delete anything; its output is advisory and every finding should be checked in the module concerned.", ""]
        return "\n".join(L)
