"""Analysis on top of extracted entities: dependency graph, consistency checks,
revision comparison, impact analysis, graph export."""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict, deque

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def dependencies(ex: dict) -> list[dict]:
    """Edge: `consumer` requires data/service from `provider` through `interface`."""
    providers, edges = defaultdict(set), []
    for c in ex["components"]:
        for p in c["ports"]:
            if p["direction"] in ("P", "PR"):
                providers[p["interface"]].add(c["name"])
    for c in ex["components"]:
        for p in c["ports"]:
            if p["direction"] in ("R", "PR"):
                for prov in sorted(providers.get(p["interface"], ())):
                    if prov != c["name"]:
                        edges.append({"consumer": c["name"], "provider": prov,
                                      "interface": p["interface"], "port": p["name"]})
    return edges


def check_consistency(ex: dict) -> list[dict]:
    issues: list[dict] = []
    comps, ifs = ex["components"], ex["interfaces"]
    if_names = {i["name"] for i in ifs}
    providers, consumers = defaultdict(set), defaultdict(set)

    def add(rule, sev, entity, msg, source):
        issues.append({"id": f"{rule}:{entity}", "rule": rule, "severity": sev, "entity": entity,
                       "message": msg, "source": source})

    for kind, items in (("component", comps), ("interface", ifs)):
        seen: dict[str, int] = defaultdict(int)
        for it in items:
            seen[it["name"]] += 1
        for name, n in seen.items():
            if n > 1:
                src = next(i["source"] for i in items if i["name"] == name)
                add("R005", "error", name, f"Duplicate {kind} definition '{name}' ({n} times)", src)

    for c in comps:
        if not c["ports"]:
            add("R006", "warning", c["name"], f"Component {c['name']} declares no ports", c["source"])
        for p in c["ports"]:
            ent = f"{c['name']}.{p['name']}"
            src = {**c["source"], "page": p.get("page", c["source"]["page"])}
            if p["interface"] not in if_names:
                add("R003", "error", ent, f"Port {ent} references undefined interface '{p['interface']}'", src)
            if p["direction"] == "?":
                add("R012", "warning", ent, f"Port {ent} has unrecognised direction '{p.get('declared_direction','')}'", src)
            m = re.match(r"^(pr|p|r)[A-Z]", p["name"])
            if m and p["direction"] != "?":
                expected = m.group(1).upper()
                if expected != p["direction"]:
                    add("R008", "warning", ent,
                        f"Port {ent} is named as a {expected}-port but declared {p['direction']}-port (naming/direction mismatch)", src)
            if p["direction"] in ("P", "PR"):
                providers[p["interface"]].add(c["name"])
            if p["direction"] in ("R", "PR"):
                consumers[p["interface"]].add(c["name"])

    for c in comps:
        for p in c["ports"]:
            if p["direction"] in ("R", "PR") and not (providers[p["interface"]] - {c["name"]}):
                ent = f"{c['name']}.{p['name']}"
                add("R001", "warning", ent,
                    f"Missing dependency: {ent} requires '{p['interface']}' but no component provides it "
                    f"(external/BSW provider? confirm)", {**c["source"], "page": p.get("page", c["source"]["page"])})

    for i in ifs:
        n = i["name"]
        if not i["type"]:
            add("R010", "warning", n, f"Interface {n} has no declared type (SenderReceiver/ClientServer/...)", i["source"])
        if not providers[n] and not consumers[n]:
            add("R004", "info", n, f"Interface {n} is defined but not used by any port", i["source"])
        elif providers[n] and not consumers[n]:
            add("R002", "info", n, f"Interface {n} is provided by {sorted(providers[n])} but never required", i["source"])
        if len(providers[n]) > 1:
            add("R011", "warning", n, f"Interface {n} has multiple providers: {sorted(providers[n])}", i["source"])

    issues.sort(key=lambda x: (SEVERITY_ORDER[x["severity"]], x["rule"], x["entity"]))
    return issues


