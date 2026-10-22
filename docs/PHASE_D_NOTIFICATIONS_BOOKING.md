# Phase D: notifications and service booking in CarSaathi

Phases A to C keep the car's papers, reminders, health and costs in one place, but the owner still has to open the app
to find out that the PUC expired last week. Phase D brings what's due to the owner, and turns "the service is due" into a
booked slot at a workshop, with the bill landing back in the car's costs.

**Notifications.** Every morning CarSaathi works out what each person should hear about and tells them once:
- papers about to expire;
- services due by date, or by the day the car's km will reach them;
- a health check that needs attention now;
- every reply from a workshop.

Everything lands in an in-app inbox. Where the person allows it, it also arrives as a daily email digest and as a
notification on their phone or laptop, even when the app is closed.

**Service booking.** From a due reminder, a fault code or a help request, the owner picks what needs doing, a workshop and
up to three days that suit them. CarSaathi then makes a private link for the workshop and sends it on WhatsApp or SMS. The
workshop needs no app or account. On its own page it confirms a time or offers another, then sends an estimate, marks the
car received and ready, or says it can't take the job. Each step reaches the owner at once. When the owner marks the
service done:
- the bill becomes a service expense;
- the odometer reading is recorded;
- the reminders for the work done move to their next date and km.

```
daily run (09:00 IST, GitHub Actions) ──> what is due, per person ──> inbox ──┬─> email digest (Brevo / SMTP, one-click unsubscribe)
  papers 30/7/1 days, on the day, weekly      (each notification once)          ├─> Web Push to browsers (RFC 8291, VAPID)
  reminders 7/1 days (date or km per day)                                       └─> Android app: the coming weeks scheduled on the phone
  health check red, booking tomorrow
owner: book ──> requested ──link (WhatsApp, SMS)──> workshop page /w#token ──> confirmed | proposed ──> received ──> ready
                                                    (no account)          each step ──> inbox + email + push at once
owner: service done ──> bill to Costs, km recorded, reminders roll forward
```

## For the owner

| | |
|---|---|
| **Inbox** | The top bar shows how many are unread. Each notification opens the right page: the car's Documents, Reminders or Health tab, or the booking. *Mark all read*. |
| **When** | Papers: 30, 7 and 1 day before they expire, on the day, then weekly for four weeks. Reminders: 7 and 1 day before, on the day, then weekly. A km-based reminder gets its date from the car's km per day (Phase C); a car without enough odometer history hears at 500 km before, and when the km are passed. A health check with a red finding (a "don't drive" fault code, a battery not charging, overheating) is announced once per check. A confirmed booking is reminded the day before. |
| **Settings** | Profile, *Notifications*: what (reminders, documents, health, bookings) and where (email, devices). *This browser* or *This phone* turns notifications on for the device in hand; the list of devices shows where they're on, with *Remove*. *Send me a test* goes to every channel that's on. |
| **Email** | One digest per day at most, plus booking updates. Each has a one-click unsubscribe link (RFC 8058), which mail apps can offer as their own *Unsubscribe* button. Demo accounts never get emails. |
| **Book a service** | *Book* on a due reminder, *Book a check* on a fault code in Health, *Book a visit* on an open help request, or the car's new *Service* tab. Due reminders are already ticked; fault codes go with their plain explanation for the mechanic. Add your own words ("AC not cooling well"), the workshop (saved for next time), up to three days with morning or afternoon, pick-up, and the number the workshop should call. |
| **Send to the workshop** | *Make the link*: *Send on WhatsApp* (for an Indian mobile number), *SMS*, *Share* or *Copy message*. Before sending, the owner sees exactly what the workshop will see. A new link replaces the old one; *Turn it off* ends it. |
| **Follow it** | The booking page shows the status, the job card and every update. *Accept* an offered time or *choose other days*. *Add to calendar* (an .ics file with a 2-hour alarm), *I've left the car there*, *Service done*. |
| **Service done** | The bill (prefilled from the workshop's) becomes a service expense from that workshop. The km are recorded, and the reminders that were booked are marked done; repeating ones move to their next date and km. |
| **Family** | Everyone sharing the car sees its bookings. When one of them changes a booking, or the workshop answers, the others are told too. |

## For the workshop

The link opens a page made for a service advisor's phone: the number plate, the car and km, the numbered job card (fault
codes with their explanation and likely causes), the preferred days, pick-up and the owner's notes. From there the workshop
can:
- **Confirm a time.** A time inside a preferred slot confirms at once; any other date or time goes to the owner as an offer.
- **Send an estimate.**
- **Send a message.**
- **Mark the car received.**
- **Mark the car ready**, with the bill.
- **Decline.**

