"""Safety check on what a car owner types, before any troubleshooting is offered.

Some problems are not for a step-by-step guide: brakes that don't work, smoke, a fuel smell, a stuck accelerator. When
the description sounds like one of these, the app shows what to do right now (stop safely, get out, call 112) first.
The patterns run on the normalised text (Hinglish and abbreviations already mapped to English) and favour recall: a
false alarm costs a tap ("I'm safe, show help"), a missed one could cost much more. The labelled set in
eval/owner_help measures both."""
from __future__ import annotations

import re

CALL_112 = {"label": "Emergency", "number": "112"}
CALL_NHAI = {"label": "National highway helpline (NHAI)", "number": "1033"}

# (id, level, pattern, negatable, title, what to do now). level "stop": stop driving now; "accident": someone may be hurt
RULES = (
    ("fire", "stop", r"\b(fire|flames?|smoke|smoking|burning smell|smell of burning|smells? (like )?burn\w*|burnt smell|sparks?)\b", True,
     "Smoke or a burning smell: stop and get out",
     ["Pull over where it's safe and switch the engine off.", "Get everyone out and move well away from the car and the traffic.",
      "Call 112. Don't open the bonnet if you see smoke or flames.", "Don't restart the car: call roadside assistance."]),
    ("fuel", "stop", r"\b(petrol|diesel|fuel|cng|gas)\b.{0,15}\b(leak\w*|smell\w*|dripping|drip\w*)\b|\b(smell\w*|leak\w*|dripping) (of )?(petrol|diesel|fuel|cng|gas)\b", True,
     "Fuel smell or leak: stop and switch off",
     ["Stop safely and switch the engine off.", "No smoking or flames nearby, and don't try to start the car.",
      "Get everyone out and away from the car.", "Call roadside assistance; call 112 if fuel is pooling or you see smoke."]),
    ("brakes", "stop", r"\bbrakes? (are |is )?(not working|failed|fail\w*|gone|very soft|soft|spongy|sinking)\b|\bbrake pedal\b.{0,20}\b(goes|went|sinks?|sank|soft|floor|spongy)\b|"
                       r"\bno brakes?\b|\bbrakes? (not|did not|do not) (work|stop)\w*|\bbrake failure\b|\bbrakes? not stopping\b|"
                       r"\bbrak\w*\b.{0,40}\b(all the way (down|to the floor)|to the floor|not slow\w*|without slowing|no effect|not stopping)\b|"
                       r"(?<!clutch )\bpedal\b.{0,20}\b(to the floor|sank|sinks|sinking|no resistance|spongy)\b", False,
     "Brakes not working properly: stop as safely as you can",
     ["Pump the brake pedal hard, and shift to a lower gear to slow down with the engine.",
      "Pull the parking brake gently and steadily, switch on the hazard lights and steer to a safe place.",
      "Once stopped, don't drive on: call roadside assistance."]),
    ("steering", "stop", r"\bsteering\b.{0,25}\b(locked|jammed|stuck|stiff|hard|heavy|not turning|failed|won'?t turn)\b|"
                         r"\bcan ?not (turn|steer) the (wheel|car|steering)\b", False,
     "Steering problem: stop safely",
     ["Hold the wheel firmly with both hands and slow down gently.", "Switch on the hazard lights and stop in a safe place.",
      "Don't drive on: call roadside assistance."]),
    ("overheat", "stop", r"\boverheat\w*|\bengine (is )?(very |too )?hot\b|\bsteam(ing)?\b|"
                         r"\btemperature (light|warning|gauge|needle)\b.{0,25}\b(red|high|on|full|max\w*|rising|going up|goes up|climbing)\b", True,
     "Engine overheating: stop and let it cool",
     ["Stop safely and switch the engine off.", "Don't open the radiator or coolant cap while the engine is hot.",
      "Wait at least 30 minutes; call roadside assistance if the temperature light comes back."]),
    ("oil", "stop", r"\b(red )?oil (pressure )?(light|warning|lamp|symbol)\b|\boil can (light|symbol)\b|\boil pressure\b", False,
     "Oil pressure warning: stop the engine",
     ["Stop safely and switch the engine off at once.", "Check the oil level once the engine has cooled.",
      "Don't drive with the light on: call roadside assistance."]),
    ("acceleration", "stop", r"\b(accelerat\w*|speeds? up|speeding up|revving|racing)\b.{0,25}\b(on its own|by itself|itself|without (me|pressing|touching))\b|"
                             r"\b(accelerator|throttle|gas pedal)\b.{0,10}\b(stuck|jammed)\b", False,
     "Car speeding up by itself: brake and shift to neutral",
     ["Press the brake firmly and keep it pressed; don't pump it.", "Shift to neutral (N), steer to a safe place and stop.",
      "Switch the engine off once stopped, and don't drive on: call roadside assistance."]),
    ("wheel", "stop", r"\b(tyre|tire)s? (burst|blew|blow ?out|exploded)\b|\bwheel (is )?(wobbl\w*|loose|came off|coming off|shaking badly)\b", False,
     "Tyre or wheel problem: stop safely",
     ["Hold the wheel firmly and don't brake hard.", "Slow down gradually and pull over in a safe place, away from traffic.",
      "Change the wheel only where it's safe, or call roadside assistance."]),
    ("ev-battery", "stop", r"\b(high voltage|hv|ev|traction|electric) battery\b.{0,25}\b(warning|fault|hot|overheat\w*|smoke|smell|hiss\w*|swell\w*)\b|"
                           r"\bbattery (is )?(smoking|swollen|swelling|hissing|on fire|bulging)\b", False,
     "Electric car battery problem: get out",
     ["Stop safely and switch the car off.", "If there is smoke, hissing or a burning smell, get everyone out, move well away and call 112.",
      "Don't touch the orange high-voltage cables; call roadside assistance."]),
    ("accident", "accident", r"\b(accident|collided|crashed into|rear[- ]ended|injur\w*|hurt|bleeding|ambulance)\b|"
                             r"\bhit (a |an |the |another |my )?(\w+ )?(car|bike|truck|wall|pole|divider|person|vehicle|scooter|auto|bus|tree|man|woman|child|kid|"
                             r"pedestrian|someone|somebody|cyclist|motorcycle|biker|cow|dog)s?\b", False,
     "If anyone is hurt, call 112",
     ["Switch on the hazard lights and keep everyone away from traffic.", "Call 112 if anyone is hurt or the road is blocked.",
      "Take photos and inform your insurer. You can report what the car did below."]),
)
_COMPILED = [(rid, level, re.compile(p), neg, title, todo) for rid, level, p, neg, title, todo in RULES]
_NEG = re.compile(r"\b(no|not|without|never|zero)\s+(\w+\s+){0,2}$")


def check(normalised: str, roadside: str = "") -> dict | None:
    """The most serious matching card, or None. normalised: lower-case English text (helpsearch.normalise)."""
    t = normalised
    if "cruise" in t:                                       # adaptive cruise speeding up to the set speed is normal
        t = re.sub(r"\b(speeds? up|speeding up|accelerat\w*)\b", "", t)
    # "the automatic emergency brake didn't work" is about the driver-assistance system (a guide and a report), not brake failure
    t = re.sub(r"\b(automatic emergency|auto emergency|automatic|auto|emergency|collision) brak\w*", "aebsystem", t)
    hits = []
    for rid, level, rx, negatable, title, todo in _COMPILED:
        for m in rx.finditer(t):
            if negatable and _NEG.search(t[max(0, m.start() - 25):m.start()]):
                continue
            hits.append((0 if level == "stop" else 1, rid, level, title, todo))
            break
    if not hits:
        return None
    _, rid, level, title, todo = min(hits)
    calls = [CALL_112] + ([{"label": "Your roadside assistance", "number": roadside}] if roadside else []) + [CALL_NHAI]
    return {"id": rid, "level": level, "title": title, "todo": todo, "call": calls, "also": [h[1] for h in sorted(hits)[1:]]}
