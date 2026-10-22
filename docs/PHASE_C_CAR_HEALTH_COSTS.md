# Phase C: car health and running costs in CarSaathi

Phases A and B covered the car's papers and what to do when something goes wrong. Phase C adds the two things owners
keep asking each other about: *what is this car really costing me?* and *is it healthy?* Each car gets two new tabs.

**Costs.** Log each fill or charge with the odometer reading, plus service bills, tyres, tolls, FASTag, parking,
challans and the rest. CarSaathi works out:
- the real mileage, fill to fill, and the cost per km;
- the spend by month and by kind of cost;
- how far the car is driven each day.

A sudden drop in mileage is pointed out, and km-based reminders get a date: "tyre rotation in 1,190 km, about 19 Nov".

**Health.** Plug a ₹500 to ₹1,500 Bluetooth OBD-II adapter into the port under the dashboard. The phone reads the car's
own diagnostics:
- the engine warning light and stored, pending and permanent fault codes;
- battery or charging voltage;
- engine temperature;
- the km driven with the light on.

Each fault code is explained in plain language: what it means, whether it's safe to drive, and common causes. Owners
without an adapter type in the codes a workshop read for them. One status per car, *Good*, *Check soon* or *Needs
attention*, lists every reason with a next step. It combines:
- the fault codes and battery and engine readings;
- the mileage trend;
- overdue services;
- expired insurance or PUC.

```
Costs   fill / charge  ──> full-to-full mileage, cost per km, drop check ─┐
        expense (+bill) ──> spend by month and kind, CSV                    ├─> Health status + reasons + what to do
        odometer readings ──> km per day ──> dates for km reminders        │
Health  OBD-II adapter ──BLE──> phone sends 12 commands, posts raw answers ─┤   (Good / Check soon / Needs attention)
        (or demo adapter, or codes typed in)    server decodes + explains ─┘
```

## For the owner

