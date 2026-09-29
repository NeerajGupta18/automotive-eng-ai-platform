"""Deterministic TARA assessment rules (impact, attack potential -> feasibility -> risk value).

Values follow the attack-potential approach used in ISO/SAE 21434 Annex G/H style examples. They are a
configurable *rule set*: verify against the licensed standard / your organisation's approved TARA
method and adjust RULESET_VERSION plus the tables below. The LLM never assigns feasibility or risk."""
from __future__ import annotations

from app.core.errors import Invalid

RULESET_VERSION = "example-attack-potential-v1"

IMPACT_LEVELS = ["negligible", "moderate", "major", "severe"]
IMPACT_CATEGORIES = ["safety", "financial", "operational", "privacy"]

ATTACK_POTENTIAL = {
    "elapsed_time": {"<=1d": 0, "<=1w": 1, "<=1m": 4, "<=6m": 17, ">6m": 19},
    "expertise": {"layman": 0, "proficient": 3, "expert": 6, "multiple_experts": 8},
    "knowledge": {"public": 0, "restricted": 3, "confidential": 7, "strictly_confidential": 11},
    "window": {"unlimited": 0, "easy": 1, "moderate": 4, "difficult": 10},
    "equipment": {"standard": 0, "specialized": 4, "bespoke": 7, "multiple_bespoke": 9},
}

# example risk matrix (impact x feasibility -> risk value 1..5)
RISK_MATRIX = {
    "severe": {"very_low": 2, "low": 3, "medium": 4, "high": 5},
    "major": {"very_low": 1, "low": 2, "medium": 3, "high": 4},
    "moderate": {"very_low": 1, "low": 2, "medium": 2, "high": 3},
    "negligible": {"very_low": 1, "low": 1, "medium": 1, "high": 1},
}
MITIGATION_THRESHOLD = 3          # risk value at/above which treatment 'reduce/avoid/share' is expected
TREATMENTS = ("reduce", "avoid", "share", "retain")
STRIDE = ("spoofing", "tampering", "repudiation", "information_disclosure", "denial_of_service", "elevation_of_privilege")

# HARA severity -> minimum sensible safety impact rating (cross-module consistency)
SAFETY_IMPACT_FROM_S = {0: "negligible", 1: "moderate", 2: "major", 3: "severe"}


def impact_rating(impact: dict) -> str:
    for c in IMPACT_CATEGORIES:
        if impact.get(c) not in IMPACT_LEVELS:
            raise Invalid(f"impact.{c} must be one of {IMPACT_LEVELS}")
    return max(impact.values(), key=IMPACT_LEVELS.index)


def attack_potential(ap: dict) -> tuple[int, str]:
    total = 0
    for k, table in ATTACK_POTENTIAL.items():
        if ap.get(k) not in table:
            raise Invalid(f"ap.{k} must be one of {list(table)}")
        total += table[ap[k]]
    return total, feasibility(total)


def feasibility(total: int) -> str:
    return "high" if total <= 13 else "medium" if total <= 19 else "low" if total <= 24 else "very_low"


def risk_value(impact_level: str, feas: str) -> int:
    return RISK_MATRIX[impact_level][feas]
