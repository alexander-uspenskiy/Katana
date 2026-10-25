"""Cortana settings: defaults in config/defaults.json, each project's changes in <project>/.cortana/settings.json.

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
from urllib.parse import urlparse

import gates
import projects

REPO = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO / "factory" / "agents"
DEFAULTS_PATH = REPO / "config" / "defaults.json"
PROMPTS_DIR = REPO / "config" / "agents"  # prompt overrides edited in the dashboard (not in git)

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

# One-click model and effort presets for every agent. Balanced is the shipped default.
PRESETS = {
    "fast": {"title": "Fast", "desc": "Sonnet and Haiku at low effort. Cheapest and quickest; fine for small changes.",
             "agents": {"orchestrator": ("haiku", "low"), "ba": ("sonnet", "low"), "architect": ("sonnet", "low"),
                        "security": ("sonnet", "low"), "developer": ("sonnet", "low"), "tester": ("haiku", "low"),
                        "reviewer": ("sonnet", "low")}},
    "balanced": {"title": "Balanced", "desc": "Opus for design and review, Sonnet for building and testing.",
                 "agents": None},  # filled from defaults.json
    "max": {"title": "Max quality", "desc": "Opus everywhere at high or xhigh effort. Best results, most usage.",
            "agents": {"orchestrator": ("opus", "high"), "ba": ("opus", "xhigh"), "architect": ("opus", "xhigh"),
                       "security": ("opus", "xhigh"), "developer": ("opus", "high"), "tester": ("opus", "high"),
                       "reviewer": ("opus", "xhigh")}},
}

# Tool allowlist entries an agent may get: built-in tools, or Bash limited to one named command.
SAFE_TOOL = re.compile(r"^(Read|Write|Edit|MultiEdit|Glob|Grep|LS|NotebookRead|NotebookEdit|TodoWrite|WebSearch"
                       r"|WebFetch(\(domain:[a-z0-9.-]+\))?"
                       r"|Bash\([\w./-]+( [\w./=@:,+-]+)*( \*|:\*)?\))$")
UNSAFE_COMMANDS = {"sudo", "su", "rm", "curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "telnet", "eval",
                   "exec", "sh", "bash", "zsh", "fish", "env", "xargs", "osascript", "launchctl", "security", "open",
                   "dd", "mkfs", "diskutil", "chown", "kill", "killall", "pkill", "crontab", "defaults", "claude"}
MAX_TOKENS_CAP = 10 ** 11
TEAMS_HOSTS = (".webhook.office.com", ".logic.azure.com", ".powerplatform.com", ".powerautomate.com")
NOTIFY_EVENTS = ("done", "failed", "blocked", "paused")


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
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = dict(merged[key], **value)  # budgets, notify: keep defaults for new keys
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
    agent["default_prompt"], agent["prompt_override"] = agent["prompt"], False
    override = PROMPTS_DIR / f"{name}.md"
    if override.is_file():
        text = override.read_text().strip()
        if text.startswith("---") and text.count("---") >= 2:  # a full agent file: keep only the body
            text = text.split("---", 2)[2].strip()
        if text:
            agent["prompt"], agent["prompt_override"] = text, True
    return agent


def agent_catalog() -> dict:
    return {p.stem: load_agent(p.stem) for p in sorted(AGENTS_DIR.glob("*.md"))}


def save_prompt(name: str, text: str) -> None:
    """Saves a prompt override, or removes it when the text is empty or the same as the default."""
    path = PROMPTS_DIR / f"{name}.md"
    if not text.strip() or text.strip() == load_agent(name)["default_prompt"]:
        path.unlink(missing_ok=True)
        return
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text.strip() + "\n")
    tmp.replace(path)


def presets() -> dict:
    """Preset name → title, description and {agent: {model, effort}}."""
    out = {}
    for key, p in PRESETS.items():
        agents = p["agents"] or {n: (a["model"], a["effort"]) for n, a in defaults()["agents"].items()}
        out[key] = {"title": p["title"], "desc": p["desc"],
                    "agents": {n: {"model": m, "effort": e} for n, (m, e) in agents.items()}}
    return out


def disabled_agents(conf: dict) -> set:
    return {n for n, a in (conf.get("agents") or {}).items() if a.get("enabled") is False}


def tool_problem(tool: str) -> str | None:
    """Why a tool allowlist entry is unsafe, or None if it is fine."""
    if not SAFE_TOOL.match(tool):
        return "is not an allowed tool. Use a built-in tool such as Read or Edit, or Bash(command *) for one command"
    command = re.match(r"^Bash\(([^ )]+)", tool)
    if command and command.group(1).split("/")[-1] in UNSAFE_COMMANDS:
        return f"gives {command.group(1)}, which agents may not run"
    return None


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
        controls = _agent_controls(name, conf or {}, catalog, errors)
        clean["agents"][name] = dict({"model": model, "effort": effort}, **controls)

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
    off = disabled_agents(clean)
    agent_stages = [st for st in stages(clean["pipeline"]) if not is_gate(st)]
    if agent_stages and all(st["agent"] in off for st in agent_stages):
        errors.append("Every stage in the plan uses a disabled agent. Enable at least one.")
    if clean["mode"] == "auto" and ROUTER in off:
        errors.append("Full auto needs the Orchestrator agent. Enable it or switch to deterministic mode.")
    clean["budgets"] = _validate_budgets(new.get("budgets") or {}, errors)
    clean["notify"] = _validate_notify(new.get("notify") or {}, errors)
    clean["setup_completed"] = str(new.get("setup_completed") or "")[:40]
    return clean, errors


def _agent_controls(name: str, conf: dict, catalog: dict, errors: list) -> dict:
    """The enabled switch and tool allowlist override of one agent. Only non-default values are kept."""
    out, title = {}, catalog[name].get("title", name)
    if conf.get("enabled") is False:
        out["enabled"] = False
    tools = conf.get("tools")
    if tools is not None and name != ROUTER:
        if isinstance(tools, str):
            tools = tools.split(",")
        tools = list(dict.fromkeys(str(t).strip() for t in tools if str(t).strip()))
        if len(tools) > 40:
            errors.append(f"{title}: at most 40 tools.")
        for tool in tools:
            problem = tool_problem(tool[:120])
            if problem:
                errors.append(f"{title}: tool “{tool[:60]}” {problem}.")
        if tools != catalog[name]["tools"]:
            out["tools"] = tools
    return out


def _validate_budgets(b: dict, errors: list) -> dict:
    clean = {"pause_near_limit": bool(b.get("pause_near_limit"))}
    for key, label in (("run_tokens", "Token cap per run"), ("daily_tokens", "Daily token cap")):
        try:
            clean[key] = int(b.get(key) or 0)
            if not 0 <= clean[key] <= MAX_TOKENS_CAP:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{label} must be a whole number of tokens, or 0 for no cap.")
            clean[key] = 0
    return clean


def _validate_notify(n: dict, errors: list) -> dict:
    events = n.get("events") or {}
    clean = {"macos": bool(n.get("macos")), "teams_webhook": str(n.get("teams_webhook") or "").strip()[:1000],
             "events": {e: bool(events.get(e)) for e in NOTIFY_EVENTS}}
    if clean["teams_webhook"] and not valid_webhook(clean["teams_webhook"]):
        errors.append("The Teams webhook must be an https URL on webhook.office.com, logic.azure.com or "
                      "powerplatform.com.")
    return clean


def valid_webhook(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and not parsed.username and any(host.endswith(h) for h in TEAMS_HOSTS)


def warnings(clean: dict) -> list:
    """Problems that don't block saving, such as a required gate whose agent is disabled."""
    off, out = disabled_agents(clean), []
    plan = stages(clean.get("pipeline", []))
    titles = {s["id"]: s["title"] for s in plan}
    for n, s in enumerate(plan, 1):
        if s.get("agent") in off and s.get("pass"):
            kind = "a required gate" if s.get("required") else "a gate"
            out.append(f"Stage {n} ({s['title']}) is {kind}, but its agent is disabled: it will be skipped"
                       + (", so full-auto runs can finish without it." if s.get("required") else "."))
        elif s.get("on_fail") and next((t.get("agent") for t in plan if t["id"] == s["on_fail"]), None) in off:
            out.append(f"Stage {n} ({s['title']}) sends failures back to {titles[s['on_fail']]}, whose agent is "
                       "disabled: rework continues at the next enabled stage.")
    return out




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


def save(clean: dict, project: Path | None = None) -> None:
    path = settings_path(project or home())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2) + "\n")
    tmp.replace(path)


def effective_agent(conf: dict, catalog: dict, name: str) -> dict:
    """The agent definition with the model, effort, tools and enabled switch chosen in settings."""
    agent = copy.deepcopy(catalog[name])
    agent.update({k: v for k, v in (conf.get("agents") or {}).get(name, {}).items() if v not in (None, "")})
    agent["enabled"] = agent.get("enabled") is not False
    return agent
