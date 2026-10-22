# Phase B: help with car problems in CarSaathi

Phase A gave car owners a garage, a document vault and reminders. Phase B answers the question owners actually call the
service centre about: "my phone won't connect", "Android Auto keeps dropping", "the car braked by itself on the flyover",
"what is this light?". It does three things:

1. **Finds the right fix from the owner's own words**, in English or Hinglish ("bluetooth connect nahi ho raha"), and puts
   safety first: if the description sounds dangerous (brakes, smoke, fuel smell, a stuck accelerator) the app says what to
   do now before offering any steps.
2. **Walks the owner through it, one step at a time**, with 26 guides written for Indian cars (Bluetooth and calls, music,
   Android Auto and CarPlay, the screen, cameras and sensors, driver assistance, keys, battery, tyres and EV charging) and
   16 dashboard warning lights.
3. **When nothing helps, hands it over properly**: a help request with the steps already tried, the answers and photos, as a
   summary the owner sends to their service centre. For driver-assistance problems the owner can also report it, anonymised
   and with their consent, to the **Phase 6 field-issue investigator**, where it joins the dealer complaints of the same
   symptom and software build and starts the agent's investigation.

The third point connects the two halves of the repository: the voice of the customer, structured by the owner app, becomes
evidence for the engineers' root-cause analysis.

```
owner's words ──> normalise (Hinglish, abbreviations, typos) ──> safety check ──> BM25 guide search ──(unsure + LLM set)──> LLM picks a guide id
                                                                     │                    │
                                                              "stop safely" card     guide, step by step ──> fixed (counted anonymously)
                                                                                          │
                                                                                     not fixed ──> help request (steps, answers, photos)
                                                                                                       │                 │
                                                                                     summary for the service centre      ADAS only, with consent:
                                                                                                                         anonymised complaint ──> Phase 6 API
                                                                                                                         (dry run by default)     /field/complaints/import
```

## For the owner

| | |
|---|---|
| **Describe it** | One box on the Help tab: "What's wrong?". The search understands Hinglish ("gaana nahi chal raha bluetooth pe"), abbreviations (AA, BT, ACC, AEB, TPMS) and typos ("andriod", "blutooth"). It uses the selected car: an EV gets charging help and EV warning lights, a petrol car doesn't. |
| **Safety first** | Ten kinds of danger (brake failure, fire or smoke, fuel smell, steering, overheating, oil pressure, unintended acceleration, tyre or wheel failure, EV battery, accident) show a red card with what to do now and one-tap calls: 112, the car's own roadside assistance number, and the NHAI highway helpline 1033. The guides only appear after "I'm safe now". |
| **Step by step** | One step on screen at a time: "That fixed it" or "Still not working"; questions branch the guide ("cable or wireless?"); "Back one step" undoes. Steps that can't be checked at once (cleaning a sensor) have a single "Done". |
| **Warning lights** | The 16 common symbols laid out like an instrument cluster. Tap one: what it means, how serious (stop safely / get it checked soon / information), what to do, and a guide where one exists. |
| **Ask for help** | When a guide ends without a fix, or nothing fits: a help request with the car, the date, the owner's description, the steps tried and the answers, and up to 3 photos. *Send to your service centre* opens the phone's share sheet (WhatsApp, email) with a ready summary. |
| **Report to the manufacturer** | For "the car braked or warned with nothing ahead" and "automatic braking didn't react": the owner sees exactly what will be shared, may add the speed, the software version and the VIN, and ticks a consent box for this one report. |
| **Roadside number** | The car's roadside assistance number (from the insurance or RSA card) is saved with the car and offered on every emergency card. |

## How it works

