"""RAG evaluation harness: measures retrieval, answers and refusals on a labelled question set.

Each run builds an isolated platform instance in a temporary directory, ingests the evaluation
corpus as approved documents, asks every question through the same code path the API uses
(HLDService.ask) and scores the results:

  retrieval   hit@1 / hit@3 / hit@5 and MRR@5 (is a chunk from an expected document + section
              ranked in the top k?)
  answers     answer_accuracy: answered AND every expected key fact appears in the answer
  refusals    false_refusal_rate on answerable questions; refusal rate on off-topic and on
              in-domain-but-unanswerable questions
  grounding   citation_valid (cited labels exist) and support_rate (cited sentences are backed by
              the chunk they cite)
  latency     p50 / p95 per question

The harness only uses the public Container API, so it also runs against older versions of the
code (that is how the Stage 5 baseline in eval/results was measured)."""
from __future__ import annotations

import dataclasses
import json
import os
import platform
import shutil
import statistics
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"
EVAL_DIR = ROOT / "eval"

# corpus: (file, version). The two BCM HLD versions are deliberately both present: the same
# section in v1.0 and v1.1 is the classic duplicate-evidence problem.
CORPUS = [
    (SAMPLES / "hld_bcm_v1.pdf", "1.0"),
    (SAMPLES / "hld_bcm_v2.pdf", "1.1"),
    (SAMPLES / "uds_bcm_spec.md", "1.0"),
    (EVAL_DIR / "corpus" / "bcm_srs.md", "1.0"),
    (EVAL_DIR / "corpus" / "pgw_hld.md", "2.0"),
]

UNANSWERABLE = ("offtopic", "indomain_unanswerable")

PRESETS = {
    # offline: runs anywhere, no downloads (what CI and the cloud demo use)
    "offline-vector": {"retrieval_mode": "vector", "embed_backend": "hash", "llm_backend": "mock"},
    "offline-hybrid": {"retrieval_mode": "hybrid", "embed_backend": "hash", "llm_backend": "mock"},
    # local AI on a laptop: real embeddings (+ ChromaDB) and a local LLM through Ollama
    "local-embeddings": {"retrieval_mode": "hybrid", "embed_backend": "sentence-transformers", "llm_backend": "mock"},
    "local-ai": {"retrieval_mode": "hybrid", "embed_backend": "sentence-transformers", "llm_backend": "ollama"},
    "local-ai-rerank": {"retrieval_mode": "hybrid", "embed_backend": "sentence-transformers", "llm_backend": "ollama",
                        "rerank_backend": "cross-encoder"},
}


def load_questions(path: Path | None = None) -> list[dict]:
    path = path or EVAL_DIR / "questions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------- scoring helpers
def _norm(s: str) -> str:
    return " ".join(s.lower().replace("_", " ").split())


def is_relevant(meta: dict, expected: dict) -> bool:
    if meta.get("doc_name") not in expected["docs"]:
        return False
    sec = _norm(meta.get("section", ""))
    return any(_norm(x) in sec for x in expected["sections"])


def first_relevant_rank(metas: list[dict], expected: dict) -> int | None:
    for i, m in enumerate(metas, start=1):
        if is_relevant(m, expected):
            return i
    return None


def keywords_found(answer: str, keywords: list[str]) -> bool:
    a = _norm(answer)
    return all(_norm(k) in a for k in keywords)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = min(len(vs) - 1, max(0, round(p / 100 * (len(vs) - 1))))
    return round(vs[idx], 1)


def summarize(rows: list[dict]) -> dict:
    ans = [r for r in rows if r["category"] not in UNANSWERABLE]
    off = [r for r in rows if r["category"] == "offtopic"]
    ind = [r for r in rows if r["category"] == "indomain_unanswerable"]
    answered = [r for r in ans if not r["refused"]]

    def rate(xs, pred):
        return round(sum(1 for x in xs if pred(x)) / len(xs), 3) if xs else None

    m = {
        "questions": len(rows), "answerable": len(ans), "unanswerable": len(off) + len(ind),
        "hit@1": rate(ans, lambda r: r["rank"] is not None and r["rank"] <= 1),
        "hit@3": rate(ans, lambda r: r["rank"] is not None and r["rank"] <= 3),
        "hit@5": rate(ans, lambda r: r["rank"] is not None and r["rank"] <= 5),
        "mrr@5": round(sum(1 / r["rank"] for r in ans if r["rank"]) / len(ans), 3) if ans else None,
        "answer_accuracy": rate(ans, lambda r: r["correct"]),
        "false_refusal_rate": rate(ans, lambda r: r["refused"]),
        "refusal_offtopic": rate(off, lambda r: r["refused"]),
        "refusal_indomain": rate(ind, lambda r: r["refused"]),
        "citation_valid": rate(answered, lambda r: r["grounded"]),
        "support_rate": (round(statistics.mean(sr), 3)
                         if (sr := [r["support_rate"] for r in answered if r["support_rate"] is not None]) else None),
        "latency_p50_ms": percentile([r["latency_ms"] for r in rows], 50),
        "latency_p95_ms": percentile([r["latency_ms"] for r in rows], 95),
        "degraded_answers": sum(1 for r in rows if r.get("degraded")),
    }
    correct_total = sum(1 for r in ans if r["correct"]) + sum(1 for r in off + ind if r["refused"])
    m["overall_accuracy"] = round(correct_total / len(rows), 3) if rows else None
    by_cat = {}
    for cat in sorted({r["category"] for r in rows}):
        rs = [r for r in rows if r["category"] == cat]
        if cat in UNANSWERABLE:
            by_cat[cat] = {"n": len(rs), "refused": rate(rs, lambda r: r["refused"])}
        else:
            by_cat[cat] = {"n": len(rs), "hit@5": rate(rs, lambda r: r["rank"] is not None and r["rank"] <= 5),
                           "correct": rate(rs, lambda r: r["correct"]), "refused": rate(rs, lambda r: r["refused"])}
    m["by_category"] = by_cat
    return m


