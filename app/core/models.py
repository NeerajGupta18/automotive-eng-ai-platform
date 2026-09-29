from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Chunk:
    id: str
    text: str
    meta: dict[str, Any]


@dataclass
class Hit:
    id: str
    text: str
    meta: dict[str, Any]
    score: float


@dataclass
class Citation:
    label: str          # "S1"
    doc_id: str
    doc_name: str
    version: str
    page: int
    section: str
    score: float
    snippet: str


@dataclass
class Answer:
    question: str
    answer: str
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.0
    confidence_label: str = "none"
    grounded: bool = False          # every cited label exists in retrieved evidence
    status: str = "needs_review"    # human review is always required
    refused: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
