"""Measure the CarSaathi help search (Phase B) on the labelled owner descriptions in eval/owner_help/queries.json.

    python scripts/eval_owner_help.py            # print the report and write eval/owner_help/results/
    python scripts/eval_owner_help.py --check    # also exit 1 if a quality gate fails (CI)

Three sets, all written by the developer:
  queries.json   used while building the search
  heldout.json   written afterwards in longer, less direct words; its first run showed weaknesses (recorded in the
                 report), which were fixed with general changes (stop words, typo limits, synonyms, safety patterns),
                 not with its own phrases
  heldout2.json  written after those fixes and never used for tuning
Gates on the first: the right guide first for at least 85% and in the top 3 for 95%; at least 75% of out-of-scope
questions answered "not sure"; the right warning light for 80%. On the held-out sets: right guide first for 70%, top 3
for 85%. On all: every urgent description gets its safety card, and no other description gets one."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from owner import helpsearch  # noqa: E402

EVAL = ROOT / "eval" / "owner_help"
SETS = {"queries": "Building set", "heldout": "Held-out 1", "heldout2": "Held-out 2"}
HELD = {"top1": 0.70, "top3": 0.85, "safety_recall": 1.0, "false_alarms": 0}
GATES = {"queries": {"top1": 0.85, "top3": 0.95, "safety_recall": 1.0, "false_alarms": 0, "unsure_out_of_scope": 0.75, "lights": 0.80},
         "heldout": HELD, "heldout2": HELD}
FIRST_RUN = {"heldout": "first run, before the general fixes: right guide first 69.8%, top 3 93.0%, safety cards 3 of 5",
             "heldout2": "first run: right guide first 90.3%, top 3 100%, safety cards 2 of 3 and one false safety card. Afterwards only "
                         "safety patterns changed (steam alone now counts as overheating; 'the automatic emergency brake didn't work' is "
                         "no longer read as brake failure; and, from the unit tests, a sinking pedal and a stiff steering wheel); the "
                         "ranking was not changed"}


def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


def evaluate(name: str = "queries") -> dict:
    path = EVAL / f"{name}.json"
    qs = json.loads(path.read_text())["queries"]
    rows, fails = [], []
    for q in qs:
        r = helpsearch.search(q["q"], fuel=q.get("fuel"))
        ids = [x["id"] for x in r["results"]]
        urgent = (r["urgent"] or {}).get("id")
        lights = [x["id"] for x in r["lights"]]
        row = {"q": q["q"], "expected": q["guide"], "got": ids[:3], "urgent": urgent, "expected_urgent": q["urgent"],
               "lights": lights, "confident": r["confident"]}
        rows.append(row)
        if isinstance(q["guide"], str) and q["guide"] != "*" and (not ids or ids[0] != q["guide"]):
            fails.append({**row, "why": "wrong guide first"})
        if urgent != q["urgent"]:
            fails.append({**row, "why": "safety card missing" if q["urgent"] else f"false safety card ({urgent})"})
        if q.get("light") and q["light"] not in lights:
            fails.append({**row, "why": "warning light not offered"})
        if q["guide"] is None and not q["urgent"] and not q.get("light") and r["confident"]:
            fails.append({**row, "why": "confident on an out-of-scope question"})
    scored = [(q, rw) for q, rw in zip(qs, rows) if isinstance(q["guide"], str) and q["guide"] != "*"]
    urgent_qs = [(q, rw) for q, rw in zip(qs, rows) if q["urgent"]]
    calm = [(q, rw) for q, rw in zip(qs, rows) if not q["urgent"]]
    oos = [(q, rw) for q, rw in zip(qs, rows) if q["guide"] is None and not q["urgent"] and not q.get("light")]
    light_qs = [(q, rw) for q, rw in zip(qs, rows) if q.get("light")]
    m = {"queries": len(qs), "scored": len(scored),
         "top1": _rate(sum(rw["got"][:1] == [q["guide"]] for q, rw in scored), len(scored)),
         "top3": _rate(sum(q["guide"] in rw["got"] for q, rw in scored), len(scored)),
         "safety_recall": _rate(sum(rw["urgent"] == q["urgent"] for q, rw in urgent_qs), len(urgent_qs)),
         "false_alarms": sum(rw["urgent"] is not None for q, rw in calm),
         "unsure_out_of_scope": _rate(sum(not rw["confident"] for q, rw in oos), len(oos)),
         "lights": _rate(sum(q["light"] in rw["lights"] for q, rw in light_qs), len(light_qs)),
         "hinglish_top1": None}
    hi = [(q, rw) for q, rw in scored if any(w in q["q"].split() for w in ("nahi", "hai", "ho", "pe", "ka", "ki", "se", "mein", "apne", "baar"))]
    m["hinglish_top1"] = round(sum(rw["got"][:1] == [q["guide"]] for q, rw in hi) / len(hi), 3) if hi else None
    m["hinglish_n"] = len(hi)
    gates = {k: (m[k] <= v if k == "false_alarms" else (m[k] is not None and m[k] >= v)) for k, v in GATES[name].items()}
    return {"set": name, "metrics": m, "gates": gates, "passed": all(gates.values()), "failures": fails, "rows": rows}


def report(results: list[dict]) -> str:
    pct = lambda x: "-" if x is None else f"{x * 100:.1f}%"                     # noqa: E731
    gate = lambda r, k: (("0" if k == "false_alarms" else pct(GATES[r["set"]][k])) if k in GATES[r["set"]] else "")  # noqa: E731
    rows = (("Right guide first", "top1"), ("Right guide in the top 3", "top3"), ("Hinglish, right guide first", "hinglish_top1"),
            ("Urgent descriptions that get their safety card", "safety_recall"), ("Safety cards on descriptions that aren't urgent", "false_alarms"),
            ("Out-of-scope questions answered 'not sure'", "unsure_out_of_scope"), ("Warning-light descriptions offered the right light", "lights"))
    lines = ["# CarSaathi help search: evaluation", "",
             "Labelled owner descriptions, all written by the developer, in plain English, Hinglish, abbreviations and typos: "
             + "; ".join(f"**{SETS[r['set']]}** {r['metrics']['queries']} ({r['set']}.json)" for r in results)
             + ". The building set was used while writing the search. Held-out 1 was written afterwards in longer, less direct words; "
             "its first run exposed weaknesses that were fixed with general changes, not its own phrases. Held-out 2 was written after "
             "those fixes and never used for tuning. They catch regressions; they don't measure accuracy on real owners' words.", "",
             "| Measure | " + " | ".join(f"{SETS[r['set']]} | gate" for r in results) + " |",
             "|---|" + "---|---|" * len(results)]
    for label, k in rows:
        cells = []
        for r in results:
            v = r["metrics"][k]
            n = {"top1": r["metrics"]["scored"], "hinglish_top1": r["metrics"]["hinglish_n"]}.get(k)
            cells.append((str(v) if k == "false_alarms" else pct(v)) + (f" ({n})" if n is not None else ""))
            cells.append(gate(r, k))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += [""] + [f"{SETS[k]}, {v}." for k, v in FIRST_RUN.items()] + [""]
    lines += [f"**{'All gates pass.' if all(r['passed'] for r in results) else 'A gate fails.'}**", ""]
    misses = [(r["set"], f) for r in results for f in r["failures"]]
    if misses:
        lines += ["## Misses", "", "| Set | Description | Expected | Got | Why |", "|---|---|---|---|---|"]
        for name, f in misses:
            lines.append(f"| {SETS[name]} | {f['q']} | {f['expected'] or '-'} / {f['expected_urgent'] or '-'} | {', '.join(f['got']) or '-'} / {f['urgent'] or '-'} | {f['why']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if a quality gate fails")
    ap.add_argument("--out", default=str(ROOT / "eval" / "owner_help" / "results"))
    a = ap.parse_args()
    results = [evaluate(name) for name in SETS]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    text = report(results)
    (out / "REPORT.md").write_text(text)
    (out / "results.json").write_text(json.dumps({r["set"]: {k: r[k] for k in ("metrics", "gates", "passed", "failures")} for r in results}, indent=2))
    print(text)
    return 0 if (all(r["passed"] for r in results) or not a.check) else 1


if __name__ == "__main__":
    sys.exit(main())
