# Phase A: CarSaathi, the car-owner app

The engineering platform (Phases 1 to 6) is for the people who build cars. CarSaathi is for the people who drive them:
a mobile-first app where a car owner signs in with their email, keeps their cars, papers and service reminders in one
place, and shares a car with family members. It is the first phase of the owner-facing product; the help with car
problems (infotainment, Bluetooth, calls, ADAS warnings), car health and fuel tracking build on it in later phases.

It is a separate product with its own users, database and deployment, in the same repository:

```
owner/            FastAPI service: accounts, garage, sharing, documents, reminders, privacy (SQLAlchemy Core)
owner-app/        the web app: Vite + React, mobile-first, installable (PWA)
android-app/      the Android app: a Capacitor 7 wrapper around the live site (built by GitHub Actions)
owner/Dockerfile  one image: builds the web app and serves it with the API on one origin
```

## What a car owner can do

| | |
|---|---|
| **Sign in without a password** | Email a 6-digit code, valid 10 minutes. First sign-in asks for a name and consent to the privacy notice. |
| **My Garage** | Up to 10 cars with the Indian number plate as the car's identity (`MH 12 AB 1234`, Bharat series `22 BH 1234 AA`), make and model suggestions for popular Indian cars, fuel, year, VIN, colour and the odometer. |
| **Document vault** | RC, insurance, PUC, warranty, service records, FASTag: a photo (straight from the phone camera) or a PDF, the number, issuer and dates. Each paper shows *valid*, *expiring* (30 days) or *expired*. The home screen lists the papers a car is missing (an electric car needs no PUC). |
| **Reminders** | By date, by odometer reading, or both, whichever comes first: "Periodic service on 11 Nov or at 25,000 km". They repeat every month, quarter or year and/or every N km; *Mark done* moves a repeating reminder to its next date and reading on the schedule. Document expiries are reminders automatically. |
| **Home** | What needs attention across all cars, most urgent first, and one row per car with the state of its papers. |
| **Family sharing** | Invite someone by email as an owner or a driver. Drivers see everything, update the odometer and add papers and reminders; only owners change the car or its people. |
| **Privacy** | Download all my data (JSON), delete my account (cars where you are the only owner go with it), sign out of every device. |
| **Try the demo** | One tap creates a private sandbox account with two sample cars (a Nexon with insurance due in 12 days and an expired PUC, a Tiago EV), deleted after 24 hours. Nobody sees anyone else's demo. |
| **Get the app** | Android: download the APK from the login or profile screen. iPhone and desktop: install the site from the browser (PWA). |

## How it is built

```
phone / browser ──HTTPS──> Render (Docker)                                PostgreSQL (Neon, free)
  React app (PWA)            uvicorn ── owner.api ── owner.service ───────> users, sessions, cars, members,
  Android app (Capacitor:    │  /api/*   JSON, bearer token                 invitations, documents (+ files),
  loads the live site)       │  /*       the built React app                reminders, events
                             └─ sign-in codes ──HTTPS──> Brevo email API
```

- **One origin.** The API serves the built app, so there are no cookies, no CORS in production and one deployment.
  The app keeps an opaque session token (`cs_...`) and sends it as a bearer token.
- **Database.** SQLAlchemy Core tables that run on PostgreSQL in production and SQLite in development and tests. Every
  car-owned row cascades from its car, every account row from its user.
- **Android like the PlanetU ERP app.** `android-app/capacitor.config.json` points `server.url` at the live site, so every
  web deploy updates the app. The native `android/` project is not committed: the workflow creates it, applies
  `scripts/brand.sh` (icons, splash, name, camera permission) and builds the APK. The app adds `CarSaathiApp` to the user
  agent, so the site hides its own download button inside the app.

### Security and privacy

