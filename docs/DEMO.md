# 12-minute demo script and interview cheat-sheet

## Setup (once)
```bash
pip install -r requirements-dev.txt
AUTH_MODE=enforced uvicorn app.api.main:app --port 8000      # copy the admin token printed at first start
API_TOKEN=<admin token> python scripts/seed_demo.py           # HLDs, HARA item, TARA system, UDS spec, C code + tool logs
streamlit run ui/streamlit_app.py                             # sign in with the admin token
```
Create two engineers on the *Administration* page (e.g. `asha` = reviewer, `ravi` = engineer) and add them to project `bcm-demo`.

## The story: one change, traced through every discipline
1. **HLD (page 1)** - ask "Which component provides the vehicle speed?" (cited answer, refuses when evidence is weak). Show the 12 consistency findings and the review queue.
2. **HARA (page 2)** - triage hazards, assess an event (S/E/C need written rationale), see the ASIL computed by a rule table, derive a safety goal, draft FSR/TSR, fill the placeholders, allocate to `SWC_DoorLockCtrl`. Try approving your own work as `ravi`: refused (four-eyes). Approve as `asha`.
3. **TARA (page 3)** - analyze the BCM architecture, assess a threat (impact + attack potential -> computed risk), treat it, derive a cybersecurity goal, recommend controls (source, assumptions, alternatives). Show the coverage checklist and the "expert review required" note.
4. **UDS (page 4)** - extract the spec from the document, validate `27 01`, `27 02 <KEY>`, `22 01 00`, generate 118 tests, run them on the simulator, then run **all injected defects** and show every one is detected. Attach a test to the FSR as verification evidence.
5. **Code review (page 5)** - analyze, show a corroborated finding (own rule + cppcheck), open a fix and show "verified by re-analysis", upload `door_lock_ctrl_v2.c` as a new revision: findings become *fixed*, decisions carry over, the new `goto` appears. Findings in `DoorLockCtrl_Run10ms` carry the component's ASIL.
6. **Traceability (page 6)** - go back to HARA, **re-assess the approved event** (D -> QM). Open *Traceability*: the goal, requirements, the UDS test evidence and the code findings are on the **re-review worklist**, with the reason. This is the point of the whole platform.
7. **Administration (page 7)** - show the audit log, click *Verify audit chain*. Tamper with one audit row in SQLite and verify again: it names the broken entry.

## Questions you will be asked, and honest answers
| Question | Answer |
|---|---|
| Why not let the LLM decide ASIL / risk / expected UDS responses? | Those are deterministic engineering rules: they must be reproducible and auditable. The LLM is optional, used for Q&A and an opt-in code review, and every LLM finding must quote the exact source line or it is dropped. |
| Is the ASIL table / risk matrix / MISRA mapping correct? | They are explicit, versioned *example* rule sets. They must be checked against your licensed ISO 26262 / 21434 / MISRA copies before use. The libraries are synthetic. |
| Does the UDS simulator prove the tests are right? | No. It shares the oracle with the generator, so it proves consistency and defect-detection power, not that the specification or the oracle rules are correct. Verify the oracle against the OEM diagnostic spec. |
| How good is the code analyzer? | It is a heuristic analyzer without a compiler front end or data-flow engine: false positives and misses are expected. It complements a certified analyzer. The value is the lifecycle (fingerprints, triage carry-over, verified fixes, gate, SARIF). |
| Is it secure? | With `AUTH_MODE=enforced`: token authentication, per-project RBAC (deny-by-default, tested over every route), token-bound four-eyes, rate limits, hash-chained audit. No SSO/MFA, single process, no encryption at rest; TLS must come from a reverse proxy. The default dev mode has no access control. |
| What did you not verify? | A real LLM, sentence-transformers embeddings, the Docker build, CAPL in CANoe, SocketCAN. |
| What would you do next? | PostgreSQL + multi-worker, OIDC gateway, a qualified static analyzer as an input, real libraries reviewed by domain owners, more UDS services (0x2F, 0x34-0x37), evaluation of retrieval quality on real HLDs. |
