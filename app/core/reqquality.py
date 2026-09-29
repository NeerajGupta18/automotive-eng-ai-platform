"""Deterministic requirement quality checks (shared by safety and cybersecurity requirements).
Errors block approval; warnings are shown to the reviewer."""
from __future__ import annotations

import re

VAGUE = ["appropriate", "adequate", "sufficient", "quickly", "as soon as possible", "asap", "etc", "and/or",
         "user-friendly", "robust", "minimal", "maximum possible", "if possible", "where possible",
         "reasonable", "normal", "approximately", "as needed", "state of the art", "fast", "slow"]
WEAK_MODALS = re.compile(r"\b(should|may|might|can|could|would)\b", re.I)
PLACEHOLDER = re.compile(r"\[[A-Za-z0-9_ ]{2,40}\]|\bTBD\b|\bTBC\b")
QUANTITY = re.compile(r"\b\d+(\.\d+)?\s?(ms|s|sec|seconds|%|v|a|hz|khz|km/h|bits?|bytes?|kb|mb|attempts?|times|°c)\b", re.I)


def check_requirement(text: str, *, require_quantity: bool = False) -> list[dict]:
    f: list[dict] = []

    def add(rule, sev, msg):
        f.append({"rule": rule, "severity": sev, "message": msg})

    t = (text or "").strip()
    words = t.split()
    if not t:
        return [{"rule": "Q000", "severity": "error", "message": "Requirement text is empty"}]
    shalls = len(re.findall(r"\bshall\b", t, re.I))
    if shalls == 0:
        add("Q001", "error", "Missing normative keyword 'shall'")
    elif shalls > 1:
        add("Q002", "warning", f"Contains {shalls} 'shall' statements; split into atomic requirements")
    for m in PLACEHOLDER.findall(t):
        add("Q004", "error", f"Unresolved placeholder: {m if isinstance(m, str) else 'placeholder'}")
        break
    hits = [v for v in VAGUE if re.search(rf"(?<![\w]){re.escape(v)}(?![\w])", t, re.I)]
    if hits:
        add("Q003", "warning", "Vague or unverifiable wording: " + ", ".join(sorted(hits)))
    if WEAK_MODALS.search(t):
        add("Q009", "warning", "Weak modal verb (should/may/can); use 'shall' for binding statements")
    if len(words) < 8:
        add("Q005", "warning", "Very short requirement; check that it is complete")
    if len(words) > 60:
        add("Q006", "warning", "Very long requirement; consider splitting")
    if require_quantity and not QUANTITY.search(t):
        add("Q008", "warning", "No measurable quantity with a unit (time, threshold, length ...)")
    return f


def has_errors(findings: list[dict]) -> bool:
    return any(x["severity"] == "error" for x in findings)
