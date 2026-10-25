"""Cortana settings: defaults in config/defaults.json, user changes in config/settings.json.

Shared by the orchestrator (reads them before every station) and the dashboard (edits them).
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO / "factory" / "agents"
DEFAULTS_PATH = REPO / "config" / "defaults.json"
SETTINGS_PATH = REPO / "config" / "settings.json"
PROMPTS_DIR = REPO / "config" / "agents"  # prompt overrides edited in the dashboard (not in git)

MODELS = ["fable", "opus", "sonnet", "haiku"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MODES = ["deterministic", "auto"]
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
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = dict(merged[key], **value)  # budgets, notify: keep defaults for new keys
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
        controls = _agent_controls(name, conf or {}, catalog, errors)
        clean["agents"][name] = dict({"model": model, "effort": effort}, **controls)

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

    off = disabled_agents(clean)
    if clean["pipeline"] and all(st["agent"] in off for st in clean["pipeline"]):
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
    titles = {s["id"]: s["title"] for s in clean.get("pipeline", [])}
    for n, s in enumerate(clean.get("pipeline", []), 1):
        if s["agent"] in off and s.get("pass"):
            kind = "a required gate" if s.get("required") else "a gate"
            out.append(f"Stage {n} ({s['title']}) is {kind}, but its agent is disabled: it will be skipped"
                       + (", so full-auto runs can finish without it." if s.get("required") else "."))
        elif s.get("on_fail") and next((t["agent"] for t in clean["pipeline"] if t["id"] == s["on_fail"]), None) in off:
            out.append(f"Stage {n} ({s['title']}) sends failures back to {titles[s['on_fail']]}, whose agent is "
                       "disabled: rework continues at the next enabled stage.")
    return out


def save(clean: dict) -> None:
    SETTINGS_PATH.parent.mkdir(exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2) + "\n")
    tmp.replace(SETTINGS_PATH)


def effective_agent(conf: dict, catalog: dict, name: str) -> dict:
    """The agent definition with the model, effort, tools and enabled switch chosen in settings."""
    agent = copy.deepcopy(catalog[name])
    agent.update({k: v for k, v in (conf.get("agents") or {}).get(name, {}).items() if v not in (None, "")})
    agent["enabled"] = agent.get("enabled") is not False
    return agent
