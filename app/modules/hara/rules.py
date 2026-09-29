"""Deterministic HARA decision rules. The LLM never decides an ASIL.

The S/E/C -> ASIL mapping below is expressed as the well-known additive property of the
ISO 26262-3 determination table (S+E+C = 10 -> D, 9 -> C, 8 -> B, 7 -> A, else QM; any zero -> QM).
It is a configurable *rule set*: verify it against the licensed standard copy used by your organisation
and replace `asil()` / RULESET_VERSION if your approved rules differ."""
from __future__ import annotations

from app.core.errors import Invalid

RULESET_VERSION = "example-additive-v1"
ASIL_RANK = {"QM": 0, "A": 1, "B": 2, "C": 3, "D": 4}

SEVERITY = {0: "S0 no injuries", 1: "S1 light/moderate injuries", 2: "S2 severe/life-threatening (survival probable)",
            3: "S3 life-threatening (survival uncertain)/fatal"}
EXPOSURE = {0: "E0 incredible", 1: "E1 very low probability", 2: "E2 low probability", 3: "E3 medium probability",
            4: "E4 high probability"}
CONTROLLABILITY = {0: "C0 controllable in general", 1: "C1 simply controllable", 2: "C2 normally controllable",
                   3: "C3 difficult to control or uncontrollable"}

GUIDEWORDS = {
    "loss": {"label": "Loss of function", "phrase": "loss of {f}"},
    "unintended": {"label": "Unintended activation", "phrase": "unintended activation of {f}"},
    "too_much": {"label": "Excessive output", "phrase": "excessive output of {f}"},
    "too_little": {"label": "Insufficient output", "phrase": "insufficient output of {f}"},
    "late": {"label": "Delayed activation", "phrase": "delayed activation of {f}"},
    "stuck": {"label": "Stuck active / no deactivation", "phrase": "{f} remaining active after a deactivation request"},
    "reversed": {"label": "Reversed behaviour", "phrase": "reversed behaviour of {f}"},
    "erratic": {"label": "Erratic / intermittent behaviour", "phrase": "erratic behaviour of {f}"},
}


def validate_sec(s: int, e: int, c: int) -> None:
    if s not in SEVERITY or e not in EXPOSURE or c not in CONTROLLABILITY:
        raise Invalid("S must be 0-3, E 0-4 and C 0-3")


def asil(s: int, e: int, c: int) -> str:
    validate_sec(s, e, c)
    if 0 in (s, e, c):
        return "QM"
    return {10: "D", 9: "C", 8: "B", 7: "A"}.get(s + e + c, "QM")


def asil_table() -> list[dict]:
    return [{"S": s, "E": e, "C": c, "asil": asil(s, e, c)}
            for s in range(1, 4) for e in range(1, 5) for c in range(1, 4)]


def max_asil(values: list[str]) -> str:
    return max(values, key=lambda a: ASIL_RANK[a]) if values else "QM"
