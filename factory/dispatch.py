"""Which projects the service should scan now, the background workers that scan them, and the launchd plist.

launchd runs `orchestrator.py dispatch` every minute and whenever a folder-watch project's input/ changes.
dispatch starts one detached worker (`orchestrator.py run --project P --scheduled`) per project that is due,
so a long run in one project never holds up another. Each worker takes its project's runs/.lock.
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import projects
import settings
from triggers import FolderTrigger

HERE = Path(__file__).resolve().parent
LABEL = "com.katana.orchestrator"
PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
DISPATCH_SECONDS = int(os.environ.get("KATANA_DISPATCH_INTERVAL", "60"))
SLACK_SECONDS = 30  # launchd fires a little early or late; count a project as due this close to its time


def service_projects() -> list:
    """(folder, settings) for every registered project the service runs: folder-interval or folder-watch.
    KATANA_HOME limits this to that one folder, for testing."""
    override = os.environ.get("KATANA_HOME")
    folders = [Path(override).expanduser().resolve()] if override else projects.registered()
    out = []
    for folder in folders:
        if folder.is_dir():
            conf = settings.load(folder)
            if settings.trigger(conf)["type"] in settings.SERVICE_TRIGGERS:
                out.append((folder, conf))
    return out


def last_scan(folder: Path) -> float:
    try:
        beat = json.loads((folder / "runs" / ".factory.json").read_text())
        return time.mktime(time.strptime(beat["tick_started"][:19], "%Y-%m-%dT%H:%M:%S"))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return 0.0


def due(folder: Path, conf: dict) -> bool:
    """A project is due once its interval has passed; a folder-watch project also as soon as a
    requirement file is waiting in input/."""
    trig = settings.trigger(conf)
    if trig["type"] == "folder-watch" and FolderTrigger(folder, conf).files():
        return True
    return time.time() - last_scan(folder) >= trig["interval_min"] * 60 - SLACK_SECONDS


def busy(folder: Path) -> bool:
    """True while a scan of this project holds its lock."""
    lock_path = Path(folder) / "runs" / ".lock"
    if not lock_path.exists():
        return False
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def spawn_worker(folder: Path, scheduled: bool) -> None:
    """Starts `orchestrator.py run` for one project in the background, in its own session so it outlives
    the dispatcher or the dashboard request that started it."""
    folder = Path(folder)
    (folder / "logs").mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(HERE / "orchestrator.py"), "run", "--project", str(folder)]
    with open(folder / "logs" / "worker.log", "a") as err:
        subprocess.Popen(cmd + (["--scheduled"] if scheduled else []), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=err, start_new_session=True, cwd=str(HERE.parent))


def watch_paths() -> list:
    """The input/ folders launchd watches: one per folder-watch project."""
    return sorted(str(f / "input") for f, conf in service_projects()
                  if settings.trigger(conf)["type"] == "folder-watch")


def plist(path_value: str, interval: int = DISPATCH_SECONDS) -> bytes:
    log = str(HERE.parent / "logs" / "launchd.log")
    job = {"Label": LABEL,
           "ProgramArguments": ["/usr/bin/python3", str(HERE / "orchestrator.py"), "dispatch"],
           "WorkingDirectory": str(HERE.parent),
           "StartInterval": interval,  # the fallback, and the clock for folder-interval projects
           "RunAtLoad": True,
           "AbandonProcessGroup": True,  # workers keep running after dispatch exits
           "EnvironmentVariables": {"PATH": path_value},
           "StandardOutPath": log, "StandardErrorPath": log}
    paths = watch_paths()
    if paths:
        job["WatchPaths"] = paths
    return plistlib.dumps(job)


def installed_watch_paths() -> list | None:
    """WatchPaths of the installed plist, or None if the service isn't installed."""
    try:
        return sorted(plistlib.loads(PLIST_PATH.read_bytes()).get("WatchPaths") or [])
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
