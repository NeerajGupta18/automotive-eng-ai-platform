"""Attack-scenario self-test for the OTA verifier.

Builds one genuine, correctly signed package with a throw-away key, derives tampered variants that
model real attacks on software updates, and runs every variant through the same checks that guard
releases. The verifier is only trustworthy if the genuine package passes AND every attack is
caught by the check that is supposed to catch it."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.modules.ota import package as pkg
from app.modules.ota.checks import Context, verify
from app.modules.vuln.sbom import parse_cyclonedx

ECU, HW, FROM, TO, RX_OLD, RX_NEW = "Gateway", "PGW-HW-B", "2.3.0", "2.4.0", "RXSWIN-GW-0023", "RXSWIN-GW-0024"
KEY_ID = "selftest-oem-ota"


def _payload(n: int = 32768) -> bytes:
    out, block = bytearray(), b"aeai-selftest"
    while len(out) < n:
        block = hashlib.sha256(block).digest()
        out += block
    return bytes(out[:n])


def _sbom(version: str) -> bytes:
    return json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5",
                       "metadata": {"component": {"name": ECU, "version": version}},
                       "components": [{"name": "zlib", "version": "1.2.13", "purl": "pkg:generic/zlib@1.2.13"}]}).encode()


def _manifest(now: datetime, **over) -> dict:
    m = {"schema": pkg.SCHEMA, "package_id": "SELFTEST-PGW-2.4.0", "target": {"ecu": ECU, "hardware_id": HW},
         "from_versions": [FROM], "to_version": TO, "type_approval_relevant": True, "rxswin": RX_NEW,
         "created": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "expires": (now + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "preconditions": {"vehicle_state": "parked", "min_battery_soc_pct": 40},
         "rollback": {"strategy": "A/B", "confirm_timeout_s": 60}, "affects": [ECU], "changes": "self-test"}
    m.update(over)
    return m


def _rewrite(zip_bytes: bytes, **changes: bytes | None) -> bytes:
    files = pkg.read_package(zip_bytes)
    for k, v in changes.items():
        k = k.replace("__", "/")
        if v is None:
            files.pop(k, None)
        else:
            files[k] = v
    return pkg.zip_entries(files)


def run() -> dict:
    now = datetime.now(timezone.utc)
    oem, attacker = pkg.generate_key(), pkg.generate_key()
    fw = {"firmware/pgw_app.bin": _payload()}

    def build(key=oem, key_id=KEY_ID, sbom_version=TO, extra=None, **over):
        return pkg.build(_manifest(now, **over), fw, key, key_id, sbom=_sbom(sbom_version), extra=extra)

    def ctx(**key_over):
        key = {"public_key_pem": pkg.public_pem(oem), "status": "approved",
               "valid_from": (now - timedelta(days=30)).isoformat(), "valid_until": (now + timedelta(days=365)).isoformat()}
        key.update(key_over)
        return Context(keys={KEY_ID: key}, baseline={ECU: {"version": FROM, "hardware_id": HW, "rxswin": RX_OLD}},
                       released_ids={"PGW-2.3.0-REL"}, now=now,
                       sbom_preview=lambda b: (lambda s: {"ecu": s["ecu"], "version": s["ecu_version"], "findings": []})(parse_cyclonedx(b)),
                       current_findings=lambda e: [], impact=lambda affects, ecu: {"items": [], "max_asil": None})

    genuine = build()
    m_bytes = pkg.read_package(genuine)["manifest.json"]
    forged_manifest = json.dumps({**json.loads(m_bytes), "to_version": "9.9.9"}, indent=1, sort_keys=True).encode()
    tampered_fw = bytearray(fw["firmware/pgw_app.bin"]); tampered_fw[100] ^= 0xFF

    scenarios = [
        ("tampered_payload", "Attacker flips one byte of the firmware image", "P04",
         _rewrite(genuine, **{"firmware__pgw_app.bin": bytes(tampered_fw)}), ctx()),
        ("forged_manifest", "Manifest edited (to_version) after signing; old signature kept", "P03",
         _rewrite(genuine, **{"manifest.json": forged_manifest}), ctx()),
        ("untrusted_key", "Package signed with the attacker's own key", "P03", build(key=attacker, key_id="attacker-key"), ctx()),
        ("key_id_spoofing", "Attacker signs with his key but claims the OEM key id", "P03", build(key=attacker), ctx()),
        ("revoked_key", "Signed with a key that was revoked after a compromise", "P03", genuine,
         ctx(status="revoked", revoked_reason="HSM compromise (self-test)")),
        ("expired_key", "Signed with a key past its validity period", "P03", genuine,
         ctx(valid_until=(now - timedelta(days=1)).isoformat())),
        ("unapproved_key", "Key registered but not approved by a second person", "P03", genuine, ctx(status="proposed")),
        ("rollback", "Correctly signed downgrade to an older, vulnerable version", "P07", build(to_version="2.2.0",
         package_id="SELFTEST-PGW-2.2.0", sbom_version="2.2.0"), ctx()),
        ("wrong_hardware", "Update built for another hardware variant", "P05",
         build(target={"ecu": ECU, "hardware_id": "PGW-HW-A"}), ctx()),
        ("wrong_base_version", "Delta update for a software version the vehicle does not have", "P06",
         build(from_versions=["2.1.0"]), ctx()),
        ("replay", "Re-sending a package id that was already released", "P08", build(package_id="PGW-2.3.0-REL"), ctx()),
        ("expired_package", "Old package used after its expiry date", "P08",
         build(expires=(now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")), ctx()),
        ("rxswin_not_updated", "Type-approval-relevant change without a new RXSWIN", "P09", build(rxswin=RX_OLD), ctx()),
        ("unlisted_file", "Extra file smuggled into the archive", "P04",
         build(extra={"firmware/backdoor.bin": b"#!/bin/sh\nnc -l 4444\n"}), ctx()),
        ("path_traversal", "Archive entry escaping the install directory (zip slip)", "P01",
         pkg.zip_entries({**pkg.read_package(genuine), "../../etc/init.d/evil": b"x"}), ctx()),
        ("signature_removed", "Signature file deleted", "P01", _rewrite(genuine, **{"manifest.sig": None}), ctx()),
        ("sbom_mismatch", "SBOM inside the package describes different software", "P11", build(sbom_version="2.3.9"), ctx()),
    ]
    rows = []
    base_result = verify(genuine, ctx())
    rows.append({"scenario": "genuine", "attack": "Genuine package signed by the trusted OEM key", "expected_check": "-",
                 "detected": None, "ok": base_result["verdict"] != "fail", "verdict": base_result["verdict"],
                 "failed_checks": base_result["failed"], "detail": "must pass"})
    for name, attack, expected, content, c in scenarios:
        r = verify(content, c)
        hit = next((x for x in r["checks"] if x["id"] == expected), {})
        detected = r["verdict"] == "fail" and hit.get("status") == "fail"
        rows.append({"scenario": name, "attack": attack, "expected_check": expected, "detected": detected, "ok": detected,
                     "verdict": r["verdict"], "failed_checks": r["failed"], "detail": hit.get("detail", "")})
    attacks = [r for r in rows if r["scenario"] != "genuine"]
    return {"genuine_passes": rows[0]["ok"], "attacks": len(attacks), "detected": sum(r["detected"] for r in attacks),
            "all_ok": all(r["ok"] for r in rows), "rows": rows}
