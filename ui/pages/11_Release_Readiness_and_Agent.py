"""Phase 5: program release readiness (GO / NO-GO) and the engineering agent with human checkpoints."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import common
from common import api, raw_get

ICON = {"block": "❌ block", "warn": "⚠️ warn", "pass": "✅ pass", "n/a": "– n/a"}
STEP_ICON = {"done": "✅", "awaiting_confirmation": "⏸️", "skipped": "⏭", "error": "❗", "blocked": "🛡️"}
RUN_ICON = {"waiting": "⏸️ waiting for you", "completed": "✅ completed", "failed": "❗ failed", "cancelled": "⏹ cancelled",
            "running": "⏳ running"}
EXAMPLES = ["Are we ready to release? Prepare the release readiness review.", "What is the impact if we change AR-005?",
            "How do we respond to CVE-2022-37434?", "Run the ADAS regression.", "Investigate field issue FI-001.",
            "Which component provides the vehicle speed?"]

common.setup("Release Readiness & Agent")
project = common.sidebar("Module: **Release readiness and engineering agent** (Phase 5). One GO / NO-GO across all "
                         "disciplines; an agent that prepares the work and stops for you before it changes anything.")
st.title("Release readiness and engineering agent")
if not project:
    st.info("Create a project workspace in the sidebar.")
    st.stop()
P = f"/projects/{project}"


def show_run(run: dict, where: str) -> None:
    k = f"{where}_{run['ref']}"
    p = run["payload"]
    st.markdown(f"**{run['ref']}** · {RUN_ICON.get(run['status'], run['status'])} · plan `{p['mode']}` ({p['why']}) · "
                f"planner {p['planner']} · started by {p['started_by']}")
    st.caption(f"Goal: {p['goal']}")
    for n in p.get("notes", []):
        st.info(n)
    for s in p["steps"]:
        kind = "write" if s.get("kind") == "write" else "read" if s.get("kind") == "read" else "?"
        label = f"{STEP_ICON.get(s['status'], '•')} {s['n']}. `{s['tool']}` ({kind}) · {s['status'].replace('_', ' ')}" \
                + (f" · by {s['by']}" if s.get("by") else "") + f" · {s['why']}"
        with st.expander(label, expanded=s["status"] == "awaiting_confirmation"):
            if s["status"] == "awaiting_confirmation":
                st.warning("This step would change something, so it waits for a person. It runs under **your** name.")
                if s["tool"] == "create_tickets":
                    st.dataframe(pd.DataFrame([{"summary": t["summary"], "priority": t["priority"], "source": t["source_ref"]}
                                               for t in s["args"]["tickets"]]), hide_index=True, width="stretch")
                    with st.popover("Ticket descriptions"):
                        for t in s["args"]["tickets"]:
                            st.markdown(f"**{t['summary']}**")
                            st.text(t["description"])
                else:
                    st.json(s.get("args") or {})
                cm = st.text_input("Comment (optional)", key=f"cm_{k}_{s['n']}")
                b1, b2, b3 = st.columns(3)
                if b1.button("Run this step", key=f"run_{k}_{s['n']}", type="primary"):
                    r = api("POST", f"{P}/agent/runs/{run['ref']}/steps/{s['n']}/confirm", json={"decision": "run", "comment": cm})
                    if r:
                        st.session_state["agent_run"] = r["ref"]
                        st.rerun()
                if b2.button("Skip it", key=f"skip_{k}_{s['n']}"):
                    r = api("POST", f"{P}/agent/runs/{run['ref']}/steps/{s['n']}/confirm", json={"decision": "skip", "comment": cm})
                    if r:
                        st.rerun()
                if b3.button("Cancel run", key=f"cancel_{k}"):
                    if api("POST", f"{P}/agent/runs/{run['ref']}/cancel"):
                        st.rerun()
            else:
                st.json(s.get("result") or {}, expanded=False)
    o = p.get("outcome")
    if o:
        (st.success if run["status"] == "completed" else st.warning)(o["summary"])
        if o.get("findings"):
            st.markdown("**Findings**\n" + "\n".join(f"- {f}" for f in o["findings"]))
        if o.get("recommendations"):
            st.markdown("**Recommended actions**\n" + "\n".join(f"- {r}" for r in o["recommendations"]))
        r = raw_get(f"{P}/agent/runs/{run['ref']}/report")
        if r is not None:
            st.download_button("⬇ Agent report (.md)", r.content, file_name=f"{run['ref']}_agent_report.md", key=f"dl_{k}")


t_ready, t_agent, t_runs, t_tools = st.tabs(["1 · Release readiness", "2 · Engineering agent", "3 · Agent runs",
                                             "4 · What the agent can do"])

# ------------------------------------------------------------------ 1 readiness
with t_ready:
    r = api("GET", f"{P}/readiness")
    if r:
        msg = {"NO-GO": st.error, "GO WITH CONDITIONS": st.warning, "GO": st.success}.get(r["verdict"], st.info)
        msg(f"### {r['verdict']}\n{r['counts']['block']} blocking · {r['counts']['warn']} with warnings · "
            f"{r['counts']['pass']} passed · {r['counts']['n/a']} not applicable")
        st.caption(r["note"] + " Each check reuses the rules of its module.")
        st.dataframe(pd.DataFrame([{"check": c["id"], "area": c["title"], "status": ICON[c["status"]], "open items": c["count"],
                                    "summary": c["summary"], "owner": c["owner"], "action": c["action"]} for c in r["checks"]]),
                     hide_index=True, width="stretch")
        for c in r["checks"]:
            if c["items"]:
                with st.expander(f"{ICON[c['status']]} {c['id']} {c['title']}: {c['count']} open item(s)"):
                    st.dataframe(pd.DataFrame(c["items"]), hide_index=True, width="stretch")
        d1, d2, d3 = st.columns(3)
        for col, fmt in ((d1, "md"), (d2, "json")):
            rr = raw_get(f"{P}/readiness/report", format=fmt)
            if rr is not None:
                col.download_button(f"⬇ Readiness report (.{fmt})", rr.content, file_name=f"{project}_readiness.{fmt}")
        if d3.button("Let the agent prepare the review", type="primary"):
            run = api("POST", f"{P}/agent/runs", json={"goal": "Prepare the release readiness review", "playbook": "release_readiness"})
            if run:
                st.session_state["agent_run"] = run["ref"]
                st.success(f"{run['ref']} started: open the *Engineering agent* tab.")
        st.markdown("**In a CI/CD pipeline:** `python scripts/release_gate.py --project " + project
                    + " --api <platform URL>` fails the pipeline on NO-GO (token in `AEAI_API_TOKEN`).")

# ------------------------------------------------------------------ 2 agent
with t_agent:
    meta = api("GET", f"{P}/agent/tools") or {"playbooks": {}, "llm_planner_available": False}
    st.markdown("Give the agent a goal. It reads what it needs on its own; anything that would **change** something "
                "(tickets, scenario proposals, validation campaigns) waits for you. It can never approve, release or delete.")
    ex = st.selectbox("Examples", [""] + EXAMPLES, format_func=lambda x: x or "choose an example…")
    goal = st.text_area("Goal", value=ex, height=80, max_chars=1000)
    c1, c2 = st.columns(2)
    pb = c1.selectbox("Plan", ["auto"] + list(meta["playbooks"]), format_func=lambda x: "auto (route the goal)" if x == "auto" else
                      f"{x}: {meta['playbooks'][x]}")
    planner = c2.radio("Planner", ["rules", "llm"], horizontal=True, disabled=not meta["llm_planner_available"],
                       format_func=lambda x: {"rules": "rule playbooks", "llm": "LLM chooses the tools"}[x],
                       help="The LLM planner needs a real LLM backend; its decisions are recorded and its tools are the same.")
    if st.button("Start", type="primary") and goal.strip():
        run = api("POST", f"{P}/agent/runs", json={"goal": goal, "playbook": pb, "planner": planner})
        if run:
            st.session_state["agent_run"] = run["ref"]
    cur = st.session_state.get("agent_run")
    if cur:
        run = api("GET", f"{P}/agent/runs/{cur}", quiet=True)
        if run:
            st.divider()
            show_run(run, "agent")

# ------------------------------------------------------------------ 3 history
with t_runs:
    runs = api("GET", f"{P}/agent/runs") or []
    if not runs:
        st.info("No agent runs yet.")
    else:
        st.dataframe(pd.DataFrame([{"run": x["ref"], "status": RUN_ICON.get(x["status"], x["status"]), "plan": x["mode"],
                                    "goal": x["goal"], "steps": x["steps"], "result": x["summary"], "by": x["started_by"],
                                    "at": x["started_at"][:16]} for x in runs]), hide_index=True, width="stretch")
        pick = st.selectbox("Open a run", [x["ref"] for x in runs])
        if pick:
            run = api("GET", f"{P}/agent/runs/{pick}")
            if run:
                show_run(run, "history")

# ------------------------------------------------------------------ 4 tools and guardrails
with t_tools:
    meta = api("GET", f"{P}/agent/tools") or {"tools": []}
    st.dataframe(pd.DataFrame([{"tool": t["name"], "kind": "✍️ write (needs confirmation)" if t["kind"] == "write" else "👁 read",
                                "description": t["description"], "arguments": ", ".join(t["params"]) or "-"} for t in meta["tools"]]),
                 hide_index=True, width="stretch")
    st.markdown("""**Guardrails, enforced by the server**
- Only these tools exist: nothing can approve, release, reject, delete, revoke keys or manage users.
- Write steps stop the run until a person confirms them; they then run under that person's name, so the
  four-eyes rules of every module still apply to what the agent produced.
- Arguments are validated; results are summarised; an LLM planner sees tool results as untrusted data.
- Step budget per run (`AGENT_MAX_STEPS`); every step, checkpoint and confirmation is in the audit log.
- A waiting run survives restarts: it is resumed by replaying its plan with the recorded results.""")
    st.caption(f"Max steps per run: {meta.get('max_steps')} · LLM planner available: {meta.get('llm_planner_available')}")
    st.code(json.dumps({"mcpServers": {"automotive-eng-ai": {"command": "python", "args": ["scripts/mcp_server.py"],
                                                            "env": {"AEAI_API_URL": "http://localhost:8000", "AEAI_PROJECT": project,
                                                                    "AEAI_API_TOKEN": "<your token>"}}}}, indent=2), language="json")
    st.caption("The same tools for AI assistants (Model Context Protocol): add this to the assistant's MCP configuration. "
               "The assistant gets exactly the permissions of the token.")
