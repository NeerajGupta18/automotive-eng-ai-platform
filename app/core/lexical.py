"""Lexical retrieval (BM25) and query-coverage scoring.

Why this exists next to the vector store:
  * Engineering text is full of exact identifiers (SWC_DoorLockCtrl, 0xF190, If_VehicleSpeed).
    Keyword search finds them reliably; embeddings often blur them.
  * Query coverage ("how much of what was asked appears anywhere in the corpus") is a cheap,
    explainable signal for refusing off-topic questions before the LLM is called.

Dependency-free on purpose: it runs on a laptop, in CI and on a 512 MB cloud instance."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from app.core.models import Hit

# English function words + question words. They carry no evidence about the corpus.
STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing done down during each either else ever every few for
from further get gets got had has have having he her here hers him his how however i if in into is it
its itself just least less let like made make many may me might more most much must my neither no nor
not now of off often on once only or other our ours out over own per please rather really same say
says she should since so some such tell than that the their them then there these they this those
through thus to too under until up upon us use used uses using very via was we were what whatever when
where whether which while who whom whose why will with within without would yet you your yours
explain describe list give show name named define defined according document documents mention
mentioned specify specified stated value values
happen happens happened carry carries carried contain contains contained mean means occur occurs
purpose exactly actually currently
""".split())

_IDENT = re.compile(r"[A-Za-z0-9_\-]+")
_PARTS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _stem(t: str) -> str:
    """Tiny, predictable suffix stripping (plural / -ing / -ed). Identifiers and hex codes are kept."""
    if len(t) <= 3 or not t.isalpha():
        return t
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("sses"):
        return t[:-2]
    if t.endswith("s") and not t.endswith(("ss", "us", "is")):
        t = t[:-1]
    if t.endswith("ing") and len(t) > 5:
        return t[:-3]
    if t.endswith("ed") and len(t) > 4:
        return t[:-2]
    return t


def tokenize(text: str, keep_stopwords: bool = False) -> list[str]:
    """Lower-cased terms. Identifiers are kept whole AND split (SWC_DoorLockCtrl -> swc_doorlockctrl,
    swc, door, lock, ctrl) so both exact and partial matches work."""
    out: list[str] = []
    for raw in _IDENT.findall(text):
        whole = raw.lower().strip("-_")
        if not whole:
            continue
        cands = [whole]
        parts = _PARTS.findall(raw)
        if len(parts) > 1 or "_" in raw or "-" in raw:
            cands += [p.lower() for p in parts]
        acronym = raw.isupper() and len(raw) >= 2       # CAN, LIN, OTA, ECU: meaningful even if "can" is a stopword
        for t in cands:
            if not keep_stopwords and len(t) < 2:
                continue
            if not keep_stopwords and t in STOPWORDS and not (acronym and t == whole):
                continue
            out.append(_stem(t))
    return out


def content_terms(text: str) -> list[str]:
    """Distinct informative terms of a question, in order."""
    seen, out = set(), []
    for t in tokenize(text):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


@dataclass
class _Doc:
    id: str
    text: str
    meta: dict
    tf: Counter
    length: int


