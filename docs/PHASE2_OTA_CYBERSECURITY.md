# Phase 2: OTA update security and vehicle cybersecurity evidence

Phase 2 adds what a vehicle program needs after the design phase: knowing which vulnerabilities
are in the software that is actually installed, proving that an over-the-air (OTA) update is
genuine and safe to release, and collecting the evidence that UN R155 (cybersecurity) and UN R156
(software updates) expect. India's draft AIS-189 and AIS-190 follow the same UN framework; the
June 2026 draft proposes phased dates starting October 2026 for Level 3+ vehicles, 2028 for
OTA-capable vehicles and 2029 for all vehicles with updatable software (check the final notification).

UI page: **Vulnerabilities & OTA** (six tabs). API: `/projects/{p}/vuln/*`, `/projects/{p}/ota/*`,
`/projects/{p}/compliance/*` (see `/docs`).

## 1. SBOM vulnerability monitoring (`app/modules/vuln`)

```
CycloneDX SBOM per ECU release -> ECU software baseline -> match against an OSV advisory feed -> findings
-> triage with VEX states -> exploitable findings flag the ECU's TARA threats -> newer SBOM resolves fixed ones
-> CycloneDX VEX export
```

- **SBOM input**: CycloneDX JSON 1.2-1.6. `metadata.component` names the ECU (as in the TARA architecture)
  and its software version; properties `aeai:hardware_id` and `aeai:rxswin` carry type-approval data.
  Nested components are flattened; size, nesting and component-count limits are enforced.
- **Advisory feed**: OSV format (the schema used by osv.dev): a JSON object, a list, or a zip export.
  The bundled demo feed has four public CVEs (zlib CVE-2022-37434, OpenSSL CVE-2022-3602, Heartbleed
  CVE-2014-0160, curl CVE-2023-38545) and three clearly marked synthetic advisories for fictional
  automotive components.
- **Matching**: by package URL (purl) or name, with OSV range semantics (`introduced` / `fixed` /
  `last_affected`, explicit version lists). Version comparison handles OpenSSL-style suffixes
  (`1.0.1f < 1.0.1g`), pre-releases and trailing zeros. A purl-qualified advisory never matches a
  different product with the same name (an npm package called `zlib` is not the C library).
- **Severity** is computed from the CVSS v3.0/3.1 vector with the FIRST formulas (`cvss.py`), not copied.
- **Triage** uses CycloneDX VEX states: `exploitable` (remediation needed), `not_affected` (needs a VEX
  justification such as `code_not_reachable` plus an explanation), `false_positive`. Dismissing a
  **high or critical** finding needs a **second person**.
- **TARA link**: marking a finding exploitable links it to every TARA threat on that ECU; threats that were
  already assessed or approved become **suspect** (with the reason), and so does everything downstream of
  them. They appear on the Traceability re-review worklist.
- **Resolution**: when a newer SBOM of the ECU no longer contains the vulnerable version, the finding is
  resolved automatically; if a later SBOM brings it back, it is reopened.
- **VEX export**: CycloneDX 1.5 with BOM-Links into the ECU's SBOM (`urn:cdx:<serial>/1#<bom-ref>`).

## 2. OTA update verification (`app/modules/ota`)

A package is a zip with `manifest.json`, `manifest.sig` (ECDSA P-256 over the exact manifest bytes),
the payload files (each pinned by SHA-256 in the manifest) and optionally `sbom.json`. The design
mirrors the role of signed target metadata in **Uptane** (the automotive OTA security framework) in a
small form; it is not an Uptane implementation.

| Check | Fails when |
|---|---|
| P01 Package structure | not a zip, path traversal (`../`), absolute paths, symlinks, duplicates, > 64 files, zip-bomb ratio, missing manifest/signature |
| P02 Manifest schema | required fields missing or malformed |
| P03 Signature and trusted key | unknown key, key not yet approved by a second person, revoked, expired / not yet valid, signature does not match |
| P04 Payload integrity | a file is modified, missing, or not listed in the manifest |
| P05 Target ECU and hardware | ECU has no baseline, or hardware variant differs |
| P06 Installed version compatible | installed version not in `from_versions` |
| P07 Anti-rollback | `to_version` is not newer than the installed version |
| P08 Freshness and replay | expired, created in the future, or package id already released |
| P09 RXSWIN | a type-approval-relevant update keeps the old RXSWIN |
| P10 Safe-update preconditions | (warning) vehicle state / battery level not declared |
| P11 SBOM delta | SBOM hash or identity mismatch; warns if the update introduces high/critical vulnerabilities; lists what it fixes |
| P12 Safety and security impact | (warning) the changed components trace to ASIL B-D goals or requirements |

