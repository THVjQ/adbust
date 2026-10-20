#!/bin/sh
# Builds web/static/adbust-applist.dex. Needs the Android SDK and a JDK.
set -e
cd "$(dirname "$0")"
SDK=${ANDROID_HOME:-$HOME/Android/Sdk}
JAR=$(ls -d "$SDK"/platforms/android-* | sort -V | tail -1)/android.jar
BT=$(ls -d "$SDK"/build-tools/* | sort -V | tail -1)
rm -rf build && mkdir -p build/classes
javac -source 11 -target 11 -classpath "$JAR" -d build/classes src/AppList.java
"$BT/d8" --min-api 26 --output build build/classes/*.class
cp build/classes.dex ../static/adbust-applist.dex
echo "built ../static/adbust-applist.dex"
