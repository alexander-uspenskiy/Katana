#!/usr/bin/env python3
"""Cortana orchestrator.

Scans <project folder>/input/ for requirement .md files and drives each one through the plan in
config/settings.json, calling the Claude Code CLI headless on the user's subscription.

- deterministic mode: runs the plan's phases in order. The stages of a phase run at the same time (up to
  max_parallel); the next phase starts when all of them have finished. A failed gate sends the work back
  to its 'on fail' stage.
- auto mode: the Orchestrator agent picks the next stage(s) each time; required gates must pass before done.

Gate steps (`"kind": "gate"`) are measured by Cortana itself, with no LLM call: see gates.py.

    python3 factory/orchestrator.py tick      # one scan (what launchd runs every 10 min)
    python3 factory/orchestrator.py watch     # scan in a loop in the foreground
    python3 factory/orchestrator.py status    # list runs
"""
from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import gates
import settings
import streamlog

MAX_ATTEMPTS = int(os.environ.get("CORTANA_MAX_ATTEMPTS", "3"))
WATCH_INTERVAL = int(os.environ.get("CORTANA_INTERVAL", "600"))
SETTLE_SECONDS = 60  # ignore input files modified more recently (still being written)
SETTING_SOURCES = os.environ.get("CORTANA_SETTING_SOURCES", "project")

# Any of these would make the CLI bill an API account instead of the subscription.
API_ENV_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SIMPLE",
)
LIMIT_PATTERN = re.compile(
    r"usage limit|rate limit|limit reached|hit your limit|quota|overloaded|\b429\b", re.I)

COMMON_RULES = """
You are one stage in an unattended software pipeline. Nobody is watching and nobody will answer
questions: make reasonable decisions, record them in your output file, and finish the job.
Work only inside the current directory. Do the work yourself; do not delegate to subagents.
Your output file must end with the VERDICT line your instructions define.
"""

# Set by configure() at the start of every tick, from settings.
ROOT = INPUT_DIR = RUNS_DIR = LOG_FILE = None
CONF: dict = {}
LOCK = threading.RLock()  # stages of one phase run in threads; they share the run dict and its state file


def configure() -> None:
    global ROOT, INPUT_DIR, RUNS_DIR, LOG_FILE, CONF
    CONF = settings.load()
    ROOT = settings.home(CONF)
    INPUT_DIR, RUNS_DIR, LOG_FILE = ROOT / "input", ROOT / "runs", ROOT / "logs" / "factory.log"
    for d in (INPUT_DIR, RUNS_DIR, LOG_FILE.parent):
        d.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with LOCK:
        print(line, flush=True)
        if LOG_FILE:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------- claude CLI

def find_claude() -> str:
    candidates = [os.environ.get("CORTANA_CLAUDE_BIN"), shutil.which("claude"),
                  str(Path.home() / ".local/bin/claude")]
    # Fallback: the CLI bundled with the Claude desktop app, newest version first.
    bundled = glob.glob(str(Path.home() / "Library/Application Support/Claude/claude-code/*/*"
                                          "/claude.app/Contents/MacOS/claude"))
    version = lambda p: [int(x) for x in re.findall(r"\d+", Path(p).parents[4].name)]
    candidates += sorted(bundled, key=version, reverse=True)
    for c in candidates:
        if c and os.access(c, os.X_OK):
            return c
    raise SystemExit("Claude Code CLI not found. Install it or set CORTANA_CLAUDE_BIN.")


def subscription_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in API_ENV_VARS}


def check_subscription(claude: str) -> dict:
    out = subprocess.run([claude, "auth", "status", "--json"], capture_output=True, text=True,
                         env=subscription_env(), timeout=60)
    try:
        status = json.loads(out.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"Could not read auth status: {out.stderr.strip() or out.stdout.strip()}")
    if not status.get("loggedIn") or not status.get("subscriptionType"):
        raise SystemExit("Not logged in with a Claude subscription. Run `claude auth login` "
                         "(or `claude setup-token` and set CLAUDE_CODE_OAUTH_TOKEN).")
    return {"subscription": status.get("subscriptionType"), "method": status.get("authMethod")}


def heartbeat(**fields) -> None:
    """Records what the factory is doing, for the dashboard."""
    path = RUNS_DIR / ".factory.json"
    try:
        beat = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        beat = {}
    beat.update(fields, pid=os.getpid(), interval=WATCH_INTERVAL, mode=CONF.get("mode"),
                updated=datetime.now().isoformat())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(beat, indent=2))
    tmp.replace(path)


