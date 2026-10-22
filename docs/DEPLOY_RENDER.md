# Deploy to Render (auto-deploy on every push)

The repository contains a Render Blueprint (`render.yaml`). After a one-time setup, every
`git push` to GitHub redeploys the live demo automatically.

## What gets deployed

One free Render web service runs the whole platform:

```
Render $PORT  ->  Streamlit UI  ->  FastAPI on 127.0.0.1:8000 (private)  ->  NumPy store + SQLite in /tmp
```

`scripts/render_start.sh` starts the API, waits until it is healthy, loads the demo project
(`bcm-demo`: HLDs, HARA, TARA, UDS spec, C code, SBOMs and OTA packages, the AEB requirements and
scenarios with a first validation campaign, the OEM's ReqIF requirements, a waiting agent run and six field complaints
investigated by the field-issue agent), then starts
the UI. Webhooks and Jira run in dry-run mode on the demo (`INTEGRATIONS_LIVE=0`): events are prepared and
signed but never sent, so the public URL cannot be used to make the server call other addresses. The build installs only
`requirements-render.txt` (no ChromaDB or model downloads) and compiles the AEB C code and its test
mutants for software-in-the-loop runs (`python scripts/sil_run.py build --mutants`). It fits the
free instance: with `PDF_PARSE_ISOLATION=1` the whole app measured 311 MB of 512 MB after visiting
every page twice, running an ADAS campaign with mutation testing, importing the OEM's ReqIF v2 and
confirming the field-issue agent's checkpoints (309 MB after Phase 5, 307 MB after Phase 4, 276 MB before it).
The three field investigations run during seeding; the app was up 14 s after start in the simulation (expect
longer on the shared free CPU).

## One-time setup

1. **Push the code to GitHub** (see the commands in the project README or in your hand-over notes).
2. Sign in at https://dashboard.render.com with your GitHub account.
3. Click **New** > **Blueprint**, select your repository, and click **Connect**.
4. Render reads `render.yaml` and shows the service `automotive-eng-ai`. It asks for two values:
   - `OPENAI_API_KEY` and `OPENAI_MODEL`: optional. To use a real hosted LLM, create a free key at
     https://console.groq.com, then copy a current model ID from Groq's model list into
     `OPENAI_MODEL`. Leave both empty and the demo uses the offline extractive answerer.
5. Click **Apply**. The first build takes a few minutes. When the status shows **Live**, open the
   URL (`https://automotive-eng-ai-xxxx.onrender.com`).

From then on, **every push to the connected branch triggers a new deploy**. You can follow it under
the service's **Events** and **Logs** tabs.

## Service 2: CarSaathi, the car-owner app (Phases A to D)

