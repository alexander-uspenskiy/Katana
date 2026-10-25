#!/bin/bash
# Installs a launchd agent that runs the orchestrator every 10 minutes while you are logged in.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.darkfactory.orchestrator"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
INTERVAL="${DF_INTERVAL:-600}"

# launchd starts jobs with a bare PATH; give the agents the tools they need.
NODE_DIR="$(dirname "$(command -v node 2>/dev/null)" 2>/dev/null || true)"
PATH_VALUE="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:${NODE_DIR:+$NODE_DIR:}/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$ROOT/logs" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$ROOT/factory/orchestrator.py</string>
    <string>tick</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATH_VALUE</string>
  </dict>
  <key>StandardOutPath</key><string>$ROOT/logs/factory.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/factory.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL: scans $ROOT/input every ${INTERVAL}s. Log: $ROOT/logs/factory.log"