def call_claude(cmd: list, prompt: str, cwd: Path, log_path: Path) -> tuple:
    """Runs one CLI call, writing its stdout to log_path. Returns (exit code, stderr)."""
    timeout = int(CONF.get("station_timeout_min", 60)) * 60
    with open(log_path, "w") as out:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.PIPE,
                                cwd=cwd, text=True, env=subscription_env())
        try:
            _, stderr = proc.communicate(prompt, timeout=timeout)
            return proc.returncode, stderr
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return -1, f"timeout after {timeout}s"


def failure(code: int, result: dict, stderr: str) -> str | None:
    """None if the call succeeded, else 'limit' or 'error'."""
    if code == 0 and result and not result.get("is_error"):
        return None
    return "limit" if LIMIT_PATTERN.search(f"{result.get('result', '')} {stderr}") else "error"


# ---------------------------------------------------------------- runs

def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "req"


def save(run: dict) -> None:
    with LOCK:
        path = RUNS_DIR / run["id"] / "state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(run, indent=2))
        tmp.replace(path)


def migrate(run: dict) -> dict:
    """Upgrades state from before phases: a flat plan, one `stage` and one `current`."""
    run["pipeline"] = settings.phases(run["pipeline"])
    if "phase" not in run:
        stage = run.pop("stage", None)
        idx = next((i for i, ph in enumerate(run["pipeline"]) if any(s["id"] == stage for s in ph)), 0)
        run["phase"] = idx if run["mode"] == "deterministic" else None
        run["next"] = [stage] if stage and run["mode"] == "auto" else []
    run.setdefault("next", [])
    run.setdefault("finished", [])
    current = run.get("current")
    run["current"] = [current] if isinstance(current, dict) else current or []
    return run


def load_runs() -> list:
    return [migrate(json.loads(p.read_text())) for p in sorted(RUNS_DIR.glob("*/state.json"))]


def git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


def claim_new_inputs() -> list:
    runs = []
    for src in sorted(INPUT_DIR.glob("*.md")):
        if src.name.lower() == "readme.md" or src.name.startswith((".", "_")):
            continue
        if time.time() - src.stat().st_mtime < SETTLE_SECONDS:
            log(f"skip {src.name}: modified less than {SETTLE_SECONDS}s ago")
            continue
        run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(src.stem)}"
        run_dir = RUNS_DIR / run_id
        workspace = run_dir / "workspace"
        (workspace / "docs").mkdir(parents=True)
        (run_dir / "logs").mkdir()
        (run_dir / "reports").mkdir()
        shutil.move(str(src), run_dir / "requirements.md")
        shutil.copy(run_dir / "requirements.md", workspace / "docs/00-requirements.md")
        git(workspace, "init", "-q", "-b", "main")
        git(workspace, "config", "user.name", "Cortana")
        git(workspace, "config", "user.email", "cortana@localhost")
        git(workspace, "add", "-A")
        git(workspace, "commit", "-q", "-m", "Add requirements")
        # The plan and mode are frozen per run, so editing settings never breaks a run in flight.
        run = {"id": run_id, "source": src.name, "status": "running", "mode": CONF["mode"],
               "pipeline": CONF["pipeline"], "phase": 0 if CONF["mode"] == "deterministic" else None,
               "next": [], "finished": [], "current": [],
               "visits": {}, "loops": {}, "attempts": 0, "created": datetime.now().isoformat(),
               "history": [], "decisions": []}
        save(run)
        log(f"claimed {src.name} as run {run_id} ({CONF['mode']} mode)")
        runs.append(run)
    return runs


# ---------------------------------------------------------------- plan logic

def flat(run: dict) -> list:
    return settings.stages(run["pipeline"])


def stage_def(run: dict, stage_id: str) -> dict:
    return next(s for s in flat(run) if s["id"] == stage_id)


def phase_of(run: dict, stage_id: str) -> int:
    return next(i for i, phase in enumerate(run["pipeline"]) if any(s["id"] == stage_id for s in phase))


def latest(run: dict, stage_id: str) -> dict | None:
    return next((h for h in reversed(run["history"]) if h["stage"] == stage_id and h["outcome"] == "ok"), None)


def gated(stage: dict) -> bool:
    """A gate step, or an agent stage with pass verdicts."""
    return settings.is_gate(stage) or bool(stage.get("pass"))


