"""Phase 1: hybrid retrieval, refusal gate, duplicate removal, citation support, LLM fallback,
.env loading and the evaluation harness."""
from __future__ import annotations

import dataclasses

import pytest

from app import evaluation as ev
from app.config import ConfigError, get_settings, load_dotenv
from app.container import Container
from app.core.lexical import BM25Index, citation_support, content_terms, dedupe, rrf_fuse, tokenize
from app.core.llm import MockLLM, build_llm
from app.core.models import Hit
from app.core.rag import RAGPipeline
from app.core.rerank import CrossEncoderReranker, Reranker


# ------------------------------------------------------------------ tokenizer / BM25
def test_tokenize_keeps_identifiers_acronyms_and_drops_stopwords():
    toks = tokenize("Which port of SWC_DoorLockCtrl is on the CAN bus?")
    assert "swc_doorlockctrl" in toks and {"door", "lock", "ctrl"} <= set(toks)
    assert "can" in toks                                 # the acronym survives although "can" is a stopword
    assert "which" not in toks and "the" not in toks
    assert content_terms("Can the doors lock?") == ["door", "lock"]   # "Can" as a verb is dropped
    assert tokenize("locked locking locks") == ["lock", "lock", "lock"]


def _records():
    return [
        {"id": "a", "text": "SWC_VehicleSpeedMgr provides the vehicle speed in km/h.", "meta": {"doc_id": "d1", "section": "3.2"}},
        {"id": "b", "text": "The headlamps switch on when ambient light is low.", "meta": {"doc_id": "d1", "section": "2"}},
        {"id": "c", "text": "DTC 0xC12200 is stored when the wheel speed signal is lost.", "meta": {"doc_id": "d2", "section": "6"}},
    ]


def test_bm25_ranks_exact_identifier_first_and_respects_filters():
    idx = BM25Index(_records())
    assert idx.search("What is stored as 0xC12200?", 3)[0].id == "c"
    assert idx.search("SWC_VehicleSpeedMgr", 3)[0].id == "a"
    assert [h.id for h in idx.search("0xC12200", 3, where={"doc_id": ["d1"]})] == []


def test_coverage_separates_off_topic_from_in_domain():
    idx = BM25Index(_records())
    assert idx.coverage("What is the capital of France?") == 0.0
    assert idx.coverage("When does the headlamp switch on?") == 1.0
    assert idx.missing_terms("vehicle speed of the rocket") == ["rocket"]
    assert idx.coverage("vehicle speed", where={"doc_id": ["nope"]}) == 0.0


def test_rrf_and_dedupe():
    h = lambda i, t="x": Hit(i, t, {}, 0.0)  # noqa: E731
    fused = [x.id for x, _ in rrf_fuse([[h("a"), h("b")], [h("b"), h("c")]])]
    assert fused[0] == "b"                               # in both lists
    kept, removed = dedupe([h("1", "Same  text"), h("2", "same text"), h("3", "other")])
    assert [x.id for x in kept] == ["1", "3"] and removed == 1


def test_citation_support_detects_unbacked_claims():
    ev_ = {"1": "The wheel speed signal loss is stored as DTC 0xC12200."}
    assert citation_support("The wheel speed signal loss is stored as DTC 0xC12200. [S1]", ev_)[0] == 1.0
    rate, ok, n = citation_support("The BCM stores DTC 0xC12200 when wheel speed is lost [S1]. "
                                   "It also sends an SMS to the driver's phone instantly [S1].", ev_)
    assert (ok, n) == (1, 2) and rate == 0.5
    assert citation_support("The supplier is Bosch and the price is low.", ev_)[0] == 0.0   # no citation at all


