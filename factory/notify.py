"""Notifications when a run is done, failed, blocked or paused: macOS Notification Center and, if the user set
one, a Microsoft Teams incoming webhook. A failed notification is logged and never stops the factory.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request

import settings

# The message travels as an argument, never as AppleScript source, so its text can't inject commands.
APPLESCRIPT = ["-e", "on run argv", "-e",
               "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
               "-e", "end run"]


def macos(title: str, subtitle: str, message: str) -> None:
    osascript = shutil.which("osascript") or "/usr/bin/osascript"
    subprocess.run([osascript, *APPLESCRIPT, message[:300], title[:80], subtitle[:120]],
                   capture_output=True, timeout=15, check=True)


def teams_payload(title: str, subtitle: str, message: str) -> dict:
    """An Adaptive Card message, which both classic incoming webhooks and Workflows webhooks accept."""
    card = {"type": "AdaptiveCard", "version": "1.4", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
                     {"type": "TextBlock", "text": subtitle, "isSubtle": True, "spacing": "None", "wrap": True},
                     {"type": "TextBlock", "text": message, "wrap": True}]}
    return {"type": "message", "text": f"{title}: {message}",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def post(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as res:  # the caller checks the URL with settings.valid_webhook
        res.read()


def send(conf: dict, event: str, subtitle: str, message: str, force: bool = False) -> list:
    """Sends one notification on every enabled channel. Returns a list of problems, empty if all went well.
    `force` ignores the per-event toggles (for the dashboard's test button)."""
    n = conf.get("notify") or {}
    if not force and not (n.get("events") or {}).get(event):
        return []
    title, problems = f"{conf.get('project') or 'Katana'}: {event}", []
    if n.get("macos"):
        try:
            macos(title, subtitle, message)
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(f"macOS notification failed: {e}")
    url = n.get("teams_webhook") or ""
    if url:
        if not settings.valid_webhook(url):
            problems.append("Teams webhook URL is not allowed; skipped.")
        else:
            try:
                post(url, teams_payload(title, subtitle, message))
            except (OSError, ValueError) as e:
                problems.append(f"Teams webhook failed: {e}")
    return problems
