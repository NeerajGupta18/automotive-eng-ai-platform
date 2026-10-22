# Phase 1: Measured AI quality

Phase 1 turns the document Q&A from "it seems to work" into "here are the numbers". It adds
hybrid retrieval, a refusal gate for off-topic questions, duplicate-version handling, a check that
cited sentences are backed by their sources, a safe fallback when the LLM is down, and an
evaluation harness that scores all of it on a labelled question set, locally and in CI.

## What changed

| Area | Before (Stage 5) | After (Phase 1) | Where |
|---|---|---|---|
| Retrieval | vector similarity only | **hybrid**: BM25 keyword search + vector search, fused with Reciprocal Rank Fusion; optional cross-encoder rerank | `app/core/lexical.py`, `app/core/rag.py`, `app/core/rerank.py` |
| Off-topic questions | answered with unrelated sentences | **refused before the LLM is called** when the question's key terms are not in the documents, with the missing terms shown | `BM25Index.coverage`, `RAGPipeline._refusal` |
| Paraphrased questions | – | with semantic embeddings, let through when the best chunk is very similar ("semantic rescue") | `RESCUE_MIN_SCORE`, `RESCUE_MIN_COVERAGE` |
| Two versions of a document | the same section cited twice (v1.0 and v1.1) | identical chunks collapsed; the count is shown | `lexical.dedupe` |
| Citations | labels checked to exist | plus **support rate**: each cited sentence must share most of its key terms with the chunk it cites | `lexical.citation_support` |
| LLM outage | request fails | extractive fallback, answer flagged `degraded` with the error | `RAGPipeline.ask`, `LLM_FALLBACK` |
| Hosted LLM without a key | request fails at query time | offline answerer at start-up, with a warning | `build_llm`, `validate` |
| `.env` | documented but never read | loaded at start-up (real environment variables win; tests ignore it) | `config.load_dotenv` |
| Quality | not measured | **79-question evaluation**, report, UI page and CI gate | `app/evaluation.py`, `scripts/run_eval.py`, `ui/pages/8_AI_Quality.py`, `.github/workflows/ci.yml` |

## The evaluation

- **Corpus** (5 synthetic documents): BCM HLD v1.0 and v1.1, the UDS diagnostic specification, a BCM
  software requirements specification and a powertrain-gateway HLD. The last two exist to make
  retrieval harder: they also talk about vehicle speed, DTCs, security access and OTA.
- **Questions** (`eval/questions.jsonl`, 79): 41 factual, 12 exact-identifier, 8 paraphrased, 3
  version-specific, 7 off-topic and 8 in-domain-but-unanswerable ("What is the CPU clock frequency
  of the BCM microcontroller?"). Every answerable question lists the documents and sections that
  contain the answer and the key facts the answer must contain.
- **Metrics**: retrieval hit@1/hit@5/MRR@5, answer accuracy (answered and contains every key fact),
  false refusals, refusal of off-topic and of in-domain unanswerable questions, citation validity,
  support rate, latency.

### Results on this machine (offline, no model downloads)

| Metric | Stage 5 baseline | Phase 1 vector-only | **Phase 1 hybrid** |
|---|---|---|---|
| Overall accuracy | 46.8% | 77.2% | **79.7%** |
| Answer accuracy (answerable) | 56.2% | 76.6% | **81.2%** |
| Right section ranked first (hit@1) | 59.4% | 59.4% | **73.4%** |
| Right section in top 5 (hit@5) | 84.4% | 87.5% | **95.3%** |
| Off-topic questions refused | 0.0% | 100.0% | **100.0%** |
| In-domain unanswerable refused | 12.5% | 62.5% | 50.0% |
| False refusals (answerable) | 3.1% | 17.2% | 12.5% |

The baseline was measured by running the same harness and questions against the Stage 5 code.
Full report with per-category results, the threshold sweep and every failure:
`eval/results/REPORT.md` (also on the **AI Quality** page of the UI).

How to read it:
- **Hybrid retrieval** is what moved hit@1 from 59% to 73%: exact identifiers such as
  `0xF190`, `SWC_DoorLockCtrl` or `If_CrashState` are found by keyword search.
- **Off-topic refusal** comes from two layers: the coverage gate (refuses before the LLM is called)
  and the stricter answerer. With a real LLM only the gate is under our control, which is why it exists.
- **False refusals are mostly paraphrases** (7 of 8). Hash embeddings only see shared words, so
  "Which software part works out how fast the car is going?" cannot match "provides the vehicle
  speed". Real embeddings are the fix; that is the next measurement (below).
- **In-domain unanswerable questions** are the hard case: they share words with the documents, so
  the gate lets them through and the answerer has to decline. The offline answerer quotes
  loosely related sentences for half of them. This is the main thing a real LLM should improve.

## Run it with real AI on your laptop

On a MacBook (Apple Silicon) with 8 GB RAM use `llama3.2:3b`; with 16 GB or more, `llama3.1:8b`.

```bash
# 1. Ollama (local LLM runtime): download the app from https://ollama.com, open it once, then
ollama pull llama3.2:3b

# 2. Real embeddings, ChromaDB and the optional reranker (downloads models on first use)
pip install -r requirements-ml.txt

# 3. Evaluate: each run is saved and added to the report
python scripts/run_eval.py --preset local-embeddings     # BGE embeddings, offline answerer
python scripts/run_eval.py --preset local-ai             # BGE embeddings + Ollama
python scripts/run_eval.py --preset local-ai-rerank      # + cross-encoder reranking
python scripts/run_eval.py --preset local-ai --set ollama_model=llama3.1:8b --label local-ai-llama3.1-8b
```

Then open `eval/results/REPORT.md` or the **AI Quality** page. The **semantic rescue sweep** in
the report shows which `RESCUE_MIN_SCORE` lets paraphrases through while still refusing every
off-topic question. BGE similarities for unrelated text are often 0.3-0.5, which is why the
default is 0.6. Set the value you choose in `.env`.

To use the same setup in the app itself, edit `.env` (see the LOCAL AI PROFILE block in
`.env.example`), then rebuild the vectors once and restart:

```bash
python scripts/manage.py reindex
```

## Quality gate in CI

`.github/workflows/ci.yml` runs the offline evaluation on every push and fails the build when
answer accuracy < 75%, hit@5 < 90%, off-topic refusal < 100%, false refusals > 15% or valid
citations < 95%. The report is uploaded as a build artifact. Run the same gate locally:

```bash
python scripts/run_eval.py --fail-under "answer_accuracy>=0.75" "hit@5>=0.90" "refusal_offtopic>=1.0"
```

## Limits of this evaluation

- 79 questions over 5 synthetic documents: enough to compare configurations and catch regressions,
  not to make claims about real HLDs. Add your own documents to `eval/corpus/` and questions to
  `eval/questions.jsonl`.
- "Answer accuracy" checks that the expected key facts appear in the answer. It does not judge
  fluency or completeness; with a real LLM, read the failures table as well.
- The support rate is lexical: a cited sentence counts as supported when most of its key terms occur
  in the cited chunk. A paraphrasing LLM can score lower while being correct.
- The real-embedding and Ollama runs were not executed in the development sandbox (no model
  downloads there). They run on your machine with the commands above.
