# Phase 4: ADAS virtual validation with software-in-the-loop (AEB)

ADAS functions such as Autonomous Emergency Braking (AEB) and Forward Collision Warning (FCW) are
validated mostly in simulation before any track test: thousands of scenario variants, run on every
software change, against requirements that trace back to safety analysis and consumer tests
(Euro NCAP today; Bharat NCAP 2.0 is proposed to add accident-avoidance tests, so check the final
rules). Phase 4 adds that workflow to the platform for one real function, end to end:

```
requirements (functional + SOTIF) ──> scenarios (Euro NCAP-style grids, SOTIF edge cases,
        │                              boundary-value analysis, plain language) ──> OpenSCENARIO export
        │                                        │
        └──────────> campaign: every approved scenario, closed loop, 10 ms steps
                       on the Python model (MIL) and on the compiled C code (SIL)
                       ├─ requirement verdicts (release-blocking / exploratory)
                       ├─ back-to-back: C code vs model on identical inputs, every step
                       └─ mutation testing: 11 deliberate defects in the C code
                     ──> release decision (second person, gated) ──> traceability and change impact
```

UI page: **ADAS Virtual Validation** (five tabs). API: `/projects/{p}/adas/*` (see `/docs`).
CLI and CI gate: `scripts/sil_run.py`.

## 1. The function under test

| Part | File | What it is |
|---|---|---|
| Reference model | `app/modules/adas/model.py` | AEB logic in Python: the "model" in model-in-the-loop |
| Production-style code | `firmware/aeb/aeb_ctrl.c/.h` | The same logic in C99 with `float`, no dynamic memory, no recursion, no I/O, every function at cyclomatic complexity ≤ 10. The platform's own code-review analyzer reports 0 findings; it compiles with `-Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror` |
| Closed-loop simulation | `app/modules/adas/sim.py` | Ego vehicle with a first-order brake actuator (τ = 0.15 s) limited by road friction, creep when the brakes are released, target vehicle (none / stationary / constant speed / braking), radar-like sensor (range, latency, seeded noise, phantom detections), optional driver braking |

The function, every 10 ms: an object must be reported and closing for **150 ms** before anything
happens (protection against short false detections). It computes **TTC** and the **deceleration
needed** to stop 1.5 m behind the object; when the vehicle ahead is braking, its deceleration is
estimated over 200 ms and taken into account (without this, the hard-braking lead vehicle of Euro
NCAP CCRb 12 m / 6 m/s² ends in a 14 km/h crash: the virtual tests found this during development).
Response: **warning** (TTC < 2.6 s or a_req > 3.5 m/s²), **partial braking** 4 m/s² (TTC < 1.6 s),
**full braking** 9 m/s² (TTC < 1.0 s or a_req > 6 m/s²); braking is held until the object is lost or
moving away, completed to standstill below 10 km/h, and the vehicle is **held** after an autonomous
stop. New braking only between 10 and 85 km/h; the brake request is never lower than the driver's.
The calibration values are examples for the demonstration, not a validated production calibration.

## 2. Scenarios

| Family | Count | Parameters | Source |
|---|---|---|---|
| CCRs: stationary vehicle ahead | 15 | ego 10–80 km/h in 5 km/h steps | Euro NCAP AEB Car-to-Car protocol v3.0.2 grid |
| CCRm: slower vehicle at 20 km/h | 11 | ego 30–80 km/h | same |
| CCRb: lead vehicle brakes | 4 | both 50 km/h; gap 12 / 40 m; 2 / 6 m/s² | same |
| Wet road (friction 0.6) | 8 | ego 10–80 km/h | SOTIF triggering condition |
| Fog (radar range 30 m) | 5 | ego 30–70 km/h | SOTIF triggering condition |
| Phantom radar object (100 ms) | 3 | 50 / 70 / 85 km/h, nothing real ahead | SOTIF: unintended braking |
| Lead vehicle pulling away | 3 | lead 20 km/h faster | SOTIF: unintended braking |
| Sensor latency 300 ms | 2 | 30 / 50 km/h | SOTIF triggering condition |
| Driver already braking | 1 | 3 m/s², late | driver arbitration |

All grids use 100 % overlap (the simulation is longitudinal). Check the current Euro NCAP protocol
version before calling results "NCAP-like"; this is not an NCAP assessment.

