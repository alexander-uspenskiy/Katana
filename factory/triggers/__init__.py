"""Requirement sources ("triggers") for Cortana. See base.py for the interface and how Jira and Confluence
connectors would plug in.

A project's settings say what starts its runs (settings["trigger"]["type"]); CONNECTORS maps that type to
the connector class that fetches the requirements. All current types read the project's input/ folder and
differ only in when they scan:

- folder-interval: the service scans every interval_min minutes.
- folder-watch: launchd starts a scan as soon as input/ changes; interval_min is the fallback rescan.
- once: no service; Run now in the dashboard or `orchestrator.py run` processes the current input.
- manual: like once, but submitting a requirement in the dashboard never starts a run by itself.

A Jira connector would add a type such as "jira-poll" mapped to a JiraTrigger class, polled on the
interval like folder-interval.
"""
from __future__ import annotations

from pathlib import Path

from .base import Item, Trigger
from .folder import SETTLE_SECONDS, FolderTrigger

CONNECTORS = {
    "folder-interval": FolderTrigger,
    "folder-watch": FolderTrigger,
    "once": FolderTrigger,
    "manual": FolderTrigger,
}


def for_project(project: Path, conf: dict, log=print, **options) -> Trigger:
    kind = (conf.get("trigger") or {}).get("type", "folder-interval")
    return CONNECTORS.get(kind, FolderTrigger)(project, conf, log=log, **options)


__all__ = ["CONNECTORS", "FolderTrigger", "Item", "SETTLE_SECONDS", "Trigger", "for_project"]