def passed(stage: dict, entry: dict | None) -> bool:
    if not entry:
        return False
    if settings.is_gate(stage):
        return entry.get("verdict") == "PASS"
    return entry.get("verdict") in stage.get("pass", [])


def failed_gates(run: dict) -> list:
    """Gates whose latest finished run did not pass."""
    return [s for s in flat(run) if gated(s) and latest(run, s["id"]) and not passed(s, latest(run, s["id"]))]


def is_build(stage: dict) -> bool:
    return stage.get("agent") == "developer"


def fail_target(run: dict, stage: dict) -> str:
    """Where a failed gate sends the work: its 'on fail' stage, else the nearest earlier build stage."""
    if stage.get("on_fail"):
        return stage["on_fail"]
    order = [s["id"] for s in flat(run)]
    earlier = order[:order.index(stage["id"])]
    return next((i for i in reversed(earlier) if is_build(stage_def(run, i))), earlier[-1] if earlier else stage["id"])


def last_build(run: dict) -> int:
    return max((i for i, h in enumerate(run["history"]) if h["outcome"] == "ok" and h.get("agent") == "developer"),
               default=-1)


def unmet_gates(run: dict) -> list:
    """Required gates that block 'done' in auto mode. A gate placed after a build stage in the plan must
    have passed after the latest build."""
    order = [s["id"] for s in flat(run)]
    builds = [s for s in flat(run) if is_build(s)]
    first_build = order.index(builds[0]["id"]) if builds else len(order)
    built = last_build(run)
    unmet = []
    if builds and built < 0:
        unmet.append(builds[0])
    for s in flat(run):
        if not s.get("required"):
            continue
        idx = max((i for i, h in enumerate(run["history"]) if h["stage"] == s["id"] and h["outcome"] == "ok"),
                  default=-1)
        stale = order.index(s["id"]) > first_build and idx < built
        if idx < 0 or stale or not passed(s, run["history"][idx]):
            unmet.append(s)
    return unmet


def count_loop(run: dict, stage: dict, verdict: str) -> bool:
    """Counts a gate failure. Returns False (and fails the run) past the rework limit."""
    run["loops"][stage["id"]] = run["loops"].get(stage["id"], 0) + 1
    limit = int(CONF.get("max_iterations", 3))
    if run["loops"][stage["id"]] > limit:
        run["status"] = "failed"
        run["reason"] = f"{stage['title']} still says {verdict} after {limit} rework loops"
        return False
    return True


def settle(run: dict, batch: list) -> list | None:
    """After a phase or batch has finished: blocks the run on BLOCKED and counts failed gates. Returns the
    failed gates, or None if the run stopped."""
    run["finished"], run["next"] = [], []
    for s in batch:
        if (latest(run, s["id"]) or {}).get("verdict") == "BLOCKED":
            run["status"], run["reason"] = "blocked", f"{s['title']} could not continue"
            return None
    failed = [s for s in batch if gated(s) and not passed(s, latest(run, s["id"]))]
    for s in failed:
        if not count_loop(run, s, latest(run, s["id"]).get("verdict")):
            return None
    return failed


def advance_phase(run: dict) -> None:
    """Deterministic mode, once every stage of the current phase has finished."""
    failed = settle(run, run["pipeline"][run["phase"]])
    if failed is None:
        return
    if failed:  # go back to the earliest phase any failed gate points at, and run on from there
        run["phase"] = min(phase_of(run, fail_target(run, s)) for s in failed)
    elif run["phase"] + 1 < len(run["pipeline"]):
        run["phase"] += 1
    else:
        run["status"] = "done"


def describe(run: dict, s: dict, catalog: dict) -> str:
    if settings.is_gate(s):
        conds = "; ".join(gates.describe(c) for c in s.get("conditions") or [])
        return (f"{s['id']}: {s['title']}, a gate step Cortana measures itself (no agent, no tokens): {conds}. "
                f"On fail the Developer gets {s['output']}.{' Required.' if s.get('required') else ''}")
    gate = f" Gate: passes on {', '.join(s['pass'])}." if s.get("pass") else ""
    code = " Changes code." if settings.writes_code(catalog, s) else " Writes only its report."
    return (f"{s['id']}: {s['title']} by {catalog[s['agent']].get('title', s['agent'])}. "
            f"{s['task']}{gate}{code}{' Required.' if s.get('required') else ''}")


