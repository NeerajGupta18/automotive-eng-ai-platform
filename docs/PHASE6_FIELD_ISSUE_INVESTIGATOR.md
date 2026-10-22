# Phase 6: the field-issue investigator (agentic AI for in-service problems)

**The problem.** When a customer says *"my car braked hard for no reason under a flyover"*, the investigation is slow and
manual: collect complaints, read the vehicle recordings, try to reproduce the event (dealers often cannot), find which
software change caused it, find a fix that does not break anything else, rate the hazard, write the 8D report. Industry
surveys put this at weeks from first report to confirmed root cause; in 2024 NHTSA expanded a phantom-braking
investigation to about 3 million vehicles after 1,294 complaints, and the owners reported that dealers could not
reproduce the problem.

**What Phase 6 adds.** An agent that runs that investigation end to end as soon as a complaint arrives, on the same
platform that already holds the AEB function's requirements, scenarios, compiled C code and release workflow, and that
stops for a person at the two decisions that belong to people:

```
 dealer complaint + event recording ─▶ grouped into an issue (symptom × software build) ─▶ investigation starts automatically
   1 read the issue, the software build and its fleet
   2 analyse each recording: interventions, warnings, radar objects (real or phantom), crashes
   3 replay the recorded inputs on the field build's compiled C code (SIL): bit-exact reproduction
   4 rebuild each situation as a closed-loop scenario; retry variations until the simulation matches the recording
   5 root cause: code vs model (coding defect), field vs last validated calibration (which parameter), with vs without AEB
   6 escape point: validation evidence of the build; would the regression or back-to-back have caught it?
   7 fix search: candidates checked against every complaint, a +20 ms margin case and the whole regression library
   8 hazard rating proposal (S/E/C → ASIL from the HARA rule table)
   ⏸ 9  safety manager confirms the hazard rating            (human checkpoint)
   ⏸ 10 corrective actions: regression scenarios, the fix to verify, tickets, 8D record   (human checkpoint)
 then the normal platform rules: a reviewer approves the field scenarios ─▶ verification campaign with the fix ─▶ a second
 person releases it ─▶ a second person closes the issue ─▶ 8D report complete
```

UI page **Field Issues**; API `/projects/{p}/field/*` (see `/docs`); agent playbook `field_investigation` (also via the
MCP server and the generic agent page: "Investigate FI-001").

## Root-cause classes and how each is decided (`app/modules/field/investigate.py`)

