"""Deterministic fix templates. A suggestion is only offered when it is mechanically safe to express, and it is
VERIFIED by re-analysing the patched source: the finding must disappear and nothing new may appear.
Fixes are proposals (unified diff) for a human to apply; nothing is changed automatically."""
from __future__ import annotations

import difflib
import re

from app.modules.codereview.analyzer import Ctx, analyze


def _stmt_start_ok(text: str, pos: int) -> bool:
    prev = text[:pos].rstrip()[-1:]
    return prev in ("", ";", "{", "}", ")")


def _ret_stmt(ctx: Ctx, pos: int) -> str | None:
    f = ctx.func_at(pos)
    if not f:
        return None
    rt = re.sub(r"[A-Za-z_]\w*$", "", re.split(r"[;}]", f.head)[-1]).strip()
    if re.search(r"\bvoid$", rt):
        return "return;"
    if "*" in rt:
        return "return NULL;"
    if re.search(r"\bbool$", rt):
        return "return false;"
    if re.search(r"\b(int|long|short|int\d+_t|Std_ReturnType)$", rt):
        return "return -1; /* TODO: use the project's error code */"
    return None


def _fix(text: str, ctx: Ctx, f: dict) -> tuple[str, str] | None:
    rule, x = f["rule"], f["_extra"]
    if rule == "CR001":
        c = x["call"]
        if len(c["raw"]) == 1 and ctx.array_len(c["raw"][0]) and _stmt_start_ok(text, c["pos"]):
            a = c["raw"][0]
            return text[:c["pos"]] + f"fgets({a}, sizeof({a}), stdin)" + text[c["end"]:], "Replace gets() with fgets(); note that fgets keeps the trailing newline."
    if rule == "CR002":
        c = x["call"]
        a = c["raw"]
        if not a or not ctx.array_len(a[0]) or not _stmt_start_ok(text, c["pos"]):
            return None
        d = a[0]
        if c["name"] == "strcpy" and len(a) == 2:
            new = f'snprintf({d}, sizeof({d}), "%s", {a[1]})'
        elif c["name"] == "strcat" and len(a) == 2:
            new = f'snprintf({d} + strlen({d}), sizeof({d}) - strlen({d}), "%s", {a[1]})'
        elif c["name"] == "sprintf" and len(a) >= 2:
            new = f"snprintf({d}, sizeof({d}), {text[c['spans'][1][0]:c['end'] - 1].strip()})"
        else:
            return None
        return text[:c["pos"]] + new + text[c["end"]:], "Use snprintf with the destination size (needs <stdio.h>); check the result if truncation matters."
    if rule == "CR004" and "call" in x:
        c, i = x["call"], x["fmt_index"]
        a, b = c["spans"][i]
        start = a + (len(text[a:b]) - len(text[a:b].lstrip()))
        return text[:start] + '"%s", ' + text[start:], "Pass the data as an argument to a literal format string."
    if rule == "CR009" and x.get("kind") == "double-free":
        m = re.compile(r"free\s*\(\s*\w+\s*\)\s*;").match(text, f["_extra"]["pos"])
        if m:
            return text[:m.start()] + text[m.end():], "Remove the second free() of the same pointer."
    if rule == "CR012" and "cmp_pos" in x and text[x["cmp_pos"]:x["cmp_pos"] + 2] == "<=":
        return text[:x["cmp_pos"]] + "<" + text[x["cmp_pos"] + 2:], "Use '<' so the loop does not index one element past the array."
    if rule == "CR022" and "close" in x:
        c = x["close"]
        line_start = text.rfind("\n", 0, f["_extra"]["pos"]) + 1
        ind = re.match(r"[ \t]*", text[line_start:]).group(0)
        return text[:c].rstrip() + f"\n{ind}    default:\n{ind}        break;\n{ind}" + text[c:], "Add a default clause; put defensive handling there if needed."
    if rule == "CR025" and x.get("stmt"):
        a, b = x["stmt"]
        return text[:a] + "{ " + text[a:b] + " }" + text[b:], "Enclose the body in braces."
    if rule == "CR007":
        semi = text.find(";", x.get("pos", 0))
        ret = _ret_stmt(ctx, x["pos"])
        if semi != -1 and ret:
            line_start = text.rfind("\n", 0, x["pos"]) + 1
            ind = re.match(r"[ \t]*", text[line_start:]).group(0)
            return text[:semi + 1] + f"\n{ind}if ({x['var']} == NULL) {{ {ret} }}" + text[semi + 1:], "Add a NULL check with an early exit; adapt the error handling to your project."
    return None


def propose(name: str, text: str, fingerprint: str) -> dict:
    ctx = Ctx(name, text)
    found = analyze(name, text, ctx)
    f = next((x for x in found if x["fingerprint"] == fingerprint), None)
    if not f:
        return {"available": False, "reason": "The finding no longer exists in the current source revision"}
    got = _fix(ctx.text, ctx, f)
    if not got:
        return {"available": False, "reason": "No safe mechanical fix is available for this finding; see the rule guidance"}
    new, desc = got
    after = analyze(name, new)
    def count(items, rule, fn):
        return sum(1 for i in items if i["rule"] == rule and i["function"] == fn)
    resolved = count(found, f["rule"], f["function"]) - count(after, f["rule"], f["function"])
    groups = {(i["rule"], i["function"]) for i in after}
    introduced = [f"{r} in {fn or 'file scope'}" for r, fn in sorted(groups) if count(after, r, fn) > count(found, r, fn)]
    diff = "\n".join(difflib.unified_diff(ctx.text.splitlines(), new.splitlines(), f"a/{name}", f"b/{name}", lineterm="", n=2))
    return {"available": True, "description": desc, "diff": diff, "verified": resolved >= 1 and not introduced,
            "verification": {"finding_resolved": resolved >= 1, "introduced": introduced,
                             "findings_before": len(found), "findings_after": len(after)}}
