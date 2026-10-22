"""Software builds in the field: which AEB firmware and calibration a vehicle runs, and controllers that behave like
that build.

  firmware      "reference" is the repository's C code (firmware/aeb). For demonstration and evaluation a build may
                instead name one of the predefined Phase 4 mutants (e.g. "M04") to stand for a build that shipped with
                a coding defect; only these fixed mutations are ever compiled, never code supplied by a user
  calibration   overrides of the AEB calibration (the same validated parameters as the ADAS what-ifs)
  validated     whether the build was released on validation evidence, and which

Controllers: the C code of the build in the loop (SIL) when a compiler or prebuilt library is available, otherwise
the Python reference model with the build's calibration (which cannot show coding defects; reported as such)."""
from __future__ import annotations

import re

from app.modules.adas import campaign as cmp
from app.modules.adas import sil
from app.modules.adas.model import CAL, AebModel, Calibration, calibration_changes, make_calibration

VERSION_RE = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(-[a-z0-9]{1,12})?$")
FIRMWARE = ("reference",) + tuple(m["id"] for m in cmp.MUTANTS)


def validate(b: dict) -> dict:
    """Normalise a build definition; raises ValueError."""
    if not isinstance(b, dict):
        raise ValueError("build must be an object")
    v = str(b.get("version", "")).strip()
    if not VERSION_RE.match(v):
        raise ValueError("version must look like 2.4.0 or 2.4.1-pilot")
    fw = str(b.get("firmware", "reference"))
    if fw not in FIRMWARE:
        raise ValueError(f"firmware must be 'reference' or one of the predefined defect variants {list(FIRMWARE[1:])}")
    cal = b.get("calibration") or {}
    if not isinstance(cal, dict):
        raise ValueError("calibration must map parameters to numbers")
    make_calibration(cal)                                   # bounds and consistency, raises ValueError
    veh = b.get("vehicles", 0)
    if isinstance(veh, bool) or not isinstance(veh, int) or not 0 <= veh <= 10_000_000:
        raise ValueError("vehicles must be a whole number 0-10,000,000")
    prev = b.get("previous")
    if prev is not None and not VERSION_RE.match(str(prev)):
        raise ValueError("previous must be a version")
    text = lambda k, n: " ".join(str(b.get(k) or "").split())[:n]  # noqa: E731
    return {"version": v, "ecu": text("ecu", 20) or "AEB", "firmware": fw, "calibration": {k: cal[k] for k in sorted(cal)},
            "vehicles": veh, "released_at": text("released_at", 30), "validated": bool(b.get("validated", False)),
            "validation_evidence": text("validation_evidence", 300), "previous": str(prev) if prev else None,
            "notes": text("notes", 500)}


def calibration(build: dict) -> Calibration:
    return make_calibration(build.get("calibration") or {})


def signature(build: dict) -> str:
    return f"{build['firmware']}|{sorted((build.get('calibration') or {}).items())}"


def describe(build: dict) -> str:
    ch = calibration_changes(calibration(build))
    cal = ", ".join(f"{k} {v['value']:g}" for k, v in ch.items()) or "default calibration"
    fw = "reference firmware" if build["firmware"] == "reference" else f"firmware variant {build['firmware']}"
    return f"AEB {build['version']} ({fw}, {cal})"


class NoAeb:
    """Counterfactual controller: the vehicle without AEB (only the driver's own braking)."""
    name = "no-aeb"

    def reset(self) -> None:
        pass

    def step(self, ego_speed, obj_valid, distance, closing, driver_brake):
        return {"brake_req": driver_brake, "aeb_req": 0.0, "ttc": 99.0, "a_req": 0.0, "a_tgt": 0.0, "fcw": False, "state": 0}


_SIL_CACHE: dict[str, str] = {}


def _lib(firmware: str) -> str:
    if firmware not in _SIL_CACHE:
        c_text, _ = sil.sources()
        if firmware != "reference":
            c_text = cmp.mutate(c_text, next(m for m in cmp.MUTANTS if m["id"] == firmware))
        _SIL_CACHE[firmware] = sil.build(c_text)["path"]
    return _SIL_CACHE[firmware]


def controller(build: dict, prefer: str = "sil", cal: Calibration | None = None):
    """-> (controller, kind) where kind is 'sil' or 'model'. cal overrides the build's calibration (fix candidates)."""
    cal = cal if cal is not None else calibration(build)
    if prefer == "sil":
        try:
            return sil.SilController(_lib(build["firmware"]), None if cal == CAL else cal), "sil"
        except (sil.SilUnavailable, OSError):
            pass
    return AebModel(cal), "model"


def model(cal: Calibration):
    return AebModel(cal)


def sil_available() -> bool:
    try:
        _lib("reference")
        return True
    except (sil.SilUnavailable, OSError):
        return False
