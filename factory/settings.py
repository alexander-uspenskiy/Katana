"""Katana settings: defaults in config/defaults.json, each project's changes in <project>/katana.config.json.
A few settings (`git.publish`) can also be set once for the user, in ~/.katana/settings.json; a project that sets
one itself always wins, and a project that leaves it empty uses the user's.

Shared by the orchestrator (reads them before every station) and the dashboard (edits them).

The plan (`pipeline`) is a list of phases; each phase is a list of stages that run at the same time.
A stage is an agent stage, or a gate step (`"kind": "gate"`) that Katana measures itself (see gates.py).
Old flat plans, a list of stages, load as one stage per phase.

A stage names the artifacts it reads (`inputs`) and writes (`outputs`): ids from the `artifacts` list, plus
two built in: `requirements` (the requirement Katana took from the trigger) and `code` (the workspace).
A gate step, or an agent stage with `pass` verdicts, is a gateway: `"gateway": "autonomous"` sends failed
work back by itself, `"manual"` stops the run and waits for a person to approve, send back or reject.
`custom_agents` holds agents defined in a project, next to the standard ones in factory/agents/.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import gates
import gitrepo
import projects

REPO = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO / "factory" / "agents"
DEFAULTS_PATH = REPO / "config" / "defaults.json"
PROMPTS_DIR = projects.USER_DIR / "agents"  # prompt overrides edited in the dashboard, shared by all projects

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
                        "reviewer": ("sonnet", "low"), "general": ("sonnet", "low")}},
    "balanced": {"title": "Balanced", "desc": "Opus for design and review, Sonnet for building and testing.",
                 "agents": None},  # filled from defaults.json
    "max": {"title": "Max quality", "desc": "Opus everywhere at high or xhigh effort. Best results, most usage.",
            "agents": {"orchestrator": ("opus", "high"), "ba": ("opus", "xhigh"), "architect": ("opus", "xhigh"),
                       "security": ("opus", "xhigh"), "developer": ("opus", "high"), "tester": ("opus", "high"),
                       "reviewer": ("opus", "xhigh"), "general": ("opus", "high")}},
}

# Tool allowlist entries an agent may get: built-in tools, or Bash limited to one named command.
SAFE_TOOL = re.compile(r"^(Read|Write|Edit|MultiEdit|Glob|Grep|LS|NotebookRead|NotebookEdit|TodoWrite|WebSearch"
                       r"|WebFetch(\(domain:[a-z0-9.-]+\))?"
                       r"|Bash\([\w./-]+( [\w./=@:,+-]+)*( \*|:\*)?\))$")
UNSAFE_COMMANDS = {"sudo", "su", "rm", "curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "telnet", "eval",
                   "exec", "sh", "bash", "zsh", "fish", "env", "xargs", "osascript", "launchctl", "security", "open",
                   "cmd", "powershell", "pwsh", "wscript", "cscript", "reg", "schtasks", "cmdkey", "runas", "start",
                   "dd", "mkfs", "diskutil", "chown", "kill", "killall", "pkill", "crontab", "defaults", "claude"}
MAX_TOKENS_CAP = 10 ** 11
TEAMS_HOSTS = (".webhook.office.com", ".logic.azure.com", ".powerplatform.com", ".powerautomate.com")
NOTIFY_EVENTS = ("done", "failed", "blocked", "paused", "approval")
GATEWAYS = ["autonomous", "manual"]
AGENT_NAME = re.compile(r"^[a-z][a-z0-9-]{1,29}$")
DEFAULT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
# Artifacts every plan can use. `requirements` is written by Katana; `code` is the run's workspace.
BUILTIN_ARTIFACTS = [
    {"id": "requirements", "title": "Requirements", "path": "docs/00-requirements.md", "builtin": True,
     "description": "The requirement Katana took from the trigger"},
    {"id": "code", "title": "Project code", "path": None, "builtin": True, "kind": "code",
     "description": "The generated project in the run's workspace"},
]


def defaults() -> dict:
    return json.loads(DEFAULTS_PATH.read_text())


def settings_path(project: Path) -> Path:
    return projects.config_path(project)


USER_SETTINGS_PATH = projects.USER_DIR / "settings.json"


def load_user() -> dict:
    """The user-level settings, {"git": {"publish": ...}}: only values that are valid are kept."""
    try:
        raw = json.loads(USER_SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    publish = ((raw.get("git") if isinstance(raw, dict) else None) or {}).get("publish")
    return {"git": {"publish": publish}} if publish in gitrepo.PUBLISH else {}


def save_user(publish: str) -> None:
    """Saves the user-level `git.publish`; an empty value removes it, so the shipped default applies."""
    USER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = USER_SETTINGS_PATH.with_name(USER_SETTINGS_PATH.name + ".tmp")
    tmp.write_text(json.dumps({"git": {"publish": publish}} if publish else {}, indent=2) + "\n")
    tmp.replace(USER_SETTINGS_PATH)


def project_publish(project: Path | None = None) -> str:
    """The project's own `git.publish`, "" when it leaves the choice to the user-level setting."""
    try:
        value = ((json.loads(settings_path(project or home()).read_text()).get("git")) or {}).get("publish")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""
    return value if value in gitrepo.PUBLISH else ""