def coverage_sweep(rows: list[dict], thresholds=None) -> list[dict]:
    """What the coverage gate alone would do at each threshold (uses the recorded coverage)."""
    thresholds = thresholds or [round(x * 0.1, 1) for x in range(0, 11)]
    if not any(r.get("coverage") is not None for r in rows):
        return []
    ans = [r for r in rows if r["category"] not in UNANSWERABLE]
    off = [r for r in rows if r["category"] == "offtopic"]
    ind = [r for r in rows if r["category"] == "indomain_unanswerable"]
    out = []
    for t in thresholds:
        gate = lambda r: (r.get("coverage") or 0.0) < t  # noqa: E731
        out.append({
            "min_coverage": t,
            "false_refusal_rate": round(sum(map(gate, ans)) / len(ans), 3) if ans else None,
            "refusal_offtopic": round(sum(map(gate, off)) / len(off), 3) if off else None,
            "refusal_indomain": round(sum(map(gate, ind)) / len(ind), 3) if ind else None,
        })
    return out


def rescue_sweep(rows: list[dict], min_coverage: float, rescue_cov: float) -> list[dict]:
    """With semantic embeddings: effect of RESCUE_MIN_SCORE on the coverage gate decision."""
    ans = [r for r in rows if r["category"] not in UNANSWERABLE]
    off = [r for r in rows if r["category"] == "offtopic"]
    ind = [r for r in rows if r["category"] == "indomain_unanswerable"]
    out = []
    for t in [round(0.30 + 0.05 * i, 2) for i in range(0, 13)]:
        def gate(r, t=t):
            cov, vec = r.get("coverage") or 0.0, r.get("vector_top") or 0.0
            return cov < min_coverage and not (vec >= t and cov >= rescue_cov)
        out.append({"rescue_min_score": t,
                    "false_refusal_rate": round(sum(map(gate, ans)) / len(ans), 3) if ans else None,
                    "refusal_offtopic": round(sum(map(gate, off)) / len(off), 3) if off else None,
                    "refusal_indomain": round(sum(map(gate, ind)) / len(ind), 3) if ind else None})
    return out


# ---------------------------------------------------------------------------- running
def _settings(overrides: dict, data_dir: Path):
    from app.config import Settings, get_settings
    base = get_settings()
    o = dict(overrides)
    o.setdefault("min_score", 0.30 if o.get("embed_backend") == "sentence-transformers" else 0.12)
    o.update({"data_dir": data_dir, "auth_mode": "off", "log_level": "ERROR"})
    known = {f.name for f in dataclasses.fields(Settings)}
    unknown = sorted(set(o) - known)
    if unknown:
        raise ValueError(f"This version of the code does not support: {', '.join(unknown)}")
    return dataclasses.replace(base, **o)