**Release rules**: a package with any failed check cannot be released; the uploader cannot release it
(four-eyes); warnings need a written justification; the package is re-verified at release time (keys
or the baseline may have changed). On release, the package SBOM becomes the ECU's new baseline (so the
vulnerabilities it fixes are resolved), and approved tests that verify the changed components are
marked **suspect** for re-run. Keys become active only after a second person approves them;
revocation is immediate and re-verifies every unreleased package.

**Attack self-test** (tab 5, `GET /ota/self-test`): builds a genuine package with a throw-away key and
17 attacks on it: tampered firmware, forged manifest, attacker key, key-id spoofing, revoked / expired /
unapproved key, signed rollback, wrong hardware, wrong base version, replay, expired package, RXSWIN not
updated, smuggled file, zip slip, removed signature, SBOM mismatch. The verifier passes only if the
genuine package passes **and** each attack is caught by the check meant to catch it. Result: **17/17**.

`scripts/ota_tool.py` creates keys (`keygen`), signs packages (`build`), checks them offline (`inspect`)
and regenerates the demo packages (`demo`). Private keys are never stored in the repository
(`keys/` and `*.private.pem` are git-ignored; the demo key was discarded after signing).

## 3. R155 / R156 evidence pack (`app/modules/compliance`)

Maps the TARA threats onto the seven threat groups of **UN R155 Annex 5 Part A** (back-end servers,
communication channels, update procedures, unintended human actions, external connectivity, data/code,
potential vulnerabilities) with explicit rules, and combines them with the monitoring and OTA evidence:

| Status | Meaning |
|---|---|
| addressed | every mapped threat is approved |
| in_progress / identified | some / none of the mapped threats are assessed |
| monitored | (group 7) SBOMs exist for the ECUs and the advisory feed is current |
| gap | nothing in the records supports this group |
| organisational | (group 4, human actions) mostly training and procedures; not judged from engineering records |

The pack (Markdown or JSON) lists coverage per group, SBOM and finding status per ECU, signing keys with
fingerprints, every package with verdict and who uploaded and released it, the self-test matrix and
the open actions. **It supports evidence preparation; it is not a compliance statement.** The group
wording is a paraphrase: check it against the official regulation text (free from unece.org).

## Demo (about 3 minutes)

The seed script loads everything. Use `ravi` (engineer) and `asha` (reviewer) in the sidebar name box.

1. **Tab 1**: three ECUs, 7 findings (2 critical). The curl 7.68.0 in the telematics unit is *not*
   flagged: the CVE starts at 7.69.0.
2. **Tab 2** as `ravi`: mark `CVE-2022-37434` (zlib, Gateway) **exploitable**. The approved TARA threat
   on the Gateway becomes suspect (open *Traceability*).
3. **Tab 4** as `ravi`: upload `data/samples/ota/pkg_gateway_2.4.0_tampered.zip` -> **P04 fail**, release
   blocked. Then `pkg_gateway_2.2.0_rollback.zip` -> **P07 fail**, and `pkg_bcm_1.1.0_untrusted_key.zip`
   -> **P03 fail**.
4. Try to release the genuine `PKG-001` as `ravi` -> refused (uploader). Switch to `asha` and release it:
   the Gateway moves to 2.4.0 and **4 vulnerabilities are resolved** automatically.
5. **Tab 5**: run the self-test -> 17/17. **Tab 6**: build the evidence pack.

## Tests

`tests/test_phase2_vuln_ota.py` (55 tests, plus tests/test_pdf_isolation.py): version ordering and OSV ranges, CVSS scores against
published values, SBOM/feed validation, exact ground-truth matching on the demo SBOMs including the
boundary cases and the same-name guard, the triage/four-eyes/VEX workflow, resolution and
reintroduction, every package attack, unsafe archives, key rules, release gates, revocation,
safety-impact gating with test re-verification, R155 mapping, the evidence pack, the API end to end,
the CLI round trip, and isolated PDF parsing. The route-classification test covers every new endpoint.

## Cloud memory (Render free instance)

PDF parsing with table detection costs about 200 MB that Python does not return to the operating
system. With `PDF_PARSE_ISOLATION=1` (set in `render.yaml`) each PDF is parsed in a short-lived child
process. Measured with the demo data: API process 270 MB -> 96 MB; whole app 451 MB -> 276 MB after
visiting every page twice (limit 512 MB). The output is identical (tested); each PDF upload takes a few
seconds longer.

## Limits

- Matching is only as good as the SBOM and the feed: components without versions or with vendor-specific
  names are reported, not guessed. Use an up-to-date OSV export for real monitoring.
- The package format is deliberately simple (one signing role). Production OTA systems use Uptane-style
  separate roles, threshold signatures and HSM-held keys; the checks here show the principles.
- The R155 mapping is rule-based and at group level; it needs review against the regulation and the
  approval authority's interpretation.
