"""Requirement templates. Numbers and safe states are deliberately left as placeholders until the
engineer supplies them; the quality gate blocks approval while placeholders remain."""
from __future__ import annotations

from app.modules.hara.rules import GUIDEWORDS


def fill_goal_text(function: str, guideword: str) -> str:
    return f"Avoid {GUIDEWORDS[guideword]['label'].lower()} of '{function}'"


def draft_fsr(item: str, function: str, guideword: str, safe_state: str, ftti_ms: int | None) -> tuple[str, str]:
    phrase = GUIDEWORDS[guideword]["phrase"].format(f=f"'{function}'")
    safe = f"'{safe_state}'" if safe_state else "[SAFE_STATE]"
    ftti = str(ftti_ms) if ftti_ms else "[FTTI_ms]"
    return (f"The {item} shall detect {phrase} and transition to the safe state {safe} within {ftti} ms.",
            "template:fsr-detect-and-safe-state")


def draft_tsr(function: str, guideword: str, safe_state: str, ftti_ms: int | None) -> tuple[str, str]:
    phrase = GUIDEWORDS[guideword]["phrase"].format(f=f"'{function}'")
    safe = f"'{safe_state}'" if safe_state else "[SAFE_STATE]"
    return (f"The software component allocated to '{function}' shall command the safe state {safe} within "
            f"[DETECTION_TIME_ms] ms after monitoring of [MONITORED_SIGNAL] detects {phrase}.",
            "template:tsr-monitor-and-command")
