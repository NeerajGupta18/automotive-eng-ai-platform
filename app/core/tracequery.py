"""Cross-module traceability queries over the shared record store and link graph (HLD/HARA/TARA/UDS/code review)."""
from __future__ import annotations

from collections import Counter

from app.core.db import Database
from app.core.errors import NotFound
from app.core.trace import Trace

LABEL_KEYS = ("title", "text", "name", "message", "situation", "description")


def label(rec: dict) -> str:
    p = rec["payload"]
    if rec["kind"] == "hazard":
        return f"{p.get('function', '')}: {p.get('malfunction', '')}"
    for k in LABEL_KEYS:
        if isinstance(p.get(k), str) and p[k]:
            return p[k][:100]
    return rec["ref"]


def node(db: Database, project: str, ref: str) -> dict:
    rec = db.rec_get(project, ref)
    if rec:
        p = rec["payload"]
        return {"ref": ref, "module": rec["module"], "kind": rec["kind"], "status": rec["status"], "label": label(rec), "rev": rec["rev"],
                "asil": p.get("asil"), "risk": p.get("risk"), "suspect_reason": p.get("suspect_reason") or p.get("changed_reason"), "external": False}
    return {"ref": ref, "module": "external", "kind": ref.split(":")[0] if ":" in ref else "node", "status": "", "label": ref, "external": True}


def neighborhood(db: Database, project: str, ref: str) -> dict:
    tr = Trace(db)
    known = db.rec_get(project, ref) is not None or any(ref in (l["src"], l["dst"]) for l in db.links(project))
    if not known:
        raise NotFound(f"{ref} is not part of this project's traceability graph")
    up, down = tr.ancestors(project, ref), tr.descendants(project, ref)
    return {"node": node(db, project, ref),
            "ancestors": sorted(({**node(db, project, r), "hops": h} for r, h in up.items()), key=lambda x: (x["hops"], x["ref"])),
            "descendants": sorted(({**node(db, project, r), "hops": h} for r, h in down.items()), key=lambda x: (x["hops"], x["ref"]))}


def suspects(db: Database, project: str) -> list[dict]:
    """Everything, in any module, that needs re-review because something upstream changed."""
    return [node(db, project, r["ref"]) for r in db.rec_list(project, status="suspect")]


def summary(db: Database, project: str) -> dict:
    recs = db.rec_list(project)
    links = db.links(project)
    reqs = [r for r in recs if (r["module"], r["kind"]) in (("hara", "fsr"), ("hara", "tsr"), ("tara", "csr")) and r["status"] != "rejected"]
    tr = Trace(db)
    linked = {l["src"] for l in links} | {l["dst"] for l in links}
    return {"records": dict(Counter(f"{r['module']}/{r['kind']}" for r in recs)), "links": dict(Counter(l["rel"] for l in links)),
            "suspect": sum(r["status"] == "suspect" for r in recs),
            "requirements_without_verification": [r["ref"] for r in reqs if not r["payload"].get("verification")],
            "requirements_without_allocation": [r["ref"] for r in reqs if not r["payload"].get("allocated_to")],
            "requirements_with_open_findings": sorted({l["src"] for l in links if l["rel"] == "violated_by"
                                                       and (db.rec_get(project, l["dst"]) or {"status": ""})["status"] in ("proposed", "assessed", "approved")}),
            "unlinked_safety_goals": [r["ref"] for r in recs if r["module"] == "hara" and r["kind"] == "goal" and r["ref"] not in {l["src"] for l in links}],
            "nodes": len(linked)}


def dot(db: Database, project: str, ref: str | None = None) -> str:
    links = db.links(project)
    if ref:
        tr = Trace(db)
        keep = {ref} | set(tr.ancestors(project, ref)) | set(tr.descendants(project, ref))
        links = [l for l in links if l["src"] in keep and l["dst"] in keep]
    colors = {"hara": "#fde2e2", "tara": "#e2e8fd", "uds": "#e2fdea", "codereview": "#fdf5e2", "vuln": "#fde2f5", "ota": "#e2f7fd", "adas": "#ebe2fd", "external": "#eeeeee"}
    nodes = {n for l in links for n in (l["src"], l["dst"])}
    out = ["digraph trace {", "  rankdir=LR; node [shape=box, style=\"rounded,filled\", fontsize=10];"]
    for n in sorted(nodes):
        i = node(db, project, n)
        style = f'fillcolor="{colors.get(i["module"], "#ffffff")}"' + (', color="#d9480f", penwidth=2' if i["status"] == "suspect" else "")
        out.append(f'  "{n}" [label="{n}\\n{i["label"][:28].replace(chr(34), chr(39))}", {style}];')
    out += [f'  "{l["src"]}" -> "{l["dst"]}" [label="{l["rel"]}", fontsize=8];' for l in links]
    return "\n".join(out + ["}"])