# ---------------------------------------------------------------- compare / impact
def compare(a: dict, b: dict) -> dict:
    """Diff two extractions (a = older revision, b = newer)."""
    def by_name(items): return {i["name"]: i for i in items}
    ca, cb, ia, ib = by_name(a["components"]), by_name(b["components"]), by_name(a["interfaces"]), by_name(b["interfaces"])
    port_set = lambda c: {(p["name"], p["direction"], p["interface"]) for p in c["ports"]}
    comp_changes = []
    for n in sorted(set(ca) & set(cb)):
        pa, pb = port_set(ca[n]), port_set(cb[n])
        if pa != pb:
            comp_changes.append({"name": n, "ports_added": sorted(pb - pa), "ports_removed": sorted(pa - pb)})
    if_changes = []
    for n in sorted(set(ia) & set(ib)):
        ea = {(e["name"], e["type"], e["unit"]) for e in ia[n]["elements"]}
        eb = {(e["name"], e["type"], e["unit"]) for e in ib[n]["elements"]}
        if ea != eb or ia[n]["type"] != ib[n]["type"]:
            if_changes.append({"name": n, "type_from": ia[n]["type"], "type_to": ib[n]["type"],
                               "elements_added": sorted(eb - ea), "elements_removed": sorted(ea - eb)})
    ia_issues = {i["id"] for i in check_consistency(a)}
    ib_issues = {i["id"] for i in check_consistency(b)}
    return {
        "components_added": sorted(set(cb) - set(ca)), "components_removed": sorted(set(ca) - set(cb)),
        "components_changed": comp_changes,
        "interfaces_added": sorted(set(ib) - set(ia)), "interfaces_removed": sorted(set(ia) - set(ib)),
        "interfaces_changed": if_changes,
        "issues_resolved": sorted(ia_issues - ib_issues), "issues_introduced": sorted(ib_issues - ia_issues),
    }


def impact(ex: dict, component: str) -> dict:
    edges = dependencies(ex)
    names = {c["name"] for c in ex["components"]}
    if component not in names:
        raise KeyError(component)
    requires, dependents = defaultdict(set), defaultdict(set)
    for e in edges:
        requires[e["consumer"]].add(e["provider"])
        dependents[e["provider"]].add(e["consumer"])

    def bfs(start, graph):
        seen, q, depth = {}, deque([(start, 0)]), {}
        while q:
            n, d = q.popleft()
            for m in graph.get(n, ()):
                if m not in seen and m != start:
                    seen[m] = d + 1
                    q.append((m, d + 1))
        return dict(sorted(seen.items(), key=lambda kv: (kv[1], kv[0])))

    return {"component": component,
            "downstream_impacted": bfs(component, dependents),   # who breaks if this changes
            "upstream_dependencies": bfs(component, requires)}   # what this relies on


# ---------------------------------------------------------------- export
def to_dot(ex: dict) -> str:
    lines = ["digraph HLD {", '  rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#eef3fb"];']
    for c in ex["components"]:
        lines.append(f'  "{c["name"]}";')
    for e in dependencies(ex):
        lines.append(f'  "{e["provider"]}" -> "{e["consumer"]}" [label="{e["interface"]}", fontsize=9];')
    lines.append("}")
    return "\n".join(lines)


def ports_csv(ex: dict) -> str:
    if_type = {i["name"]: i["type"] for i in ex["interfaces"]}
    prov = defaultdict(set)
    for c in ex["components"]:
        for p in c["ports"]:
            if p["direction"] in ("P", "PR"):
                prov[p["interface"]].add(c["name"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["component", "port", "direction", "interface", "interface_type", "providers", "page"])
    for c in ex["components"]:
        for p in c["ports"]:
            w.writerow([c["name"], p["name"], p["direction"], p["interface"], if_type.get(p["interface"], ""),
                        ";".join(sorted(prov[p["interface"]] - {c["name"]})) if p["direction"] != "P" else "",
                        p.get("page", "")])
    return buf.getvalue()