**New scenarios are proposals a second person approves:**
- **Boundary-value analysis** (a test-derivation method ISO 26262-6 lists): for every approved
  requirement, scenarios at the edges of its speed range and just outside the activation window,
  skipping values that are already covered. With the catalog it finds exactly one gap: AEB-009
  (activation window) and proposes 8 km/h (an attentive driver creeping to a stop; the AEB must stay
  quiet) and 90 km/h (warning only; the driver reacts 1.4 s later).
- **Plain language**: "Truck stopped in fog at 60 km/h on a wet road" becomes a concrete scenario,
  with what was **understood**, what was **assumed** (e.g. the start distance) and what the 1-D
  simulation **cannot represent** (night, curves, cut-ins, pedestrians, partial overlap). Indian traffic
  words are understood (auto-rickshaw, tractor, cattle on the road). An LLM can be used when configured;
  its values are accepted only within bounds and only if the number appears in the description.
- **Manual** scenarios through the API, validated field by field (known fields, numeric bounds,
  consistent target type) so a user cannot start a 1-hour simulation.

**OpenSCENARIO export**: every scenario as ASAM OpenSCENARIO 1.1 plus a straight OpenDRIVE 1.7 road
(zip download or `sil_run.py xosc`). Friction and fog are expressed as road condition and weather; the
files validate against the ASAM schemas (checked in CI with the schemas shipped in the
`scenariogeneration` package). They were not played in a simulator such as esmini here.

## 3. Requirements (`data/samples/adas/aeb_requirements.json`)

Each requirement has a selector (families, speed range, exact parameters) and a machine check.

| Id | Requirement | Check | Release-blocking |
|---|---|---|---|
| AEB-001 | Avoid a stationary vehicle, 10–50 km/h | no collision | yes |
| AEB-002 | Stationary vehicle 55–80 km/h: avoid or reduce impact speed ≥ 50 % | avoid or reduce | yes |
| AEB-003 | Avoid a vehicle at 20 km/h, ego 30–80 km/h | no collision | yes |
| AEB-004 | Avoid a braking lead vehicle (CCRb grid) | no collision | yes |
| AEB-005 | Warning ≥ 0.8 s before autonomous braking (CCRs, CCRm) | warning time | yes |
| AEB-006 | Never reduce the driver's braking | brake request ≥ driver | yes |
| AEB-007 | Hold the vehicle after an autonomous stop (≤ 0.05 m) | hold | yes |
| AEB-008 | No excessively early stop (gap ≤ 12 m) | stop gap | yes |
| AEB-009 | No reaction below 10 km/h; warning only above 85 km/h | activation window | yes |
| AEB-S01 | Ignore phantom detections < 150 ms | no warning, no braking | yes |
| AEB-S02 | No reaction to a vehicle pulling away | no warning, no braking | yes |
| AEB-S03 | Wet road: avoid ≤ 50 km/h, reduce ≥ 50 % above | avoid or reduce | exploratory |
| AEB-S04 | Fog: warning still ≥ 0.8 s before braking | warning time | exploratory |
| AEB-S05 | Fog: avoid up to 70 km/h | no collision | exploratory |
| AEB-S06 | 300 ms latency: avoid up to 50 km/h | no collision | exploratory |

Per requirement the result is **pass**, **fail**, **not verified** (no applicable scenario exercised
it) or **not covered** (no scenario at all). A campaign passes only if every release-blocking
requirement passes; exploratory (SOTIF) failures are reported as findings to analyse as triggering
conditions, not hidden.

## 4. MIL, SIL and back-to-back

The C file is compiled into a shared library (`cc -std=c99 -O2 -shared`) and called through
`ctypes` with exactly the same interface as the model. Builds are cached by a hash of the sources,
flags and platform; every campaign records that hash and the firmware version as evidence. A layout
check (`aeb_ctx_size()`) refuses to run if the Python structures no longer match the header.

**Back-to-back** (ISO 26262-6 lists back-to-back comparison between model and code): the inputs the
model saw in every scenario are replayed into the C code and the outputs compared step by step: state
and warning exactly, brake request within 0.001 m/s² (C uses `float`, Python `double`). The C code's
**own default calibration** is used and compared with the model's, so a wrong constant in the firmware
is caught even if no scenario reacts to it. Result: **0 differences over 50,424 time steps** in 54
scenarios.