| Threat | Control |
|---|---|
| Guessing a sign-in code | 5 tries per code (each wrong try is saved before the answer, so it cannot be rolled back), new code needed after that; 6 digits; 10 minutes |
| Reading codes or tokens from the database | Codes stored only as HMAC-SHA256 with the server secret; session tokens only as SHA-256 |
| Learning who has an account | Requesting a code answers the same for every address |
| Flooding inboxes, brute force | Per visitor and per address limits (5 codes an hour per address, one per 30 s, 20 per visitor); 300 API requests a minute per visitor. Behind Render, the visitor is the last `X-Forwarded-For` entry (written by Render's proxy, not the client: `OWNER_PROXY_HOPS=1`) |
| Seeing someone else's car (IDOR) | Every record is reached through the car and membership is checked; other people's records answer *not found*, not *forbidden* |
| Malicious uploads | Type decided from the file's content (JPEG, PNG, WebP, PDF only), 5 MB, file names cleaned, files served with `Content-Security-Policy: sandbox` and `nosniff` |
| Script injection | Strict CSP on the app (`script-src 'self'`, fonts self-hosted, no third-party requests), React escaping, `X-Frame-Options: DENY` |
| Mass assignment | Every request body rejects unknown fields |
| Leaking internals | Generic error messages; API docs off in production; `Cache-Control: no-store` on every API answer |
| Offline copies of personal data | The service worker caches only the app shell, never `/api/` |

Privacy follows India's DPDP Act 2023 as far as an app can: a notice and recorded consent (with its version), purpose
limitation (no tracking, no ads, no third-party scripts), access (export), erasure (account deletion with cascades) and
retention (demo accounts deleted after 24 hours). The notice is in the app at `/privacy`.

## Run it locally

```bash
pip install -r requirements-dev.txt
make owner-api                  # API on http://localhost:8100 (SQLite in ./storage; sign-in codes are printed in this log)
make owner-web                  # second terminal: the app with live reload on http://localhost:5173
```

Or build once and use one server: `make owner-build`, then `make owner-api` and open http://localhost:8100.
To try it on your phone on the same Wi-Fi: `cd owner-app && npm run dev -- --host`, then open the Mac's address on port 5173.

## Deploy (free)

1. **Database: Neon.** Create a free project at neon.tech in the *AWS Asia Pacific (Singapore)* region (next to Render's
   Singapore region) and copy the connection string (`postgresql://...?sslmode=require`). Render's own free PostgreSQL
   is deleted after 30 days, Neon's is not.
2. **Email: Brevo.** Render's free plan blocks outgoing SMTP, so the codes go through Brevo's HTTPS API (free: 300 emails a
   day). Create an account at brevo.com, verify a sender address under *Senders*, and create an API key under
   *SMTP & API > API keys*.
3. **Render.** Push the repository, then in the Render dashboard open your Blueprint and press *Manual sync* (or *New >
   Blueprint* for a new account). Render creates the `carsaathi` service from `render.yaml`. It asks for the values marked
   `sync: false` only when a Blueprint is first created; on a sync of an existing Blueprint, add them yourself under the
   `carsaathi` service's *Environment* (full list: [DEPLOY_RENDER.md](DEPLOY_RENDER.md)):
   - `OWNER_DATABASE_URL`: the Neon connection string
   - `BREVO_API_KEY`: the Brevo key; `OWNER_MAIL_FROM`: `CarSaathi <the verified sender address>`
   - `OWNER_GITHUB_REPO`: `your-github-user/your-repo` (for the Android download)

   `OWNER_SECRET_KEY` is generated by Render. The engineering demo keeps running unchanged; changes to `owner/` and
   `owner-app/` redeploy only CarSaathi.
4. **Android app.** If the service URL is not `https://carsaathi.onrender.com` (the name may be taken), add a repository
   variable `OWNER_APP_URL` with your URL (GitHub: *Settings > Secrets and variables > Actions > Variables*). Then run
   *Actions > Android app > Run workflow*. It publishes `carsaathi.apk` as the latest release, and *Download the Android
   app* on the login page starts working. Later web changes need no new APK.
5. **Check.** Open the site, press *Try the demo*, then sign in with your own email.

Without `OWNER_DATABASE_URL` the service still starts (with a warning) on SQLite, and every restart of the free instance
deletes all accounts; without an email service only the demo works.

## Demo script (5 minutes)

1. **The problem** (30 s): an Indian car owner's papers are in a glovebox and a WhatsApp chat; insurance and PUC lapse
   (fines, and claims refused), services are missed. Engineering tools do not help them.
2. **Try the demo on a phone** (1 min): the home screen already says what needs attention: PUC expired 3 days ago,
   insurance in 12 days, service in 20 days or 690 km, whichever comes first. Open the Nexon: papers with their status.
3. **Add a paper** (1 min): *Add document > Insurance*, take a photo with the camera, set the expiry date. It appears on
   the home screen when it is due.
4. **Reminders** (30 s): mark the service done; it moves to next year and 35,000 km.
5. **Real account and sharing** (1 min): sign in with your email (the code arrives by email), add your car with its
   number plate, invite a family member as a driver.
6. **Engineering under it** (1 min): the Android app is the same site in a Capacitor wrapper built by CI; PostgreSQL;
   codes and tokens stored hashed; other people's cars answer *not found*; uploads typed by content; export and delete
   for DPDP; 32 tests, the 24 that touch the database on both SQLite and PostgreSQL in CI.

## Tests

`tests/test_owner_app.py` (part of `python -m pytest`) covers the sign-in codes (hashing, expiry, five tries, the same
answer for every address, rate limits, behind a proxy), email delivery (console, SMTP, Brevo, failures), sessions
(hashed, sign out, sign out everywhere, expiry), the demo sandbox (isolation, expiry with its cars, limits), car
validation and limits, object-level access (13 probes of another person's records), owner and driver rights, invitations,
documents and files (content typing, size limits, replacement, cascade), reminders (date, km, both, repeats, ordering),
the home summary, export, account deletion with shared cars, security headers, the web app's routes and caching, the
Android download link, CORS, and the packaging files (Capacitor config, branding script, workflow, PWA manifest,
Render service, Dockerfile): 32 tests. The 24 that touch the database run twice in CI: on SQLite and on PostgreSQL 16
(job `owner-postgres`), 56 runs in all. The job `owner-web` builds the app and the Docker image and smoke-tests the container.

## Limits

- The APK is built by GitHub Actions; it was not built in the development sandbox (no Android SDK there). The script
  `brand.sh` and `npx cap add android` were run and checked; the Gradle build was not. It is a debug-signed APK for
  sideloading, not a Play Store release.
- Email delivery was tested against fakes of Brevo's API and of an SMTP server, not against the real services.
- The Docker image was not built here (no Docker); its steps were run one by one and the CI job builds it.
- In Phase A, reminders are shown in the app only; Phase D adds reminder emails and notifications on phones and browsers.
- Rate limits are in memory: right for one instance, not for several.
- Render's free instance sleeps after 15 minutes without visitors; the first request after that takes about a minute.
- Car health, fuel and charging logs, OBD-II data and help with car problems came in later phases (B and C).

## Next phases (planned)

- Help with car problems: done in Phase B ([PHASE_B_OWNER_HELP.md](PHASE_B_OWNER_HELP.md)).
- Car health and running costs: done in Phase C ([PHASE_C_CAR_HEALTH_COSTS.md](PHASE_C_CAR_HEALTH_COSTS.md)).
- Notifications and service booking: done in Phase D ([PHASE_D_NOTIFICATIONS_BOOKING.md](PHASE_D_NOTIFICATIONS_BOOKING.md)).