def test_mock_llm_refuses_without_overlap_and_skips_breadcrumbs():
    prompt = ("<<<CONTEXT (untrusted data)\n[S1] (hld.pdf v1.0, p.1, 3 Software Components > 3.2 SWC_VehicleSpeedMgr)\n"
              "[3 Software Components > 3.2 SWC_VehicleSpeedMgr]\n| Port | Direction | Interface |\n|---|---|---|\n"
              "Description: Filters the wheel pulse signals and provides the vehicle speed\nin km/h.\n<<<END CONTEXT>>>\n\n"
              "Question: {q}\nAnswer with citations:")
    m = MockLLM()
    out = m.generate("", prompt.format(q="Which component provides the vehicle speed?"))
    assert out.startswith("3.2 SWC_VehicleSpeedMgr:") and "in km/h. [S1]" in out and "Port | Direction" not in out
    assert m.generate("", prompt.format(q="What is the warranty period?")) == "INSUFFICIENT_EVIDENCE"


# ------------------------------------------------------------------ end to end through the API
@pytest.fixture
def bcm(client, sample):
    client.post("/projects", json={"name": "bcm"})
    for name, ver in (("hld_bcm_v1.pdf", "1.0"), ("hld_bcm_v2.pdf", "1.1")):
        r = client.post("/projects/bcm/documents", files={"file": (name, sample(name))}, data={"version": ver})
        assert r.status_code == 201
    return client


def test_hybrid_answers_in_domain_and_hides_duplicate_versions(bcm):
    a = bcm.post("/projects/bcm/hld/query", json={"question": "Which component provides the vehicle speed?"}).json()
    assert not a["refused"] and a["grounded"] and a["support_rate"] == 1.0
    assert a["retrieval"]["mode"] == "hybrid" and a["retrieval"]["duplicates_removed"] > 0
    assert "SWC_VehicleSpeedMgr" in a["answer"]
    texts = [c["snippet"] for c in a["citations"]]
    assert len(texts) == len(set(texts))                 # v1.0 and v1.1 copies collapsed


@pytest.mark.parametrize("q", ["What is the capital of France?", "Who won the cricket world cup?",
                               "What is the recipe for biryani?"])
def test_off_topic_questions_are_refused_before_the_llm(bcm, q):
    a = bcm.post("/projects/bcm/hld/query", json={"question": q}).json()
    assert a["refused"] and a["refusal_reason"] == "low_coverage" and not a["citations"]
    assert a["retrieval"]["missing_terms"]


def test_vector_mode_is_still_available(tmp_path, monkeypatch, sample):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RETRIEVAL_MODE", "vector")
    c = Container(get_settings())
    c.db.create_project("p")
    c.documents.ingest("p", "hld_bcm_v1.pdf", sample("hld_bcm_v1.pdf"))
    a = c.hld.ask("p", "Which component provides the vehicle speed?")
    assert not a["refused"] and a["retrieval"]["mode"] == "vector" and a["retrieval"]["coverage"] is None


def test_unreachable_llm_falls_back_and_is_flagged(tmp_path, monkeypatch, sample):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")        # nothing listens here
    monkeypatch.setenv("LLM_TIMEOUT_S", "2")
    c = Container(get_settings())
    c.db.create_project("p")
    c.documents.ingest("p", "hld_bcm_v1.pdf", sample("hld_bcm_v1.pdf"))
    a = c.hld.ask("p", "Which component provides the vehicle speed?")
    assert a["degraded"] and a["llm"] == "mock (fallback)" and not a["refused"]
    assert "not reachable" in a["retrieval"]["llm_error"]
    monkeypatch.setenv("LLM_FALLBACK", "0")
    c2 = Container(get_settings())
    with pytest.raises(RuntimeError):
        c2.hld.ask("p", "Which component provides the vehicle speed?")