def run(overrides: dict, questions: list[dict], k: int = 5, label: str = "", progress=None) -> dict:
    from app.container import Container

    tmp = Path(tempfile.mkdtemp(prefix="aeai-eval-"))
    try:
        settings = _settings(overrides, tmp / "storage")
        c = Container(settings)
        c.db.create_project("eval")
        t_ingest = time.perf_counter()
        for path, version in CORPUS:
            c.documents.ingest("eval", path.name, path.read_bytes(), version, "approved", "eval")
        ingest_s = round(time.perf_counter() - t_ingest, 2)
        doc_ids = [d["id"] for d in c.db.documents("eval", "approved")]
        where = {"doc_id": doc_ids}
        rows = []
        for i, q in enumerate(questions, start=1):
            rag = c.rag("eval")
            if hasattr(rag, "retrieve"):
                hits, _ = rag.retrieve(q["question"], k, where)
            else:                                                   # Stage 1-5 code: vector search only
                hits = rag.store.query(rag.embedder.embed_query(q["question"]), k, where)
            t0 = time.perf_counter()
            a = c.hld.ask("eval", q["question"], k=k, actor="eval")
            latency = a.get("latency_ms") or round((time.perf_counter() - t0) * 1000, 1)
            exp = q.get("expected")
            metas = [h.meta for h in hits]
            row = {
                "id": q["id"], "category": q["category"], "question": q["question"],
                "refused": bool(a["refused"]), "refusal_reason": a.get("refusal_reason", ""),
                "answer": a["answer"],
                "rank": first_relevant_rank(metas, exp) if exp else None,
                "retrieved": [f"{m.get('doc_name')} | {m.get('section')}" for m in metas],
                "correct": bool(exp) and not a["refused"] and keywords_found(a["answer"], exp["keywords"]),
                "grounded": bool(a.get("grounded")),
                "support_rate": a.get("support_rate"),          # None: this code version does not measure it
                "coverage": (a.get("retrieval") or {}).get("coverage"),
                "vector_top": (a.get("retrieval") or {}).get("vector_top"),
                "latency_ms": latency, "degraded": bool(a.get("degraded")),
            }
            rows.append(row)
            if progress:
                progress(i, len(questions), row)
        llm_name = c.llm.name
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    s = settings
    return {
        "label": label or "run",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {"retrieval_mode": getattr(s, "retrieval_mode", "vector"), "embed_backend": s.embed_backend,
                   "embed_model": s.embed_model if s.embed_backend != "hash" else "",
                   "llm": llm_name, "llm_model": (s.ollama_model if llm_name == "ollama" else
                                                  s.openai_model if llm_name == "openai_compat" else ""),
                   "vector_backend": s.vector_backend, "rerank": getattr(s, "rerank_backend", "none"),
                   "min_coverage": getattr(s, "min_coverage", None), "min_score": s.min_score,
                   "rescue_min_score": getattr(s, "rescue_min_score", None), "k": k},
        "environment": {"python": platform.python_version(), "machine": platform.machine(),
                        "system": platform.system(), "ingest_seconds": ingest_s},
        "metrics": summarize(rows),
        "coverage_sweep": coverage_sweep(rows),
        "rescue_sweep": (rescue_sweep(rows, getattr(s, "min_coverage", 0.5), getattr(s, "rescue_min_coverage", 0.2))
                         if getattr(c.embedder, "semantic", False) else []),
        "rows": rows,
    }


# ---------------------------------------------------------------------------- reporting
REPORT_METRICS = [
    ("overall_accuracy", "Overall accuracy", True), ("answer_accuracy", "Answer accuracy (answerable)", True),
    ("hit@1", "Retrieval hit@1", True), ("hit@5", "Retrieval hit@5", True), ("mrr@5", "MRR@5", True),
    ("false_refusal_rate", "False refusals (answerable)", False),
    ("refusal_offtopic", "Refused off-topic", True), ("refusal_indomain", "Refused in-domain unanswerable", True),
    ("citation_valid", "Citations valid", True), ("support_rate", "Cited sentences supported", True),
    ("latency_p50_ms", "Latency p50 (ms)", False), ("latency_p95_ms", "Latency p95 (ms)", False),
]


def _fmt(v, key):
    if v is None:
        return "–"
    if key.startswith("latency"):
        return f"{v:.0f}"
    return f"{v * 100:.1f}%" if isinstance(v, float) and v <= 1 else str(v)


