"""Update readiness checks for one OTA package (pure function: no database, easy to test).

Each check returns {id, title, status, detail, relates_to}; status is pass | fail | warn | info | skip.
Verdict: fail if any check fails, else warn if any warns, else pass. A failed package can never be
released (enforced in the service). `relates_to` names the requirement area the check supports;
it is orientation for reviewers, not a compliance claim."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.modules.ota.package import (RESERVED, PackageError, parse_time, read_package, validate_manifest,
                                     verify_signature)
from app.modules.vuln.versions import compare

CHECKS = [
    ("P01", "Package structure", "Safe archive with manifest and signature"),
    ("P02", "Manifest schema", "Complete, well-formed update description"),
    ("P03", "Signature and trusted key", "UN R156 / AIS-190: authenticity and integrity of updates"),
    ("P04", "Payload integrity", "Every file pinned by SHA-256; nothing unlisted"),
    ("P05", "Target ECU and hardware", "Update matches the vehicle configuration"),
    ("P06", "Installed version compatible", "UN R156: compatibility with the vehicle configuration"),
    ("P07", "Anti-rollback", "No downgrade to older, possibly vulnerable software"),
    ("P08", "Freshness and replay", "Expiry and one-time release of a package id"),
    ("P09", "RXSWIN", "UN R156: software identification of type-approved systems"),
    ("P10", "Safe-update preconditions", "UN R156: the update can be performed safely"),
    ("P11", "SBOM delta", "Vulnerabilities fixed / introduced by the update (ISO/SAE 21434)"),
    ("P12", "Safety and security impact", "Change impact on safety goals, requirements, tests, threats"),
]
TITLES = {cid: (t, rel) for cid, t, rel in CHECKS}


@dataclass
class Context:
    keys: dict[str, dict]                                  # key_id -> {public_key_pem, status, valid_from, valid_until, revoked_reason}
    baseline: dict[str, dict]                              # ecu -> {version, hardware_id, rxswin}
    released_ids: set[str] = field(default_factory=set)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sbom_preview: Callable[[bytes], dict] | None = None   # sbom bytes -> {"ecu","version","findings":[...]} or raises
    current_findings: Callable[[str], list[dict]] | None = None   # ecu -> current findings
    impact: Callable[[list[str], str], dict] | None = None        # (affects, ecu) -> impact summary


def _r(cid: str, status: str, detail: str) -> dict:
    title, rel = TITLES[cid]
    return {"id": cid, "title": title, "status": status, "detail": detail, "relates_to": rel}


def verify(content: bytes, ctx: Context) -> dict:
    results: list[dict] = []
    sha = hashlib.sha256(content or b"").hexdigest()
    try:
        files = read_package(content)
        results.append(_r("P01", "pass", f"{len(files)} files, {len(content)} bytes"))
    except PackageError as e:
        results.append(_r("P01", "fail", str(e)))
        return _finish(results, None, sha, skip_from=1)
    try:
        manifest = json.loads(files["manifest.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        results.append(_r("P02", "fail", f"manifest.json is not valid JSON: {e}"))
        return _finish(results, None, sha, skip_from=2)
    errs = validate_manifest(manifest)
    if errs:
        results.append(_r("P02", "fail", "; ".join(errs)))
        return _finish(results, manifest if isinstance(manifest, dict) else None, sha, skip_from=2)
    results.append(_r("P02", "pass", f"{manifest['package_id']}: {manifest['target']['ecu']} "
                                     f"{', '.join(manifest['from_versions'])} -> {manifest['to_version']}"))
    m, ecu = manifest, manifest["target"]["ecu"]

    # P03 signature
    try:
        sig = json.loads(files["manifest.sig"])
        key_id = str(sig.get("key_id", ""))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        sig, key_id = {}, ""
    key = ctx.keys.get(key_id)
    if not key_id or not sig:
        results.append(_r("P03", "fail", "manifest.sig is missing or unreadable"))
    elif sig.get("alg") != "ecdsa-p256-sha256":
        results.append(_r("P03", "fail", f"Unsupported signature algorithm {sig.get('alg')!r}"))
    elif key is None:
        results.append(_r("P03", "fail", f"Signed with key '{key_id}', which is not in the trusted key registry"))
    elif key.get("status") == "revoked":
        results.append(_r("P03", "fail", f"Key '{key_id}' was revoked: {key.get('revoked_reason') or 'no reason given'}"))
    elif key.get("status") != "approved":
        results.append(_r("P03", "fail", f"Key '{key_id}' is not active yet (status {key.get('status')}; needs a second person's approval)"))
    elif key.get("valid_until") and ctx.now > parse_time(key["valid_until"]):
        results.append(_r("P03", "fail", f"Key '{key_id}' expired on {key['valid_until']}"))
    elif key.get("valid_from") and ctx.now < parse_time(key["valid_from"]):
        results.append(_r("P03", "fail", f"Key '{key_id}' is not valid before {key['valid_from']}"))
    elif not verify_signature(files["manifest.json"], sig, key["public_key_pem"]):
        results.append(_r("P03", "fail", f"Signature does not match the manifest (key '{key_id}'): the manifest was altered or signed with another key"))
    else:
        results.append(_r("P03", "pass", f"Valid ECDSA P-256 signature by trusted key '{key_id}'"))

    # P04 payload
    listed = {p["path"]: p for p in m["payload"]}
    allowed = set(listed) | RESERVED | ({m["sbom"]["path"]} if m.get("sbom") else set())
    problems = []
    for path, p in listed.items():
        data = files.get(path)
        if data is None:
            problems.append(f"{path} missing")
        elif hashlib.sha256(data).hexdigest() != p["sha256"]:
            problems.append(f"{path} SHA-256 mismatch (file was modified)")
        elif len(data) != p["size"]:
            problems.append(f"{path} size {len(data)} != {p['size']}")
    extra = sorted(set(files) - allowed)
    if extra:
        problems.append(f"files not listed in the manifest: {', '.join(extra)}")
    results.append(_r("P04", "fail", "; ".join(problems)) if problems else
                   _r("P04", "pass", f"{len(listed)} payload file(s) match their SHA-256 and size"))

    # P05 target
    base = ctx.baseline.get(ecu)
    if base is None:
        results.append(_r("P05", "fail", f"ECU '{ecu}' has no registered software baseline (upload its SBOM first)"))
    elif base.get("hardware_id") and base["hardware_id"] != m["target"]["hardware_id"]:
        results.append(_r("P05", "fail", f"Hardware {m['target']['hardware_id']} does not match the vehicle configuration ({base['hardware_id']})"))
    else:
        results.append(_r("P05", "pass", f"{ecu} on hardware {m['target']['hardware_id']}"))

    # P06 / P07 versions
    if base is None:
        results.append(_r("P06", "skip", "No baseline"))
        results.append(_r("P07", "skip", "No baseline"))
    else:
        installed = base["version"]
        if any(compare(installed, v) == 0 for v in m["from_versions"]):
            results.append(_r("P06", "pass", f"Installed {installed} is an allowed starting version"))
        else:
            results.append(_r("P06", "fail", f"Installed {installed} is not in from_versions {m['from_versions']}"))
        c = compare(m["to_version"], installed)
        if c > 0:
            results.append(_r("P07", "pass", f"{installed} -> {m['to_version']} is an upgrade"))
        else:
            results.append(_r("P07", "fail", f"to_version {m['to_version']} is {'the same as' if c == 0 else 'older than'} "
                                             f"the installed {installed} (downgrade / rollback)"))

    # P08 freshness / replay
    issues = []
    if ctx.now > parse_time(m["expires"]):
        issues.append(f"package expired on {m['expires']}")
    if parse_time(m["created"]) > ctx.now + timedelta(minutes=5):
        issues.append(f"created timestamp {m['created']} is in the future")
    if m["package_id"] in ctx.released_ids:
        issues.append(f"package id {m['package_id']} was already released (replay)")
    results.append(_r("P08", "fail", "; ".join(issues)) if issues else
                   _r("P08", "pass", f"Valid until {m['expires']}; package id not used before"))

    # P09 RXSWIN
    if not m["type_approval_relevant"]:
        results.append(_r("P09", "info", "Declared not relevant for type approval; RXSWIN unchanged by design"))
    elif base is not None and base.get("rxswin") and base["rxswin"] == m["rxswin"]:
        results.append(_r("P09", "fail", f"Type-approval-relevant update keeps RXSWIN {m['rxswin']}: it must be updated"))
    else:
        results.append(_r("P09", "pass", f"RXSWIN {(base or {}).get('rxswin') or '?'} -> {m['rxswin']}"))

    # P10 preconditions
    pre = m.get("preconditions") or {}
    missing = [k for k in ("vehicle_state", "min_battery_soc_pct") if k not in pre]
    if missing:
        results.append(_r("P10", "warn", f"Not declared: {', '.join(missing)} (how is a safe installation ensured?)"))
    else:
        rb = (m.get("rollback") or {}).get("strategy")
        results.append(_r("P10", "pass", f"Vehicle {pre['vehicle_state']}, battery >= {pre['min_battery_soc_pct']}%"
                                         + (f", rollback: {rb}" if rb else "")))

    # P11 SBOM delta
    results.append(_sbom_delta(m, files, ecu, ctx))

    # P12 impact
    affects = list(dict.fromkeys(m.get("affects") or [ecu]))
    if ctx.impact is None:
        results.append(_r("P12", "skip", "No traceability context"))
    else:
        imp = ctx.impact(affects, ecu)
        n = sum(len(v) for k, v in imp.items() if isinstance(v, list))
        asil = imp.get("max_asil") or "none"
        if asil in ("B", "C", "D"):
            results.append(_r("P12", "warn", f"Touches ASIL {asil} elements: safety re-validation required before release "
                                             f"({n} linked items, see impact)"))
        elif n:
            results.append(_r("P12", "info", f"{n} linked items to re-verify (see impact)"))
        else:
            results.append(_r("P12", "info", f"No traced requirements, tests or threats for {', '.join(affects)}"))
        return _finish(results, m, sha, impact=imp)
    return _finish(results, m, sha)


def _sbom_delta(m: dict, files: dict, ecu: str, ctx: Context) -> dict:
    if not m.get("sbom"):
        return _r("P11", "warn", "No SBOM in the package: the vulnerability impact of the update cannot be assessed")
    data = files.get(m["sbom"]["path"])
    if data is None or hashlib.sha256(data).hexdigest() != m["sbom"]["sha256"]:
        return _r("P11", "fail", "SBOM missing or its SHA-256 does not match the manifest")
    if ctx.sbom_preview is None:
        return _r("P11", "info", "SBOM present (no advisory context to compare)")
    try:
        after = ctx.sbom_preview(data)
    except Exception as e:                                    # noqa: BLE001 - report, do not crash the verification
        return _r("P11", "fail", f"SBOM unreadable: {e}")
    if after["ecu"] != ecu or compare(after["version"], m["to_version"]) != 0:
        return _r("P11", "fail", f"SBOM describes {after['ecu']} {after['version']}, manifest says {ecu} {m['to_version']}")
    before = {f["advisory"]: f for f in (ctx.current_findings(ecu) if ctx.current_findings else [])}
    now_ = {f["advisory"]: f for f in after["findings"]}
    fixed, introduced = sorted(set(before) - set(now_)), sorted(set(now_) - set(before))
    serious = [a for a in introduced if now_[a]["severity"] in ("critical", "high")]
    detail = (f"Fixes {len(fixed)}: {', '.join(fixed) or '-'}; introduces {len(introduced)}: {', '.join(introduced) or '-'}; "
              f"still open: {len(set(now_) & set(before))}")
    out = _r("P11", "warn" if serious else "pass", detail + (f" (new high/critical: {', '.join(serious)})" if serious else ""))
    out["fixed"], out["introduced"] = fixed, introduced
    return out


def _finish(results: list[dict], manifest: dict | None, sha: str, skip_from: int | None = None, impact: dict | None = None) -> dict:
    if skip_from is not None:
        done = {r["id"] for r in results}
        results += [_r(cid, "skip", "Not evaluated: earlier check failed") for cid, _, _ in CHECKS if cid not in done]
    statuses = {r["status"] for r in results}
    verdict = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
    return {"verdict": verdict, "checks": results, "manifest": manifest, "sha256": sha, "impact": impact or {},
            "failed": [r["id"] for r in results if r["status"] == "fail"]}
