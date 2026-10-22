"""Owner reports to the engineering platform (Phase B -> Phase 6).

When an owner says the car braked with nothing ahead, warned for no reason, or didn't brake, that is a field complaint the
Phase 6 field-issue investigator can work on. With the owner's explicit consent for that one report, the owner service
sends an anonymised complaint to the platform's API (POST /projects/{project}/field/complaints/import), where it is grouped
with dealer complaints and investigated automatically.

What is sent (and shown to the owner before they agree): the car's make, model, year and fuel; the odometer rounded to
1,000 km; the date; what the car did, where and how fast (from the owner's answers); the steps already tried; the VIN
and the software version only if the owner adds them. Never the name, email, phone number, number plate or the owner's
free text. The text is written so the platform's complaint rules read the right symptom, place and speed (checked by
the tests against the Phase 6 rules).

Delivery: OWNER_FIELD_LIVE=0 (default) prepares the report and stops (dry run). Live: sent at once; failures are retried in
the background (every 5 minutes, at most 8 tries), then marked failed."""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

log = logging.getLogger("owner.bridge")
VERSION_RE = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(-[a-z0-9]{1,12})?$")      # the platform's build format
MAX_TRIES = 8
WHAT = {"unexpected_braking": "The car braked hard on its own with nothing ahead.",
        "false_warning": "The forward collision warning came on with nothing ahead; the car did not slow down.",
        "no_braking": "The car did not brake automatically before the obstacle ahead."}
WHERE = {"flyover": "on a flyover", "bridge": "on a bridge", "gantry": "under a sign gantry", "toll": "at a toll plaza",
         "tunnel": "in a tunnel", "highway": "on a highway", "expressway": "on an expressway", "city": "in city traffic",
         "parking": "in a parking area"}
TRIED = {("adas-phantom", "s4"): "cleaned the camera and radar sensor area", ("adas-phantom", "s5"): "checked the forward alert distance setting",
         ("adas-no-brake", "s3"): "checked the driver-assistance settings and messages", ("adas-no-brake", "s4"): "cleaned the camera and radar sensor area"}


class BridgeError(ValueError):
    pass


def compose(guide: dict, car: dict | None, facts: dict, transcript: list[dict], *, happened_on: str, speed_kmh: int | None,
            sw_version: str, include_vin: bool) -> dict:
    """-> the complaint item exactly as it will be sent."""
    symptom = facts.get("symptom") or guide["report"]["default_symptom"]
    if symptom not in WHAT:
        raise BridgeError("This kind of problem can't be reported to the manufacturer")
    if speed_kmh is not None and not 0 < speed_kmh <= 200:
        raise BridgeError("Speed must be between 1 and 200 km/h")
    sw_version = (sw_version or "").strip()
    if sw_version and not VERSION_RE.match(sw_version):
        raise BridgeError("Software version must look like 2.4.0")
    parts = ["Owner report from the CarSaathi app."]
    if car:
        odo = f", about {round(car['odometer_km'] / 1000) * 1000:,} km" if car.get("odometer_km") else ""
        parts.append(f"Car: {car['make']} {car['model']}{' ' + str(car['year']) if car.get('year') else ''}, {car['fuel']}{odo}.")
    parts.append(WHAT[symptom])
    # worded for the platform's complaint rules, which have no negation: "no crash" would read as a crash
    if facts.get("crash"):
        parts.append("The car hit the vehicle or object ahead." if symptom == "no_braking" else "There was a collision afterwards.")
    loc = facts.get("location")
    if loc in WHERE:
        parts.append(f"It happened {WHERE[loc]}.")
    if speed_kmh:
        parts.append(f"Speed was about {speed_kmh} km/h.")
    tried = [TRIED[(guide["id"], t.get("step"))] for t in transcript
             if (guide["id"], t.get("step")) in TRIED and t.get("answer") in ("done", "not_fixed")]
    if tried:
        parts.append("Already tried: " + "; ".join(dict.fromkeys(tried)) + ".")
    vin = (car or {}).get("vin", "") if include_vin else ""
    return {"text": " ".join(parts), "vin": vin, "build": sw_version, "reported_at": happened_on, "dealer": "CarSaathi owner app"}


def send(settings, item: dict) -> tuple[bool, str, str]:
    """-> (delivered, remote complaint ref, error). Never raises."""
    url = f"{settings.field_api_url}/projects/{settings.field_project}/field/complaints/import"
    headers = {"Content-Type": "application/json", "Accept": "application/json", "X-User": "carsaathi"}
    if settings.field_token:
        headers["Authorization"] = f"Bearer {settings.field_token}"
    req = urllib.request.Request(url, data=json.dumps({"complaints": [item], "investigate": True}).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:                     # noqa: S310 - configured URL
            body = json.loads(r.read() or b"{}")
        refs = body.get("complaints") or []
        return True, str(refs[0])[:40] if refs else "", ""
    except urllib.error.HTTPError as e:
        return False, "", f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        return False, "", type(e).__name__
