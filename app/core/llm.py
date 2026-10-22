"""Pluggable LLM layer. Swap the backend with LLM_BACKEND=... no code changes."""
from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod

import requests

from app.config import Settings, _is_local
from app.core.lexical import content_terms


class LLM(ABC):
    name = "base"

    @abstractmethod
    def generate(self, system: str, prompt: str, temperature: float = 0.1) -> str: ...


class MockLLM(LLM):
    """Deterministic extractive answerer for tests, CI, offline demos and as the fallback when a
    real model is unreachable.

    It reads the [S#] context blocks that the RAG layer puts in the prompt and returns the
    sentences that share enough informative terms with the question, each prefixed with the
    section it came from and followed by its citation label. If no sentence shares enough
    terms it answers INSUFFICIENT_EVIDENCE, like a real model is instructed to."""

    name = "mock"
    _block = re.compile(r"\[S(\d+)\]([^\n]*)\n(.*?)(?=\n\[S\d+\]|\n<<<END|\Z)", re.S)

    @staticmethod
    def _sentences(body: str) -> list[str]:
        """Re-join lines wrapped by PDF extraction, keep table rows whole, drop the breadcrumb line."""
        units, para = [], []
        lines = [ln.strip() for ln in body.splitlines()]
        for i, s in enumerate(lines):
            if s.startswith("[") and s.endswith("]"):          # "[3 Software Components > 3.2 X]"
                continue
            if not s or s.startswith(("|", "#")):
                if para:
                    units.append(" ".join(para))
                    para = []
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                is_header = nxt.startswith("|") and set(nxt) <= set("|-: ")
                if s.startswith("|") and not set(s) <= set("|-: ") and not is_header:
                    units.append(s)
                continue
            # PDF extraction wraps long sentences: continue the paragraph only after a long,
            # unterminated line. Short "Key: value" lines stay separate statements.
            if para and (len(para[-1]) < 60 or para[-1].endswith((".", "!", "?", ":"))):
                units.append(" ".join(para))
                para = []
            para.append(s)
        if para:
            units.append(" ".join(para))
        out = []
        for u in units:
            out += [x for x in re.split(r"(?<=[.!?])\s+", u) if x]
        return out

    @staticmethod
    def _required(n_terms: int) -> int:
        if n_terms <= 1:
            return n_terms
        if n_terms <= 4:
            return 2
        return math.ceil(0.4 * n_terms)

    def generate(self, system: str, prompt: str, temperature: float = 0.1) -> str:
        q = re.search(r"Question:\s*(.+)", prompt)
        q_terms = set(content_terms(q.group(1) if q else ""))
        if not q_terms:
            return "INSUFFICIENT_EVIDENCE"
        need = self._required(len(q_terms))
        scored: list[tuple[float, int, str, str]] = []
        for order, m in enumerate(self._block.finditer(prompt)):
            label, header, body = m.group(1), m.group(2), m.group(3)
            sec = header.rsplit(",", 1)[-1].strip(" )") if "," in header else ""
            sec = sec.split(">")[-1].strip()
            for sent in self._sentences(body):
                sent = sent.strip(" |-#*")
                if len(sent) < 15:
                    continue
                own = set(content_terms(sent))
                toks = own | set(content_terms(sec))
                overlap = len(q_terms & toks)
                if overlap >= need and q_terms & own:          # the sentence itself must address the question
                    scored.append((overlap / (len(toks) ** 0.5), -order, sent, f"{label}|{sec}"))
        if not scored:
            return "INSUFFICIENT_EVIDENCE"
        scored.sort(reverse=True)
        out, seen = [], set()
        for _, _, sent, lab in scored:
            key = sent.lower()
            if key in seen:
                continue
            seen.add(key)
            label, _, sec = lab.partition("|")
            prefix = f"{sec}: " if sec and sec.lower() not in key else ""
            out.append(f"{prefix}{sent} [S{label}]")
            if len(out) == 3:
                break
        return " ".join(out)


class OllamaLLM(LLM):
    name = "ollama"

    def __init__(self, s: Settings):
        self.url, self.model, self.timeout = s.ollama_url.rstrip("/"), s.ollama_model, s.llm_timeout_s

    def generate(self, system: str, prompt: str, temperature: float = 0.1) -> str:
        try:
            r = requests.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "options": {"temperature": temperature, "num_ctx": 4096},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
        except requests.ConnectionError as e:
            raise RuntimeError(f"Ollama is not reachable at {self.url} (is the Ollama app running?)") from e
        if r.status_code == 404:
            raise RuntimeError(f"Ollama model '{self.model}' not found: run  ollama pull {self.model}")
        r.raise_for_status()
        return r.json()["message"]["content"].strip()


class OpenAICompatLLM(LLM):
    """Any OpenAI-compatible server: vLLM, LM Studio, llama.cpp, TGI, an enterprise gateway,
    or a hosted provider (e.g. Groq) for the cloud demo."""

    name = "openai_compat"

    def __init__(self, s: Settings):
        self.base, self.model, self.key = s.openai_base_url.rstrip("/"), s.openai_model, s.openai_api_key
        self.timeout = s.llm_timeout_s

    def generate(self, system: str, prompt: str, temperature: float = 0.1) -> str:
        r = requests.post(
            f"{self.base}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={
                "model": self.model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


def build_llm(s: Settings) -> LLM:
    if s.llm_backend == "openai_compat" and not _is_local(s.openai_base_url) and (
            s.openai_api_key in ("", "not-needed") or not s.openai_model or s.openai_model == "local-model"):
        return MockLLM()        # hosted provider not configured (validate() logs why)
    backends = {"mock": lambda: MockLLM(), "ollama": lambda: OllamaLLM(s), "openai_compat": lambda: OpenAICompatLLM(s)}
    if s.llm_backend not in backends:
        raise ValueError(f"Unknown LLM_BACKEND '{s.llm_backend}'. Options: {list(backends)}")
    return backends[s.llm_backend]()
