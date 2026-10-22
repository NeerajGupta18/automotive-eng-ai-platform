"""Help content of the owner app (Phase B): troubleshooting guides, help topics and dashboard warning lights.

The content lives in owner/guides/*.json so it can be reviewed and edited without touching code; it is checked when it
loads (and by the tests), so a broken guide fails at start-up instead of in front of a car owner.

A guide is a small decision graph. Steps run in order unless a step names its next one:
  check  an action the owner tries; "That fixed it" ends the guide as fixed, "Still not working" goes on
         (with "confirm": "do" there is one "Done" button: for actions whose effect can't be seen at once)
  ask    a question; each option names the next step and may record facts (e.g. where it happened)
  info   an explanation; "warn": true shows it as a safety notice
Special targets: "done" (fixed), "escalate" (get help: what to tell the service centre, and a support request).
Running past the last step escalates."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).resolve().parent / "guides"
STEP_TYPES = ("check", "ask", "info")
SPECIAL = ("done", "escalate")
FUELS = ("petrol", "diesel", "cng", "electric", "hybrid")
REPORT_SYMPTOMS = ("unexpected_braking", "false_warning", "no_braking")         # the Phase 6 field-issue symptoms an owner can report
LOCATIONS = ("flyover", "bridge", "gantry", "toll", "tunnel", "highway", "expressway", "city", "parking", "other")
FACT_KEYS = {"crash": bool, "symptom": str, "location": str}


class GuideError(ValueError):
    pass


def _check_guide(g: dict, topics: set[str]) -> None:
    gid = g.get("id", "?")

    def need(cond, msg):
        if not cond:
            raise GuideError(f"guide {gid}: {msg}")
    need(isinstance(g.get("id"), str) and g["id"].replace("-", "").isalnum(), "id must be letters, digits and dashes")
    need(g.get("category") in topics, f"unknown category {g.get('category')!r}")
    for k in ("title", "summary"):
        need(isinstance(g.get(k), str) and 5 <= len(g[k]) <= 160, f"{k} missing or too long")
    need(isinstance(g.get("symptoms"), list) and len(g["symptoms"]) >= 4, "needs at least 4 symptom phrases")
    need(isinstance(g.get("keywords"), list), "keywords must be a list")
    need(g.get("safety") in ("normal", "caution"), "safety must be normal or caution")
    need(set(g.get("fuels", FUELS)) <= set(FUELS), "unknown fuel")
    if "report" in g:
        need(g["report"].get("default_symptom") in REPORT_SYMPTOMS, "report.default_symptom must be a field-issue symptom")
    esc = g.get("escalate") or {}
    need(isinstance(esc.get("text"), str) and esc["text"], "escalate.text missing")
    need(isinstance(esc.get("tell_service"), list) and esc["tell_service"], "escalate.tell_service missing")

    steps = g.get("steps") or []
    need(steps, "no steps")
    ids = [s.get("id") for s in steps]
    need(len(ids) == len(set(ids)) and all(isinstance(i, str) for i in ids), "step ids must be unique")
    need(not set(ids) & set(SPECIAL), "a step may not be called done or escalate")
    edges: dict[str, list[str]] = {}
    for i, s in enumerate(steps):
        sid = s["id"]
        need(s.get("type") in STEP_TYPES, f"step {sid}: unknown type")
        need(isinstance(s.get("text"), str) and 5 <= len(s["text"]) <= 400, f"step {sid}: text missing or longer than 400")
        following = steps[i + 1]["id"] if i + 1 < len(steps) else "escalate"
        if s["type"] == "ask":
            opts = s.get("options") or []
            need(2 <= len(opts) <= 8, f"step {sid}: an ask needs 2 to 8 options")
            targets = []
            for o in opts:
                need(isinstance(o.get("label"), str) and o["label"], f"step {sid}: option without a label")
                targets.append(o.get("next", following))
                for k, v in (o.get("facts") or {}).items():
                    need(k in FACT_KEYS and isinstance(v, FACT_KEYS[k]), f"step {sid}: bad fact {k}")
                    if k == "symptom":
                        need(v in REPORT_SYMPTOMS, f"step {sid}: unknown symptom {v}")
                    if k == "location":
                        need(v in LOCATIONS, f"step {sid}: unknown location {v}")
        else:
            need("options" not in s, f"step {sid}: only an ask has options")
            need(s.get("confirm", "fix") in ("fix", "do"), f"step {sid}: confirm must be fix or do")
            targets = [s.get("next", following)]
        for t in targets:
            need(t in ids or t in SPECIAL, f"step {sid}: next {t!r} doesn't exist")
        edges[sid] = [t for t in targets if t not in SPECIAL]
    # every step reachable from the first, and no loops (a guide must always end)
    seen, stack = set(), [ids[0]]
    while stack:
        n = stack.pop()
        if n not in seen:
            seen.add(n)
            stack.extend(edges[n])
    need(seen == set(ids), f"unreachable steps: {sorted(set(ids) - seen)}")
    state: dict[str, int] = {}

    def visit(n: str) -> None:
        state[n] = 1
        for m in edges[n]:
            need(state.get(m) != 1, f"loop through step {m}")
            if m not in state:
                visit(m)
        state[n] = 2
    visit(ids[0])


@lru_cache(maxsize=1)
def load() -> dict:
    """-> {"topics": [...], "guides": {id: guide}, "lights": [...]} (validated)."""
    topics = json.loads((DIR / "topics.json").read_text(encoding="utf-8"))["topics"]
    tids = {t["id"] for t in topics}
    guides: dict[str, dict] = {}
    for f in sorted(DIR.glob("*.json")):
        if f.name in ("topics.json", "warning_lights.json"):
            continue
        for g in json.loads(f.read_text(encoding="utf-8"))["guides"]:
            _check_guide(g, tids)
            if g["id"] in guides:
                raise GuideError(f"guide {g['id']}: defined twice")
            guides[g["id"]] = g
    lights = json.loads((DIR / "warning_lights.json").read_text(encoding="utf-8"))["lights"]
    lids = set()
    for li in lights:
        if li["id"] in lids or li.get("color") not in ("red", "amber", "green", "blue") or li.get("severity") not in ("stop", "soon", "info"):
            raise GuideError(f"warning light {li.get('id')}: duplicate id, colour or severity")
        if li.get("guide") and li["guide"] not in guides:
            raise GuideError(f"warning light {li['id']}: unknown guide {li['guide']}")
        if not li.get("todo") or not li.get("meaning"):
            raise GuideError(f"warning light {li['id']}: meaning and todo needed")
        lids.add(li["id"])
    for t in topics:
        if not any(g["category"] == t["id"] for g in guides.values()):
            raise GuideError(f"topic {t['id']} has no guides")
    return {"topics": topics, "guides": guides, "lights": lights}


def applies(g: dict, fuel: str | None) -> bool:
    return not fuel or fuel in g.get("fuels", FUELS)


def summary(g: dict) -> dict:
    return {k: g[k] for k in ("id", "category", "title", "summary", "safety")} | {"reportable": "report" in g}