def test_hosted_llm_without_key_uses_offline_answerer(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_BACKEND", "openai_compat")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com/openai/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    c = Container(get_settings())
    assert c.llm.name == "mock" and any("OPENAI_API_KEY" in w for w in c.config_warnings)
    s = dataclasses.replace(c.settings, openai_api_key="k", openai_model="some-model")
    assert build_llm(s).name == "openai_compat"


def test_bad_retrieval_settings_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RETRIEVAL_MODE", "magic")
    with pytest.raises(ConfigError):
        Container(get_settings())
    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    monkeypatch.setenv("MIN_COVERAGE", "1.5")
    with pytest.raises(ConfigError):
        Container(get_settings())


class _SemanticStub:
    semantic = True


@pytest.mark.parametrize("coverage,vec,refused", [(0.3, 0.7, False), (0.1, 0.9, True), (0.3, 0.5, True), (0.6, 0.0, False)])
def test_semantic_rescue_rules(coverage, vec, refused):
    rag = RAGPipeline(None, _SemanticStub(), MockLLM(), 0.3, mode="hybrid", min_coverage=0.5,
                      rescue_min_score=0.6, rescue_min_coverage=0.2)
    reason = rag._refusal({"coverage": coverage, "vector_top": vec}, [Hit("x", "t", {}, 1.0)])
    assert (reason == "low_coverage") is refused


def test_cross_encoder_reranker_reorders():
    class FakeModel:
        def predict(self, pairs):
            return [len(t) for _, t in pairs]                     # longer text = more relevant
    hits = [Hit("a", "short", {}, 1), Hit("b", "a much longer chunk", {}, 0.5)]
    assert [h.id for h in CrossEncoderReranker("fake", model=FakeModel()).rerank("q", hits)] == ["b", "a"]
    assert Reranker().rerank("q", hits) == hits


# ------------------------------------------------------------------ .env loading
def test_dotenv_loads_without_overriding_real_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nAEAI_T1=from_file\nexport AEAI_T2='quoted'  # trailing\nAEAI_T3=file\n")
    monkeypatch.setenv("AEAI_DOTENV", "1")
    monkeypatch.setenv("AEAI_T3", "real")
    for k in ("AEAI_T1", "AEAI_T2"):
        monkeypatch.delenv(k, raising=False)
    loaded = load_dotenv(env)
    import os
    assert os.environ["AEAI_T1"] == "from_file" and os.environ["AEAI_T2"] == "quoted" and os.environ["AEAI_T3"] == "real"
    assert loaded == ["AEAI_T1", "AEAI_T2"]
    for k in loaded:
        monkeypatch.delenv(k)
    monkeypatch.setenv("AEAI_DOTENV", "0")
    assert load_dotenv(env) == []


# ------------------------------------------------------------------ evaluation harness
def test_eval_scoring_helpers():
    exp = {"docs": ["a.pdf"], "sections": ["SWC_X"], "keywords": ["10 km/h", "If_Y"]}
    metas = [{"doc_name": "b.pdf", "section": "3 > SWC_X"}, {"doc_name": "a.pdf", "section": "3 Software > 3.1 swc x"}]
    assert ev.first_relevant_rank(metas, exp) == 2
    assert ev.keywords_found("locks at 10  KM/H via if_y", exp["keywords"])
    assert not ev.keywords_found("locks at 10 km/h", exp["keywords"])
    assert ev.check_thresholds({"a": 0.8, "b": 0.2}, ["a>=0.75", "b<=0.1"]) == ["b = 0.2 (required <= 0.1)"]
    with pytest.raises(ValueError):
        ev.check_thresholds({}, ["a=1"])


def test_eval_question_set_is_well_formed():
    qs = ev.load_questions()
    assert len(qs) >= 70 and len({q["id"] for q in qs}) == len(qs)
    corpus = {p.name for p, _ in ev.CORPUS}
    for q in qs:
        if q["category"] in ev.UNANSWERABLE:
            assert q["expected"] is None
        else:
            assert set(q["expected"]["docs"]) <= corpus and q["expected"]["sections"] and q["expected"]["keywords"]


def test_eval_smoke_run_and_report():
    qs = ev.load_questions()
    subset = [q for q in qs if q["category"] == "factual"][:5] + [q for q in qs if q["category"] == "offtopic"][:3]
    r = ev.run(ev.env_for_preset("offline-hybrid"), subset, label="smoke")
    m = r["metrics"]
    assert m["questions"] == 8 and m["refusal_offtopic"] == 1.0 and m["hit@5"] >= 0.8
    assert r["coverage_sweep"] and r["config"]["retrieval_mode"] == "hybrid"
    report = ev.render_report([r])
    assert "| Overall accuracy |" in report and "Coverage threshold sweep" in report