def render_report(results: list[dict]) -> str:
    lines = ["# RAG evaluation report", "",
             "Generated by `python scripts/run_eval.py`. Question set: `eval/questions.jsonl` "
             f"({results[0]['metrics']['questions']} questions: {results[0]['metrics']['answerable']} answerable, "
             f"{results[0]['metrics']['unanswerable']} that must be refused). Corpus: 5 synthetic documents "
             "(BCM HLD v1.0 and v1.1, UDS spec, BCM requirements spec, powertrain gateway HLD).", "",
             "## Comparison", "",
             "| Metric | " + " | ".join(r["label"] for r in results) + " |",
             "|---|" + "---|" * len(results)]
    for key, title, _ in REPORT_METRICS:
        lines.append(f"| {title} | " + " | ".join(_fmt(r["metrics"].get(key), key) for r in results) + " |")
    lines += ["", "## Configurations", "", "| Run | Retrieval | Embeddings | LLM | Rerank | Created |", "|---|---|---|---|---|---|"]
    for r in results:
        c = r["config"]
        emb = c["embed_backend"] + (f" ({c['embed_model']})" if c.get("embed_model") else "")
        llm = c["llm"] + (f" ({c['llm_model']})" if c.get("llm_model") else "")
        lines.append(f"| {r['label']} | {c['retrieval_mode']} | {emb} | {llm} | {c.get('rerank') or 'none'} | {r['created']} |")
    lines += ["", "## By question category", ""]
    cats = sorted({cat for r in results for cat in r["metrics"]["by_category"]})
    lines += ["| Category | " + " | ".join(r["label"] for r in results) + " |", "|---|" + "---|" * len(results)]
    for cat in cats:
        cells = []
        for r in results:
            b = r["metrics"]["by_category"].get(cat)
            if not b:
                cells.append("–")
            elif cat in UNANSWERABLE:
                cells.append(f"refused {_fmt(b['refused'], '')} (n={b['n']})")
            else:
                cells.append(f"correct {_fmt(b['correct'], '')}, hit@5 {_fmt(b['hit@5'], '')} (n={b['n']})")
        lines.append(f"| {cat} | " + " | ".join(cells) + " |")
    sweep_src = next((r for r in reversed(results) if r.get("coverage_sweep")), None)
    if sweep_src:
        lines += ["", f"## Coverage threshold sweep ({sweep_src['label']})", "",
                  "What the coverage gate alone would refuse at each `MIN_COVERAGE` (LLM-level refusals come on top).", "",
                  "| MIN_COVERAGE | False refusals | Off-topic refused | In-domain unanswerable refused |", "|---|---|---|---|"]
        for s in sweep_src["coverage_sweep"]:
            lines.append(f"| {s['min_coverage']:.1f} | {_fmt(s['false_refusal_rate'], '')} | "
                         f"{_fmt(s['refusal_offtopic'], '')} | {_fmt(s['refusal_indomain'], '')} |")
    rescue_src = next((r for r in reversed(results) if r.get("rescue_sweep")), None)
    if rescue_src:
        lines += ["", f"## Semantic rescue sweep ({rescue_src['label']})", "",
                  "Paraphrased questions share few words with the documents. With semantic embeddings they are let "
                  "through when the best chunk scores at least `RESCUE_MIN_SCORE` (and coverage >= `RESCUE_MIN_COVERAGE`). "
                  "Pick the lowest value that still refuses all off-topic questions.", "",
                  "| RESCUE_MIN_SCORE | False refusals | Off-topic refused | In-domain unanswerable refused |", "|---|---|---|---|"]
        for s in rescue_src["rescue_sweep"]:
            lines.append(f"| {s['rescue_min_score']:.2f} | {_fmt(s['false_refusal_rate'], '')} | "
                         f"{_fmt(s['refusal_offtopic'], '')} | {_fmt(s['refusal_indomain'], '')} |")
    last = results[-1]
    fails = [r for r in last["rows"] if (r["category"] in UNANSWERABLE and not r["refused"]) or
             (r["category"] not in UNANSWERABLE and not r["correct"])]
    lines += ["", f"## Failures in `{last['label']}` ({len(fails)})", "",
              "| ID | Category | Question | What happened |", "|---|---|---|---|"]
    for r in fails:
        if r["category"] in UNANSWERABLE:
            what = "answered instead of refusing"
        elif r["refused"]:
            what = f"refused ({r['refusal_reason']})"
        elif r["rank"] is None:
            what = "expected section not retrieved"
        else:
            what = f"key fact missing from answer (evidence at rank {r['rank']})"
        lines.append(f"| {r['id']} | {r['category']} | {r['question']} | {what} |")
    lines += ["", "Synthetic documents and questions; numbers describe this corpus only. "
              "Re-run with your own documents and questions before drawing conclusions about real HLDs.", ""]
    return "\n".join(lines)


def check_thresholds(metrics: dict, rules: list[str]) -> list[str]:
    """rules like 'answer_accuracy>=0.8' or 'false_refusal_rate<=0.1'. Returns violations."""
    bad = []
    for rule in rules:
        for op in (">=", "<="):
            if op in rule:
                key, val = rule.split(op)
                have = metrics.get(key.strip())
                want = float(val)
                if have is None or (op == ">=" and have < want) or (op == "<=" and have > want):
                    bad.append(f"{key.strip()} = {have} (required {op} {want})")
                break
        else:
            raise ValueError(f"Bad threshold rule '{rule}' (use metric>=value or metric<=value)")
    return bad


def env_for_preset(name: str) -> dict:
    if name not in PRESETS:
        raise ValueError(f"Unknown preset '{name}'. Options: {', '.join(PRESETS)}")
    return dict(PRESETS[name])


def default_results_dir() -> Path:
    return Path(os.getenv("EVAL_RESULTS_DIR", EVAL_DIR / "results"))