**Content as data.** The guides and lights are JSON in `owner/guides/`, written for this project (no manufacturer text
is copied: menus differ between cars, so steps say where settings *usually* are and point to the owner's manual). Each
guide is a small decision graph checked when it loads: every target exists, every step is reachable, there are no loops,
facts recorded by answers use known values. A broken guide stops the service at start-up, not in front of an owner.

**Search** (`owner/helpsearch.py`). Hinglish phrases and abbreviations are mapped to English (`nahi` -> not, `awaaz` ->
sound, `brake lag gaya` -> braked suddenly, `AA` -> android auto), words are stemmed, and BM25 ranks the guides on word
stems and word pairs over their titles, summaries, 6 to 11 symptom phrases each, and keywords. Typos in longer words are
corrected against the guide vocabulary. Guides for one kind of car rank lower when the words don't point to it. The search
reports whether it is confident; when it isn't, the app says so and offers topics and a help request instead of a guess.
The description is sent in the body of a POST, so it never appears in URLs or access logs, and it isn't stored.

**Optional LLM, on a short leash.** With `OWNER_LLM_BASE_URL` and `OWNER_LLM_MODEL` (any OpenAI-compatible API, e.g. Groq),
the LLM is asked only when the search is unsure, may only answer with one of the existing guide ids (validated, otherwise
ignored), never writes steps, and gets the description with emails, phone numbers, number plates and VINs removed. If it
fails or times out, the rules' answer stands. Off by default; the privacy notice says when it is on.

**Safety check** (`owner/safety.py`). Patterns on the normalised text, tuned for recall: a false alarm costs one tap, a
miss could cost much more. Care is taken where words overlap: "the automatic emergency brake didn't work" is a
driver-assistance report, not brake failure; "no smoke" is not smoke; adaptive cruise speeding up to its set speed is normal.

**Help requests** (`owner/helpdesk.py`). The app sends which step and which answer; the server rebuilds the transcript
from the guide, and the facts (what the car did, where) come from the guide's own options, never from the request. A
request belongs to the owner who made it (others get *not found*), survives the car being removed, and is in the data
export and deleted with the account.

**Reports to the manufacturer** (`owner/bridge.py`). The report is a complaint for the Phase 6 API
(`POST /projects/{project}/field/complaints/import`), written so the platform's own complaint rules read the intended
symptom, place and speed: the tests run all 64 combinations of answers through those rules. It contains the car's make,
model, year and fuel, the odometer rounded to 1,000 km, the date, what the car did, where and how fast, the steps already
tried, and the VIN and software version only if the owner adds them. Never the name, email, phone, number plate, the
owner's own description or photos. Delivery modes: not connected (no `OWNER_FIELD_API_URL`: kept with the request), dry run
(`OWNER_FIELD_LIVE=0`, the default: prepared and checked, not sent), live (sent at once; failures retried every 5 minutes,
8 tries, then marked failed). Demo accounts never send.

**Learning which guides work.** When a guide ends, the app records the guide, the outcome (fixed or escalated) and the step,
with no user, car or description. `GET /api/help/stats` with `OWNER_ADMIN_TOKEN` shows per guide how often it fixed the
problem and at which step: the steps that never fix anything are the ones to rewrite.

**Database.** Schema 2 adds `help_cases`, `case_photos`, `help_outcomes` and `field_reports`, and the roadside number on cars.
An existing Phase A database is migrated at start-up (the new tables, then `ALTER TABLE cars ADD COLUMN roadside_phone`),
once, recorded in `schema_version`; tested on SQLite and PostgreSQL.

## Measured

`python scripts/eval_owner_help.py` (CI job `owner-help-quality`) runs the search on 210 labelled owner descriptions in
three sets, all written by the developer. Full report: [eval/owner_help/results/REPORT.md](../eval/owner_help/results/REPORT.md).

| | Building set (124) | Held-out 1 (50) | Held-out 2 (36) |
|---|---|---|---|
| Right guide first | 99.0% | 83.7% (first run 69.8%) | 90.3% |
| Right guide in the top 3 | 100% | 97.7% (first run 93.0%) | 100% |
| Urgent descriptions that get their safety card | 100% | 100% (first run 3 of 5) | 100% (first run 2 of 3) |
| Safety cards on descriptions that aren't urgent | 0 | 0 | 0 (first run 1) |
| Out-of-scope questions answered "not sure" | 100% | 100% | 100% |

How to read it: the building set was used while writing the search, so its 99% says little. Held-out 1 was written
afterwards in longer, less direct words; its first run (69.8%, and two missed safety cards) led to general fixes (stop
words, stricter typo correction, synonyms such as windshield and cut off, safety patterns), not to its own phrases.
Held-out 2 was written after that and its ranking result is untouched: **about 9 in 10 descriptions get the right guide
first, and the right guide is almost always in the top 3**. Its first run also missed one urgent description and raised
one false alarm; both safety patterns were fixed, because a known safety miss is not left in place. None of this is real
owners' language: the first thing to do with real users is to read the help requests that said "none of these".

## Try it

Locally, with the demo account:

```bash
make owner-build && make owner-api       # http://localhost:8100, then "Try the demo" and the Help tab
```

The bridge to the engineering platform, end to end:

```bash
make api                                  # terminal 1: the platform API on :8000
curl -X POST localhost:8000/projects -H 'X-User: ravi' -H 'content-type: application/json' -d '{"name":"bcm-demo"}'
curl -X POST 'localhost:8000/projects/bcm-demo/field/samples/load?investigate=false' -H 'X-User: ravi'
make owner-bridge                         # terminal 2: the owner app, sending reports live
```

Sign in with your email (the code is in terminal 2), add a car with a VIN, run *Car braked or warned with nothing ahead*,
save the help request and report it with software version 2.4.0. In the platform it arrives as a new complaint (e.g.
FC-007) in issue *Unexpected braking on AEB 2.4.0*, next to the four dealer complaints, and the agent starts investigating
(run in the development sandbox: complaint FC-007 joined FI-001 and started RUN-001).

## Demo script (4 minutes)

1. **Safety first** (30 s): on the Help tab type "bonnet se dhuan nikal raha hai". The red card says stop and get out, with
   112, the car's roadside number and 1033. No troubleshooting for this.
2. **Everyday problem** (1 min): "android auto keeps disconnecting": the cable guide; mark a step as fixed. Mention the
   anonymous count of which step fixed it.
3. **Warning lights** (30 s): the instrument cluster; tap the red oil can.
4. **ADAS complaint** (1.5 min): "flyover pe achanak brake lag gaya" -> *Car braked or warned with nothing ahead*: no crash,
   braked on its own, on a flyover; clean the sensors; save the request with a photo; *Send to your service centre*; then
   *Report to the manufacturer*: show exactly what is shared and what isn't, tick consent.
5. **The engineering side** (30 s): in the platform, the report is complaint FC-007 in the issue for AEB 2.4.0, and the
   Phase 6 agent investigates it with the dealers' recordings.

## Limits

- The guides are general: menu names, buttons and limits differ between makes and models, so they point to the owner's
  manual for exact steps. They were written by a developer, not reviewed by a manufacturer's service engineers.
- The search was measured on descriptions written by the developer, not on real owners' words.
- On the public Render demo the bridge is a dry run: the engineering demo's API is private on Render's free plan. The live
  bridge was tested against the platform API running locally and against a stand-in server for retries and failures.
- A report needs the owner to know the software version for the platform to match it to a build; without it, it goes
  into an issue for build *unknown*.
- The LLM picker was tested with scripted answers only.
- Help requests are not sent to a dealer system: the owner shares the summary. Booking a service visit is a later phase.