A C compiler is needed for SIL: on Linux `gcc`, on macOS the Xcode Command Line Tools
(`xcode-select --install` provides `cc`), on Windows for example MSYS2 `gcc` (set `CC` if it is not on
the path). Without one the page and the CLI fall back to the model.

Only repository code and the 11 predefined mutants are ever compiled; the API never compiles code
supplied by a user. Libraries are loaded only from `build/sil` in the code directory, an
operator-set `AEB_SIL_DIR` or a private temporary directory of the running process, never from the
data directory, so a restored backup cannot plant native code. Render compiles the library during deployment (`render.yaml`), the Docker image
during its build; without a compiler and a prebuilt library, campaigns run on the model only and
cannot be released.

## 5. Mutation testing: how good are the tests?

Eleven small defects are put into the C code one at a time. A mutant is **killed** when a
release-blocking requirement that passed on the real code fails.

| Mutant | Defect | Catalog only | Catalog + boundary scenarios |
|---|---|---|---|
| M01 | object confirmed one cycle later | survives (back-to-back catches it) | survives (back-to-back) |
| M02 | full braking needs TTC **and** a_req | survives (back-to-back) | survives (back-to-back) |
| M03 | lead vehicle's deceleration ignored | killed by AEB-004 (CCRb 12 m / 6 m/s²) | killed |
| M04 | no hold at standstill | killed by AEB-001/007 | killed |
| M05 | driver request not arbitrated | killed by AEB-006 | killed |
| M06 | confirmation 50 ms instead of 150 ms | killed by AEB-S01 (phantom object) | killed |
| M07 | stop gap added instead of subtracted | survives (back-to-back) | survives (back-to-back) |
| M08 | warning flag off during partial braking | survives (back-to-back) | survives (back-to-back) |
| M09 | braking released below 10 km/h | killed by AEB-004/007 | killed |
| M10 | lower speed limit not applied | **survives** | killed by AEB-009 (8 km/h) |
| M11 | upper speed limit not applied | **survives** | killed by AEB-009 (90 km/h) |
| | **Score** | **5 / 11** | **7 / 11** |

What this shows: closing the coverage gap with the two boundary scenarios kills two more mutants;
outcome-based requirements alone miss timing and threshold changes that do not cause a crash (M01,
M02, M07, M08), and **back-to-back catches every survivor**. M08 also shows a gap in the requirement
set itself (nothing requires the warning to stay on while braking), which is exactly what mutation
testing is for. CI fails if the score drops below 60 %.

## 6. Campaigns, release and change impact

A campaign runs every **approved** scenario against every **approved** requirement on the C code
(or the model when SIL is unavailable), with back-to-back and optionally mutation testing, and an
optional **calibration what-if** (validated ranges and ordering, e.g. full < partial < warning TTC).
It stores the firmware hash, the calibration and the exact versions of all scenarios and requirements.

Release (`decision: approved`) is refused when:
- the person releasing ran the campaign (four-eyes), or no comment is given;
- the evidence came from the model only, or back-to-back did not pass;
- a release-blocking requirement is not passed (failed, not verified or **not covered**);
- anything changed since the run (requirements, scenarios or the firmware source): the campaign is
  shown as out of date and must be re-run.

Traceability links each requirement to its scenarios (`verified_by`) and campaigns (`verified_in`).
Changing an approved requirement or scenario makes the released campaign **suspect** with the reason;
it appears on the *Traceability* re-review worklist next to suspect HARA, TARA, UDS and code items.

Calibration what-ifs worth showing:

| Change | Effect |
|---|---|
| `confirm_cycles` 15 → 5 (react after 50 ms) | phantom objects cause braking: **AEB-S01 fails** (faster reaction trades against false braking) |
| `a_full_mps2` 9 → 6, `areq_full_mps2` 6 → 7 (weaker brakes) | **AEB-003 and AEB-004 fail** |
| `ttc_partial_s` 1.6 → 1.2, `areq_full_mps2` 6 → 8 (later braking) | dry-road requirements still pass, but wet road and latency collisions appear (AEB-S03, S06) |

The fog finding (AEB-S04: warning 0.42 s / 0.05 s / 0 s before braking at 50 / 60 / 70 km/h) cannot be
calibrated away: the radar sees the object too late. That is a perception limit to mitigate (driver
information about reduced availability, camera fusion), the kind of conclusion SOTIF analysis is for.

