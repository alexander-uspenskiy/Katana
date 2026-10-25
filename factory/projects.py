"""Katana project registry: config/projects.json lists the project folders Katana knows and the active one.

A project folder holds input/, runs/, logs/ and .katana/settings.json (that project's settings).
The dashboard shows the active project; the service processes every registered project.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO / "config" / "projects.json"
LEGACY_SETTINGS = REPO / "config" / "settings.json"  # before projects, settings lived here
SUBDIRS = ("input", "runs", "logs", ".katana")
MAX_RECENT = 20


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
    REGISTRY_PATH.parent.mkdir(exist_ok=True)
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
    for d in SUBDIRS:
        (path / d).mkdir(parents=True, exist_ok=True)


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
    """Registers a project folder (creating input/, runs/, logs/ and .katana/ in it) and makes it active."""
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
        target = project / ".katana" / "settings.json"
        if not target.exists():
            target.write_text(json.dumps(old, indent=2) + "\n")
        LEGACY_SETTINGS.rename(LEGACY_SETTINGS.with_suffix(".json.migrated"))
    elif any((REPO / "runs").glob("*/state.json")):
        project = REPO
        ensure_structure(project)
    if project:
        reg = {"active": str(project),
               "projects": [{"path": str(project), "opened": datetime.now().isoformat(timespec="seconds")}]}
    save(reg)
