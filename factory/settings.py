"""Cortana settings: defaults in config/defaults.json, each project's changes in <project>/.cortana/settings.json.

Shared by the orchestrator (reads them before every station) and the dashboard (edits them).
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

import projects

REPO = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO / "factory" / "agents"
DEFAULTS_PATH = REPO / "config" / "defaults.json"

MODELS = ["fable", "opus", "sonnet", "haiku"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MODES = ["deterministic", "auto"]
# What starts a project's runs. folder-interval and folder-watch are run by the service; once and manual
# only by Run now in the dashboard or `orchestrator.py run`. See factory/triggers/ for the connectors.
TRIGGERS = ["folder-interval", "folder-watch", "once", "manual"]
SERVICE_TRIGGERS = ("folder-interval", "folder-watch")
ROUTER = "orchestrator"  # the agent that picks the next stage in auto mode; never a pipeline stage

STAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
OUTPUT_PATH = re.compile(r"^[\w-]+(/[\w-]+)*\.md$")
VERDICT = re.compile(r"^[A-Z][A-Z_]{1,30}$")


def defaults() -> dict:
    return json.loads(DEFAULTS_PATH.read_text())


def settings_path(project: Path) -> Path:
    return Path(project) / ".cortana" / "settings.json"


def load(project: Path | None = None) -> dict:
    """Defaults merged with the settings of `project` (the active project if omitted)."""
    project = project or home()
    merged = defaults()
    try:
        user = json.loads(settings_path(project).read_text()) if project else {}
    except (OSError, json.JSONDecodeError):
        user = {}
    if project and "project" not in user:
        merged["project"] = Path(project).name
    for key, value in user.items():
        if key == "agents" and isinstance(value, dict):
            for name, conf in value.items():
                merged["agents"].setdefault(name, {}).update(conf)
        else:
            merged[key] = value
    return merged


def home(conf: dict | None = None) -> Path | None:
    """The active project folder (holds input/, runs/, logs/ and .cortana/), or None before one is chosen.
    CORTANA_HOME overrides it, for testing. `conf` is accepted for older callers and ignored."""
    override = os.environ.get("CORTANA_HOME")
    return Path(override).expanduser().resolve() if override else projects.active()


def trigger(conf: dict) -> dict:
    t = dict(defaults()["trigger"], **(conf.get("trigger") or {}))
    return t if t["type"] in TRIGGERS else defaults()["trigger"]


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

    t = new.get("trigger") or {}
    clean["trigger"] = {"type": t.get("type")}
    if t.get("type") not in TRIGGERS:
        errors.append("Choose what starts a run.")
    try:
        clean["trigger"]["interval_min"] = int(t.get("interval_min"))
        if not 1 <= clean["trigger"]["interval_min"] <= 1440:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Scan interval must be a whole number of minutes from 1 to 1440.")

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


def save(clean: dict, project: Path | None = None) -> None:
    path = settings_path(project or home())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2) + "\n")
    tmp.replace(path)


def effective_agent(conf: dict, catalog: dict, name: str) -> dict:
    """The agent definition with the model and effort chosen in settings."""
    agent = copy.deepcopy(catalog[name])
    agent.update({k: v for k, v in (conf.get("agents") or {}).get(name, {}).items() if v})
    return agent
