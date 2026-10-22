#!/usr/bin/env bash
# Branding and native settings for the generated android/ project (it is not committed: CI recreates it on every build,
# so everything native lives here). Run from android-app/ after `npx cap add android`.
set -euo pipefail
cd "$(dirname "$0")/.."
RES=android/app/src/main/res
MANIFEST=android/app/src/main/AndroidManifest.xml
[ -d "$RES" ] || { echo "Run 'npx cap add android' first"; exit 1; }

# launcher icons (number plate on signage blue)
for d in mdpi hdpi xhdpi xxhdpi xxxhdpi; do
  cp res/mipmap-$d/ic_launcher.png res/mipmap-$d/ic_launcher_round.png res/mipmap-$d/ic_launcher_foreground.png "$RES/mipmap-$d/"
done
sed -i 's#<color name="ic_launcher_background">[^<]*</color>#<color name="ic_launcher_background">\#1F4FA3</color>#' "$RES/values/ic_launcher_background.xml"

# splash: the plate centred on blue at any screen shape (a stretched square image would distort it)
find "$RES" -path "$RES/drawable*" -name 'splash.png' -delete
cat > "$RES/drawable/splash.xml" <<'XML'
<?xml version="1.0" encoding="utf-8"?>
<layer-list xmlns:android="http://schemas.android.com/apk/res/android">
    <item android:drawable="@color/ic_launcher_background" />
    <item><bitmap android:gravity="center" android:src="@mipmap/ic_launcher_foreground" /></item>
</layer-list>
XML
grep -q windowSplashScreenBackground "$RES/values/styles.xml" || sed -i \
  's#<item name="android:background">@drawable/splash</item>#<item name="android:background">@drawable/splash</item>\n        <item name="windowSplashScreenBackground">@color/ic_launcher_background</item>#' \
  "$RES/values/styles.xml"

# status-bar icon of reminders (Phase D, local notifications): Android shows only its shape, in white
cat > "$RES/drawable/ic_stat_carsaathi.xml" <<'XML'
<vector xmlns:android="http://schemas.android.com/apk/res/android" android:width="24dp" android:height="24dp"
    android:viewportWidth="24" android:viewportHeight="24">
    <path android:fillColor="#FFFFFFFF" android:pathData="M5,11L6.5,6.5C6.8,5.6 7.6,5 8.5,5h7c0.9,0 1.7,0.6 2,1.5L19,11c1.1,0 2,0.9 2,2v4c0,0.6 -0.4,1 -1,1h-1v1.5c0,0.8 -0.7,1.5 -1.5,1.5S16,19.3 16,18.5V18H8v0.5C8,19.3 7.3,20 6.5,20S5,19.3 5,18.5V18H4c-0.6,0 -1,-0.4 -1,-1v-4C3,11.9 3.9,11 5,11zM7.2,11h9.6l-1.1,-3.4C15.6,7.2 15.3,7 15,7H9C8.7,7 8.4,7.2 8.3,7.6zM6.5,15.5C7.3,15.5 8,14.8 8,14s-0.7,-1.5 -1.5,-1.5S5,13.2 5,14S5.7,15.5 6.5,15.5zM17.5,15.5c0.8,0 1.5,-0.7 1.5,-1.5s-0.7,-1.5 -1.5,-1.5S16,13.2 16,14S16.7,15.5 17.5,15.5z"/>
</vector>
XML

# permissions: the camera for photos of documents (the web page asks with a file input); the internet is already declared
add_permission() {
  grep -q "android.permission.$1\"" "$MANIFEST" || sed -i "s#</manifest>#    <uses-permission android:name=\"android.permission.$1\" />\n</manifest>#" "$MANIFEST"
}
add_permission CAMERA
grep -q 'android.hardware.camera"' "$MANIFEST" || sed -i 's#</manifest>#    <uses-feature android:name="android.hardware.camera" android:required="false" />\n</manifest>#' "$MANIFEST"

# app name shown under the icon
sed -i 's#<string name="app_name">[^<]*</string>#<string name="app_name">CarSaathi</string>#; s#<string name="title_activity_main">[^<]*</string>#<string name="title_activity_main">CarSaathi</string>#' \
  "$RES/values/strings.xml"
echo "Branding applied"
