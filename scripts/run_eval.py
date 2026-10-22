"""Evaluate the RAG pipeline on the labelled question set and write a report.

Examples
  python scripts/run_eval.py                                   # offline hybrid (no downloads, ~10 s)
  python scripts/run_eval.py --preset offline-vector           # Stage-1 style vector-only retrieval
  python scripts/run_eval.py --preset local-ai                 # BGE embeddings + Ollama (see docs/PHASE1_AI_QUALITY.md)
  python scripts/run_eval.py --preset local-ai --set ollama_model=llama3.1:8b --label llama3.1-8b
  python scripts/run_eval.py --set min_coverage=0.4 --label cov-0.4 --no-save
  python scripts/run_eval.py --fail-under "answer_accuracy>=0.8" "refusal_offtopic>=1.0"   # CI quality gate
  python scripts/run_eval.py --report-only                     # rebuild eval/results/REPORT.md

Results: eval/results/<label>.json and eval/results/REPORT.md (comparison of every saved run)."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOG_LEVEL", "ERROR")

from app import evaluation as ev  # noqa: E402

ORDER = {"stage5-baseline": 10, "offline-vector": 20, "offline-hybrid": 30, "local-embeddings": 40,
         "local-ai": 50, "local-ai-rerank": 60}


def _coerce(key: str, raw: str):
    from app.config import Settings
    types = {f.name: str(f.type) for f in dataclasses.fields(Settings)}
    if key not in types:
        raise SystemExit(f"Unknown setting '{key}'. Known: {', '.join(sorted(types))}")
    t = types[key]
    if "bool" in t:
        return raw.lower() in ("1", "true", "yes", "on")
    if "float" in t:
        return float(raw)
    if "int" in t:
        return int(raw)
    return raw


def load_saved(results_dir: Path) -> list[dict]:
    runs = []
    for p in sorted(results_dir.glob("*.json")):
        try:
            runs.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            print(f"skipping unreadable {p}", file=sys.stderr)
    runs.sort(key=lambda r: (r.get("order", 50), r.get("created", "")))
    return runs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", default="offline-hybrid", choices=sorted(ev.PRESETS))
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a setting, e.g. min_coverage=0.4")
    ap.add_argument("--label", help="name of this run (default: the preset name)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--questions", type=Path, default=ev.EVAL_DIR / "questions.jsonl")
    ap.add_argument("--limit", type=int, help="only the first N questions (smoke test)")
    ap.add_argument("--out", type=Path, default=ev.default_results_dir())
    ap.add_argument("--no-save", action="store_true", help="print metrics only")
    ap.add_argument("--fail-under", nargs="*", default=[], metavar="RULE",
                    help="exit 1 if a metric misses its threshold, e.g. answer_accuracy>=0.8 false_refusal_rate<=0.1")
    ap.add_argument("--report-only", action="store_true", help="rebuild REPORT.md from saved runs and exit")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args()

    if a.report_only:
        runs = load_saved(a.out)
        if not runs:
            print(f"No saved runs in {a.out}")
            return 1
        (a.out / "REPORT.md").write_text(ev.render_report(runs))
        print(f"Wrote {a.out / 'REPORT.md'} ({len(runs)} runs)")
        return 0

    overrides = ev.env_for_preset(a.preset)
    for item in a.set:
        key, _, raw = item.partition("=")
        overrides[key.strip()] = _coerce(key.strip(), raw.strip())
    questions = ev.load_questions(a.questions)[: a.limit or None]
    label = a.label or a.preset

    def progress(i, n, row):
        if a.quiet:
            return
        mark = "refused" if row["refused"] else ("ok" if row["correct"] or row["category"] in ev.UNANSWERABLE else "MISS")
        if row["category"] in ev.UNANSWERABLE:
            mark = "ok (refused)" if row["refused"] else "MISS (answered)"
        print(f"[{i:3}/{n}] {row['id']} {row['category']:22} {mark:16} {row['latency_ms']:7.0f} ms  {row['question'][:60]}")

    print(f"Evaluating '{label}': {overrides}")
    result = ev.run(overrides, questions, k=a.k, label=label, progress=progress)
    result["order"] = ORDER.get(label, 70)            # column order in REPORT.md
    m = result["metrics"]
    print("\n" + "\n".join(f"  {title:34} {ev._fmt(m.get(key), key)}" for key, title, _ in ev.REPORT_METRICS))

    if not a.no_save:
        a.out.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in label)
        (a.out / f"{safe}.json").write_text(json.dumps(result, indent=1))
        runs = load_saved(a.out)
        (a.out / "REPORT.md").write_text(ev.render_report(runs))
        print(f"\nSaved {a.out / (safe + '.json')} and updated {a.out / 'REPORT.md'}")

    bad = ev.check_thresholds(m, a.fail_under)
    if bad:
        print("\nQUALITY GATE FAILED:\n  " + "\n  ".join(bad))
        return 1
    if a.fail_under:
        print("\nQuality gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