class BM25Index:
    """Okapi BM25 over the chunks of one project (k1=1.5, b=0.75)."""

    def __init__(self, records: list[dict], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs: list[_Doc] = []
        for r in records:
            toks = tokenize(r["text"])
            self.docs.append(_Doc(r["id"], r["text"], r["meta"], Counter(toks), len(toks)))
        self.n = len(self.docs)
        self.avgdl = (sum(d.length for d in self.docs) / self.n) if self.n else 0.0
        self.df: Counter = Counter()
        for d in self.docs:
            self.df.update(d.tf.keys())

    def idf(self, term: str) -> float:
        n_t = self.df.get(term, 0)
        return math.log(1 + (self.n - n_t + 0.5) / (n_t + 0.5))

    def _allowed(self, where: dict | None) -> list[_Doc]:
        if not where:
            return self.docs
        out = []
        for d in self.docs:
            ok = True
            for key, want in where.items():
                have = d.meta.get(key)
                if isinstance(want, (list, tuple, set)):
                    ok = have in want
                else:
                    ok = have == want
                if not ok:
                    break
            if ok:
                out.append(d)
        return out

    def search(self, query: str, k: int, where: dict | None = None) -> list[Hit]:
        terms = content_terms(query)
        if not terms or not self.n:
            return []
        scored = []
        for d in self._allowed(where):
            s = 0.0
            for t in terms:
                f = d.tf.get(t, 0)
                if f:
                    denom = f + self.k1 * (1 - self.b + self.b * d.length / (self.avgdl or 1))
                    s += self.idf(t) * f * (self.k1 + 1) / denom
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        return [Hit(d.id, d.text, d.meta, round(s, 4)) for s, d in scored[:k]]

    def coverage(self, query: str, where: dict | None = None) -> float:
        """Share of the question's informative terms that occur anywhere in the searchable chunks.
        'What is the capital of France?' scores 0 on an HLD; 'Which interface carries the crash
        state?' scores 1. Deliberately simple so a refusal can be explained term by term."""
        terms = content_terms(query)
        if not terms:
            return 0.0
        vocab = self._vocab(where)
        if not vocab:
            return 0.0
        return round(sum(t in vocab for t in terms) / len(terms), 4)

    def missing_terms(self, query: str, where: dict | None = None) -> list[str]:
        vocab = self._vocab(where)
        return [t for t in content_terms(query) if t not in vocab]

    def _vocab(self, where: dict | None) -> set[str]:
        vocab: set[str] = set()
        for d in self._allowed(where):
            vocab.update(d.tf.keys())
        return vocab


def rrf_fuse(rankings: list[list[Hit]], k: int = 60) -> list[tuple[Hit, float]]:
    """Reciprocal Rank Fusion: robust to the very different score scales of BM25 and cosine."""
    scores: dict[str, float] = {}
    first: dict[str, Hit] = {}
    for ranking in rankings:
        for rank, h in enumerate(ranking, start=1):
            scores[h.id] = scores.get(h.id, 0.0) + 1.0 / (k + rank)
            first.setdefault(h.id, h)
    return sorted(((first[i], s) for i, s in scores.items()), key=lambda x: -x[1])


_WS = re.compile(r"\s+")


def dedupe(hits: list[Hit]) -> tuple[list[Hit], int]:
    """Drop chunks whose text is identical to a better-ranked one (e.g. the same section in
    v1.0 and v1.1 of a document). Returns (kept, removed_count)."""
    seen, kept = set(), []
    for h in hits:
        key = _WS.sub(" ", h.text).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(h)
    return kept, len(hits) - len(kept)


_LABEL = re.compile(r"\[S(\d+)\]")
_SENT = re.compile(r"(?<=[.!?])\s+|(?<=\])\s+(?=[A-Z0-9])|\n+")
_TRAILING_LABELS = re.compile(r"([.!?])\s*((?:\[S\d+\]\s*)+)")


def citation_support(answer: str, evidence: dict[str, str], min_overlap: float = 0.5) -> tuple[float, int, int]:
    """Check that each cited sentence is backed by the text it cites.

    A sentence is supported when at least `min_overlap` of its informative terms occur in one of
    the chunks it cites. Substantive sentences with no citation count as unsupported.
    Returns (support_rate, supported, checked)."""
    supported = checked = 0
    # "...0xC12200. [S1] Next claim" -> "...0xC12200 [S1]. Next claim": a label belongs to the text before it
    answer = _TRAILING_LABELS.sub(lambda m: " " + m.group(2).strip() + m.group(1) + " ", answer)
    for sent in _SENT.split(answer):
        labels = _LABEL.findall(sent)
        terms = set(content_terms(_LABEL.sub(" ", sent)))
        if len(terms) < 3:                       # fragments, "Yes.", headings
            continue
        checked += 1
        best = 0.0
        for lab in labels:
            ev = evidence.get(lab)
            if ev is None:
                continue
            ev_terms = set(tokenize(ev))
            best = max(best, len(terms & ev_terms) / len(terms))
        if labels and best >= min_overlap:
            supported += 1
    return (round(supported / checked, 3) if checked else 1.0), supported, checked
