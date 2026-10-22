"""MCP server: lets AI assistants (Claude Desktop, IDE copilots, in-house agents) use the platform through the
Model Context Protocol, with exactly the permissions of the API token they are given.

  python scripts/mcp_server.py            (stdio transport; configure it as a local MCP server in the client)

Environment: AEAI_API_URL (default http://localhost:8000), AEAI_API_TOKEN (needed when AUTH_MODE=enforced),
AEAI_PROJECT (default project), AEAI_USER (identity in development mode only).

Design: a thin, dependency-free JSON-RPC 2.0 server (MCP stdio transport, newline-delimited) that calls the REST
API. Authorization, rate limits and the audit log therefore apply exactly as for any other client. Reads are
exposed directly. The only way to change something is `start_agent_run`, and agent runs stop at human checkpoints
that can be confirmed only in the platform: an assistant can prepare work, never approve, release or delete."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SERVER_INFO = {"name": "automotive-eng-ai", "title": "Automotive Engineering AI Platform", "version": "1.0.0"}
SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("Tools for an automotive engineering workspace: release readiness (GO/NO-GO), traceability impact, the "
                "re-review worklist, document Q&A with citations, ADAS validation status, vulnerabilities, and engineering "
                "agent runs. Agent runs pause at human checkpoints that are confirmed in the platform UI; nothing here can "
                "approve, release or delete.")
REF = {"type": "string", "pattern": "^[A-Z]{1,8}-[0-9]{1,6}$", "description": "record reference, e.g. AR-005, F-012, VF-001"}
PROJECT = {"type": "string", "description": "project name (default: AEAI_PROJECT)", "maxLength": 64}


def _schema(props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": {"project": PROJECT, **(props or {})}, "required": required or [],
            "additionalProperties": False}


TOOLS = [
    {"name": "list_projects", "title": "List projects", "description": "Projects this token can access.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "release_readiness", "title": "Release readiness",
     "description": "GO / NO-GO across functional safety, cybersecurity, diagnostics, code review, vulnerabilities, OTA, ADAS, "
                    "traceability, R155 evidence, audit integrity, customer requirements and field issues, with owner and action per blocker.",
     "inputSchema": _schema(), "annotations": {"readOnlyHint": True}},
    {"name": "trace_impact", "title": "Traceability impact",
     "description": "Everything upstream and downstream of a record (what it depends on, what a change to it affects).",
     "inputSchema": _schema({"ref": REF}, ["ref"]), "annotations": {"readOnlyHint": True}},
    {"name": "suspect_items", "title": "Re-review worklist", "description": "Items made suspect by upstream changes, with the reason.",
     "inputSchema": _schema(), "annotations": {"readOnlyHint": True}},
    {"name": "search_documents", "title": "Ask the project documents",
     "description": "Answer from the approved engineering documents with citations; refuses when the documents do not say.",
     "inputSchema": _schema({"question": {"type": "string", "minLength": 3, "maxLength": 1000}}, ["question"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "adas_status", "title": "ADAS validation status",
     "description": "AEB requirements/scenarios, SIL availability, latest validation campaign and the current release.",
     "inputSchema": _schema(), "annotations": {"readOnlyHint": True}},
    {"name": "vulnerabilities", "title": "Vulnerability findings", "description": "Vulnerability findings from SBOM monitoring.",
     "inputSchema": _schema({"ecu": {"type": "string", "maxLength": 60},
                             "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]}}),
     "annotations": {"readOnlyHint": True}},
    {"name": "start_agent_run", "title": "Start an engineering agent run",
     "description": "Give the platform's engineering agent a goal (e.g. 'release readiness review', 'impact of changing AR-005', "
                    "'respond to CVE-2022-37434'). Read steps run now; any write step waits for a person to confirm it in the platform.",
     "inputSchema": _schema({"goal": {"type": "string", "minLength": 3, "maxLength": 1000},
                             "playbook": {"type": "string", "enum": ["auto", "release_readiness", "change_impact",
                                                                     "vulnerability_response", "adas_regression", "documents",
                                                                     "field_investigation"]}},
                            ["goal"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}},
    {"name": "get_agent_run", "title": "Agent run status", "description": "Status, steps and result of an agent run.",
     "inputSchema": _schema({"ref": REF}, ["ref"]), "annotations": {"readOnlyHint": True}},
    {"name": "field_issues", "title": "Field issues",
     "description": "Customer complaints from the field grouped into issues, with the agent's root cause, the confirmed hazard rating and "
                    "what waits for a person. With ref (e.g. FI-001): the investigation result and the complaints.",
     "inputSchema": _schema({"ref": REF}), "annotations": {"readOnlyHint": True}},
]


class ApiError(RuntimeError):
    pass


class Api:
    def __init__(self, base: str, token: str = "", user: str = "mcp-client", timeout: int = 120):
        self.base, self.token, self.user, self.timeout = base.rstrip("/"), token, user, timeout

    def call(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        url = self.base + urllib.parse.quote(path) + (("?" + urllib.parse.urlencode({k: v for k, v in query.items() if v})) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        else:
            req.add_header("X-User", self.user)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode()).get("detail", "")
            except ValueError:
                detail = ""
            raise ApiError(f"HTTP {e.code}: {detail or e.reason}") from e
        except urllib.error.URLError as e:
            raise ApiError(f"cannot reach the platform API at {self.base}: {e.reason}") from e


def _project(args: dict) -> str:
    p = str(args.get("project") or os.getenv("AEAI_PROJECT", "")).strip()
    if not p:
        raise ApiError("no project given (argument 'project' or environment variable AEAI_PROJECT)")
    return p


def _run_summary(run: dict) -> dict:
    p = run["payload"]
    steps = [{"n": s["n"], "tool": s["tool"], "status": s["status"]} for s in p["steps"]]
    out = {"ref": run["ref"], "status": run["status"], "plan": p["mode"], "steps": steps, "outcome": p.get("outcome")}
    if run["status"] == "waiting":
        out["waiting_for"] = (f"Step {p['steps'][-1]['n']} ({p['steps'][-1]['tool']}) needs a person to confirm it on the "
                              f"platform's Engineering Agent page.")
    return out


def call_tool(api: Api, name: str, args: dict) -> dict:
    if name == "list_projects":
        return {"projects": [p["name"] for p in api.call("GET", "/projects")]}
    p = _project(args)
    base = f"/projects/{p}"
    if name == "release_readiness":
        r = api.call("GET", f"{base}/readiness")
        return {"verdict": r["verdict"], "counts": r["counts"],
                "checks": [{k: c[k] for k in ("id", "title", "status", "summary", "owner", "action", "count")} | {"examples": c["items"][:5]}
                           for c in r["checks"]]}
    if name == "trace_impact":
        n = api.call("GET", f"{base}/trace/nodes/{args['ref']}")
        short = lambda x: {k: x.get(k) for k in ("ref", "module", "kind", "status", "label", "hops")}  # noqa: E731
        return {"node": short(n["node"]), "upstream": [short(x) for x in n["ancestors"][:20]],
                "downstream": [short(x) for x in n["descendants"][:40]]}
    if name == "suspect_items":
        return {"items": api.call("GET", f"{base}/trace/suspect")[:50]}
    if name == "search_documents":
        a = api.call("POST", f"{base}/hld/query", {"question": args["question"]})
        return {"answer": a["answer"], "refused": a["refused"], "refusal_reason": a["refusal_reason"],
                "citations": [{k: c[k] for k in ("label", "doc_name", "version", "page", "section")} for c in a["citations"]]}
    if name == "field_issues":
        if args.get("ref"):
            i = api.call("GET", f"{base}/field/issues/{args['ref']}")
            run = i.get("run") or {}
            return {"issue": i["summary"], "outcome": (run.get("payload") or {}).get("outcome"),
                    "complaints": [{"ref": c["ref"], "text": c["payload"]["text"][:300]} for c in i["complaint_records"][:20]]}
        return {"issues": api.call("GET", f"{base}/field/issues")[:50]}
    if name == "adas_status":
        s = api.call("GET", f"{base}/adas/status")
        return {k: s[k] for k in ("library_loaded", "requirements", "scenarios", "latest_campaign", "current_release")} | \
            {"sil_available": s["sil"]["available"], "firmware": s["sil"]["firmware_version"]}
    if name == "vulnerabilities":
        rows = api.call("GET", f"{base}/vuln/findings", query={"ecu": args.get("ecu"), "severity": args.get("severity")})
        return {"findings": [{"ref": f["ref"], "title": f["payload"]["title"], "severity": f["payload"]["severity"],
                              "score": f["payload"]["score"], "status": f["status"]} for f in rows[:50]]}
    if name == "start_agent_run":
        run = api.call("POST", f"{base}/agent/runs", {"goal": args["goal"], "playbook": args.get("playbook") or "auto"})
        return _run_summary(run)
    if name == "get_agent_run":
        return _run_summary(api.call("GET", f"{base}/agent/runs/{args['ref']}"))
    raise KeyError(name)


def _check_args(tool: dict, args: dict) -> str | None:
    schema = tool["inputSchema"]
    if not isinstance(args, dict):
        return "arguments must be an object"
    extra = set(args) - set(schema["properties"])
    if extra:
        return f"unknown argument(s): {sorted(extra)}"
    for r in schema.get("required", []):
        if r not in args:
            return f"missing required argument '{r}'"
    import re
    for k, v in args.items():
        spec = schema["properties"][k]
        if not isinstance(v, str):
            return f"'{k}' must be a string"
        if "pattern" in spec and not re.match(spec["pattern"], v):
            return f"'{k}' has an invalid format"
        if "enum" in spec and v not in spec["enum"]:
            return f"'{k}' must be one of {spec['enum']}"
        if len(v) > spec.get("maxLength", 1000) or len(v) < spec.get("minLength", 0):
            return f"'{k}' has an invalid length"
    return None


def handle(msg: dict, api: Api, state: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    is_request = "id" in msg

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result} if is_request else None

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}} if is_request else None

    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return err(-32600, "invalid request")
    if method == "initialize":
        requested = params.get("protocolVersion")
        state["initialized"] = True
        return ok({"protocolVersion": requested if requested in SUPPORTED else SUPPORTED[0],
                   "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        tool = next((t for t in TOOLS if t["name"] == params.get("name")), None)
        if tool is None:
            return err(-32602, f"unknown tool: {params.get('name')}")
        args = params.get("arguments") or {}
        problem = _check_args(tool, args)
        if problem:
            return ok({"content": [{"type": "text", "text": f"Invalid arguments: {problem}"}], "isError": True})
        try:
            data = call_tool(api, tool["name"], args)
        except ApiError as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
        return ok({"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)}], "structuredContent": data,
                   "isError": False})
    return err(-32601, f"method not found: {method}")


def main() -> int:
    api = Api(os.getenv("AEAI_API_URL", "http://localhost:8000"), os.getenv("AEAI_API_TOKEN", ""), os.getenv("AEAI_USER", "mcp-client"))
    state: dict = {}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            if isinstance(msg, list):                      # batches (2025-03-26) are answered one by one
                outs = [o for o in (handle(m, api, state) for m in msg if isinstance(m, dict)) if o]
                out = outs or None
            else:
                out = handle(msg, api, state) if isinstance(msg, dict) else {"jsonrpc": "2.0", "id": None,
                                                                            "error": {"code": -32600, "message": "invalid request"}}
        if out is not None:
            sys.stdout.write(json.dumps(out, default=str) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
