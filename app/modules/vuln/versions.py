"""Version comparison and OSV-style affected-range evaluation for embedded software components.

Component versions in ECU software are not always SemVer: OpenSSL uses letter suffixes (1.0.1f),
some vendors use four numbers (4.4.2.1) or 'v' prefixes. The key below orders all of these sensibly:
  * numeric runs compare as numbers, letter runs as text   (1.0.1f < 1.0.1g < 1.0.2)
  * a shorter version is lower than one that extends it      (1.0.1 < 1.0.1a, 2.4 < 2.4.1)
  * a pre-release after '-' is lower than the release        (3.0.0-alpha1 < 3.0.0)
"""
from __future__ import annotations

import re

_TOKEN = re.compile(r"\d+|[a-z]+")


def _parts(s: str) -> tuple:
    parts = [(0, int(t)) if t.isdigit() else (1, t) for t in _TOKEN.findall(s)]
    while len(parts) > 1 and parts[-1] == (0, 0):          # 2.4 == 2.4.0
        parts.pop()
    return tuple(parts)


def version_key(v: str) -> tuple:
    v = (v or "").strip().lower()
    if v.startswith("v") and v[1:2].isdigit():
        v = v[1:]
    main, sep, pre = v.partition("-")
    # (release parts, 1 = final release / 0 = pre-release, pre-release parts)
    return (_parts(main), 0 if sep and pre else 1, _parts(pre))


def compare(a: str, b: str) -> int:
    ka, kb = version_key(a), version_key(b)
    return (ka > kb) - (ka < kb)


def is_valid(v: str) -> bool:
    return bool(v) and bool(_TOKEN.findall(v.strip().lower())) and len(v) <= 64


def in_ranges(version: str, ranges: list[dict], versions: list[str] | None = None) -> bool:
    """OSV semantics: a version is affected if it is listed explicitly, or if for some range it is
    >= an 'introduced' event and < the next 'fixed' (or <= the next 'last_affected')."""
    if not is_valid(version):
        return False
    if versions and any(compare(version, x) == 0 for x in versions):
        return True
    for r in ranges or []:
        if r.get("type", "ECOSYSTEM").upper() == "GIT":           # commit ranges cannot be evaluated on a version string
            continue
        events = r.get("events") or []
        introduced = None
        for ev in events:
            if "introduced" in ev:
                introduced = ev["introduced"]
            elif introduced is not None and ("fixed" in ev or "last_affected" in ev or "limit" in ev):
                lo_ok = introduced in ("0", "") or compare(version, introduced) >= 0
                if "fixed" in ev:
                    hi_ok = compare(version, ev["fixed"]) < 0
                elif "last_affected" in ev:
                    hi_ok = compare(version, ev["last_affected"]) <= 0
                else:
                    hi_ok = compare(version, ev["limit"]) < 0
                if lo_ok and hi_ok:
                    return True
                introduced = None
        if introduced is not None and (introduced in ("0", "") or compare(version, introduced) >= 0):
            return True                                           # open-ended: introduced, never fixed
    return False


def fixed_versions(ranges: list[dict]) -> list[str]:
    out = []
    for r in ranges or []:
        for ev in r.get("events") or []:
            if "fixed" in ev:
                out.append(ev["fixed"])
    return sorted(set(out), key=version_key)
