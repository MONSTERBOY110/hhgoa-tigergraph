#!/usr/bin/env bash
# Re-render the concept B PNGs from their HTML sources with headless Edge (Git Bash on Windows).
# --disable-lcd-text keeps text antialiasing grey (no ClearType colour fringes);
# --force-color-profile=srgb keeps #141A21, #C2491A and #EEF0F3 exact.
# Pages use fixed pixel layouts because headless Edge widens small windows (a 400 px window lays out at about 496 px).
# The header loads Oswald and IBM Plex Mono from Google Fonts, so it needs network access.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd -W 2>/dev/null || pwd)"
EDGE="/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
PROFILE="C:/Users/MONSTER/AppData/Local/Temp/edge-B-k-monogram"
FLAGS=(--headless=new --disable-gpu --hide-scrollbars --disable-lcd-text --force-color-profile=srgb --user-data-dir="$PROFILE" --virtual-time-budget=4000)
"$EDGE" "${FLAGS[@]}" --window-size=400,400 --screenshot="$DIR/avatar-400.png" "file:///$DIR/avatar-400.html"
"$EDGE" "${FLAGS[@]}" --window-size=1500,500 --screenshot="$DIR/header-1500x500.png" "file:///$DIR/header-1500x500.html"
# the preview reads avatar-400.png, so it renders last
"$EDGE" "${FLAGS[@]}" --window-size=560,140 --screenshot="$DIR/preview-48.png" "file:///$DIR/preview-48.html"