## 7. CLI and CI

```bash
python scripts/sil_run.py build --mutants                      # compile the library and the mutants (build/sil)
python scripts/sil_run.py campaign --mutation --out sil-report # requirements, back-to-back, mutation; exit 1 on failure
python scripts/sil_run.py campaign --calibration '{"confirm_cycles": 5}'   # what-if from the terminal
python scripts/sil_run.py scenario CCRb-12m-6 --target sil     # one scenario, all metrics
python scripts/sil_run.py xosc --out scenarios/ --validate     # OpenSCENARIO files, checked against the ASAM XSDs
```

CI job `sil-validation`: strict compile with warnings as errors, the code-review gate on
`firmware/aeb` (any finding fails), the SIL campaign with a minimum mutation score of 60 %, the
OpenSCENARIO schema check, and the validation report as a build artifact. The CLI needs only the
Python standard library and a C compiler.

## Demo (about 4 minutes)

The seed loads the requirements, the 52 catalog scenarios, one plain-language proposal and a first
campaign (`CMP-001`). Use `ravi` (engineer) and `asha` (reviewer) in the sidebar name box.

1. **Tab 4**: `CMP-001` **failed**: AEB-009 is not covered by any scenario. Asha tries to release it:
   refused. Point at the SOTIF finding AEB-S04 (fog).
2. **Tab 1** as `ravi`: *Propose scenarios for uncovered boundaries*: 8 km/h and 90 km/h are proposed.
3. **Tab 2** as `asha`: approve both, and approve Ravi's "Truck stopped in fog at 60 km/h on a wet road"
   (show what was understood, assumed and not modelled). As `ravi` the last one is refused (four-eyes).
4. **Tab 3**: simulate `CCRb-12m-6` on the **compiled C code**: the lead brakes at 6 m/s², AEB brakes
   at 1.77 s and stops 4.8 m behind it. Then the what-if `confirm_cycles = 5` on `GHOST-70`: phantom braking.
5. **Tab 4** as `ravi`: run a campaign with **mutation testing**: PASSED, back-to-back 0 differences,
   mutation score 64 %. As `asha`: release it with a comment.
6. **Tab 1**: change AEB-005 (warning time 0.8 → 1.0 s). **Traceability**: the released campaign is
   now suspect, with the reason.
7. **Tab 5**: the C source, 0 code-review findings, the mutant list. Download the validation report and
   the OpenSCENARIO zip.

## Tests

`tests/test_phase4_adas.py` (34 tests): confirmation time, staged response, activation window,
release/hold/driver arbitration, the required-deceleration formulas against hand calculations, the
200 ms deceleration estimate, calibration validation, deterministic simulation, the Euro NCAP grid
values, every check type, plain-language parsing (six descriptions), scenario input validation,
boundary-value analysis, LLM guardrails with a fake model, OpenSCENARIO/OpenDRIVE well-formedness and
schema validity, the code-review gate and a strict compile of the firmware, SIL build caching and
layout check, C defaults equal to the model, back-to-back over every step, the exact mutant kill set,
a calibration what-if on the C code, the CLI exit codes, and the API workflow end to end (coverage gap,
blocked release, proposals and four-eyes, model-only and self-release refused, release, report,
change impact to *suspect* and the traceability worklist). The route-classification test covers
every new endpoint (running one simulation is a read; releases need a reviewer).

## Limits

- One-dimensional, longitudinal simulation: straight road, 100 % overlap, no lateral motion, no
  pedestrians or cyclists, simplified brake (first-order lag) and sensor (range, latency, Gaussian
  noise, phantom) models. It is a regression and comparison harness for the control logic, not a
  vehicle-dynamics or sensor simulation, and it does not replace HIL, vehicle and proving-ground tests.
- The Euro NCAP grid values (including the CCRm target speed of 20 km/h and the CCRb gaps and
  decelerations) are taken from the v3.0.2 protocol as recalled for this work; check them against the
  current protocol before quoting them.
- Requirements and calibration are a demonstration set, not derived from a real HARA of a vehicle.
- SIL runs the C code on the development machine's compiler and CPU. It does not show target timing,
  fixed-point effects or compiler differences of an ECU toolchain (that is what processor-in-the-loop
  and HIL add).
- The OpenSCENARIO files validate against the schemas but were not run in a third-party simulator here.