| | |
|---|---|
| **A fill in 10 seconds** | Date, odometer (filled in from the last reading), litres or kWh, rupees paid. The price per litre is worked out as they type, and anything implausible is refused: ₹3 a litre, 400 litres, an odometer lower than the last fill. CNG cars log both kg of CNG and litres of petrol. |
| **Real mileage** | Mileage is worked out the full-to-full way, as fuel-economy tests do. Between two fills to the brim (or two charges to the limit), the distance is divided by everything added after the first one, part fills included. A part fill on its own never produces a misleading number. |
| **Spend** | This month, the last 12 months, cost per km for fuel and expenses together, a month-by-month chart (fuel against other costs) and a breakdown by kind of cost. *Download all entries (CSV)* for reimbursement or tax records. |
| **Mileage drop** | When the latest mileage is more than 15% below the usual (the median of the five before), the car's Costs and Health tabs say so, with likely causes. |
| **Predicted dates** | From the odometer history (every reading from fills, expenses and updates), CarSaathi knows the car's km per day over the last 120 days. A reminder at 25,500 km becomes "about 19 Nov", and turns *due soon* when that date is within 30 days, not only when the km are close. |
| **Health check with an adapter** | In the Android app or Chrome on a phone or laptop: *Connect adapter*, choose it in the list, and within about half a minute the codes, battery and temperature are read and explained. iPhones have no Web Bluetooth, so the owner types in the codes instead. *Try the demo adapter* shows the whole flow without hardware. Reading never changes anything in the car: CarSaathi never clears codes. |
| **Fault codes in plain words** | 95 common generic codes (P0171 *Engine running too lean*, P0420, P0300 misfires, P0457 *fuel cap loose*, U0100 and more), each with what it means, whether to drive (*OK to drive* / *get it checked soon* / *don't drive*) and common causes. Other codes are explained by their family, and manufacturer-specific ones are flagged as needing the service centre's tool. |
| **One status** | *Good*, *Check soon* or *Needs attention*, with every reason and its next step: what a code means, a Phase B guide (for example *Car won't start* for a low battery), the warning-light card, the reminders, the documents, or the costs. It is deliberately not a score out of 100: the inputs don't support that precision. |

## How it works

**Exact money.** Amounts are stored in paise and quantities in thousandths (ml, g, Wh). Totals are exact, and floats appear
only in results.

**Odometer history** (`owner/odometer.py`). Every reading is kept, with its source: typed in, a fill, or an expense. A
reading never lowers the car's odometer; only an owner editing the car can correct it. km per day uses the last 120 days,
needs readings at least 7 days and 50 km apart, and otherwise stays unknown rather than guessed.

**OBD-II, decoded on the server** (`owner/obd.py`). The phone is a pipe. It opens a Bluetooth LE link to the ELM327 adapter
and sets the adapter up (`ATZ`, `ATE0`, `ATL0`, `ATS1`, `ATH0`, `ATSP0`). It then sends twelve read-only commands and posts
the raw text answers:
- `ATI`, `ATRV` (battery voltage at the port, even with the engine off), `ATDPN` (protocol);
- mode 01 PIDs 01 (engine light and code count), 05 (coolant), 0C (rpm), 42 (module voltage), 21 and 31 (km with the light on, km since codes were cleared);
- modes 03, 07 and 0A (stored, pending and permanent codes).

The server decodes them, so one tested decoder serves the Android app, Chrome and the demo adapter. It handles:
- echo on or off, spaces or none, `SEARCHING...` and `BUS INIT`;
- several control units answering;
- CAN multi-frame answers (`00A` / `0:` / `1:`, put back together and cut to length);
- the older protocols' frames of three code slots padded with zeros;
- every ELM327 error message.

An answer that can't be read leaves only that value unknown. The raw answers are kept with the reading, so a strange
result can be checked later.

**The phone side** (`owner-app/src/lib/obd.js`). One small ELM327 driver sends a command and collects the answer up to the
`>` prompt. Answers that arrive in many small BLE packets are put back together. A command with no answer becomes NO DATA,
and its late answer is not handed to the next command (tested with Node's test runner).

The link is the Capacitor Bluetooth LE plugin. In the Android app it uses the phone's Bluetooth; in Chrome the same
plugin uses Web Bluetooth. It is loaded only when someone taps *Connect adapter*. It looks for the GATT services common
ELM327 BLE adapters use (FFF0, FFE0 and 18F0), with a fallback to any service that has a notify and a write
characteristic. The Android build gets the plugin and its Bluetooth permissions from `android-app/package.json`, which
also starts a new APK build.

**Health rules** (`owner/health.py`). Each finding has a level, and the car's status is the worst one:

| Input | Red (needs attention) | Amber (check soon) | Info |
|---|---|---|---|
| Stored code | "don't drive" codes (P0524 low oil pressure, P0217 overheating, P0093 fuel leak) | every other stored code | pending codes (seen once) |
| Battery, engine off | below 12.0 V | 12.0 to 12.4 V | |
| Battery, engine running | below 13.2 V (not charging) | above 15.2 V | |
| Coolant, engine running | 113 °C and above | 106 to 112 °C | |
| Mileage | | latest more than 15% below usual | |
| Reminders | | overdue | due soon |
| Papers | insurance expired | PUC expired | |
| Health check | | engine light on but no code given | none yet, or older than 60 days |

**Database.** Schema 3 adds `fuel_logs`, `expenses` (with `expense_files`), `odometer_readings` and `health_scans`. All of
them cascade from the car. An existing database is migrated at start-up, and each car's history starts with its current
odometer reading.

## Measured and tested

- Mileage, km per day and predicted dates against hand calculations, including a part fill between two full ones, unordered
  entries and repeated readings.
- The decoder on recorded answers: CAN with multi-frame codes; ISO 9141 with zero padding; echo and no spaces; two control
  units answering; the car not answering; an impossible voltage; unknown commands and oversized answers.
- The fault-code list: every code is a valid code, every drive level is known, and every linked guide exists.
- The battery and coolant thresholds, the rules that make up the status, and the demo account's story: P0171, a 12.3 V
  battery and a 17% mileage drop give *Check soon*.
- Fills: price and quantity limits, the odometer staying in date order, CNG cars logging kg and litres, free EV charging.
- Expenses: receipts served sandboxed, the CSV export (a cell starting with `=` is written as text), permissions (drivers
  log, only owners or the person who added an entry delete it, others get *not found*), the data export, account deletion,
  and the migration from schema 2.
- 22 Python tests (on SQLite and PostgreSQL) and 4 Node tests for the Bluetooth driver.

## Demo script (4 minutes)

1. **Home** (20 s): the demo Nexon shows *Health: check soon, P0171: Engine running too lean*.
2. **Health tab** (1 min): the reasons, worst first. *What this means* on P0171 gives the plain explanation, *get it checked
   soon* and the likely causes. The battery at 12.3 V links to the *Car won't start* guide. The mileage drop and the expired
   PUC are listed too.
3. **Check with an OBD adapter** (1 min): explain the ₹500 adapter and the port. Run *Try the demo adapter*: it sends the
   same twelve commands a real adapter gets, and the server decodes the raw answers. Then *Enter fault codes* with
   "P0420 P0457" to show the dictionary and the fallback for an unknown code.
4. **Costs tab** (1.5 min): this month, 12 months, ₹ per km, mileage 12.6 km/l against a usual 15.2. Show the
   month-by-month chart (tap a bar) and the mileage line. *Add a fill* at 24,705 km with 30.2 L shows the mileage recovering
   to 17 km/l and the reminder dates moving. Download the CSV.
5. **The engineering connection** (20 s): the same diagnostic protocols (OBD-II modes and PIDs, ISO 15765 CAN) that the
   platform's UDS module (Stage 3) tests are what the owner app reads in the field.

## Limits

- **Real adapters:** not tested here. The Bluetooth link is written against the ELM327 command set and the Capacitor
  plugin's documented API, and tested with recorded answers, the demo adapter and Node tests. The Android APK with the
  plugin is built by CI, not here.
- **Adapter compatibility:** cheap ELM327 clones vary. Some only speak classic Bluetooth (SPP), which phones can't use from
  a web page or this plugin; owners need a **Bluetooth LE** adapter.
- **iPhone:** there is no Web Bluetooth on iPhone, so owners type codes in there.
- **Fault codes:** the list covers generic codes; manufacturer-specific meanings need the service centre's tool. A code
  points to where to look; a workshop confirms the cause.
- **Battery thresholds** are for 12 V lead-acid batteries: an EV's traction battery isn't read over OBD-II this way.
- **Mileage depends on full fills:** owners who rarely fill to the brim get fewer mileage points (the app says so instead
  of guessing).
- Push notifications and email reminders are a later phase.
