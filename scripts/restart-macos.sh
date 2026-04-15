#!/usr/bin/env bash
set -euo pipefail

LABEL="${CCBOT_LAUNCHD_LABEL:-com.ccbot}"
PLIST="${CCBOT_LAUNCHD_PLIST:-$HOME/Library/LaunchAgents/${LABEL}.plist}"

if [[ ! -f "$PLIST" ]]; then
    echo "Error: launchd plist not found: $PLIST"
    exit 1
fi

echo "Reloading launchd agent: $LABEL"
launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

sleep 2

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "ccbot restarted successfully."
    echo "----------------------------------------"
    tail -n 20 "$HOME/.ccbot/ccbot.stdout.log" 2>/dev/null || true
    tail -n 20 "$HOME/.ccbot/ccbot.stderr.log" 2>/dev/null || true
    echo "----------------------------------------"
else
    echo "Error: ccbot failed to start."
    exit 1
fi
