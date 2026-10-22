"""CVSS v3.0 / v3.1 base score from a vector string (FIRST specification formulas), so the severity
of an advisory is computed from its vector instead of being copied as an opaque number."""
from __future__ import annotations

import math

AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
AC = {"L": 0.77, "H": 0.44}
PR_U = {"N": 0.85, "L": 0.62, "H": 0.27}
PR_C = {"N": 0.85, "L": 0.68, "H": 0.5}
UI = {"N": 0.85, "R": 0.62}
CIA = {"H": 0.56, "L": 0.22, "N": 0.0}
REQUIRED = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")


class CVSSError(ValueError):
    pass


def _roundup(x: float) -> float:
    """CVSS v3.1 Roundup: smallest value with one decimal that is >= x (robust to float noise)."""
    i = round(x * 100000)
    return i / 100000.0 if i % 10000 == 0 else (math.floor(i / 10000) + 1) / 10.0


def parse(vector: str) -> dict:
    v = (vector or "").strip()
    if not v.startswith(("CVSS:3.1/", "CVSS:3.0/")):
        raise CVSSError("Only CVSS v3.0/v3.1 vectors are supported")
    metrics = {}
    for part in v.split("/")[1:]:
        k, _, val = part.partition(":")
        metrics[k] = val
    missing = [k for k in REQUIRED if k not in metrics]
    if missing:
        raise CVSSError(f"Vector is missing base metrics: {', '.join(missing)}")
    tables = {"AV": AV, "AC": AC, "UI": UI, "C": CIA, "I": CIA, "A": CIA, "PR": PR_U, "S": {"U": 0, "C": 0}}
    for k in REQUIRED:
        if metrics[k] not in tables[k]:
            raise CVSSError(f"Invalid value {k}:{metrics[k]}")
    return metrics


def base_score(vector: str) -> float:
    m = parse(vector)
    changed = m["S"] == "C"
    iss = 1 - (1 - CIA[m["C"]]) * (1 - CIA[m["I"]]) * (1 - CIA[m["A"]])
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
    pr = (PR_C if changed else PR_U)[m["PR"]]
    exploitability = 8.22 * AV[m["AV"]] * AC[m["AC"]] * pr * UI[m["UI"]]
    if impact <= 0:
        return 0.0
    total = 1.08 * (impact + exploitability) if changed else impact + exploitability
    return _roundup(min(total, 10.0))


def band(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score == 0:
        return "none"
    if score < 4.0:
        return "low"
    if score < 7.0:
        return "medium"
    if score < 9.0:
        return "high"
    return "critical"


SEVERITY_RANK = {"unknown": 0, "none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
