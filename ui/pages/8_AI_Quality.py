"""AI quality: results of the labelled RAG evaluation (scripts/run_eval.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import common

RESULTS = Path(__file__).resolve().parents[2] / "eval" / "results"
UNANSWERABLE = ("offtopic", "indomain_unanswerable")
METRICS = [
    ("overall_accuracy", "Overall accuracy"), ("answer_accuracy", "Answer accuracy"),
    ("hit@1", "Retrieval hit@1"), ("hit@5", "Retrieval hit@5"), ("mrr@5", "MRR@5"),
    ("false_refusal_rate", "False refusals"), ("refusal_offtopic", "Off-topic refused"),
    ("refusal_indomain", "In-domain unanswerable refused"), ("citation_valid", "Citations valid"),
    ("support_rate", "Cited sentences supported"),
]

common.setup("AI Quality")
common.sidebar("Module: **AI Quality**. Measured answer quality of the document Q&A on a labelled question set.")
st.title("AI quality: measured, not assumed")
st.markdown(
    "Every change to retrieval or to the model is scored on **79 labelled questions** over 5 synthetic documents: "
    "64 with a known answer and source section, and 15 that **must be refused** (7 off-topic, 8 that sound "
    "relevant but are not answered anywhere). Generated with `python scripts/run_eval.py`.")

runs = []
for p in sorted(RESULTS.glob("*.json")):
    try:
        runs.append(json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError):
        continue
runs.sort(key=lambda r: (r.get("order", 50), r.get("created", "")))
if not runs:
    st.info("No evaluation results yet. Run `python scripts/run_eval.py` and reload this page.")
    st.stop()

labels = [r["label"] for r in runs]
chosen = st.multiselect("Runs to compare", labels, default=labels)
sel = [r for r in runs if r["label"] in chosen]
if not sel:
    st.stop()
current = sel[-1]
base = sel[0] if len(sel) > 1 else None


def pct(v):
    return None if v is None else round(v * 100, 1)


st.subheader(f"Latest selected run: {current['label']}")
cfg = current["config"]
st.caption(f"Retrieval **{cfg['retrieval_mode']}** · embeddings **{cfg['embed_backend']}** "
           f"{cfg.get('embed_model') or ''} · LLM **{cfg['llm']}** {cfg.get('llm_model') or ''} · rerank **{cfg.get('rerank') or 'none'}** "
           f"· measured {current['created']}")
m, b = current["metrics"], (base["metrics"] if base else {})
cols = st.columns(4)
for col, (key, title) in zip(cols, [("overall_accuracy", "Overall accuracy"), ("answer_accuracy", "Answer accuracy"),
                                    ("refusal_offtopic", "Off-topic refused"), ("hit@5", "Right section in top 5")]):
    v = pct(m.get(key))
    d = None if not base or b.get(key) is None or v is None else f"{v - pct(b[key]):+.1f} pts vs {base['label']}"
    col.metric(title, "–" if v is None else f"{v:.1f}%", d)

st.subheader("Comparison")
table = pd.DataFrame({r["label"]: [pct(r["metrics"].get(k)) for k, _ in METRICS] for r in sel},
                     index=[t for _, t in METRICS])
st.dataframe(table.style.format(lambda v: "–" if pd.isna(v) else f"{v:.1f}%"), use_container_width=True)
st.caption("False refusals: lower is better. Everything else: higher is better.")
chart = table.loc[["Overall accuracy", "Answer accuracy", "Off-topic refused", "In-domain unanswerable refused"]].T
st.bar_chart(chart, stack=False)

st.subheader("By question category")
cats = sorted({c for r in sel for c in r["metrics"]["by_category"]})
rows = []
for cat in cats:
    row = {"category": cat}
    for r in sel:
        bc = r["metrics"]["by_category"].get(cat)
        if bc:
            key = "refused" if cat in UNANSWERABLE else "correct"
            row[r["label"]] = f"{bc[key] * 100:.0f}% {'refused' if key == 'refused' else 'correct'} (n={bc['n']})"
    rows.append(row)
st.dataframe(pd.DataFrame(rows).set_index("category"), use_container_width=True)
st.caption("Paraphrased questions share few words with the documents, so keyword-style (hash) embeddings cannot "
           "find them. That is the gap real embeddings close: run the `local-ai` preset on your machine.")

if current.get("coverage_sweep"):
    with st.expander("How the refusal threshold was chosen (coverage sweep)"):
        sw = pd.DataFrame(current["coverage_sweep"]).set_index("min_coverage") * 100
        sw.columns = ["False refusals %", "Off-topic refused %", "In-domain unanswerable refused %"]
        st.line_chart(sw)
        st.caption(f"Current MIN_COVERAGE = {cfg.get('min_coverage')}: the lowest value that refuses every off-topic "
                   "question while keeping false refusals low.")

st.subheader(f"Every question in {current['label']}")
only_fail = st.checkbox("Show failures only", True)
qrows = []
for r in current["rows"]:
    ok = r["refused"] if r["category"] in UNANSWERABLE else r["correct"]
    if only_fail and ok:
        continue
    qrows.append({"id": r["id"], "category": r["category"], "question": r["question"], "result": "✅" if ok else "❌",
                  "refused": r["refused"], "reason": r.get("refusal_reason", ""), "rank": r.get("rank"),
                  "answer": r["answer"][:300]})
st.dataframe(pd.DataFrame(qrows), use_container_width=True, hide_index=True)
st.info("Synthetic documents and questions: these numbers describe this test set only. "
        "Add your own documents and questions to eval/ before drawing conclusions about real HLDs.")