The page states what happens to each answer ("Demo gets this as confirmed" or "goes as an offer they accept").

## How it works

**Each notification once** (`owner/notify.py`). A notification's key names the item, its due date or km, and the stage
(`doc:<id>:<expiry>:d7`, `rem:<id>:<date>|<km>:late1`). The database keeps the key unique per person. Starting the daily
run twice, or on two servers at once, never repeats one. After an edit (a new expiry date, a moved reminder), the item gets
new keys and is announced again. A daily run that was missed doesn't flood the owner: only the furthest stage reached is
made. Delivery is recorded per notification (sent, off, failed). One person's failure never stops the run for the others.

**The daily run** has to be started from outside, because a free Render instance sleeps. Two ways start it:
- `.github/workflows/owner-reminders.yml` calls `POST /api/notify/run` at 03:30 UTC (09:00 IST), retrying while the
  instance wakes up;
- `python -m owner.notify run` (or `make owner-notify`) from any cron.

The endpoint answers *not found* without `OWNER_CRON_TOKEN`, compared in constant time. The run also deletes expired demo
accounts.

**Web Push without a vendor SDK** (`owner/webpush.py`). The browser subscribes with the server's VAPID public key and gives
an endpoint on its vendor's push service. The server then:
- encrypts each message for that browser only (RFC 8291: ECDH on P-256, HKDF-SHA256, AES-128-GCM in the RFC 8188 `aes128gcm` coding);
- signs a short-lived ES256 token (RFC 8292);
- POSTs the message to the endpoint.

The push service passes on a message it can't read; even its `Topic` header is a hash. The endpoint comes from the client,
so it must be HTTPS on a known push service (Google, Mozilla, Apple, Microsoft); anything else is refused, which closes the
server-side request forgery route. A 404 or 410 from the push service forgets the subscription. Three or more notifications
at once become one summary push. The service worker shows the push and opens the right page when it's tapped.

**The Android app** is a WebView, which can't receive Web Push. Each time it opens, it fetches the date-based notifications
of the next 45 days (`GET /api/notifications/schedule`) and replaces its schedule on the phone with them, at 9:00 India time.
It uses Capacitor Local Notifications, whose plugin restores them after a restart and shows a white car in the status bar.
Booking updates reach Android users by email and in the inbox.

**Booking** (`owner/booking.py`). What needs doing is rebuilt on the server from the car's own records (reminder titles,
fault-code explanations, help requests), never taken from the client's text. The rules:

| Rule | Value |
|---|---|
| Open bookings per car | at most 3 |
| Jobs per booking | 1 to 12 |
| Preferred days | 1 to 3, from today to 60 days ahead |
| Confirmed times | 7:00 to 21:00, up to 90 days ahead |

The statuses are requested, proposed, confirmed, at the workshop, ready, done, cancelled and declined. Each side can only
take the steps its current status allows.

**The workshop link is a capability.**
- The token is random (192 bits), and only its SHA-256 is stored.
- It travels in the URL fragment (`/w#…`), which browsers never send to a server, so it stays out of access logs and
  `Referer` headers. The page sends it in an `X-Booking-Token` header.
- It works for 30 days and can be turned off or replaced at any time. When the service is done or the workshop declines,
  it stays read-only for 7 days; cancelling ends it at once.
- The workshop sees only that booking: car, jobs, days, the owner's first name and the number chosen for the booking.
  No email address, documents or other cars.
- Its answers are rate-limited per address and per booking.

**Calendar.** The .ics file follows RFC 5545: escaped text, lines folded at 75 bytes without splitting a character, the
time converted from India time to UTC.

**Database.** Schema 4 adds `notification_prefs`, `push_subscriptions`, `notifications`, `workshops`, `bookings` and
`booking_events`. They are new tables only, so an existing database upgrades at start-up with nothing to rewrite. The data
export includes the inbox, settings, devices, workshops and bookings. Deleting an account removes them, and clears the
name, phone and link from bookings left on a co-owner's car.

## Settings

| Setting | What it does |
|---|---|
| `OWNER_PUBLIC_URL` | The site's address in emails and in the workshop link. On Render it defaults to the service's own address (`RENDER_EXTERNAL_URL`). |
| `OWNER_CRON_TOKEN` | The token of the daily run (32 characters or more). Render generates it; copy it into the GitHub secret of the same name, and set the repository variable `OWNER_APP_URL`. |
| `OWNER_VAPID_PUBLIC_KEY`, `OWNER_VAPID_PRIVATE_KEY` | The Web Push key pair from `python -m owner.notify vapid` (`make owner-vapid`). Set it once: a new pair makes every browser turn notifications on again. Development makes its own pair in `DATA_DIR`. |
| `OWNER_VAPID_SUBJECT` | `mailto:` or `https:` contact for the push services. |
| `OWNER_REMINDER_EMAILS` | `0` turns off the daily digest; booking emails still go. |

