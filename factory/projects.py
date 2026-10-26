"""Katana project registry: ~/.katana/projects.json lists the project folders Katana knows and the active one.

User-level state lives in ~/.katana/ (KATANA_USER_DIR overrides it): projects.json, auth.json, agents/ (edited
prompts), logs/launchd.log and projects/<key>/ with each project's logs/ (factory.log, worker.log) and usage/ (a
small record per run, see ledger.py). It is shared by every project and never in git, so the dashboard can total
the token spend of the whole account, including projects that were since removed from the list.

A project folder holds input/ (requirements), katana.config.json (that project's settings) and .katana/runs/,
where Katana keeps what each run generates.
The dashboard shows the active project; the service processes every registered project.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
USER_DIR = Path(os.environ.get("KATANA_USER_DIR") or Path.home() / ".katana").expanduser()
REGISTRY_PATH = USER_DIR / "projects.json"
LEGACY_SETTINGS = REPO / "config" / "settings.json"  # before projects, settings lived here
CONFIG_NAME = "katana.config.json"
PROJECTS_DIR = USER_DIR / "projects"  # one folder per project folder: logs/, usage/ and project.json
LOCAL_DIR = ".katana"  # inside the project folder: runs/ (and, before logs moved to the user level, logs/)
MAX_RECENT = 20


def upgrade_user_dir() -> None:
    """Moves user-level files from the old home, the repository's config/, into USER_DIR. Safe to repeat."""
    for name in ("projects.json", "auth.json", "agents"):
        old, new = REPO / "config" / name, USER_DIR / name
        if old.exists() and not new.exists():
            try:
                USER_DIR.mkdir(parents=True, exist_ok=True)
                old.rename(new)
            except OSError:
                pass  # another process moved it first


upgrade_user_dir()


def config_path(project) -> Path:
    return Path(project) / CONFIG_NAME


def project_key(project) -> str:
    """The name of the project's folder under ~/.katana/projects/: its own name plus a hash of its full path."""
    path = _norm(project)
    slug = re.sub(r"[^a-z0-9]+", "-", Path(path).name.lower()).strip("-")[:40] or "project"
    return f"{slug}-{hashlib.sha1(path.encode()).hexdigest()[:8]}"


def state_dir(project) -> Path:
    """The project's folder under ~/.katana/projects/, for its logs and usage statistics."""
    return PROJECTS_DIR / project_key(project)


def local_dir(project) -> Path:
    return Path(project) / LOCAL_DIR


def runs_dir(project) -> Path:
    return local_dir(project) / "runs"


def logs_dir(project) -> Path:
    return state_dir(project) / "logs"


def usage_dir(project) -> Path:
    """One small JSON record per run (see ledger.py): what the account-wide totals are built from."""
    return state_dir(project) / "usage"


def _norm(path) -> str:
    return str(Path(path).expanduser().resolve())


def load() -> dict:
    """{"active": path or None, "projects": [{"path", "opened"}], most recently opened first}."""
    if not REGISTRY_PATH.exists():
        migrate()
    try:
        reg = json.loads(REGISTRY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        reg = {}
    projects = [p for p in reg.get("projects") or [] if isinstance(p, dict) and p.get("path")]
    return {"active": reg.get("active") or None, "projects": projects}


def save(reg: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    reg["projects"] = sorted(reg["projects"], key=lambda p: p.get("opened") or "", reverse=True)[:MAX_RECENT]
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2) + "\n")
    tmp.replace(REGISTRY_PATH)


def active() -> Path | None:
    path = load()["active"]
    return Path(path) if path else None


def registered() -> list:
    return [Path(p["path"]) for p in load()["projects"]]


def ensure_structure(path: Path) -> None:
    upgrade_layout(path)
    (path / "input").mkdir(parents=True, exist_ok=True)
    for d in (runs_dir(path), logs_dir(path)):
        d.mkdir(parents=True, exist_ok=True)
    write_meta(path)


def write_meta(path) -> None:
    """Records which folder a state directory belongs to, so the account totals can name it later."""
    meta = {"path": _norm(path), "name": Path(_norm(path)).name}
    target = state_dir(path) / "project.json"
    try:
        if read_meta(target.parent) != meta:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(meta, indent=2) + "\n")
    except OSError:
        pass


