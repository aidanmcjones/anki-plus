#!/bin/bash
# Rebuilds /Applications/Anki+.app, the Dock launcher for this fork.
# The bundle wraps ./run: clicking it rebuilds anything changed in the
# repo and launches the app. Run this after cloning to a new Mac, or if
# the bundle in /Applications is ever lost.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/Anki+.app"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
cp "$HERE/AnkiPlus.icns" "$APP/Contents/Resources/anki.icns"
cp "$HERE/anki-plus-launcher.sh" "$APP/Contents/MacOS/anki-dev"
chmod +x "$APP/Contents/MacOS/anki-dev"
codesign --force --deep -s - "$APP"
touch "$APP"
echo "Installed $APP — drag it to the Dock to pin."
