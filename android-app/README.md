# CarSaathi Android app

A Capacitor 7 wrapper around the live CarSaathi site, built the same way as the PlanetU ERP app:

- `capacitor.config.json` points `server.url` at the deployed site, so every Render deploy updates the app without a new APK.
  The app adds `CarSaathiApp` to the user agent, so the site hides its own "Download the Android app" button inside the app.
- The native `android/` project is **not committed**. `.github/workflows/android-app.yml` creates it on every build
  (`npx cap add android`, then `scripts/brand.sh` for icons, splash, name and the camera permission) and runs `./gradlew assembleDebug`
  with Node 20 and Java 21. The APK is published as a GitHub release `android-<run number>` (latest) named `carsaathi.apk`.
- The site's login page offers `/get-app/android`, which redirects to the latest release when `OWNER_GITHUB_REPO` is set on the
  owner service (or to `OWNER_APK_URL`).

Set the repository variable `OWNER_APP_URL` (Settings > Secrets and variables > Actions > Variables) to your owner service URL if it
is not `https://carsaathi.onrender.com`; the workflow writes it into the config before building.

Local build (needs Android Studio / the Android SDK and Java 21):

```bash
cd android-app && npm install && npx cap add android && bash scripts/brand.sh && cd android && ./gradlew assembleDebug
```

The APK is debug-signed (for testing and sideloading). A Play Store release needs a release keystore and `assembleRelease`.
