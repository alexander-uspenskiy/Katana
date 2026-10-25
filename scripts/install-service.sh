#!/bin/bash
# Installs (or regenerates) the launchd agent that runs Katana while you are logged in.
#
# The plist is built from config/projects.json: launchd runs `orchestrator.py dispatch` every
# KATANA_DISPATCH_INTERVAL seconds (60) and also as soon as the input/ folder of any folder-watch project
# changes (WatchPaths). dispatch starts a background worker for each project that is due. Run this again
# after adding a folder-watch project; the dashboard does that for you when the service is installed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.katana.orchestrator"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
INTERVAL="${KATANA_DISPATCH_INTERVAL:-60}"

# launchd starts jobs with a bare PATH; give the agents the tools they need.
NODE_DIR="$(dirname "$(command -v node 2>/dev/null)" 2>/dev/null || true)"
PATH_VALUE="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:${NODE_DIR:+$NODE_DIR:}/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$ROOT/logs" "$HOME/Library/LaunchAgents"
# KATANA_HOME limits the orchestrator to one project, which would drop the others from the plist.
env -u KATANA_HOME KATANA_DISPATCH_INTERVAL="$INTERVAL" \
  /usr/bin/python3 "$ROOT/factory/orchestrator.py" service-plist --path "$PATH_VALUE" > "$PLIST.tmp"
plutil -lint -s "$PLIST.tmp"
mv "$PLIST.tmp" "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
WATCHED="$(/usr/libexec/PlistBuddy -c 'Print :WatchPaths' "$PLIST" 2>/dev/null | grep -c '/input' || true)"
echo "Installed $LABEL: checks every ${INTERVAL}s which projects are due, and watches ${WATCHED} input folder(s)."
echo "Each project's log: <project>/logs/factory.log · launchd output: $ROOT/logs/launchd.log"
