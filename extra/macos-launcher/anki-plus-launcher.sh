#!/bin/bash
# Launches the Anki fork at ~/dev/anki: rebuilds anything changed, then starts the app.
ANKI_SRC="$HOME/dev/anki"
export PATH="$HOME/.cargo/bin:$HOME/.local/node/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LOG="$HOME/Library/Logs/AnkiDev.log"
cd "$ANKI_SRC" || exit 1
if ! ./run >"$LOG" 2>&1; then
    osascript -e 'display notification "Build or launch failed. See ~/Library/Logs/AnkiDev.log" with title "Anki Dev"'
    exit 1
fi
