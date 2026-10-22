"""Software update management (UN R156 SUMS / India AIS-190 style evidence).

  trusted keys   registered by one person, active only after a second person approves (four-eyes);
                 revocation is immediate and re-verifies every unreleased package
  packages       stored by SHA-256, verified on upload and again at release time
  release        four-eyes, impossible while any check fails; on release the package SBOM becomes the
                 ECU's new software baseline (so fixed vulnerabilities are resolved automatically) and the
                 verification evidence (tests) linked to the changed components is flagged for re-run
  report         per-package update readiness report in Markdown or JSON (audit evidence)"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app.core.db import now
from app.core.errors import Conflict, Invalid
from app.core.workflow import Workflow
from app.modules.ota import checks, selftest
from app.modules.ota.package import PackageError, fingerprint, load_public, parse_time, read_package
from app.modules.vuln.sbom import parse_cyclonedx

MAX_PACKAGE = 50 * 1024 * 1024
ASIL_RANK = {"QM": 0, "A": 1, "B": 2, "C": 3, "D": 4}
KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


class OTAService(Workflow):
    def __init__(self, container):
        super().__init__(container, "ota")

    # ================================================================ trusted signing keys
    def add_key(self, project: str, key_id: str, owner: str, public_key_pem: str, actor: str,
                valid_from: str = "", valid_until: str = "", purpose: str = "ota-signing") -> dict:
        if not KEY_ID_RE.match(key_id or ""):
            raise Invalid("key_id: 3-64 characters of letters, digits . _ -")
        if len((owner or "").strip()) < 2:
            raise Invalid("owner is required (who controls the private key, e.g. 'OEM release HSM')")
        try:
            load_public(public_key_pem)
            fp = fingerprint(public_key_pem)
        except PackageError as e:
            raise Invalid(str(e)) from e
        for label, value in (("valid_from", valid_from), ("valid_until", valid_until)):
            if value:
                try:
                    parse_time(value)
                except ValueError as e:
                    raise Invalid(f"{label} must be an ISO-8601 timestamp") from e
        for k in self.list(project, "key"):
            if k["payload"]["fingerprint"] == fp and k["status"] != "revoked":
                raise Conflict(f"This public key is already registered as {k['ref']} ({k['payload']['key_id']})")
            if k["payload"]["key_id"] == key_id:
                raise Conflict(f"key_id '{key_id}' is already used by {k['ref']}")
        payload = {"title": f"{key_id} ({owner.strip()})", "key_id": key_id, "owner": owner.strip(), "purpose": purpose,
                   "algorithm": "ecdsa-p256-sha256", "public_key_pem": public_key_pem.strip() + "\n", "fingerprint": fp,
                   "valid_from": valid_from, "valid_until": valid_until, "revoked_reason": ""}
        rec, _ = self.create(project, "key", "KEY", payload, actor, machine=False)
        self.audit(actor, "key.add", project, rec["ref"], {"key_id": key_id, "fingerprint": fp[:16]})
        return rec

    def keys(self, project: str) -> list[dict]:
        return self.list(project, "key")

    def decide_key(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        self.get(project, ref, "key")
        out = self.decide(project, ref, actor, decision, comment)
        if decision == "approved":
            self.reverify_open(project, actor)
        return out

    def revoke_key(self, project: str, ref: str, reason: str, actor: str) -> dict:
        rec = self.get(project, ref, "key")
        if rec["status"] == "revoked":
            raise Conflict(f"{ref} is already revoked")
        if len((reason or "").strip()) < 10:
            raise Invalid("Give the revocation reason (at least 10 characters)")
        out = self.db.rec_update(project, ref, actor, {**rec["payload"], "revoked_reason": reason.strip(),
                                 "revoked_at": now(), "revoked_by": actor}, "revoked", event="revoked")
        self.audit(actor, "key.revoke", project, ref, {"reason": reason.strip()})
        reverified = self.reverify_open(project, actor)
        released = [p["ref"] for p in self.list(project, "package", "approved")
                    if p["payload"].get("signed_by") == rec["payload"]["key_id"]]
        return {**out, "reverified": reverified, "released_with_this_key": released}

    def _key_ctx(self, project: str) -> dict[str, dict]:
        return {k["payload"]["key_id"]: {**k["payload"], "status": k["status"]} for k in self.keys(project)}

    # ================================================================ packages
    def _store(self, project: str, sha: str, content: bytes) -> Path:
        d = self.c.settings.data_dir / "ota" / _safe(project)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sha}.zip"
        if not p.exists():
            p.write_bytes(content)
        return p

    def _load(self, project: str, rec: dict) -> bytes:
        p = self.c.settings.data_dir / "ota" / _safe(project) / f"{rec['payload']['sha256']}.zip"
        if not p.exists():
            raise Conflict(f"Stored package file for {rec['ref']} is missing")
        return p.read_bytes()

    def context(self, project: str, exclude_ref: str | None = None) -> checks.Context:
        vuln = self.c.vuln
        base = {ecu: {"version": r["payload"]["ecu_version"], "hardware_id": r["payload"]["hardware_id"],
                      "rxswin": r["payload"]["rxswin"]} for ecu, r in vuln.baseline(project).items()}
        released = {p["payload"]["package_id"] for p in self.list(project, "package", "approved")
                    if p["ref"] != exclude_ref and p["payload"].get("package_id")}

        def preview(b: bytes) -> dict:
            s = parse_cyclonedx(b)
            return {"ecu": s["ecu"], "version": s["ecu_version"], "findings": vuln.preview(project, s)}

        def current(ecu: str) -> list[dict]:
            return [{"advisory": f["payload"]["advisory"], "severity": f["payload"]["severity"]}
                    for f in vuln.findings(project, ecu=ecu) if f["status"] in ("proposed", "assessed", "approved")]

        return checks.Context(keys=self._key_ctx(project), baseline=base, released_ids=released,
                              now=datetime.now(timezone.utc), sbom_preview=preview, current_findings=current,
                              impact=lambda affects, ecu: self.impact(project, affects, ecu))

    def add_package(self, project: str, filename: str, content: bytes, actor: str) -> dict:
        if not content:
            raise Invalid("Empty package")
        if len(content) > MAX_PACKAGE:
            raise Invalid(f"Package exceeds {MAX_PACKAGE // (1024 * 1024)} MB")
        result = checks.verify(content, self.context(project))
        m = result["manifest"] or {}
        sha = result["sha256"]
        self._store(project, sha, content)
        payload = {**self._payload_from(filename, result, content), "uploaded_by": actor}
        rec, created = self.create(project, "package", "PKG", payload, actor, key=sha, machine=False)
        if created:
            ecu = (m.get("target") or {}).get("ecu")
            if ecu:
                self.db.link_add(project, rec["ref"], f"COMP:{ecu}", "updates", actor)
            self.audit(actor, "package.add", project, rec["ref"], {"verdict": result["verdict"], "failed": result["failed"],
                                                                    "package_id": m.get("package_id"), "sha256": sha[:16]})
        return {**rec, "created": created}

    @staticmethod
    def _payload_from(filename: str, result: dict, content: bytes) -> dict:
        m = result["manifest"] or {}
        try:
            sig = json.loads(read_package(content)["manifest.sig"])
            signed_by = str(sig.get("key_id", ""))
        except Exception:                                     # noqa: BLE001 - unreadable packages are reported by the checks
            signed_by = ""
        target = m.get("target") or {}
        title = (f"{target.get('ecu', '?')} {', '.join(m.get('from_versions') or ['?'])} -> {m.get('to_version', '?')}"
                 if m else f"Unreadable package {filename}")
        return {"title": title, "filename": filename, "sha256": result["sha256"], "size": len(content),
                "package_id": m.get("package_id", ""), "ecu": target.get("ecu", ""), "to_version": m.get("to_version", ""),
                "signed_by": signed_by, "verdict": result["verdict"], "failed": result["failed"], "checks": result["checks"],
                "impact": result["impact"], "manifest": m, "verified_at": now()}

    def packages(self, project: str) -> list[dict]:
        return [{**p, "payload": {k: v for k, v in p["payload"].items() if k not in ("manifest", "checks", "impact")}}
                for p in self.list(project, "package")]

    def package(self, project: str, ref: str) -> dict:
        return self.get(project, ref, "package")

    def verify_package(self, project: str, ref: str, actor: str) -> dict:
        rec = self.get(project, ref, "package")
        content = self._load(project, rec)
        result = checks.verify(content, self.context(project, exclude_ref=ref))
        payload = {**rec["payload"], **self._payload_from(rec["payload"]["filename"], result, content)}
        out = self.db.rec_update(project, ref, actor, payload, None, event="reverified")
        self.audit(actor, "package.verify", project, ref, {"verdict": result["verdict"], "failed": result["failed"]})
        return out

    def reverify_open(self, project: str, actor: str) -> list[str]:
        done = []
        for p in self.list(project, "package"):
            if p["status"] in ("proposed", "assessed"):
                self.verify_package(project, p["ref"], actor)
                done.append(p["ref"])
        return done

    def decide_package(self, project: str, ref: str, decision: str, comment: str, actor: str) -> dict:
        """Release (approved) or reject an update. Re-verifies first: keys or the baseline may have changed."""
        rec = self.get(project, ref, "package")
        if decision == "approved":
            uploader = rec["payload"].get("uploaded_by") or rec["created_by"]
            if uploader == actor:
                raise Conflict(f"Four-eyes rule: {actor} uploaded {ref} and cannot release it")
            rec = self.verify_package(project, ref, "rules")          # system re-verification right before release
        p = rec["payload"]

        def gate(r):
            if r["payload"]["verdict"] == "fail":
                raise Conflict(f"{ref} cannot be released: failed checks {', '.join(r['payload']['failed'])}")
            warns = [c["id"] for c in r["payload"]["checks"] if c["status"] == "warn"]
            if warns and len((comment or "").strip()) < 20:
                raise Invalid(f"Checks {', '.join(warns)} raised warnings: explain in the comment (min 20 characters) "
                              "why the release is acceptable")

        out = self.decide(project, ref, actor, decision, comment, gate=gate)
        if decision != "approved":
            return out
        extra = {"released_at": now(), "released_by": actor}
        files = read_package(self._load(project, out))
        sbom_path = (p["manifest"].get("sbom") or {}).get("path")
        if sbom_path and sbom_path in files:
            res = self.c.vuln.add_sbom(project, f"{ref}/sbom.json", files[sbom_path], actor, source=f"ota:{ref}")
            extra["baseline_sbom"] = res["sbom"]["ref"]
            extra["vulnerabilities_resolved"] = res["scan"].get("resolved", 0)
            self.db.link_add(project, ref, res["sbom"]["ref"], "installs", actor)
        flagged = self._flag_reverification(project, ref, p, actor)
        extra["reverify_needed"] = flagged
        out = self.db.rec_update(project, ref, actor, {**out["payload"], **extra}, "approved", event="released", approve=True)
        self.audit(actor, "package.release", project, ref, {"package_id": p["package_id"], **{k: v for k, v in extra.items() if k != "reverify_needed"},
                                                             "reverify": len(flagged)})
        return out

    # ================================================================ impact via traceability
    def impact(self, project: str, affects: list[str], ecu: str) -> dict:
        tr = self.trace
        reqs, goals, tests = [], [], []
        for name in affects:
            for r in tr.parents(project, f"COMP:{name}"):
                if r not in reqs and self.db.rec_get(project, r):
                    reqs.append(r)
        for r in reqs:
            for a in tr.ancestors(project, r):
                rec = self.db.rec_get(project, a)
                if rec and rec["kind"] in ("goal", "cgoal") and a not in goals:
                    goals.append(a)
            for t in tr.children(project, r):
                if t.startswith("TEST:") and t not in tests:
                    tests.append(t)
        names = set(affects) | {ecu}
        threats = [t["ref"] for t in self.db.rec_list(project, "tara", "threat")
                   if t["payload"].get("component") in names and t["status"] != "rejected"]
        code = [f["ref"] for f in self.db.rec_list(project, "codereview", "finding")
                if f["payload"].get("component") in names and f["status"] in ("proposed", "assessed", "approved")]
        vulns = [f["ref"] for f in self.db.rec_list(project, "vuln", "vfinding")
                 if f["payload"].get("ecu") == ecu and f["status"] in ("proposed", "assessed", "approved")]
        asils = [self.db.rec_get(project, x)["payload"].get("asil") for x in reqs + goals]
        asils += [self.db.rec_get(project, x)["payload"].get("asil") for x in code]
        asils = [a for a in asils if a in ASIL_RANK]
        return {"requirements": reqs, "safety_and_cyber_goals": goals, "tests": tests, "threats": threats,
                "code_findings": code, "open_vulnerabilities": vulns,
                "max_asil": max(asils, key=ASIL_RANK.get) if asils else None}

    def _flag_reverification(self, project: str, ref: str, p: dict, actor: str) -> list[str]:
        """The software changed: approved verification evidence for the changed components must be re-run."""
        imp = p.get("impact") or {}
        flagged = []
        reason = f"Software changed by {ref} ({p['ecu']} -> {p['to_version']}): re-run this verification"
        for t in imp.get("tests", []):
            rec = self.db.rec_get(project, t.removeprefix("TEST:"))
            if rec and rec["status"] == "approved":
                self.db.rec_update(project, rec["ref"], actor, {**rec["payload"], "suspect_reason": reason}, "suspect",
                                   event="suspect")
                flagged.append(rec["ref"])
        for t in imp.get("tests", []):
            self.db.link_add(project, ref, t, "requires_reverification", actor)
        return flagged

    # ================================================================ reports and self-test
    def report(self, project: str, ref: str, fmt: str = "md") -> tuple[str, str]:
        rec = self.get(project, ref, "package")
        p = rec["payload"]
        if fmt == "json":
            return json.dumps({"ref": ref, "status": rec["status"], "history": self.db.rec_history(project, ref), **p},
                              indent=1, default=str), "application/json"
        if fmt != "md":
            raise Invalid("format must be md or json")
        m = p.get("manifest") or {}
        icon = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "info": "info", "skip": "skip"}
        lines = [f"# Update readiness report: {ref}", "",
                 f"- Package: `{p['filename']}` ({p['size']} bytes, SHA-256 `{p['sha256']}`)",
                 f"- Package id: `{p.get('package_id') or '-'}` · Target: **{p.get('ecu') or '-'}** "
                 f"{', '.join(m.get('from_versions') or [])} -> **{p.get('to_version') or '-'}** "
                 f"· hardware {(m.get('target') or {}).get('hardware_id', '-')}",
                 f"- Signed by key: `{p.get('signed_by') or '-'}` · RXSWIN: {m.get('rxswin') or '-'}",
                 f"- **Verdict: {p['verdict'].upper()}** · Status: {rec['status']}"
                 + (f" (released {p.get('released_at')} by {p.get('released_by')}, uploaded by {p.get('uploaded_by') or rec['created_by']})"
                    if rec["status"] == "approved" else ""),
                 f"- Last verified: {p.get('verified_at')}", "", "## Checks", "", "| Check | Result | Detail | Supports |", "|---|---|---|---|"]
        for c in p["checks"]:
            lines.append(f"| {c['id']} {c['title']} | {icon[c['status']]} | {c['detail']} | {c['relates_to']} |")
        imp = p.get("impact") or {}
        if imp:
            lines += ["", "## Change impact (traceability)", ""]
            for k in ("requirements", "safety_and_cyber_goals", "tests", "threats", "code_findings", "open_vulnerabilities"):
                lines.append(f"- {k.replace('_', ' ').capitalize()}: {', '.join(imp.get(k) or []) or '-'}")
            lines.append(f"- Highest ASIL touched: {imp.get('max_asil') or 'none'}")
        if p.get("reverify_needed"):
            lines += ["", f"Flagged for re-verification at release: {', '.join(p['reverify_needed'])}"]
        lines += ["", "## Decision history", ""]
        for h in self.db.rec_history(project, ref):
            lines.append(f"- {h['ts']} · {h['actor']} · {h['event']} · {h['status']}")
        lines += ["", "_Generated by the Automotive Engineering AI platform. Supports SUMS evidence preparation; it is not a "
                      "type-approval or certification statement._"]
        return "\n".join(lines) + "\n", "text/markdown"

    def self_test(self, project: str, actor: str) -> dict:
        res = selftest.run()
        self.audit(actor, "selftest", project, None, {"attacks": res["attacks"], "detected": res["detected"],
                                                      "genuine_passes": res["genuine_passes"]})
        return res
