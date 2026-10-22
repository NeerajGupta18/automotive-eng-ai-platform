"""OTA package tool: signing keys, signed update packages, offline inspection, demo set.

  python scripts/ota_tool.py keygen --key-id oem-ota-2026 --out keys/
  python scripts/ota_tool.py build --manifest manifest.json --key keys/oem-ota-2026.private.pem \\
         --key-id oem-ota-2026 --payload build/pgw_app.bin --sbom sbom.json --out pgw_2.4.0.zip
  python scripts/ota_tool.py inspect pgw_2.4.0.zip --pubkey keys/oem-ota-2026.public.pem
  python scripts/ota_tool.py demo --out data/samples/ota        # regenerate the demo packages

Keep private keys out of the repository (keys/ and *.private.pem are git-ignored). In production the
signing key lives in an HSM and this signing step runs inside the release pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.ota import package as pkg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def keygen(a) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    key = pkg.generate_key()
    priv, pub = out / f"{a.key_id}.private.pem", out / f"{a.key_id}.public.pem"
    fd = os.open(priv, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pkg.private_pem(key))
    pub.write_text(pkg.public_pem(key))
    print(f"private key: {priv} (keep secret)\npublic key:  {pub}\nfingerprint: {pkg.fingerprint(pub.read_text())}")
    return 0


def build(a) -> int:
    manifest = json.loads(Path(a.manifest).read_text())
    key = pkg.load_private(Path(a.key).read_bytes())
    files = {f"firmware/{Path(p).name}": Path(p).read_bytes() for p in a.payload}
    sbom = Path(a.sbom).read_bytes() if a.sbom else None
    Path(a.out).write_bytes(pkg.build(manifest, files, key, a.key_id, sbom=sbom))
    print(f"wrote {a.out} ({Path(a.out).stat().st_size} bytes, SHA-256 {hashlib.sha256(Path(a.out).read_bytes()).hexdigest()})")
    return 0


def inspect(a) -> int:
    files = pkg.read_package(Path(a.package).read_bytes())
    m = json.loads(files["manifest.json"])
    sig = json.loads(files["manifest.sig"])
    print(json.dumps(m, indent=1))
    print(f"signed by key_id: {sig.get('key_id')} ({sig.get('alg')})")
    if a.pubkey:
        ok = pkg.verify_signature(files["manifest.json"], sig, Path(a.pubkey).read_text())
        print("signature:", "VALID" if ok else "INVALID")
        bad = [p["path"] for p in m["payload"] if hashlib.sha256(files.get(p["path"], b"")).hexdigest() != p["sha256"]]
        print("payload hashes:", "OK" if not bad else f"MISMATCH {bad}")
        return 0 if ok and not bad else 1
    return 0


def _firmware(seed: str, n: int = 48 * 1024) -> bytes:
    out, block = bytearray(), seed.encode()
    while len(out) < n:
        block = hashlib.sha256(block).digest()
        out += block
    return bytes(out[:n])


def demo(a) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    oem, other = pkg.generate_key(), pkg.generate_key()
    (out / "ota_demo_key.public.pem").write_text(pkg.public_pem(oem))
    sbom24 = (ROOT / "data" / "samples" / "sbom" / "sbom_gateway_2.4.0.cdx.json").read_bytes()
    common = {"schema": pkg.SCHEMA, "created": "2026-10-20T08:00:00Z", "expires": "2028-12-31T00:00:00Z",
              "type_approval_relevant": True, "preconditions": {"vehicle_state": "parked", "min_battery_soc_pct": 40,
                                                                "estimated_install_s": 600},
              "rollback": {"strategy": "A/B", "confirm_timeout_s": 60}}
    gw = {**common, "target": {"ecu": "Gateway", "hardware_id": "PGW-HW-B"}, "from_versions": ["2.3.0"],
          "affects": ["Gateway", "SWC_GatewayFirewall"]}
    good = pkg.build({**gw, "package_id": "PGW-2.4.0-R1", "to_version": "2.4.0", "rxswin": "RXSWIN-GW-0024",
                      "changes": "Security update: zlib 1.2.13, OpenSSL 3.0.7, curl 8.4.0, acme-secoc 1.3.0"},
                     {"firmware/pgw_app_2.4.0.bin": _firmware("pgw-2.4.0")}, oem, "oem-ota-2026", sbom=sbom24)
    files = pkg.read_package(good)
    fw = bytearray(files["firmware/pgw_app_2.4.0.bin"]); fw[4096] ^= 0x01
    tampered = pkg.zip_entries({**files, "firmware/pgw_app_2.4.0.bin": bytes(fw)})
    rollback = pkg.build({**gw, "package_id": "PGW-2.2.0-R9", "to_version": "2.2.0", "rxswin": "RXSWIN-GW-0022",
                          "changes": "Older release (downgrade)"}, {"firmware/pgw_app_2.2.0.bin": _firmware("pgw-2.2.0")},
                         oem, "oem-ota-2026")
    untrusted = pkg.build({**common, "package_id": "BCM-1.1.0-R1", "target": {"ecu": "BCM", "hardware_id": "BCM-HW-C"},
                           "from_versions": ["1.0.0"], "to_version": "1.1.0", "rxswin": "RXSWIN-BCM-0110",
                           "affects": ["BCM", "SWC_DoorLockCtrl"], "changes": "Door lock timing change"},
                          {"firmware/bcm_app_1.1.0.bin": _firmware("bcm-1.1.0")}, other, "supplier-test-key")
    outputs = {"pkg_gateway_2.4.0_signed.zip": good, "pkg_gateway_2.4.0_tampered.zip": tampered,
               "pkg_gateway_2.2.0_rollback.zip": rollback, "pkg_bcm_1.1.0_untrusted_key.zip": untrusted}
    for name, data in outputs.items():
        (out / name).write_bytes(data)
    if a.keep_private:
        (out / "ota_demo_key.private.pem").write_bytes(pkg.private_pem(oem))
    print("\n".join(f"wrote {out / n}" for n in ["ota_demo_key.public.pem", *outputs]))
    print("the demo private key was " + ("kept" if a.keep_private else "discarded (packages are signed; nothing secret is stored)"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("--key-id", required=True); k.add_argument("--out", default="keys")
    b = sub.add_parser("build"); b.add_argument("--manifest", required=True); b.add_argument("--key", required=True)
    b.add_argument("--key-id", required=True); b.add_argument("--payload", nargs="+", required=True)
    b.add_argument("--sbom"); b.add_argument("--out", required=True)
    i = sub.add_parser("inspect"); i.add_argument("package"); i.add_argument("--pubkey")
    d = sub.add_parser("demo"); d.add_argument("--out", default=str(ROOT / "data" / "samples" / "ota"))
    d.add_argument("--keep-private", action="store_true")
    a = ap.parse_args()
    return {"keygen": keygen, "build": build, "inspect": inspect, "demo": demo}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
