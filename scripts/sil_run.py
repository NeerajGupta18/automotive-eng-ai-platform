"""AEB virtual validation without the server: build the SIL library, run campaigns (CI gate), export scenarios.

  python scripts/sil_run.py build [--mutants]
      compile firmware/aeb into build/sil (Render runs this during deployment so the server needs no compiler)
  python scripts/sil_run.py campaign [--target sil|model|auto] [--mutation] [--no-bva] [--out DIR]
                                     [--min-mutation-score 0.6] [--calibration '{"ttc_partial_s": 1.2}']
      library requirements + scenario catalog + boundary-value scenarios; exit code 1 if a release-blocking
      requirement or back-to-back fails (or the mutation score is below the minimum)
  python scripts/sil_run.py scenario CCRb-12m-6 [--target sil]
  python scripts/sil_run.py xosc --out scenarios/ [--validate]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.adas import authoring, campaign, sil  # noqa: E402
from app.modules.adas import requirements as rq  # noqa: E402
from app.modules.adas.model import AebModel, calibration_changes, make_calibration  # noqa: E402
from app.modules.adas.report import markdown  # noqa: E402
from app.modules.adas.scenarios import ROAD_FILE, catalog, road_xodr, to_openscenario  # noqa: E402
from app.modules.adas.sim import simulate  # noqa: E402


def scenario_set(with_bva: bool) -> tuple[list[dict], list[dict]]:
    reqs = rq.load_library()["requirements"]
    scns = [authoring.validate_scenario(s) for s in catalog()]
    if with_bva:
        scns += [p["scenario"] for p in authoring.propose_boundary(reqs, scns)]
    return reqs, scns


def cmd_build(a) -> int:
    b = sil.build()
    print(f"SIL library: {b['path']} ({'cached' if b['cached'] else 'compiled'}) hash {b['hash'][:16]}")
    if b["warnings"]:
        print(b["warnings"])
    if a.mutants:
        c_text, _ = sil.sources()
        for m in campaign.MUTANTS:
            mb = sil.build(campaign.mutate(c_text, m))
            print(f"  mutant {m['id']}: {Path(mb['path']).name}")
    return 0


def cmd_campaign(a) -> int:
    reqs, scns = scenario_set(not a.no_bva)
    cal = make_calibration(json.loads(a.calibration) if a.calibration else None)
    res = campaign.run_campaign(scns, reqs, target=a.target, cal=cal, do_b2b=True, do_mutation=a.mutation)
    v = res["verdict"]
    print(f"{res['target'].upper()} campaign on {len(scns)} scenarios, {len(reqs)} requirements: "
          f"{'PASSED' if v['passed'] else 'FAILED'} ({res['seconds']} s)")
    for r in res["requirements"]:
        first = next((f"{x['scenario']}: {x['detail']}" for x in r["scenarios"] if x["verdict"] == "fail"), "")
        print(f"  {r['id']:8s} {r['verdict']:12s} {'blocking' if r['release_blocking'] else 'exploratory':11s} {r['title'][:55]:55s} {first}")
    b = res.get("back_to_back")
    if b:
        print(f"Back-to-back: {'passed' if b['passed'] else 'FAILED'} over {b['steps']} steps "
              f"(max brake diff {b['max_brake_diff']}, calibration mismatch {b['calibration_mismatch'] or 'none'})")
    m = res.get("mutation")
    if m:
        print(f"Mutation score {m['score']:.0%}: {m['killed']} killed, {m['survived']} survived "
              f"({m['survivors_caught_by_b2b']} of them caught by back-to-back)")
        for row in m["rows"]:
            print(f"  {row['id']} {row['status']:8s} {row['description']}"
                  + (f"  <- {', '.join(row['killed_by'])} ({row['scenario']})" if row["status"] == "killed" else ""))
    for n in res["notes"]:
        print(f"note: {n}")
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        rec = {"ref": "CI", "status": "proposed", "approved_by": None, "approved_at": None, "stale": [],
               "payload": {**res, "calibration_changes": calibration_changes(cal), "run_by": "ci", "run_at": "",
                           "scenario_set": [{"id": s["id"]} for s in scns], "requirement_set": [{"id": r["id"]} for r in reqs],
                           "note": "scripts/sil_run.py campaign"}}
        (out / "validation_report.md").write_text(markdown("ci", rec), encoding="utf-8")
        (out / "validation_result.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
        print(f"Report written to {out}/validation_report.md")
    ok = v["passed"]
    if m and a.min_mutation_score is not None and (m["score"] or 0) < a.min_mutation_score:
        print(f"Mutation score {m['score']:.0%} is below the minimum {a.min_mutation_score:.0%}")
        ok = False
    return 0 if ok else 1


def cmd_scenario(a) -> int:
    _, scns = scenario_set(True)
    s = next((x for x in scns if x["id"] == a.id), None)
    if s is None:
        print(f"unknown scenario {a.id}; known: {', '.join(x['id'] for x in scns)}")
        return 2
    ctrl = sil.SilController(sil.build()["path"]) if a.target == "sil" else AebModel()
    m = simulate(s, ctrl)["metrics"]
    print(json.dumps({"scenario": s, "metrics": m}, indent=2))
    return 0


def cmd_xosc(a) -> int:
    _, scns = scenario_set(True)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / ROAD_FILE).write_text(road_xodr(), encoding="utf-8")
    for s in scns:
        (out / f"{s['id']}.xosc").write_text(to_openscenario(s), encoding="utf-8")
    print(f"{len(scns)} OpenSCENARIO files and {ROAD_FILE} written to {out}")
    if a.validate:
        # ASAM schemas as shipped with the scenariogeneration package (pip install scenariogeneration)
        import scenariogeneration
        import xmlschema
        schemas = Path(scenariogeneration.__file__).resolve().parent.parent / "schemas"
        xosc = xmlschema.XMLSchema(str(schemas / "OpenSCENARIO_1_1.xsd"))
        xodr = xmlschema.XMLSchema(str(schemas / "opendrive_17_core.xsd"))
        xodr.validate(str(out / ROAD_FILE))
        for s in scns:
            xosc.validate(str(out / f"{s['id']}.xosc"))
        print(f"All files are valid against OpenSCENARIO 1.1 and OpenDRIVE 1.7 schemas ({schemas})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--mutants", action="store_true")
    c = sub.add_parser("campaign")
    c.add_argument("--target", default="sil", choices=["sil", "model", "auto"])
    c.add_argument("--mutation", action="store_true")
    c.add_argument("--no-bva", action="store_true", help="catalog only, without the boundary-value scenarios")
    c.add_argument("--out")
    c.add_argument("--min-mutation-score", type=float)
    c.add_argument("--calibration", help="JSON object of calibration changes")
    s = sub.add_parser("scenario")
    s.add_argument("id")
    s.add_argument("--target", default="model", choices=["sil", "model"])
    x = sub.add_parser("xosc")
    x.add_argument("--out", required=True)
    x.add_argument("--validate", action="store_true", help="check against the ASAM XSDs (needs scenariogeneration)")
    a = ap.parse_args()
    try:
        return {"build": cmd_build, "campaign": cmd_campaign, "scenario": cmd_scenario, "xosc": cmd_xosc}[a.cmd](a)
    except sil.SilUnavailable as e:
        print(f"SIL unavailable: {e}", file=sys.stderr)
        return 3
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
