#!/usr/bin/env python3
"""The background service that scans projects on a schedule: a launchd agent on macOS, a Task Scheduler task on
Windows. The dashboard and the install scripts both go through here.

    python3 factory/service.py install|uninstall|start|stop|status

Both run `orchestrator.py dispatch` every minute while you are logged in. launchd also runs it the moment a
folder-watch project's input/ changes; Task Scheduler has no such trigger, so a new file waits for the next
check (at most a minute).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

import dispatch
import plat

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
LABEL = dispatch.LABEL
TASK_NAME = "Katana"
ACTIONS = ("install", "start", "stop", "uninstall")
_cache = {"at": 0.0, "value": None}


def supported() -> bool:
    return plat.IS_MAC or plat.IS_WIN


def kind_name() -> str:
    return "launchd agent" if plat.IS_MAC else "Task Scheduler task" if plat.IS_WIN else "service"


def label() -> str:
    return TASK_NAME if plat.IS_WIN else LABEL


def definition() -> str:
    """What the service is stored as: the plist file, or the task's name in Task Scheduler."""
    return f"Task Scheduler: \\{TASK_NAME}" if plat.IS_WIN else str(dispatch.PLIST_PATH)


def _run(cmd: list, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                          creationflags=plat.NO_WINDOW if plat.IS_WIN else 0)


# ---------------------------------------------------------------- status

def _task_xml() -> str | None:
    """The registered task's definition, or None if there is no Katana task."""
    try:
        out = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def status(fresh: bool = False) -> dict:
    """{"installed", "loaded", "watching"}, cached for 30 seconds (launchctl and schtasks are slowish)."""
    if not fresh and _cache["value"] and time.time() - _cache["at"] <= 30:
        return _cache["value"]
    if plat.IS_WIN:
        xml = _task_xml()
        value = {"installed": xml is not None, "loaded": xml is not None and "<Enabled>false</Enabled>" not in xml,
                 "watching": []}
    elif plat.IS_MAC:
        loaded = _run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"]).returncode == 0
        value = {"installed": dispatch.PLIST_PATH.exists(), "loaded": loaded,
                 "watching": dispatch.installed_watch_paths() or []}
    else:
        value = {"installed": False, "loaded": False, "watching": []}
    value.update(instant_watch=plat.IS_MAC, install_cmd=r"scripts\install-service.ps1" if plat.IS_WIN
                 else "scripts/install-service.sh")
    _cache.update(at=time.time(), value=value)
    return value


def view(fresh: bool = False) -> dict:
    try:
        log_tail = dispatch.LAUNCHD_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
    except OSError:
        log_tail = []
    return dict(status(fresh), label=label(), plist=definition(), log_path=str(dispatch.LAUNCHD_LOG), log=log_tail,
                interval=dispatch.DISPATCH_SECONDS, platform=sys.platform, kind=kind_name(), supported=supported())


# ---------------------------------------------------------------- actions

def _task_definition() -> bytes:
    """The Task Scheduler XML: every minute from today, several runs at once allowed (a worker started by one run
    must not stop the next one), no battery or idle conditions, only while the user is logged on."""
    minutes = max(1, round(dispatch.DISPATCH_SECONDS / 60))
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
    start = time.strftime("%Y-%m-%dT00:00:00")
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Katana: scans projects that are due and starts a background worker for each.</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition><Interval>PT{minutes}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <AllowHardTerminate>true</AllowHardTerminate>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(plat.background_python())}</Command>
      <Arguments>"{escape(str(HERE / 'orchestrator.py'))}" dispatch</Arguments>
      <WorkingDirectory>{escape(str(HERE.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    return b"\xff\xfe" + xml.encode("utf-16-le")


def _win_install() -> subprocess.CompletedProcess:
    dispatch.LAUNCHD_LOG.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(_task_definition())
        out = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", name, "/F"])
    finally:
        Path(name).unlink(missing_ok=True)
    if out.returncode == 0:
        _run(["schtasks", "/Run", "/TN", TASK_NAME])  # don't wait for the first minute to pass
    return out


def action(name: str) -> tuple:
    """Runs one service action. Returns (ok, message)."""
    if not supported():
        return False, "Background scans need macOS (launchd) or Windows (Task Scheduler). Run `watch` instead."
    current = status(fresh=True)
    try:
        if plat.IS_WIN:
            if name == "install":
                out = _win_install()
            elif name == "uninstall":
                out = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]) if current["installed"] else None
            elif name == "start":
                if not current["installed"]:
                    return False, "Install the service first."
                if current["loaded"]:
                    return True, "The service is already running."
                out = _run(["schtasks", "/Change", "/TN", TASK_NAME, "/ENABLE"])
            elif name == "stop":
                if not current["loaded"]:
                    return True, "The service is already stopped."
                out = _run(["schtasks", "/Change", "/TN", TASK_NAME, "/DISABLE"])
            else:
                return False, "Unknown action."
        else:
            target = f"gui/{os.getuid()}"
            if name == "install":
                cmd = [str(SCRIPTS / "install-service.sh")]
            elif name == "uninstall":
                cmd = [str(SCRIPTS / "uninstall-service.sh")]
            elif name == "start":
                if not current["installed"]:
                    return False, "Install the service first."
                if current["loaded"]:
                    return True, "The service is already running."
                cmd = ["launchctl", "bootstrap", target, str(dispatch.PLIST_PATH)]
            elif name == "stop":
                if not current["loaded"]:
                    return True, "The service is already stopped."
                cmd = ["launchctl", "bootout", f"{target}/{LABEL}"]
            else:
                return False, "Unknown action."
            out = _run(cmd)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    finally:
        _cache["at"] = 0
    if out is not None and out.returncode != 0:
        return False, (out.stdout + out.stderr).strip()[-600:] or f"{name} failed"
    return True, {"install": "Installed and started.", "uninstall": "Uninstalled.", "start": "Started.",
                  "stop": "Stopped." if plat.IS_MAC else "Stopped. Scans already running finish on their own."}[name]


def sync() -> str | None:
    """macOS only: rewrites the launchd plist when the folders it watches changed. Returns a note for the page.
    The Windows task needs no update, since each check reads the project list itself."""
    if not plat.IS_MAC:
        return None
    installed = dispatch.installed_watch_paths()
    if installed is None or os.environ.get("KATANA_HOME") or installed == dispatch.watch_paths():
        return None
    out = _run([str(SCRIPTS / "install-service.sh")])
    _cache["at"] = 0
    return "Service updated to watch the new input folders." if out.returncode == 0 \
        else f"Couldn't update the service: {(out.stderr or out.stdout).strip()[:300]}"


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "status":
        for k, v in view(fresh=True).items():
            if k != "log":
                print(f"{k}: {v}")
        return
    if command not in ACTIONS:
        raise SystemExit("usage: service.py install|uninstall|start|stop|status")
    ok, message = action(command)
    print(message)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