def decide(claude: str, run: dict):
    """Auto mode: asks the Orchestrator agent what runs next. Returns a list of stage ids, ['done'],
    or an outcome ('limit' / 'error') if the call failed."""
    run_dir = RUNS_DIR / run["id"]
    workspace = run_dir / "workspace"
    catalog = settings.agent_catalog()
    router = settings.effective_agent(CONF, catalog, settings.ROUTER)
    ids = [s["id"] for s in flat(run)]
    unmet = unmet_gates(run)

    lines = [f"Requirement: {run['source']}", "", "Stage catalogue, by plan phase (stages in one phase may run together):"]
    for p, phase in enumerate(run["pipeline"], 1):
        lines += [f"- [phase {p}] {describe(run, s, catalog)}" for s in phase]
    lines += ["", "History so far:"]
    for i, h in enumerate(run["history"], 1):
        misses = [f"{r['label']} {gates.fmt_value(r)} (needs {r['op']} {r['threshold']})"
                  for r in h.get("results") or [] if r["outcome"] == "block"]
        lines.append(f"{i}. {h['stage']} → {h['outcome']}{' ' + h['verdict'] if h.get('verdict') else ''}"
                     + (f": {'; '.join(misses)}" if misses else ""))
    if not run["history"]:
        lines.append("(nothing has run yet)")
    docs = sorted(str(p.relative_to(workspace)) for p in (workspace / "docs").glob("*.md"))
    lines += ["", f"Documents present: {', '.join(docs)}",
              f"Required gates not yet satisfied: {', '.join(s['id'] for s in unmet) or 'none'}",
              f"Stages used: {sum(h.get('kind') != 'gate' for h in run['history'])} of {CONF.get('max_stations', 20)}",
              "", "Which stage or stages run next? Choose several only if at most one of them changes code. "
              "Answer [\"done\"] only if every required gate is satisfied."]
    schema = {"type": "object", "required": ["next", "reason"], "additionalProperties": False,
              "properties": {"next": {"type": "array", "minItems": 1,
                                      "items": {"type": "string", "enum": ids + ["done"]}},
                             "reason": {"type": "string"}}}
    visit = len(run["decisions"]) + 1
    cmd = [claude, "-p", "--append-system-prompt", router["prompt"], "--model", router["model"],
           "--effort", router["effort"], "--tools", "", "--permission-prompts", "none",
           "--setting-sources", SETTING_SOURCES, "--output-format", "json",
           "--json-schema", json.dumps(schema), "--name", f"cortana-{run['id']}-orchestrator-{visit}"]

    started = time.time()
    log_path = run_dir / "logs" / f"orchestrator-{visit}-{datetime.now():%H%M%S}.json"
    cur = {"stage": None, "agent": settings.ROUTER, "title": "Choosing the next stage", "visit": visit,
           "model": router["model"], "effort": router["effort"],
           "started": datetime.now().isoformat(), "log": f"logs/{log_path.name}"}
    with LOCK:
        run["current"].append(cur)
        save(run)
    code, stderr = call_claude(cmd, "\n".join(lines), workspace, log_path)
    with LOCK:
        run["current"] = [c for c in run["current"] if c is not cur]
    try:
        result = json.loads(log_path.read_text().strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        result = {}
    answer = result.get("structured_output") or {}
    picks = answer.get("next")
    picks = [picks] if isinstance(picks, str) else picks if isinstance(picks, list) else []
    decision = {"at": datetime.now().isoformat(), "started": datetime.fromtimestamp(started).isoformat(),
                "seconds": round(time.time() - started), "model": router["model"], "effort": router["effort"],
                "models": sorted(result.get("modelUsage") or {}),
                "tokens": streamlog.tokens_from_usage(result.get("usage") or {}),
                "cost_usd": result.get("total_cost_usd"), "next": picks, "reason": answer.get("reason", "")}
    problem = failure(code, result, stderr)
    if problem:
        decision.update(outcome=problem, error=f"{result.get('result', '')} {stderr}".strip()[:500])
        run["decisions"].append(decision)
        return problem
    decision["outcome"] = "ok"
    choice = list(dict.fromkeys(c for c in picks if c in ids + ["done"]))
    if "done" in choice and unmet:
        choice = [unmet[0]["id"]]
        decision["override"] = f"Required gates not met; running {choice[0]} instead."
    elif "done" in choice:
        choice = ["done"]
    elif not choice:
        choice = [unmet[0]["id"] if unmet else "done"]
        decision["override"] = f"Invalid answer; running {choice[0]} instead."
    coders = [c for c in choice if c != "done" and settings.writes_code(catalog, stage_def(run, c))]
    if len(coders) > 1:
        choice = [c for c in choice if c not in coders[1:]]
        decision["override"] = f"Only one stage that changes code can run at a time; {', '.join(coders[1:])} waits."
    decision["chosen"] = choice
    run["decisions"].append(decision)
    log(f"{run['id']}: orchestrator chose {', '.join(choice)}: {decision['reason'][:120]}")
    return choice


# ---------------------------------------------------------------- stations

def make_snapshot(run_dir: Path, workspace: Path, name: str) -> Path | None:
    """A git worktree of the workspace (HEAD plus uncommitted changes) for one report-only stage, so it
    reads a stable copy while the phase's code-writing stage keeps changing the shared workspace."""
    path = run_dir / "worktrees" / name
    try:
        with LOCK:  # git locks .git/worktrees; add them one at a time
            git(workspace, "worktree", "add", "--detach", "-f", str(path), "HEAD")
        changed = subprocess.run(["git", "ls-files", "-z", "-m", "-o", "--exclude-standard"], cwd=workspace,
                                 capture_output=True, text=True, check=True).stdout.split("\0")
        for rel in filter(None, changed):
            if (workspace / rel).is_file():
                (path / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(workspace / rel, path / rel)
        return path
    except (subprocess.CalledProcessError, OSError) as e:
        log(f"{run_dir.name}: no worktree for {name} ({e}); it shares the workspace")
        return None


def drop_snapshot(workspace: Path, path: Path) -> None:
    with LOCK:
        subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=workspace, capture_output=True)
        subprocess.run(["git", "worktree", "prune"], cwd=workspace, capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


def run_station(claude: str, run: dict, stage: dict, alongside: list, snapshot: Path | None = None) -> tuple:
    """Runs one agent stage (in its own thread when a phase has several). Returns (outcome, verdict) with
    outcome ok|limit|error."""
    catalog = settings.agent_catalog()
    agent = settings.effective_agent(CONF, catalog, stage["agent"])
    coder = settings.writes_code(catalog, stage)
    run_dir = RUNS_DIR / run["id"]
    workspace = run_dir / "workspace"
    cwd = snapshot or workspace
    output = cwd / stage["output"]
    visit = run["visits"].get(stage["id"], 0) + 1

    task = [f"Phase {phase_of(run, stage['id']) + 1} of {len(run['pipeline'])}: {stage['title']} (run {visit}).",
            stage["task"], f"Write your output to {stage['output']}."]
    if alongside:
        task.append(f"Running at the same time in this workspace: {', '.join(alongside)}. "
                    + ("You are the only one of them that may change code." if coder
                       else "Do not change any file except your output."))
    feedback = [g["output"] for g in failed_gates(run) if g["id"] != stage["id"]
                and (fail_target(run, g) == stage["id"] or (run["mode"] == "auto" and is_build(stage)))]
    if feedback:
        task.append(f"Fix every finding in: {', '.join(feedback)}.")
    cmd = [claude, "-p",
           "--append-system-prompt", COMMON_RULES + "\n" + agent["prompt"],
           "--model", agent["model"], "--effort", agent["effort"],
           "--allowedTools", ", ".join(agent["tools"]) or "Read",
           "--permission-mode", "acceptEdits", "--permission-prompts", "none",
           "--setting-sources", SETTING_SOURCES,
           "--output-format", "stream-json", "--verbose",
           "--name", f"cortana-{run['id']}-{stage['id']}-{visit}"]

    started = time.time()
    stamp = f"{stage['id']}-run{visit}-{datetime.now():%H%M%S}"
    log_path = run_dir / "logs" / f"{stamp}.jsonl"
    # The dashboard reads `current` and tails the log while the stage runs.
    cur = {"stage": stage["id"], "agent": stage["agent"], "title": stage["title"], "visit": visit,
           "model": agent["model"], "effort": agent["effort"], "alongside": alongside,
           "worktree": bool(snapshot), "started": datetime.now().isoformat(), "log": f"logs/{log_path.name}"}
    with LOCK:
        run["current"].append(cur)
        save(run)
    log(f"{run['id']}: {stage['title']} ({stage['agent']}, {agent['model']}/{agent['effort']}, run {visit}) started"
        + (f" alongside {', '.join(alongside)}" if alongside else ""))
    code, stderr = call_claude(cmd, "\n".join(task), cwd, log_path)
    if stderr:
        (run_dir / "logs" / f"{stamp}.stderr").write_text(stderr)

    summary = streamlog.summarize(log_path)
    result = summary["result"] or {}
    entry = {"stage": stage["id"], "agent": stage["agent"], "title": stage["title"], "visit": visit,
             "model": agent["model"], "effort": agent["effort"],
             "models": sorted(result.get("modelUsage") or {}) or [m for m in [summary["model"]] if m],
             "started": datetime.fromtimestamp(started).isoformat(), "finished": datetime.now().isoformat(),
             "seconds": round(time.time() - started),
             "api_seconds": round((result.get("duration_api_ms") or 0) / 1000),
             "turns": result.get("num_turns"), "tokens": summary["tokens"],
             "cost_usd": result.get("total_cost_usd"), "tools": summary["tools"],
             "denials": len(result.get("permission_denials") or []),
             "session_id": result.get("session_id"), "log": f"logs/{log_path.name}"}
    if alongside:
        entry["alongside"] = alongside

    problem = failure(code, result, stderr)
    verdict = None
    if problem:
        entry.update(outcome=problem, error=f"{result.get('result', '')} {stderr}".strip()[:500])
    elif not output.exists() or output.stat().st_mtime < started:
        problem = "error"
        entry.update(outcome="error", error=f"{stage['output']} was not written")
    else:
        verdicts = re.findall(r"^\**VERDICT:\s*([A-Z_]+)", output.read_text(), re.M)
        verdict = verdicts[-1] if verdicts else None
        if snapshot:  # the report goes back to the shared workspace
            (workspace / stage["output"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(output, workspace / stage["output"])
        shutil.copy(output, run_dir / "reports" / f"{stage['id']}-run{visit}.md")
        entry.update(outcome="ok", verdict=verdict)
    with LOCK:
        run["current"] = [c for c in run["current"] if c is not cur]
        run["history"].append(entry)
        if not problem:
            run["visits"][stage["id"]] = visit
            run["finished"].append(stage["id"])
        save(run)
    log(f"{run['id']}: {stage['title']} " + (f"failed: {entry['error'][:160]}" if problem else f"finished, verdict {verdict}"))
    return problem or "ok", verdict


def gate_context(run: dict, workspace: Path, out_dir: Path) -> dict:
    """What a gate step measures from: the latest report of each role, and whether it predates the build."""
    built = last_build(run)
    sources = {}
    for role in ("tester", "reviewer", "security"):
        idx = max((i for i, h in enumerate(run["history"]) if h.get("agent") == role and h["outcome"] == "ok"),
                  default=-1)
        if idx >= 0:
            stage = stage_def(run, run["history"][idx]["stage"])
            sources[role] = {"path": str(workspace / stage["output"]), "stage": stage["title"], "stale": idx < built}
    tokens = sum(sum((h.get("tokens") or {}).values()) for h in run["history"] + run["decisions"])
    return {"workspace": str(workspace), "out_dir": str(out_dir), "conf": CONF.get("gates") or {},
            "sources": sources, "run_tokens": tokens, "env": subscription_env()}


def run_gate(run: dict, gate: dict) -> str:
    """Measures a gate step: Cortana runs the checks itself, with no CLI call. Returns ok or error."""
    run_dir = RUNS_DIR / run["id"]
    workspace = run_dir / "workspace"
    visit = run["visits"].get(gate["id"], 0) + 1
    out_dir = run_dir / "gates" / f"{gate['id']}-run{visit}"
    started = time.time()
    cur = {"stage": gate["id"], "agent": None, "kind": "gate", "title": gate["title"], "visit": visit,
           "model": None, "effort": None, "started": datetime.now().isoformat(),
           "log": f"gates/{out_dir.name}/gate.log"}
    with LOCK:
        run["current"].append(cur)
        save(run)
    log(f"{run['id']}: {gate['title']} (gate, run {visit}) measuring")
    entry = {"stage": gate["id"], "kind": "gate", "agent": None, "title": gate["title"], "visit": visit,
             "model": None, "effort": None, "models": [], "started": cur["started"], "tokens": {}, "tools": {},
             "log": cur["log"]}
    try:
        result = gates.evaluate(gate, gate_context(run, workspace, out_dir))
    except Exception as e:  # a crash in a measurement is retried like a failed stage, not a gate failure
        result = None
        entry.update(outcome="error", error=f"{type(e).__name__}: {e}"[:500])
    if result:
        target = stage_def(run, fail_target(run, gate))["title"]
        report = gates.render_report(gate, result, visit, target)
        (workspace / gate["output"]).parent.mkdir(parents=True, exist_ok=True)
        (workspace / gate["output"]).write_text(report)
        (run_dir / "reports" / f"{gate['id']}-run{visit}.md").write_text(report)
        entry.update(outcome="ok", verdict=result["verdict"], results=result["results"], tests=result["tests"],
                     findings=len(result["findings"]), blocking=result["blocking"], warnings=result["warnings"])
    entry.update(finished=datetime.now().isoformat(), seconds=round(time.time() - started))
    with LOCK:
        run["current"] = [c for c in run["current"] if c is not cur]
        run["history"].append(entry)
        if result:
            run["visits"][gate["id"]] = visit
            run["finished"].append(gate["id"])
        save(run)
    if result:
        log(f"{run['id']}: {gate['title']} {result['verdict']}: " + ", ".join(
            f"{r['label']} {gates.fmt_value(r)} {r['status']}" for r in result["results"]))
    else:
        log(f"{run['id']}: {gate['title']} failed to measure: {entry['error']}")
    return entry["outcome"]


def run_batch(claude: str, run: dict, batch: list) -> str | None:
    """Runs stages together: agent stages in parallel threads (at most max_parallel at once), then gate
    steps. Returns None, or the problem ('limit' / 'error') after recording it."""
    agents = [s for s in batch if not settings.is_gate(s)]
    if agents:
        catalog = settings.agent_catalog()
        workspace = RUNS_DIR / run["id"] / "workspace"
        snapshots = {}
        if CONF.get("worktrees") and len(agents) > 1:  # snapshot before the code-writing stage starts
            for s in agents:
                if not settings.writes_code(catalog, s):
                    snapshots[s["id"]] = make_snapshot(RUNS_DIR / run["id"], workspace,
                                                       f"{s['id']}-run{run['visits'].get(s['id'], 0) + 1}")
        width = max(1, min(int(CONF.get("max_parallel") or 1), len(agents)))
        titles = [s["title"] for s in agents]
        try:
            with ThreadPoolExecutor(max_workers=width) as pool:
                outcomes = list(pool.map(lambda s: run_station(claude, run, s, [t for t in titles if t != s["title"]],
                                                               snapshots.get(s["id"])), agents))
        finally:
            for path in filter(None, snapshots.values()):
                drop_snapshot(workspace, path)
        bad = [s for s, (outcome, _) in zip(agents, outcomes) if outcome != "ok"]
        if bad:
            problem = "limit" if any(o == "limit" for o, _ in outcomes) else "error"
            errors = "; ".join(next(h.get("error", "") for h in reversed(run["history"]) if h["stage"] == s["id"])
                               for s in bad)
            handle_problem(run, problem, ", ".join(s["title"] for s in bad), errors)
            return problem
    for g in [s for s in batch if settings.is_gate(s)]:
        if run_gate(run, g) != "ok":
            handle_problem(run, "error", g["title"], run["history"][-1].get("error", ""))
            return "error"
    run["attempts"] = 0
    return None

def write_summary(run: dict) -> None:
    lines = [f"# Run {run['id']}", "",
             f"- Source: `{run['source']}`",
             f"- Mode: {run['mode']}",
             f"- Status: **{run['status']}**" + (f" ({run['reason']})" if run.get("reason") else ""),
             f"- Rework loops: {sum(run['loops'].values())}",
             "- Code: `workspace/` (git repo), reports: `reports/`, raw logs: `logs/`, gate measurements: `gates/`", "",
             "| # | Phase | Stage | Agent | Run | Outcome | Verdict | Model | Effort | Seconds |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for n, h in enumerate(run["history"], 1):
        lines.append(f"| {n} | {phase_of(run, h['stage']) + 1} | {h['title']} | {h.get('agent') or 'Cortana'} | "
                     f"{h['visit']} | {h['outcome']} | {h.get('verdict') or ''} | {h.get('model') or '-'} | "
                     f"{h.get('effort') or '-'} | {h['seconds']} |")
    gate_runs = [h for h in run["history"] if h.get("kind") == "gate" and h.get("results")]
    if gate_runs:
        lines += ["", "## Quality gates", ""]
        for h in gate_runs:
            lines.append(f"- **{h['title']}** run {h['visit']}: {h['verdict']} · " + "; ".join(
                f"{gates.condition_text(r)} → {gates.fmt_value(r)} {r['status']}" for r in h["results"]))
    if run["decisions"]:
        lines += ["", "## Orchestrator decisions", ""]
        lines += [f"{n}. **{', '.join(d.get('chosen') or []) if isinstance(d.get('chosen'), list) else d.get('chosen') or d['outcome']}**: "
                  f"{d.get('reason', '')} {d.get('override', '')}".rstrip()
                  for n, d in enumerate(run["decisions"], 1)]
    (RUNS_DIR / run["id"] / "SUMMARY.md").write_text("\n".join(lines) + "\n")


def handle_problem(run: dict, problem: str, what: str, error: str) -> None:
    """Pauses the run on a usage limit; otherwise counts a retry and fails the run past the limit."""
    if problem == "limit":
        run["status"] = "paused"
        log(f"{run['id']}: usage limit hit, pausing until the next tick")
        return
    run["attempts"] += 1
    log(f"{run['id']}: {what} failed (attempt {run['attempts']}/{MAX_ATTEMPTS}): {error}")
    if run["attempts"] >= MAX_ATTEMPTS:
        run["status"] = "failed"
        run["reason"] = f"{what} failed {MAX_ATTEMPTS} times"


def process(claude: str, run: dict) -> bool:
    """Drives a run until it finishes or pauses. Returns False if a usage limit was hit."""
    run["status"] = "running"
    run["current"] = []  # anything listed here was cut off when the factory last stopped
    while run["status"] == "running":
        used = sum(h.get("kind") != "gate" for h in run["history"])
        if used >= int(CONF.get("max_stations", 20)):
            run["status"] = "failed"
            run["reason"] = f"stopped after {used} stages (limit {CONF.get('max_stations')})"
            break
        if run["mode"] == "auto" and not run["next"]:
            choice = decide(claude, run)
            if choice in ("limit", "error"):
                handle_problem(run, choice, "orchestrator", run["decisions"][-1].get("error", ""))
                save(run)
                if run["status"] != "failed":
                    return run["status"] != "paused"
                break
            run["attempts"] = 0
            if choice == ["done"]:
                run["status"] = "done"
                break
            run["next"] = choice
            save(run)
        batch = [stage_def(run, i) for i in run["next"]] if run["mode"] == "auto" \
            else run["pipeline"][run["phase"]]
        problem = run_batch(claude, run, [s for s in batch if s["id"] not in run["finished"]])
        if problem:
            save(run)
            if run["status"] != "failed":
                return run["status"] != "paused"
            break
        if run["mode"] == "auto":
            settle(run, batch)
        else:
            advance_phase(run)
        save(run)
    save(run)
    write_summary(run)
    log(f"{run['id']}: {run['status']}")
    return True


# ---------------------------------------------------------------- commands

def tick() -> None:
    configure()
    with open(RUNS_DIR / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another tick is still running, skipping")
            return
        heartbeat(state="scanning", error=None, tick_started=datetime.now().isoformat())
        try:
            claude = find_claude()
            heartbeat(auth=check_subscription(claude), cli=claude)
            pending = [r for r in load_runs() if r["status"] in ("running", "paused")]
            for run in pending + claim_new_inputs():
                heartbeat(state="working", run=run["id"])
                if not process(claude, run):
                    heartbeat(limit_hit=datetime.now().isoformat())
                    break
        except SystemExit as e:
            heartbeat(error=str(e))
            raise
        finally:
            heartbeat(state="idle", run=None, tick_finished=datetime.now().isoformat())


def status() -> None:
    configure()
    runs = load_runs()
    if not runs:
        print(f"No runs yet. Drop a requirements .md file into {INPUT_DIR}.")
    for r in runs:
        at = f"phase {r['phase'] + 1}/{len(r['pipeline'])}" if r["phase"] is not None and r["status"] != "done" \
            else ",".join(r["next"]) or "-"
        print(f"{r['id']:<50} {r['status']:<8} {r['mode']:<13} at={at:<15} "
              f"stages run={len(r['history'])} loops={sum(r['loops'].values())}")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if command == "tick":
        tick()
    elif command == "watch":
        configure()
        log(f"watching {INPUT_DIR} every {WATCH_INTERVAL}s (Ctrl+C to stop)")
        while True:
            try:
                tick()
            except SystemExit as e:
                log(f"tick aborted: {e}")
            time.sleep(WATCH_INTERVAL)
    elif command == "status":
        status()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