Without the VAPID keys, email and the inbox still work. Without the cron token, nothing starts the daily run, and the
startup check says so.

## Measured and tested

- **Push encryption:**
  - byte for byte against the worked example in RFC 8291, Appendix A;
  - decrypted by the independent `http_ece` library;
  - the VAPID signature verified against the public key.

  Endpoints off the known push services are refused, as are malformed keys.
- **The daily run:**
  - each stage made once, even when run twice;
  - the email digest, with names escaped in HTML;
  - one-click unsubscribe;
  - no emails for demo accounts;
  - pushes to a stand-in push service, decrypted, with summary pushes and forgotten subscriptions after 410;
  - the Android schedule;
  - the token on the run endpoint;
  - the test notification's limit.
- **Booking:**
  - the whole flow, from book to link to confirm, estimate, received and ready, then done, with the bill in Costs and the
    reminder moved on;
  - an offered time accepted by the owner;
  - limits and access: another account gets *not found*, a stale or wrong link is refused, steps out of order are refused;
  - a driver booking, with co-members told;
  - the calendar file;
  - the workshop's view holding no account data;
  - export and erasure;
  - the upgrade from a Phase C database.
- **Web app (Node tests):** the phone's schedule planner, the VAPID key conversion and the device names.
- **In a browser (Playwright, Chromium):**
  - the inbox and its badge;
  - the settings;
  - booking from a fault code;
  - the workshop's page in a separate browser confirming, estimating, receiving and marking ready;
  - the owner completing the service, and the bill appearing in Costs;
  - the expired-link page, in light and dark.
- **Android:** the Android project generated with both plugins (Bluetooth LE and local notifications), and the branding
  script adding the status-bar icon.

## Demo script (5 minutes)

1. **Home** (30 s). The inbox badge shows 2: *PUC certificate expired 3 days ago* and *Insurance expires in 12 days*. These
   are what the daily run sends. Tap one and it opens the car's Documents.
2. **Notifications settings** (45 s). Explain what and where. On a phone browser over HTTPS, *Turn on*, then *Send me a
   test*: it arrives as a phone notification. Mention the one email a day with one-click unsubscribe, and the Android app
   scheduling the coming weeks on the phone.
3. **Book from a fault code** (1 min). Nexon, Health, P0171, *Book a check*. The due reminders are already ticked and the
   code carries its explanation. Add "AC not cooling well", pick Friday morning, *Create booking request*, *Make the link*.
   Show what the workshop will see.
4. **Be the workshop** (1.5 min). Open the link on a second phone or laptop: no sign-in. *Confirm a time*, then *Send an
   estimate* of ₹4,850, *Car received*, *Car ready* with ₹5,120. After each one, the owner's badge goes up.
5. **Finish** (1 min). On the owner's side, *Add to calendar*, then *Service done*. ₹5,120 appears in Costs as a service at
   Sample Service Centre, and the periodic service reminder moves to its next date and km.
6. **The thread through the phases** (15 s). The reminder dates come from Phase C's km per day, the job card from Phase C's
   fault codes and Phase B's help requests. The daily run is started by GitHub Actions like the platform's CI gates.

## Limits

- **Real push services were not reached from here.** The sandbox has no route to Google's, Mozilla's or Apple's push
  services, and headless Chromium refuses notification permission. Encryption and signatures were checked against the RFC
  example and an independent library, and delivery against a stand-in push service.
- **The Android app has no push.** Booking updates reach it by email and in the inbox; only the date-based reminders are
  scheduled on the phone. The schedule is refreshed when the app opens and covers 45 days. Without the exact-alarm
  permission, Android 12 and later may show them some minutes late.
- **iPhone:** Web Push works only for CarSaathi added to the Home Screen (iOS 16.4 and later).
- **The daily run depends on GitHub's scheduler.** It can start late, and GitHub disables scheduled workflows in a public
  repository after 60 days without activity. `python -m owner.notify run` on any cron is the alternative.
- **Email:** Brevo's free plan sends 300 emails a day; delivery was tested against fakes of Brevo and SMTP.
- **Booking has no workshop directory, availability calendar or payment.** The owner brings the workshop's number; the
  workshop confirms by hand. The link works for whoever holds it until it ends, so it should go only to the workshop.
- **Times are India time only.**
