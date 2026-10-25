"""Cortana settings: defaults in config/defaults.json, user changes in config/settings.json.

Shared by the orchestrator (reads them before every station) and the dashboard (edits them).
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO / "factory" / "agents"
DEFAULTS_PATH = REPO / "config" / "defaults.json"
SETTINGS_PATH = REPO / "config" / "settings.json"

MODELS = ["fable", "opus", "sonnet", "haiku"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MODES = ["deterministic", "auto"]
ROUTER = "orchestrator"  # the agent that picks the next stage in auto mode; never a pipeline stage

STAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
OUTPUT_PATH = re.compile(r"^[\w-]+(/[\w-]+)*\.md$")
VERDICT = re.compile(r"^[A-Z][A-Z_]{1,30}$")


def defaults() -> dict:
    return json.loads(DEFAULTS_PATH.read_text())


def load() -> dict:
    merged = defaults()
    try:
        user = json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        user = {}
    for key, value in user.items():
        if key == "agents" and isinstance(value, dict):
            for name, conf in value.items():
                merged["agents"].setdefault(name, {}).update(conf)
        else:
            merged[key] = value
    return merged


def home(conf: dict) -> Path:
    """The project folder: holds input/, runs/ and logs/."""
    override = os.environ.get("CORTANA_HOME")
    return Path(override or conf.get("home") or REPO).expanduser().resolve()


def load_agent(name: str) -> dict:
    text = (AGENTS_DIR / f"{name}.md").read_text()
    _, front, body = text.split("---", 2)
    agent = {"name": name, "prompt": body.strip()}
    for line in front.strip().splitlines():
        key, _, value = line.partition(":")
        agent[key.strip()] = value.strip()
    agent["tools"] = [t.strip() for t in agent.get("tools", "").split(",") if t.strip()]
    return agent


def agent_catalog() -> dict:
    return {p.stem: load_agent(p.stem) for p in sorted(AGENTS_DIR.glob("*.md"))}


def validate(new: dict) -> tuple:
    """Returns (clean settings, list of errors). Only known keys survive."""
    errors, catalog = [], agent_catalog()
    clean = {"project": str(new.get("project") or "Cortana")[:60]}

    folder = str(new.get("home") or "").strip()
    if folder:
        path = Path(folder).expanduser()
        if not path.is_absolute():
            errors.append("Project folder must be an absolute path.")
        elif path.exists() and not path.is_dir():
            errors.append("Project folder exists but is not a folder.")
        elif not path.exists() and not path.parent.is_dir():
            errors.append("Project folder's parent folder does not exist.")
    clean["home"] = folder

    clean["mode"] = new.get("mode")
    if clean["mode"] not in MODES:
        errors.append("Mode must be deterministic or auto.")
    for key, label, low, high in (("max_iterations", "Max rework loops per gate", 1, 10),
                                  ("max_stations", "Max stages per run", 3, 100),
                                  ("station_timeout_min", "Stage time limit", 5, 240)):
        try:
            clean[key] = int(new.get(key))
            if not low <= clean[key] <= high:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{label} must be a whole number from {low} to {high}.")

    clean["agents"] = {}
    for name, conf in (new.get("agents") or {}).items():
        if name not in catalog:
            errors.append(f"Unknown agent: {name}.")
            continue
        model, effort = (conf or {}).get("model"), (conf or {}).get("effort")
        if model not in MODELS or effort not in EFFORTS:
            errors.append(f"{catalog[name].get('title', name)}: pick a model and an effort level.")
        clean["agents"][name] = {"model": model, "effort": effort}

    stages = new.get("pipeline") or []
    clean["pipeline"] = []
    if not stages:
        errors.append("The plan needs at least one stage.")
    ids = [str(s.get("id", "")) for s in stages]
    for n, s in enumerate(stages, 1):
        label = f"Stage {n}"
        sid = str(s.get("id", ""))
        if not STAGE_ID.match(sid):
            errors.append(f"{label}: id must be lowercase letters, digits and dashes.")
        elif ids.count(sid) > 1:
            errors.append(f"{label}: id {sid} is used twice.")
        agent = s.get("agent")
        if agent not in catalog or agent == ROUTER:
            errors.append(f"{label}: choose an agent.")
        output = str(s.get("output", ""))
        if not OUTPUT_PATH.match(output):
            errors.append(f"{label}: output must be a relative .md path such as docs/review.md.")
        verdicts = [str(v).strip().upper() for v in s.get("pass") or [] if str(v).strip()]
        if any(not VERDICT.match(v) for v in verdicts):
            errors.append(f"{label}: pass verdicts are uppercase words such as APPROVE.")
        on_fail = s.get("on_fail") or None
        if verdicts and on_fail and on_fail not in ids:
            errors.append(f"{label}: 'on fail' must point to a stage in the plan.")
        stage = {"id": sid, "title": str(s.get("title") or sid)[:60], "agent": agent, "output": output,
                 "task": str(s.get("task") or "")[:2000]}
        if verdicts:
            stage.update({"pass": verdicts, "on_fail": on_fail, "required": bool(s.get("required"))})
        clean["pipeline"].append(stage)
    return clean, errors


def save(clean: dict) -> None:
    SETTINGS_PATH.parent.mkdir(exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2) + "\n")
    tmp.replace(SETTINGS_PATH)


def effective_agent(conf: dict, catalog: dict, name: str) -> dict:
    """The agent definition with the model and effort chosen in settings."""
    agent = copy.deepcopy(catalog[name])
    agent.update({k: v for k, v in (conf.get("agents") or {}).get(name, {}).items() if v})
    return agent