The second service in `render.yaml`, `carsaathi`, is built from `owner/Dockerfile`: the owner API and the React app in
one image. Unlike the engineering demo it keeps real accounts, so its data lives in a PostgreSQL database outside Render
(Render's free PostgreSQL is deleted after 30 days). Changes to `owner/` and `owner-app/` redeploy only this service.

### Accounts and keys to prepare

| What | Where | Goes into |
|---|---|---|
| PostgreSQL database | neon.tech: a free project in *AWS Asia Pacific (Singapore)*; copy the connection string (`postgresql://...?sslmode=require`) | `OWNER_DATABASE_URL` |
| Sending email (sign-in codes, reminders) | brevo.com: verify a sender address under *Senders*, create a key under *SMTP & API > API keys* | `BREVO_API_KEY`, and `OWNER_MAIL_FROM` = `CarSaathi <the verified address>` |
| Web Push keys | on your computer, in the repository: `pip install cryptography`, then `python -m owner.notify vapid` | `OWNER_VAPID_PUBLIC_KEY`, `OWNER_VAPID_PRIVATE_KEY`, `OWNER_VAPID_SUBJECT` = `mailto:<your email>` |
| The repository's name | `your-user/your-repo` on GitHub | `OWNER_GITHUB_REPO` |
| Optional: an LLM for the help search | console.groq.com (the engineering demo's key works) | `OWNER_LLM_BASE_URL` = `https://api.groq.com/openai/v1`, `OWNER_LLM_MODEL` (e.g. `llama-3.1-8b-instant`), `OWNER_LLM_API_KEY` |

Render generates `OWNER_SECRET_KEY`, `OWNER_ADMIN_TOKEN` and `OWNER_CRON_TOKEN`. Leave `OWNER_PUBLIC_URL` empty (the app
uses the service's own `onrender.com` address) and `OWNER_FIELD_API_URL` empty (reports to the manufacturer stay dry runs
on the public demo). Never put the VAPID private key, the Brevo key or the database URL in git.

### Steps

1. **Push.** If the Blueprint exists already (the engineering demo), Render applies the new `render.yaml` and creates
   `carsaathi`; if it isn't listed after a few minutes, open *Blueprints*, your Blueprint, *Manual sync*. On a new account,
   *New > Blueprint* creates both services and asks for every `sync: false` value in one form.
2. **Secrets.** Render asks for `sync: false` values only when a Blueprint is first created. On an existing Blueprint, open
   the `carsaathi` service, *Environment*, add the values from the table, then *Save, rebuild, and deploy*.
3. **Check.** When it shows *Live*, open the address (`https://carsaathi.onrender.com`, or the one Render shows if that
   name was taken), press *Try the demo*, then sign in with your own email: the code arrives through Brevo.
4. **GitHub settings** (*Settings > Secrets and variables > Actions*):
   - variable `OWNER_APP_URL` = the CarSaathi address (the Android app loads it; the daily reminder run calls it);
   - secret `OWNER_CRON_TOKEN` = the value Render generated (`carsaathi`, *Environment*).
5. **Android app.** *Actions > Android app > Run workflow* publishes `carsaathi.apk` as the latest release, and *Download
   the Android app* on the login page starts working. (A build that ran before `OWNER_APP_URL` was set points at
   `https://carsaathi.onrender.com`; run it again if your address differs.)
6. **Daily reminders.** *Actions > CarSaathi daily reminders > Run workflow* answers with a line like
   `{"date": ..., "people": ..., "created": ...}`. From then on it runs every day at 9:00 India time.

## Free-tier behaviour

- A Render workspace gets **750 free instance hours a month**, shared by its free services. Both services sleep when
  idle, so a demo uses far fewer; if the hours run out, Render suspends the free services until the next month.
- The service **sleeps after about 15 minutes without visitors**. The next visit wakes it, which
  takes around a minute. Open the URL a few minutes before an interview.
- The disk is **ephemeral**: uploads and approvals made on the live demo are lost on every restart
  or redeploy, and the demo data is loaded again. That is intended for a public demo.
- The demo runs with `AUTH_MODE=off` and synthetic data only. Anyone with the URL can use it. For
  anything real, set `AUTH_MODE=enforced` and an `ADMIN_TOKEN` (see the README security section).
- With a hosted LLM configured, questions asked on the demo use your provider quota.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Build fails on a package | Check the build log; the build runs `pip install -r requirements-render.txt` |
| Deploy stuck on "health check" | The UI must answer on `/_stcore/health`. Check the logs for `API did not become healthy` |
| UI says "Cannot reach the API" | The API process stopped; the log shows why. Use **Manual Deploy** > **Restart** |
| Answers say `LLM: mock` | No `OPENAI_API_KEY` / `OPENAI_MODEL` set (by design). Add them under **Environment** |
| Answers flagged "degraded" | The hosted LLM returned an error (wrong model ID, quota). The log line `llm_error` says which |
| ADAS page: "C code (SIL) unavailable" | The build log shows `SIL build skipped`: the build image had no C compiler. Campaigns then run on the Python model and cannot be released; run `python scripts/sil_run.py campaign` locally or rely on the CI job |
| ADAS campaign with mutation testing is slow | Nothing is compiled at run time (prebuilt), but the scenario set is simulated once per mutant; on the free instance's small CPU share this can take about a minute |

Checked in the development sandbox: the start script, the lightweight requirements, the health check
and the seeding were run exactly as Render runs them (with `PORT` set and no LLM key). Render itself
(Blueprint import, build image, URL) can only be checked from your Render account.