def load(project: Path | None = None) -> dict:
    """Defaults merged with the settings of `project` (the active project if omitted)."""
    project = project or home()
    if project:
        projects.upgrade_layout(project)
    merged = defaults()
    merged["git"] = dict(merged.get("git") or {}, **load_user().get("git", {}))  # user level over the defaults
    try:
        user = json.loads(settings_path(project).read_text()) if project else {}
    except (OSError, json.JSONDecodeError):
        user = {}
    if project and "project" not in user:
        merged["project"] = Path(project).name
    for key, value in user.items():
        if key == "git" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "publish" or v in gitrepo.PUBLISH}  # "" inherits
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
    """The active project folder (holds input/, katana.config.json and .katana/), or None before one is chosen.
    KATANA_HOME overrides it, for testing. `conf` is accepted for older callers and ignored."""
    override = os.environ.get("KATANA_HOME")
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


def custom_agent(name: str, spec: dict) -> dict:
    """An agent defined in a project's settings. Its model and effort live in `agents`, like any agent's."""
    prompt = str(spec.get("prompt") or "").strip()
    tools = spec.get("tools")
    return {"name": name, "title": str(spec.get("title") or name), "role": str(spec.get("role") or ""),
            "model": "sonnet", "effort": "medium", "prompt": prompt, "default_prompt": prompt, "prompt_override": False,
            "writes_code": "yes" if spec.get("writes_code") else "no", "custom": True,
            "tools": list(tools) if isinstance(tools, list) else list(DEFAULT_TOOLS)}


def agent_catalog(conf: dict | None = None) -> dict:
    """Standard agents (factory/agents/) plus the custom agents of `conf` (the active project's if omitted)."""
    out = {p.stem: load_agent(p.stem) for p in sorted(AGENTS_DIR.glob("*.md"))}
    custom = (conf if conf is not None else load()).get("custom_agents")
    for name, spec in (custom if isinstance(custom, dict) else {}).items():
        if name not in out and isinstance(spec, dict):
            out[name] = custom_agent(name, spec)
    return out


def set_custom_prompt(name: str, text: str, project: Path | None = None) -> bool:
    """Saves the prompt of a custom agent in the project's settings. False if there is no such agent."""
    project = project or home()
    path = settings_path(project)
    try:
        user = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    spec = (user.get("custom_agents") or {}).get(name)
    if not isinstance(spec, dict):
        return False
    spec["prompt"] = text.strip()
    save(user, project)
    return True


def artifact_table(conf: dict) -> dict:
    """Artifact id → {id, title, path, description}: the built-in ones, then the project's."""
    out = {a["id"]: dict(a) for a in BUILTIN_ARTIFACTS}
    for a in conf.get("artifacts") or []:
        if isinstance(a, dict) and a.get("id") and a["id"] not in out:
            out[a["id"]] = {"id": a["id"], "title": a.get("title") or a["id"], "path": a.get("path"),
                            "description": a.get("description") or ""}
    return out


def produced(stage: dict, catalog: dict, table: dict) -> list:
    """Artifact ids a stage writes: its `outputs`, the artifact at its output path, and `code` for coders."""
    ids = list(stage.get("outputs") or [])
    ids += [a["id"] for a in table.values() if a.get("path") and a["path"] == stage.get("output")]
    if writes_code(catalog, stage):
        ids.append("code")
    return list(dict.fromkeys(ids))


