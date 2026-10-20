#!/usr/bin/env bash
# Build the fake-adware test APK with the bare SDK tools - no Gradle.
#   ANDROID_HOME defaults to ~/Android/Sdk. Output: testapp/build/adbust-test.apk
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
BT="$SDK/build-tools/$(ls "$SDK/build-tools" | sort -V | tail -1)"
PLATFORM="$SDK/platforms/$(ls "$SDK/platforms" | grep -E '^android-[0-9]+$' | sort -V | tail -1)"
JAR="$PLATFORM/android.jar"

rm -rf build && mkdir -p build/classes build/dex

javac --release 8 -classpath "$JAR" -d build/classes \
    $(find src -name '*.java') 2>&1 | grep -v 'warning: \[options\]' || true
"$BT/d8" --min-api 24 --lib "$JAR" --output build/dex \
    $(find build/classes -name '*.class')

"$BT/aapt2" link -o build/unsigned.apk -I "$JAR" \
    --manifest AndroidManifest.xml --min-sdk-version 24 --target-sdk-version 34
(cd build/dex && zip -q ../unsigned.apk classes.dex)
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk

# Throwaway debug key, generated once per checkout. Never use it for anything real.
KS=debug.keystore
[ -f "$KS" ] || keytool -genkeypair -keystore "$KS" -storepass android \
    -keypass android -alias test -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=adbust test" >/dev/null 2>&1
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:android --key-pass pass:android \
    --out build/adbust-test.apk build/aligned.apk
echo "built testapp/build/adbust-test.apk"
