"""Measure the field-issue agent on labelled cases (Phase 6).

    python scripts/eval_field_agent.py                 # all cases in eval/field/cases.json -> eval/field/results/
    python scripts/eval_field_agent.py --check         # CI gate: exit 1 below the thresholds

Every case is filed as a complaint in its own temporary project (ADAS library loaded, sample build registry), the
agent investigates it exactly as in the application (playbook field_investigation, up to its first human
checkpoint), and the result is compared with the known cause:

  root_cause_accuracy   the issue's root-cause class matches the label
  symptom_accuracy      the complaint text was understood as the labelled symptom
  reproduction_rate     recordings of real events reproduced bit-exactly on the field build (SIL) and in closed loop
  fix_success_rate      calibration cases: the responsible parameter found and a fix verified against the complaint and the
                        regression library (or, when the change prevented collisions, "validate" instead of revert);
                        coding defects: the expected rollback build
  false_defects         cases without a malfunction that the agent called a defect (must be 0)
  missed_defects        malfunctions the agent called "no defect"

The cases are synthetic (generated from known causes by scripts/make_field_data.py): this measures that the
investigation chain works end to end, not its accuracy on real fleet data."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MALFUNCTION = ("implementation_defect", "calibration_regression", "sensor_false_target")
THRESHOLDS = {"root_cause_accuracy": 0.95, "symptom_accuracy": 0.95, "reproduction_rate": 0.95, "fix_success_rate": 0.9}


def _step(run: dict, tool: str) -> dict | None:
    return next((s["result"] for s in run["payload"]["steps"] if s["tool"] == tool and s["status"] == "done"), None)


def evaluate(cases_path: Path, quiet: bool = False) -> dict:
    tmp = tempfile.mkdtemp(prefix="aeai-field-eval-")
    os.environ.update({"DATA_DIR": tmp, "VECTOR_BACKEND": "numpy", "AUTH_MODE": "off", "FIELD_AUTO_INVESTIGATE": "1",
                       "LLM_BACKEND": "mock", "FIELD_TRIAGE_LLM": "0", "LOG_LEVEL": "WARNING"})
    from app.config import get_settings
    from app.container import Container
    from app.modules.field import builds as B

    cont = Container(get_settings())
    data = json.loads(cases_path.read_text())
    base = cases_path.parent
    rows = []
    for case in data["cases"]:
        t0 = time.perf_counter()
        prj = f"eval-{case['id'].lower()}"
        cont.db.create_project(prj)
        cont.adas.load_library(prj, "eval-reviewer")
        cont.field.load_sample_builds(prj, "eval")
        content = (base / case["log"]).read_bytes() if case.get("log") else None
        out = cont.field.submit(prj, case["text"], "eval", build=case["build"], log_name=(case.get("log") or "").split("/")[-1] or None,
                                log_content=content)
        run = out["run"]
        exp = case["expected"]
        diag = _step(run, "diagnose_field_issue") or {}
        rep = _step(run, "replay_field_logs") or {}
        rec = _step(run, "reconstruct_field_scenarios") or {}
        fix = _step(run, "search_field_fix")
        got = diag.get("class")
        sym = out["complaint"]["payload"]["symptom"]
        row = {"case": case["id"], "build": case["build"], "expected": exp["class"], "got": got, "correct": got == exp["class"],
               "symptom_ok": sym == exp.get("symptom", "unexpected_braking"), "symptom": sym,
               "culprits": ",".join(diag.get("culprits") or []), "run_status": run["status"],
               "waiting_at": run["payload"]["steps"][-1]["tool"] if run["status"] == "waiting" else "",
               "seconds": round(time.perf_counter() - t0, 2)}
        real_event = exp["class"] not in ("insufficient_data",) and case.get("log")
        if real_event:
            row["reproduced"] = bool(rep.get("reproduced")) and rep.get("reproduced") == rep.get("of")
            row["reconstructed"] = bool(rec.get("matched")) and rec.get("matched") == rec.get("of")
        if "lever" in exp or "rollback" in exp:
            r_ = (fix or {}).get("recommended") or {}
            if "rollback" in exp:
                row["fix_ok"] = r_.get("rollback_to") == exp["rollback"]
                row["fix"] = f"rollback to {r_.get('rollback_to')}"
            else:
                lever_found = exp["lever"] in (diag.get("culprits") or [])
                changed = exp["lever"] in (r_.get("changes") or {})
                row["fix_ok"] = lever_found and (changed or (fix or {}).get("kind") == "validate")
                row["fix"] = ("validate (prevented collisions)" if (fix or {}).get("kind") == "validate" else
                              ", ".join(f"{k} {v['from']:g}→{v['to']:g}" for k, v in (r_.get("changes") or {}).items()) or "none")
        rows.append(row)
        if not quiet:
            print(f"{case['id']:4} {'OK ' if row['correct'] else 'BAD'} expected {exp['class']:22} got {str(got):22} "
                  f"{row.get('fix', '')} ({row['seconds']} s)")
    n = len(rows)
    rp = [r for r in rows if "reproduced" in r]
    fx = [r for r in rows if "fix_ok" in r]
    metrics = {"root_cause_accuracy": round(sum(r["correct"] for r in rows) / n, 3),
               "symptom_accuracy": round(sum(r["symptom_ok"] for r in rows) / n, 3),
               "reproduction_rate": round(sum(r["reproduced"] and r["reconstructed"] for r in rp) / len(rp), 3) if rp else None,
               "fix_success_rate": round(sum(r["fix_ok"] for r in fx) / len(fx), 3) if fx else None,
               "false_defects": sum(1 for r in rows if r["expected"] not in MALFUNCTION and r["got"] in MALFUNCTION),
               "missed_defects": sum(1 for r in rows if r["expected"] in MALFUNCTION and r["got"] not in MALFUNCTION),
               "mean_seconds_per_case": round(sum(r["seconds"] for r in rows) / n, 2)}
    by_class = {}
    for r in rows:
        b = by_class.setdefault(r["expected"], {"cases": 0, "correct": 0})
        b["cases"] += 1
        b["correct"] += r["correct"]
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cases": n, "metrics": metrics, "by_class": by_class,
            "sil": B.sil_available(), "thresholds": THRESHOLDS, "rows": rows,
            "note": "Synthetic cases generated from known causes (scripts/make_field_data.py); measures the investigation chain end to "
                    "end, not accuracy on real fleet data."}


def markdown(res: dict) -> str:
    m = res["metrics"]
    L = ["# Field-issue agent: evaluation", "", f"{res['cases']} labelled cases · generated {res['generated_at'][:16]} UTC · "
         f"recordings replayed on {'the compiled C code (SIL)' if res['sil'] else 'the Python model (no compiler)'}", "",
         "| Metric | Value | Threshold |", "|---|---|---|"]
    for k, v in m.items():
        thr = res["thresholds"].get(k)
        L.append(f"| {k} | {v} | {'≥ ' + str(thr) if thr is not None else ('0' if k == 'false_defects' else '')} |")
    L += ["", "| Root cause | Cases | Correct |", "|---|---|---|"]
    L += [f"| {k} | {v['cases']} | {v['correct']} |" for k, v in res["by_class"].items()]
    L += ["", "| Case | Build | Expected | Agent | Symptom | Culprit | Fix | Reproduced | s |", "|---|---|---|---|---|---|---|---|---|"]
    for r in res["rows"]:
        L.append(f"| {r['case']} | {r['build']} | {r['expected']} | {'✅' if r['correct'] else '❌'} {r['got']} | {r['symptom']} | {r['culprits']} | "
                 f"{r.get('fix', '')}{' ✅' if r.get('fix_ok') else ' ❌' if 'fix_ok' in r else ''} | "
                 f"{'' if 'reproduced' not in r else ('yes' if r['reproduced'] and r['reconstructed'] else 'no')} | {r['seconds']} |")
    L += ["", res["note"], ""]
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", default=str(ROOT / "eval" / "field" / "cases.json"))
    ap.add_argument("--out", default=str(ROOT / "eval" / "field" / "results"))
    ap.add_argument("--check", action="store_true", help="exit 1 when a metric is below its threshold or a false defect occurs")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args()
    res = evaluate(Path(a.cases), a.quiet)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "field_agent.json").write_text(json.dumps(res, indent=2) + "\n")
    (out / "FIELD_AGENT_REPORT.md").write_text(markdown(res))
    print(json.dumps(res["metrics"], indent=2))
    if a.check:
        bad = [k for k, thr in THRESHOLDS.items() if res["metrics"][k] is not None and res["metrics"][k] < thr]
        if res["metrics"]["false_defects"]:
            bad.append("false_defects")
        if bad:
            sys.exit(f"Field agent below threshold: {', '.join(bad)}")


if __name__ == "__main__":
    main()
