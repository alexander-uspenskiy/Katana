#!/bin/bash
# Stops and removes the Cortana launchd agent. Runs and input files are kept.
set -euo pipefail

LABEL="com.cortana.orchestrator"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed $LABEL"
