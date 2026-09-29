"""Pluggable LLM layer. Swap the backend with LLM_BACKEND=... no code changes."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod

import requests

from app.config import Settings


class LLM(ABC):
    name = "base"

    @abstractmethod
    def generate(self, system: str, prompt: str, temperature: float = 0.1) -> str: ...


class MockLLM(LLM):
    """Deterministic extractive 'LLM' for tests, CI and offline demos.

    It reads the [S#] context blocks that the RAG layer puts in the prompt and
    returns the best-matching sentences with their citation labels. It exercises
    the whole pipeline (retrieval, citations, guardrails) without a model."""

    name = "mock"
    _block = re.compile(r"\[S(\d+)\][^\n]*\n(.*?)(?=\n\[S\d+\]|\n<<<END|\Z)", re.S)

    def generate(self, system: str, prompt: str, temperature: float = 0.1) -> str:
        q = re.search(r"Question:\s*(.+)", prompt)
        q_tokens = set(re.findall(r"[a-z0-9_]+", (q.group(1) if q else "").lower()))
        scored: list[tuple[float, str, str]] = []
        for m in self._block.finditer(prompt):
            label, body = m.group(1), m.group(2)
            for sent in re.split(r"(?<=[.!?])\s+|\n+", body):
                sent = sent.strip(" |-#*")
                if len(sent) < 15:
                    continue
                toks = set(re.findall(r"[a-z0-9_]+", sent.lower()))
                overlap = len(q_tokens & toks)
                if overlap:
                    scored.append((overlap / (len(toks) ** 0.5), sent, label))
        if not scored:
            return "The retrieved sections do not contain enough information to answer."
        scored.sort(reverse=True)
        return " ".join(f"{s} [S{l}]" for _, s, l in scored[:3])


class OllamaLLM(LLM):
    name = "ollama"

    def __init__(self, s: Settings):
        self.url, self.model = s.ollama_url.rstrip("/"), s.ollama_model

    def generate(self, system: str, prompt: str, temperature: float = 0.1) -> str:
        r = requests.post(
            f"{self.url}/api/chat",
            json={
                "model": self.model,
                "stream": False,
                "options": {"temperature": temperature},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=300,
        )
        r.raise_for_status()
        return r.json()["message"]["content"].strip()


class OpenAICompatLLM(LLM):
    """Any OpenAI-compatible server: vLLM, LM Studio, llama.cpp, TGI, or an
    enterprise gateway. Keeps the code inside the organisation boundary."""

    name = "openai_compat"

    def __init__(self, s: Settings):
        self.base, self.model, self.key = s.openai_base_url.rstrip("/"), s.openai_model, s.openai_api_key

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
            timeout=300,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


def build_llm(s: Settings) -> LLM:
    backends = {"mock": lambda: MockLLM(), "ollama": lambda: OllamaLLM(s), "openai_compat": lambda: OpenAICompatLLM(s)}
    if s.llm_backend not in backends:
        raise ValueError(f"Unknown LLM_BACKEND '{s.llm_backend}'. Options: {list(backends)}")
    return backends[s.llm_backend]()
