# CarSaathi help search: evaluation

Labelled owner descriptions, all written by the developer, in plain English, Hinglish, abbreviations and typos: **Building set** 124 (queries.json); **Held-out 1** 50 (heldout.json); **Held-out 2** 36 (heldout2.json). The building set was used while writing the search. Held-out 1 was written afterwards in longer, less direct words; its first run exposed weaknesses that were fixed with general changes, not its own phrases. Held-out 2 was written after those fixes and never used for tuning. They catch regressions; they don't measure accuracy on real owners' words.

| Measure | Building set | gate | Held-out 1 | gate | Held-out 2 | gate |
|---|---|---|---|---|---|---|
| Right guide first | 99.0% (100) | 85.0% | 83.7% (43) | 70.0% | 90.3% (31) | 70.0% |
| Right guide in the top 3 | 100.0% | 95.0% | 97.7% | 85.0% | 100.0% | 85.0% |
| Hinglish, right guide first | 100.0% (21) |  | 100.0% (1) |  | 100.0% (4) |  |
| Urgent descriptions that get their safety card | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| Safety cards on descriptions that aren't urgent | 0 | 0 | 0 | 0 | 0 | 0 |
| Out-of-scope questions answered 'not sure' | 100.0% | 75.0% | 100.0% |  | 100.0% |  |
| Warning-light descriptions offered the right light | 100.0% | 80.0% | - |  | - |  |

Held-out 1, first run, before the general fixes: right guide first 69.8%, top 3 93.0%, safety cards 3 of 5.
Held-out 2, first run: right guide first 90.3%, top 3 100%, safety cards 2 of 3 and one false safety card. Afterwards only safety patterns changed (steam alone now counts as overheating; 'the automatic emergency brake didn't work' is no longer read as brake failure; and, from the unit tests, a sinking pedal and a stiff steering wheel); the ranking was not changed.

**All gates pass.**

## Misses

| Set | Description | Expected | Got | Why |
|---|---|---|---|---|
| Building set | my new iphone is not showing the car in bluetooth | bt-pair / - | carplay-start, bt-pair, call-contacts / - | wrong guide first |
| Held-out 1 | two phones paired and it always picks the other one | bt-reconnect / - | bt-pair, call-audio, bt-reconnect / - | wrong guide first |
| Held-out 1 | on long drives the phone connection cuts off and comes back after a minute | bt-drops / - | projection-drops, bt-drops, audio-usb / - | wrong guide first |
| Held-out 1 | phone says connected to car audio yet the songs come out of the phone speaker | audio-bt-music / - | audio-no-sound, bt-pair, audio-bt-music / - | wrong guide first |
| Held-out 1 | carplay keeps restarting itself mid route | projection-drops / - | screen-frozen, projection-drops, adas-lane / - | wrong guide first |
| Held-out 1 | when i say call mom the car calls someone else | voice / - | call-contacts, call-audio, bt-drops / - | wrong guide first |
| Held-out 1 | when I put the car in reverse the screen stays blank | camera-reverse / - | screen-frozen, camera-reverse, parking-sensors / - | wrong guide first |
| Held-out 1 | after I got the windshield changed the lane assist doesn't work anymore | adas-unavailable / - | adas-lane, adas-unavailable, voice / - | wrong guide first |
| Held-out 2 | audio from the phone keeps cutting in and out over bluetooth while driving | bt-drops / - | audio-bt-music, bt-drops, projection-drops / - | wrong guide first |
| Held-out 2 | touchscreen registers touches in the wrong place | screen-touch / - | nav-gps, screen-touch, screen-frozen / - | wrong guide first |
| Held-out 2 | Nexon ne expressway par bina wajah emergency brake lagaya | adas-phantom / - | adas-no-brake, adas-phantom, adas-acc / - | wrong guide first |