def gateway(stage: dict) -> str | None:
    """'autonomous' or 'manual' for a gateway (a gate step, or a stage with pass verdicts); None for other stages."""
    if not (is_gate(stage) or stage.get("pass")):
        return None
    return stage.get("gateway") if stage.get("gateway") in GATEWAYS else "autonomous"


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
    if command and re.sub(r"\.(exe|cmd|bat|com)$", "", command.group(1).split("/")[-1].lower()) in UNSAFE_COMMANDS:
        return f"gives {command.group(1)}, which agents may not run"
    return None


def validate(new: dict) -> tuple:
    """Returns (clean settings, list of errors). Only known keys survive."""
    clean, errors, _ = _validate(new)
    return clean, errors


def diagnose(new: dict) -> dict:
    """Everything wrong or doubtful with a settings draft, for the graph editor: {errors, warnings}, each a list
    of {message, stage}, where `stage` is the id of the stage it is about (or None)."""
    clean, errors, issues = _validate(new)
    plan = [s for phase in phases(new.get("pipeline")) for s in phase]
    by_label = {}
    for p, phase in enumerate(phases(new.get("pipeline")), 1):
        for k, s in enumerate(phase, 1):
            by_label[(p, k if len(phase) > 1 else 0)] = s.get("id")
    explicit = {i["message"]: i["stage"] for i in issues}
    out = {"errors": [], "warnings": []}
    for message in errors:
        m = re.match(r"^Phase (\d+)(?:, stage (\d+))?", message)
        stage = explicit.get(message) or (by_label.get((int(m.group(1)), int(m.group(2) or 0))) if m else None)
        out["errors"].append({"message": message, "stage": stage if stage in {s.get("id") for s in plan} else None})
    for i in issues:
        if i["level"] == "warning":
            out["warnings"].append({"message": i["message"], "stage": i.get("stage")})
    out["warnings"] += [{"message": w, "stage": None} for w in warnings(clean) if w not in explicit]
    return out


def _validate(new: dict) -> tuple:
    errors, issues = [], []
    custom = _validate_custom_agents(new.get("custom_agents"), errors)
    catalog = agent_catalog({"custom_agents": custom})
    artifacts = _validate_artifacts(new.get("artifacts"), errors)
    table = artifact_table({"artifacts": artifacts})
    clean = {"project": str(new.get("project") or "Katana")[:60]}

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
    gi = new.get("git") or {}
    clean["git"] = {"mode": gi.get("mode") or "auto", "repo": str(gi.get("repo") or "").strip()[:500],
                    "publish": gi.get("publish") or ""}  # "" = use the user-level setting
    if clean["git"]["publish"] and clean["git"]["publish"] not in gitrepo.PUBLISH:
        errors.append("Git: choose how to publish the branch: pull request, push, merge or keep it local.")
    if clean["git"]["mode"] not in gitrepo.MODES:
        errors.append("Git: choose auto or off.")
    if clean["git"]["repo"] and not Path(clean["git"]["repo"]).expanduser().is_dir():
        errors.append("Git repository folder: that folder doesn't exist. Leave it empty to use the project folder.")
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
                stage = validate_gate(s, sid, label, on_fail, errors)
                stage.update(_validate_io(s, stage, label, catalog, artifacts, table, errors))
                stage["gateway"] = _validate_gateway(s, label, errors)
                out_phase.append(stage)
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
                stage.update(max_loops(s, label, errors))
                stage["gateway"] = _validate_gateway(s, label, errors)
            stage.update(_validate_io(s, stage, label, catalog, artifacts, table, errors))
            out_phase.append(stage)
        check_phase(catalog, out_phase, p, errors)
        clean["pipeline"].append(out_phase)
    clean["artifacts"], clean["custom_agents"] = artifacts, custom
    for issue in plan_issues(clean, catalog):
        issues.append(issue)
        if issue["level"] == "error":
            errors.append(issue["message"])
    off = disabled_agents(clean)
    agent_stages = [st for st in stages(clean["pipeline"]) if not is_gate(st)]
    if agent_stages and all(st["agent"] in off for st in agent_stages):
        errors.append("Every stage in the plan uses a disabled agent. Enable at least one.")
    if clean["mode"] == "auto" and ROUTER in off:
        errors.append("Full auto needs the Orchestrator agent. Enable it or switch to deterministic mode.")
    clean["budgets"] = _validate_budgets(new.get("budgets") or {}, errors)
    clean["notify"] = _validate_notify(new.get("notify") or {}, errors)
    clean["setup_completed"] = str(new.get("setup_completed") or "")[:40]
    return clean, errors, issues