def read_meta(directory: Path) -> dict:
    try:
        meta = json.loads((Path(directory) / "project.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def known_state() -> list:
    """Every project that has state under ~/.katana/projects/, registered or not:
    [{"key", "dir", "path", "name"}]. A project taken off the list keeps its history here."""
    out = []
    for d in sorted(PROJECTS_DIR.glob("*")) if PROJECTS_DIR.is_dir() else []:
        if d.is_dir():
            meta = read_meta(d)
            out.append({"key": d.name, "dir": d, "path": meta.get("path"), "name": meta.get("name") or d.name})
    return out


def locked(lock_path: Path) -> bool:
    """True while another process holds the flock on `lock_path`."""
    if not lock_path.exists():
        return False
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def _move_into(old: Path, new: Path) -> None:
    """Moves what is in `old` into `new`, leaving anything `new` already has. Removes `old` once empty."""
    new.mkdir(parents=True, exist_ok=True)
    for child in old.iterdir():
        target = new / child.name
        if not target.exists():
            try:
                shutil.move(str(child), str(target))
            except (OSError, shutil.Error):
                pass  # another process moved it first
    try:
        old.rmdir()
    except OSError:
        pass


def upgrade_layout(path) -> None:
    """Moves a project from an older layout to the current one: settings from .katana/settings.json to
    katana.config.json, runs/ from the project root into .katana/, and the logs (from the project root or from
    .katana/logs) into ~/.katana/projects/<key>/logs/. Safe to repeat; a scan in progress keeps its files where
    they are until the next call."""
    path = Path(path)
    local = path / LOCAL_DIR
    old_conf = local / "settings.json"
    if old_conf.is_file() and not config_path(path).exists():
        old_conf.replace(config_path(path))
    if locked(path / "runs" / ".lock") or locked(local / "runs" / ".lock"):
        return
    # Only what Katana itself wrote: a project may have its own runs/ or logs/ folder.
    ours = {"runs": lambda d: (d / ".factory.json").exists() or any(d.glob("*/state.json")),
            "logs": lambda d: (d / "factory.log").exists() or (d / "worker.log").exists()}
    old_runs = path / "runs"
    if old_runs.is_dir() and not runs_dir(path).exists() and ours["runs"](old_runs):
        local.mkdir(exist_ok=True)
        try:
            old_runs.rename(runs_dir(path))
        except OSError:
            pass  # another process moved it first
    old_logs = [d for d in (path / "logs", local / "logs") if d.is_dir() and (d != path / "logs" or ours["logs"](d))]
    if old_logs:
        USER_DIR.mkdir(parents=True, exist_ok=True)
        with open(USER_DIR / ".migrate.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for old in old_logs:
                if old.is_dir():
                    _move_into(old, logs_dir(path))
    try:
        local.rmdir()  # nothing left in it
    except OSError:
        pass
    if state_dir(path).is_dir():
        write_meta(path)


def upgrade_all() -> None:
    """upgrade_layout for every registered project, so the dashboard sees their runs at once."""
    for folder in registered():
        if folder.is_dir():
            upgrade_layout(folder)


def check_folder(path: str, must_exist: bool) -> str | None:
    """An error message, or None if `path` can be opened (must_exist) or created as a project."""
    if not path or not Path(path).expanduser().is_absolute():
        return "Enter the folder's full path, for example /Users/you/Projects/my-app."
    p = Path(path).expanduser()
    if p.exists() and not p.is_dir():
        return "That path is a file, not a folder."
    if must_exist and not p.exists():
        return "That folder doesn't exist. Use Create new project to make it."
    if not p.exists() and not p.parent.is_dir():
        return f"The parent folder {p.parent} doesn't exist."
    return None


def open_project(path, activate: bool = True) -> Path:
    """Registers a project folder (creating input/ and .katana/ in it) and makes it active."""
    p = Path(_norm(path))
    ensure_structure(p)
    reg = load()
    reg["projects"] = [e for e in reg["projects"] if _norm(e["path"]) != str(p)]
    reg["projects"].append({"path": str(p), "opened": datetime.now().isoformat(timespec="seconds")})
    if activate:
        reg["active"] = str(p)
    save(reg)
    return p


def forget(path) -> None:
    """Removes a project from the list. Its folder and files are left alone."""
    reg = load()
    reg["projects"] = [e for e in reg["projects"] if _norm(e["path"]) != _norm(path)]
    if reg["active"] and _norm(reg["active"]) == _norm(path):
        reg["active"] = None
    save(reg)


def migrate() -> None:
    """First start after projects were added: moves config/settings.json into its project folder, or
    registers the repository itself if it already has runs. Writes the registry either way."""
    reg = {"active": None, "projects": []}
    project = None
    if LEGACY_SETTINGS.exists():
        try:
            old = json.loads(LEGACY_SETTINGS.read_text())
        except (OSError, json.JSONDecodeError):
            old = {}
        project = Path(os.path.expanduser(old.pop("home", "") or str(REPO))).resolve()
        ensure_structure(project)
        target = config_path(project)
        if not target.exists():
            target.write_text(json.dumps(old, indent=2) + "\n")
        LEGACY_SETTINGS.rename(LEGACY_SETTINGS.with_suffix(".json.migrated"))
    elif any((REPO / "runs").glob("*/state.json")) or any(runs_dir(REPO).glob("*/state.json")):
        project = REPO
        ensure_structure(project)
    if project:
        reg = {"active": str(project),
               "projects": [{"path": str(project), "opened": datetime.now().isoformat(timespec="seconds")}]}
    save(reg)