| Class | How the agent decides it | What it proposes |
|---|---|---|
| Implementation defect | The recording replays bit-exactly on the build's C code, but the reference model with the *same* calibration gives different outputs; the first differing cycle is reported | Rollback to the newest validated build that does not show the problem (checked on every recording); a code fix verified with back-to-back |
| Calibration regression | Replayed with the last validated calibration, the reaction is different; reverting one parameter at a time isolates the responsible one | The smallest change that removes the behaviour in every complaint and the margin case without a new regression failure. If the change prevented collisions in every case: *validate*, do not revert |
| Sensor false target | A stationary object appeared mid-range and vanished while still ahead (phantom), and the validated calibration reacts the same way | Sensor supplier analysis; an interim mitigation only if one passes every check (usually none does) |
| Correct intervention | A real object; simulated without AEB (with the driver's recorded braking) the car would have hit it | Customer response; no defect |
| System limitation | Speed outside the activation window, low friction (full braking requested, less achieved), object detected too late | Customer information; no defect |
| Insufficient data | No recording, the complained event is not in it, or it does not replay on the stated software (the agent names the build it does match) | Request the recording / correct the version |

Open-loop replays are only exact until the recorded intervention changes the vehicle's motion, so reactions are compared
up to 200 ms after the recorded intervention; counterfactuals and fix candidates use the closed-loop reconstruction.

## Guardrails

- **The agent decides nothing.** All analysis tools only read. The only writes are two steps a person confirms (hazard
  rating, corrective actions), which then run under that person's name. Scenario proposals, the verification campaign,
  its release and the closure follow the existing four-eyes rules (tested: the person who confirmed the actions cannot
  approve the scenarios they created; the person who ran the verification cannot close the issue).
- **Closing needs evidence.** A calibration fix can only be closed when the verification campaign with exactly that
  calibration, including the field scenarios, is released by a second person.
- **Untrusted inputs.** Recordings: fixed columns, numeric ranges, 10 ms time base, size and row limits, sanitised
  header. Complaint text: rules by default; an LLM (`FIELD_TRIAGE_LLM=1`, off by default) only through validated JSON
  with known symptoms, a speed only if that number is in the text, fallback to the rules. The text never decides the
  root cause; the data does. Only predefined firmware variants are ever compiled.
- **Durable and audited.** The investigation is a normal agent run: replayable, resumable after a restart, every step,
  checkpoint and confirmation in the hash-chained audit log. A new complaint restarts an investigation that has not
  written anything yet; after the actions it marks the issue "new evidence" instead.
- **Release readiness RR12** blocks while a field issue with a malfunction is open.

## Measured on labelled cases (`scripts/eval_field_agent.py`, CI gate)

27 synthetic cases with known causes across all classes, each run through the real application path (complaint →
automatic agent run up to its first checkpoint): see [eval/field/results/FIELD_AGENT_REPORT.md](../eval/field/results/FIELD_AGENT_REPORT.md).

| Metric | Result | CI threshold |
|---|---|---|
| Root cause correct | 27/27 | ≥ 95 % |
| Complaint text understood (incl. a Hindi-English text) | 27/27 | ≥ 95 % |
| Real events reproduced (SIL replay and closed loop) | 24/24 | ≥ 95 % |
| Fix verified / correct rollback / "validate, don't revert" | 12/12 | ≥ 90 % |
| False defects (a correct system called defective) | 0 | 0 |

Included on purpose: a wrong software version in the complaint (the agent names the build the recording matches), a
cut-in that appears mid-range like a phantom but is real, a long phantom on the regressed build that is *not* the
regression's fault, a calibration change that caused braking but prevented a collision, a recording with sensor latency
the agent estimates from the data, a mixed-language complaint. **Honest limit:** the cases are generated from the same
simulation the agent uses, so these numbers show that the investigation chain works end to end, not its accuracy on real
fleet data. Real recordings are noisier, have more signals and causes this 1-D model cannot represent.

## Demo (about 5 minutes; the seed loads 6 complaints → 3 issues, all investigated)

1. **Field Issues → 1 Inbox:** six dealer complaints were grouped into three issues and investigated automatically:
   FI-001 *calibration regression* (4 complaints on AEB 2.4.0), FI-002 *correct intervention*, FI-003 *coding defect* in a
   pilot build.
2. **2 Investigation, FI-001:** what the agent did in 8 read steps (a few seconds); per complaint: reproduced 100 % on
   the C code, the phantom object at 20-26 m for 70-120 ms, `confirm_cycles` 15 → 5 in the 2.4.0 calibration is the
   cause, the other change (`ttc_warn_s`) is not. The chart: recorded brake request, the re-simulation on top of it, and
   no braking with the fix. Fix search: 5 and 10 fail the existing GHOST regression; 15 fixes every complaint and the
   margin case. Escape: the existing regression would have caught it, 2.4.0 went out without it.
3. As **asha** (safety manager): confirm the hazard rating (S2 E4 C2 → ASIL B, escalation). Confirm the actions: three
   tickets (fix, hold the OTA rollout, process gap) and four field scenarios.
4. **3 Verification and 8D** as **ravi**: approve the field scenarios (asha could not: she proposed them), run the
   verification campaign (SIL, back-to-back) with `confirm_cycles = 15`; as **asha**: release it, then close the issue
   (ravi could not: he ran the verification). The 8D report is complete. Release readiness RR12 follows.
   (If the campaign fails on AEB-009, close that coverage gap on the ADAS page first: Phase 4 demo step.)
5. Contrast: **FI-002** — "too aggressive" braking that would otherwise have hit the car at 42 km/h: no defect, one
   checkpoint, a customer-response ticket. **FI-003** — the code deviates from the model at standstill: rollback to 2.3.0.
6. **4 Builds and agent quality:** the build registry and the evaluation (27/27).

## Files

`app/modules/field/` logs (format, analysis) · builds (registry, controllers) · investigate (replay, reconstruction,
diagnosis, escape, fix search, hazard proposal) · triage (complaint text) · service (complaints, issues, automatic
investigation, gates, verification, closure) · report (8D) — `app/api/field.py` — agent tools and the
`field_investigation` playbook in `app/modules/agent/` — `ui/pages/13_Field_Issues.py` — `scripts/make_field_data.py`
(synthetic data), `scripts/eval_field_agent.py` (evaluation) — `data/samples/field/` (builds, demo complaints and
recordings) — `eval/field/` (27 labelled cases) — database migration 5 (`field_logs`).

## Limits

- One function (AEB) in a 1-D longitudinal simulation: lateral situations, sensor fusion, camera data and the vehicle
  network beyond these signals are out of scope. Coding-defect builds are the predefined Phase 4 mutants.
- Hazard ratings use the example rule table and simple rules for S/E/C; a safety manager confirms or changes them.
- No connection to a real warranty or dealer system; complaints arrive through the API (single or batch import).
- Exposure counts come from the build registry; real fleet telemetry would give rates per condition.