def _validate_custom_agents(raw, errors: list) -> dict:
    """Agents defined in the project: name → {title, role, prompt, writes_code, tools}."""
    standard, out = {p.stem for p in AGENTS_DIR.glob("*.md")}, {}
    for name, spec in (raw if isinstance(raw, dict) else {}).items():
        spec = spec if isinstance(spec, dict) else {}
        title = str(spec.get("title") or "").strip()[:40]
        label = f"Agent {title or name}"
        if not AGENT_NAME.match(str(name)):
            errors.append(f"{label}: the name must be lowercase letters, digits and dashes, starting with a letter.")
            continue
        if name in standard:
            errors.append(f"{label}: {name} is the name of a standard agent.")
            continue
        if not title:
            errors.append(f"{label}: give the agent a title.")
        prompt = str(spec.get("prompt") or "").strip()
        if not prompt:
            errors.append(f"{label}: write the agent's prompt.")
        elif len(prompt) > 50_000:
            errors.append(f"{label}: the prompt must be at most 50,000 characters.")
        tools = spec.get("tools")
        tools = DEFAULT_TOOLS if tools is None else tools.split(",") if isinstance(tools, str) else tools
        tools = list(dict.fromkeys(str(t).strip() for t in tools if str(t).strip()))
        if len(tools) > 40:
            errors.append(f"{label}: at most 40 tools.")
        for tool in tools:
            problem = tool_problem(tool[:120])
            if problem:
                errors.append(f"{label}: tool “{tool[:60]}” {problem}.")
        out[name] = {"title": title or name, "role": str(spec.get("role") or "").strip()[:200], "prompt": prompt,
                     "writes_code": bool(spec.get("writes_code")), "tools": tools}
    return out


def _validate_artifacts(raw, errors: list) -> list:
    """The project's documents that stages read and write: [{id, title, path, description}]."""
    out = []
    ids = {a["id"] for a in BUILTIN_ARTIFACTS}
    paths = {a["path"] for a in BUILTIN_ARTIFACTS if a["path"]}
    for n, a in enumerate(raw if isinstance(raw, list) else [], 1):
        a = a if isinstance(a, dict) else {}
        aid, path, label = str(a.get("id") or ""), str(a.get("path") or ""), f"Artifact {n}"
        if not STAGE_ID.match(aid):
            errors.append(f"{label}: id must be lowercase letters, digits and dashes.")
        elif aid in ids:
            errors.append(f"{label}: id {aid} is already used.")
        if not OUTPUT_PATH.match(path):
            errors.append(f"{label}: path must be a relative .md path such as docs/spec.md.")
        elif path in paths:
            errors.append(f"{label}: {path} is already another artifact's path.")
        ids.add(aid)
        paths.add(path)
        out.append({"id": aid, "title": str(a.get("title") or aid).strip()[:60], "path": path,
                    "description": str(a.get("description") or "").strip()[:200]})
    return out


def _register_artifact(artifacts: list, table: dict, path: str, title: str) -> str:
    """Adds the document at `path` to the project's artifacts under a new id."""
    base = re.sub(r"[^a-z0-9]+", "-", Path(path).stem.lower()).strip("-")[:30] or "doc"
    aid, n = base, 2
    while aid in table:
        aid, n = f"{base}-{n}", n + 1
    artifacts.append({"id": aid, "title": title[:60], "path": path, "description": ""})
    table[aid] = dict(artifacts[-1])
    return aid


def _validate_io(s: dict, stage: dict, label: str, catalog: dict, artifacts: list, table: dict, errors: list) -> dict:
    """A stage's `inputs` and `outputs`. The document at its own output path is always one of its outputs."""
    out = {}
    for key, verb in (("inputs", "reads"), ("outputs", "writes")):
        ids = list(dict.fromkeys(str(x).strip() for x in s.get(key) or [] if str(x).strip()))
        for aid in ids:
            if aid not in table:
                errors.append(f"{label}: {verb} the artifact “{aid[:40]}”, which doesn't exist.")
        out[key] = [aid for aid in ids if aid in table]
    path = stage.get("output")
    own = next((a["id"] for a in table.values() if a.get("path") == path), None)
    if own is None and path and OUTPUT_PATH.match(path):
        own = _register_artifact(artifacts, table, path, stage["title"])
    if own and own not in out["outputs"]:
        out["outputs"].append(own)
    return out


