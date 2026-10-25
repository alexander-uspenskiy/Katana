"""Cortana settings: defaults in config/defaults.json, user changes in config/settings.json.

Shared by the orchestrator (reads them before every station) and the dashboard (edits them).

The plan (`pipeline`) is a list of phases; each phase is a list of stages that run at the same time.
A stage is an agent stage, or a gate step (`"kind": "gate"`) that Cortana measures itself (see gates.py).
Old flat plans, a list of stages, load as one stage per phase.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

import gates

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
        elif key == "gates" and isinstance(value, dict):
            merged.setdefault("gates", {}).update(value)
        else:
            merged[key] = value
    merged["pipeline"] = phases(merged.get("pipeline"))
    return merged


def phases(pipeline) -> list:
    """The plan as a list of phases. A flat list of stages (the old format) becomes one stage per phase."""
    out = []
    for item in pipeline or []:
        phase = [item] if isinstance(item, dict) else item if isinstance(item, list) else []
        phase = [s for s in phase if isinstance(s, dict)]
        if phase:
            out.append(phase)
    return out


def stages(pipeline) -> list:
    """Every stage of the plan in order, phase by phase."""
    return [s for phase in phases(pipeline) for s in phase]


def is_gate(stage: dict) -> bool:
    return stage.get("kind") == "gate"


def writes_code(catalog: dict, stage: dict) -> bool:
    """Agents marked `writes_code: yes` change the code; the others only write their own report."""
    if is_gate(stage):
        return False
    return str((catalog.get(stage.get("agent")) or {}).get("writes_code", "")).lower() in ("yes", "true")


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
                                  ("station_timeout_min", "Stage time limit", 5, 240),
                                  ("max_parallel", "Max stages at once", 1, 8)):
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

    clean["worktrees"] = bool(new.get("worktrees"))
    g = new.get("gates") or {}
    clean["gates"] = {"test_command": str(g.get("test_command") or "").strip()[:500],
                      "coverage_command": str(g.get("coverage_command") or "").strip()[:500],
                      "when_unavailable": g.get("when_unavailable") or "warn",
                      "security_source": g.get("security_source") or "auto"}
    if clean["gates"]["when_unavailable"] not in gates.UNAVAILABLE:
        errors.append("When a gate tool is missing: choose warn, block or ignore.")
    if clean["gates"]["security_source"] not in gates.SECURITY_SOURCES:
        errors.append("Security findings source: choose auto, tools, report or both.")
    try:
        clean["gates"]["timeout_min"] = int(g.get("timeout_min") or 10)
        if not 1 <= clean["gates"]["timeout_min"] <= 120:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Gate time limit must be a whole number from 1 to 120.")

    plan = phases(new.get("pipeline"))
    clean["pipeline"] = []
    if not plan:
        errors.append("The plan needs at least one stage.")
    ids = [str(s.get("id", "")) for phase in plan for s in phase]
    for p, phase in enumerate(plan, 1):
        out_phase = []
        for k, s in enumerate(phase, 1):
            label = f"Phase {p}, stage {k}" if len(phase) > 1 else f"Phase {p}"
            sid = str(s.get("id", ""))
            if not STAGE_ID.match(sid):
                errors.append(f"{label}: id must be lowercase letters, digits and dashes.")
            elif ids.count(sid) > 1:
                errors.append(f"{label}: id {sid} is used twice.")
            on_fail = s.get("on_fail") or None
            if on_fail and on_fail not in ids:
                errors.append(f"{label}: 'on fail' must point to a stage in the plan.")
            if s.get("kind") == "gate":
                out_phase.append(validate_gate(s, sid, label, on_fail, errors))
                continue
            agent = s.get("agent")
            if agent not in catalog or agent == ROUTER:
                errors.append(f"{label}: choose an agent.")
            output = str(s.get("output", ""))
            if not OUTPUT_PATH.match(output):
                errors.append(f"{label}: output must be a relative .md path such as docs/review.md.")
            verdicts = [str(v).strip().upper() for v in s.get("pass") or [] if str(v).strip()]
            if any(not VERDICT.match(v) for v in verdicts):
                errors.append(f"{label}: pass verdicts are uppercase words such as APPROVE.")
            stage = {"id": sid, "title": str(s.get("title") or sid)[:60], "agent": agent, "output": output,
                     "task": str(s.get("task") or "")[:2000]}
            if verdicts:
                stage.update({"pass": verdicts, "on_fail": on_fail, "required": bool(s.get("required"))})
            out_phase.append(stage)
        check_phase(catalog, out_phase, p, errors)
        clean["pipeline"].append(out_phase)
    return clean, errors


def validate_gate(s: dict, sid: str, label: str, on_fail, errors: list) -> dict:
    conditions = []
    for n, c in enumerate(s.get("conditions") or [], 1):
        c = c or {}
        metric = c.get("metric")
        if metric not in gates.METRICS:
            errors.append(f"{label}, condition {n}: choose a metric.")
            continue
        try:
            value = float(c.get("value"))
            if value < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{label}, condition {n}: the threshold must be a number of 0 or more.")
            value = 0
        if c.get("op") not in gates.OPS:
            errors.append(f"{label}, condition {n}: choose a comparison.")
        if c.get("severity") not in gates.SEVERITIES:
            errors.append(f"{label}, condition {n}: severity is block or warn.")
        cond = {"metric": metric, "op": c.get("op"), "value": int(value) if value == int(value) else value,
                "severity": c.get("severity")}
        if "level" in gates.METRICS[metric]:
            cond["level"] = c.get("level") if c.get("level") in gates.LEVELS else gates.METRICS[metric]["level"]
        conditions.append(cond)
    if not conditions and not any(e.startswith(label + ", condition") for e in errors):
        errors.append(f"{label}: a gate needs at least one condition.")
    output = str(s.get("output") or gates.REPORT)
    if not OUTPUT_PATH.match(output):
        errors.append(f"{label}: the gate report must be a relative .md path such as {gates.REPORT}.")
    return {"id": sid, "title": str(s.get("title") or sid)[:60], "kind": "gate", "output": output,
            "conditions": conditions, "on_fail": on_fail, "required": bool(s.get("required"))}


def check_phase(catalog: dict, phase: list, p: int, errors: list) -> None:
    """Stages in one phase run at the same time in one workspace, so at most one may change the code."""
    coders = [s for s in phase if writes_code(catalog, s)]
    if len(coders) > 1:
        names = ", ".join(s["title"] for s in coders)
        errors.append(f"Phase {p}: only one stage per phase may change code ({names}). Move the others to "
                      "their own phase.")
    if any(is_gate(s) for s in phase) and not all(is_gate(s) for s in phase):
        errors.append(f"Phase {p}: a gate measures the phases before it, so put it in a phase of its own.")
    outputs = [s.get("output") for s in phase]
    if len(phase) > 1 and len(set(outputs)) < len(outputs):
        errors.append(f"Phase {p}: stages that run at the same time need different output documents.")


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
