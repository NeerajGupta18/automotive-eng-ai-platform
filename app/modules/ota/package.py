"""OTA update package format, safe reading, signing and signature verification.

A package is a zip:
    manifest.json   what the update is (target ECU and hardware, from/to versions, RXSWIN, payload hashes ...)
    manifest.sig    {"key_id": ..., "alg": "ecdsa-p256-sha256", "sig": base64(DER)} over the exact bytes of manifest.json
    <payload files> listed in manifest.payload with SHA-256 and size
    sbom.json       optional CycloneDX SBOM of the software after the update

The signature covers the manifest, and the manifest pins every payload file by hash, so changing
any byte of the package is detected. The design follows the role of signed target metadata in
Uptane (the automotive OTA security framework) in a deliberately small form; it is not an
Uptane implementation (no director/image repository split, no threshold signatures)."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import stat
import zipfile
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

SCHEMA = "aeai-ota-manifest/1"
ALG = "ecdsa-p256-sha256"
MAX_ENTRIES = 64
MAX_TOTAL = 200 * 1024 * 1024
MAX_RATIO = 200                       # zip-bomb guard: uncompressed / compressed
RESERVED = {"manifest.json", "manifest.sig"}
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class PackageError(ValueError):
    pass


# ---------------------------------------------------------------------------- keys
def generate_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def private_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def public_pem(key) -> str:
    pub = key.public_key() if hasattr(key, "public_key") else key
    return pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def load_public(pem: str) -> ec.EllipticCurvePublicKey:
    try:
        key = serialization.load_pem_public_key(pem.encode() if isinstance(pem, str) else pem)
    except (ValueError, TypeError) as e:
        raise PackageError(f"Not a PEM public key: {e}") from e
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise PackageError("Only ECDSA P-256 (secp256r1) public keys are accepted")
    return key


def load_private(pem: bytes) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise PackageError("Only ECDSA P-256 private keys are supported")
    return key


def fingerprint(pem: str) -> str:
    der = load_public(pem).public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def sign(manifest_bytes: bytes, key: ec.EllipticCurvePrivateKey, key_id: str) -> bytes:
    sig = key.sign(manifest_bytes, ec.ECDSA(hashes.SHA256()))
    return json.dumps({"key_id": key_id, "alg": ALG, "sig": base64.b64encode(sig).decode()}, indent=1).encode()


def verify_signature(manifest_bytes: bytes, sig_doc: dict, public_key_pem: str) -> bool:
    try:
        raw = base64.b64decode(sig_doc.get("sig", ""), validate=True)
        load_public(public_key_pem).verify(raw, manifest_bytes, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError, PackageError):
        return False


# ---------------------------------------------------------------------------- reading
def read_package(content: bytes) -> dict[str, bytes]:
    """Return {name: bytes}. Rejects path traversal, absolute paths, links, duplicates and zip bombs."""
    if not content:
        raise PackageError("Empty package")
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as e:
        raise PackageError(f"Not a zip archive: {e}") from e
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ENTRIES:
        raise PackageError(f"Too many files in package ({len(infos)} > {MAX_ENTRIES})")
    files, total = {}, 0
    for i in infos:
        name = i.filename
        if (name.startswith(("/", "\\")) or "\\" in name or ":" in name or
                any(part in ("..", "") for part in name.split("/"))):
            raise PackageError(f"Unsafe path in package: {name!r}")
        if stat.S_ISLNK(i.external_attr >> 16):
            raise PackageError(f"Symbolic link in package: {name!r}")
        if name in files:
            raise PackageError(f"Duplicate entry: {name!r}")
        total += i.file_size
        if total > MAX_TOTAL:
            raise PackageError("Package expands beyond the size limit")
        if i.compress_size and i.file_size / max(i.compress_size, 1) > MAX_RATIO:
            raise PackageError(f"Suspicious compression ratio for {name!r} (possible zip bomb)")
        files[name] = zf.read(i)
    for req in RESERVED:
        if req not in files:
            raise PackageError(f"Package has no {req}")
    return files


def parse_time(s: str) -> datetime:
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def validate_manifest(m: dict) -> list[str]:
    """Schema problems (empty list = valid)."""
    errs = []
    if not isinstance(m, dict):
        return ["manifest.json must be a JSON object"]
    if m.get("schema") != SCHEMA:
        errs.append(f"schema must be '{SCHEMA}'")
    if not ID_RE.match(str(m.get("package_id", ""))):
        errs.append("package_id: 1-64 characters of letters, digits . _ -")
    t = m.get("target")
    if not isinstance(t, dict) or not t.get("ecu") or not t.get("hardware_id"):
        errs.append("target.ecu and target.hardware_id are required")
    fv = m.get("from_versions")
    if not isinstance(fv, list) or not fv or not all(isinstance(x, str) and x for x in fv):
        errs.append("from_versions must be a non-empty list of versions")
    if not isinstance(m.get("to_version"), str) or not m.get("to_version"):
        errs.append("to_version is required")
    for k in ("created", "expires"):
        try:
            parse_time(m.get(k, ""))
        except (ValueError, TypeError):
            errs.append(f"{k} must be an ISO-8601 timestamp")
    if not isinstance(m.get("type_approval_relevant"), bool):
        errs.append("type_approval_relevant must be true or false")
    elif m["type_approval_relevant"] and not m.get("rxswin"):
        errs.append("rxswin is required when type_approval_relevant is true")
    pl = m.get("payload")
    if not isinstance(pl, list) or not pl:
        errs.append("payload must list at least one file")
    else:
        for i, p in enumerate(pl):
            if not isinstance(p, dict) or not p.get("path") or not HEX64.match(str(p.get("sha256", ""))) \
                    or not isinstance(p.get("size"), int) or p["size"] < 0:
                errs.append(f"payload[{i}] needs path, sha256 (64 lowercase hex) and size")
            elif p["path"] in RESERVED:
                errs.append(f"payload[{i}] uses a reserved name")
    if "sbom" in m and (not isinstance(m["sbom"], dict) or not m["sbom"].get("path") or not HEX64.match(str(m["sbom"].get("sha256", "")))):
        errs.append("sbom must have path and sha256")
    for k in ("affects",):
        if k in m and (not isinstance(m[k], list) or not all(isinstance(x, str) for x in m[k])):
            errs.append(f"{k} must be a list of names")
    return errs


# ---------------------------------------------------------------------------- building (CLI, demo, self-test)
def build(manifest: dict, files: dict[str, bytes], key: ec.EllipticCurvePrivateKey, key_id: str,
          sbom: bytes | None = None, extra: dict[str, bytes] | None = None) -> bytes:
    """Fill payload hashes/sizes from `files`, sign, and zip. `extra` adds unlisted entries (tests)."""
    m = dict(manifest)
    m["payload"] = [{"path": p, "sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for p, b in files.items()]
    if sbom is not None:
        m["sbom"] = {"path": "sbom.json", "sha256": hashlib.sha256(sbom).hexdigest()}
    mbytes = json.dumps(m, indent=1, sort_keys=True).encode()
    return zip_entries({"manifest.json": mbytes, "manifest.sig": sign(mbytes, key, key_id), **files,
                        **({"sbom.json": sbom} if sbom is not None else {}), **(extra or {})})


def zip_entries(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()