def _validate_gateway(s: dict, label: str, errors: list) -> str:
    mode = s.get("gateway") or "autonomous"
    if mode not in GATEWAYS:
        errors.append(f"{label}: gateway type is autonomous or manual.")
        return "autonomous"
    return mode


def plan_issues(clean: dict, catalog: dict) -> list:
    """Problems with how the plan fits together, as [{level: error|warning, stage, message}]: feedback loops that
    don't go back, and artifacts a stage reads that nothing before it writes. A feedback loop makes what its phases
    write readable by those same phases on the next pass, so that is not a problem."""
    plan, table = phases(clean.get("pipeline")), artifact_table(clean)
    where = {s["id"]: i for i, ph in enumerate(plan) for s in ph}
    by_id = {s["id"]: s for ph in plan for s in ph}
    flat_ids = [s["id"] for ph in plan for s in ph]
    out = []

    def add(level, stage, message):
        out.append({"level": level, "stage": stage["id"], "message": f"{stage['title']}: {message}"})

    def back_of(s):  # where a failed gateway sends the work: its 'on fail', else the nearest earlier build stage
        if s.get("on_fail"):
            return s["on_fail"]
        earlier = flat_ids[:flat_ids.index(s["id"])]
        return next((x for x in reversed(earlier) if by_id[x].get("agent") == "developer"), earlier[-1] if earlier else None)

    loops = [(where[t], g) for g, ph in enumerate(plan) for s in ph if gateway(s)
             for t in [back_of(s)] if t in where and where[t] <= g]  # (target phase, gateway phase)

    for i, phase in enumerate(plan):
        earlier = {a for ph in plan[:i] for s in ph for a in produced(s, catalog, table)}
        again = [(t, g) for t, g in loops if t <= i <= g]  # loops over this phase
        for s in phase:
            target = s.get("on_fail")
            if gateway(s) and target in where:
                if where[target] > i:
                    add("error", s, f"the feedback loop goes forward to {by_id[target]['title']}. It must go back to an "
                                    "earlier phase.")
                elif target == s["id"] and is_gate(s):
                    add("error", s, "a gate can't send work back to itself.")
            elif gateway(s) and is_gate(s) and i == 0:
                add("warning", s, "it is the first step, so there is nothing to measure or go back to.")
            beside = {a for o in phase if o is not s for a in produced(o, catalog, table)}
            back = {a for t, g in again for ph in plan[t:g + 1] for o in ph if not (is_gate(s) and o is s)
                    for a in produced(o, catalog, table)}  # what those phases wrote on the previous pass
            for a in s.get("inputs") or []:
                if a == "requirements" or a in earlier or a in back:
                    continue
                name = table.get(a, {}).get("title", a)
                if a in beside:
                    add("warning", s, f"reads {name}, which a stage in the same phase writes at the same time.")
                else:
                    add("warning", s, f"reads {name}, but no earlier stage writes it.")
    return out


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
    out += [i["message"] for i in plan_issues(clean, agent_catalog(clean)) if i["level"] == "warning"]
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
            "conditions": conditions, "on_fail": on_fail, "required": bool(s.get("required")),
            **max_loops(s, label, errors)}


def max_loops(s: dict, label: str, errors: list) -> dict:
    """A loop's own rework limit, kept only when set. Without it the loop uses max_iterations."""
    if s.get("max_loops") in (None, ""):
        return {}
    try:
        n = int(s["max_loops"])
        if not 1 <= n <= 10:
            raise ValueError
        return {"max_loops": n}
    except (TypeError, ValueError):
        errors.append(f"{label}: max rework loops must be a whole number from 1 to 10.")
        return {}


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
    written = [a for s in phase for a in s.get("outputs") or [] if a != "code"]
    if len(phase) > 1 and len(set(written)) < len(written):
        errors.append(f"Phase {p}: stages that run at the same time can't write the same artifact.")


def save(clean: dict, project: Path | None = None) -> None:
    path = settings_path(project or home())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(clean, indent=2) + "\n")
    tmp.replace(path)


def effective_agent(conf: dict, catalog: dict, name: str) -> dict:
    """The agent definition with the model, effort, tools and enabled switch chosen in settings."""
    agent = copy.deepcopy(catalog[name])
    agent.update({k: v for k, v in (conf.get("agents") or {}).get(name, {}).items() if v not in (None, "")})
    agent["enabled"] = agent.get("enabled") is not False
    return agent
