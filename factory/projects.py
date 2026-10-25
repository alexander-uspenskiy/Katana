"""Katana project registry: ~/.katana/projects.json lists the project folders Katana knows and the active one.

User-level state lives in ~/.katana/ (KATANA_USER_DIR overrides it): projects.json, auth.json, agents/ (edited
prompts) and logs/launchd.log. It is shared by every project and never in git.

A project folder holds input/ (requirements), katana.config.json (that project's settings) and .katana/, where
Katana keeps everything it generates: .katana/runs/ and .katana/logs/.
The dashboard shows the active project; the service processes every registered project.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
USER_DIR = Path(os.environ.get("KATANA_USER_DIR") or Path.home() / ".katana").expanduser()
REGISTRY_PATH = USER_DIR / "projects.json"
LEGACY_SETTINGS = REPO / "config" / "settings.json"  # before projects, settings lived here
CONFIG_NAME = "katana.config.json"
STATE_DIR = ".katana"
SUBDIRS = ("input", f"{STATE_DIR}/runs", f"{STATE_DIR}/logs")
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


def state_dir(project) -> Path:
    return Path(project) / STATE_DIR


def runs_dir(project) -> Path:
    return state_dir(project) / "runs"


def logs_dir(project) -> Path:
    return state_dir(project) / "logs"


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
    for d in SUBDIRS:
        (path / d).mkdir(parents=True, exist_ok=True)


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


def upgrade_layout(path) -> None:
    """Moves a project from the old layout (runs/ and logs/ in the project root, settings in
    .katana/settings.json) to the current one. Safe to repeat; a scan in progress keeps its runs/ where it is
    until the next call."""
    path = Path(path)
    state = state_dir(path)
    old_conf = state / "settings.json"
    if old_conf.is_file() and not config_path(path).exists():
        old_conf.replace(config_path(path))
    if locked(path / "runs" / ".lock"):
        return
    # Only what Katana itself wrote: a project may have its own runs/ or logs/ folder.
    ours = {"runs": lambda d: (d / ".factory.json").exists() or any(d.glob("*/state.json")),
            "logs": lambda d: (d / "factory.log").exists() or (d / "worker.log").exists()}
    for name, is_ours in ours.items():
        old, new = path / name, state / name
        if old.is_dir() and not new.exists() and is_ours(old):
            state.mkdir(exist_ok=True)
            try:
                old.rename(new)
            except OSError:
                pass  # another process moved it first


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
