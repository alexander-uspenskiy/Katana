#!/usr/bin/env python3
"""Cortana orchestrator.

Takes requirements from each registered project's trigger (its input/ folder today; see factory/triggers/)
and drives each one through the plan in that project's .cortana/settings.json, calling the Claude Code CLI
headless on the user's subscription.

- deterministic mode: runs the plan's stages in order; a failed gate sends work back to its 'on fail' stage.
- auto mode: the Orchestrator agent picks the next stage each time; required gates must pass before done.

    python3 factory/orchestrator.py run [--project PATH]   # process the current input now, then exit
    python3 factory/orchestrator.py tick      # scan every folder-interval and folder-watch project now
    python3 factory/orchestrator.py watch     # scan those projects when due, in the foreground
    python3 factory/orchestrator.py dispatch  # what launchd runs: a background worker per due project
    python3 factory/orchestrator.py status [--project PATH]   # list runs
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import dispatch
import projects
import settings
import streamlog
import triggers

MAX_ATTEMPTS = int(os.environ.get("CORTANA_MAX_ATTEMPTS", "3"))
WATCH_SETTLE_SECONDS = 5  # folder-watch: a new file must be unchanged this long before it is claimed
WATCH_WAIT_SECONDS = 30   # ...and a watch scan waits at most this long for it
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

# Set by configure() before each project is scanned, from that project's settings.
ROOT = INPUT_DIR = RUNS_DIR = LOG_FILE = TRIGGER = None
CONF: dict = {}


def configure(project: Path, settle: int = triggers.SETTLE_SECONDS) -> None:
    global ROOT, INPUT_DIR, RUNS_DIR, LOG_FILE, CONF, TRIGGER
    ROOT = Path(project).expanduser().resolve()
    CONF = settings.load(ROOT)
    INPUT_DIR, RUNS_DIR, LOG_FILE = ROOT / "input", ROOT / "runs", ROOT / "logs" / "factory.log"
    for d in (INPUT_DIR, RUNS_DIR, LOG_FILE.parent):
        d.mkdir(parents=True, exist_ok=True)
    TRIGGER = triggers.for_project(ROOT, CONF, log=log, settle=settle)


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
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
    trig = settings.trigger(CONF)
    beat.update(fields, pid=os.getpid(), interval=trig["interval_min"] * 60, trigger=trig["type"],
                mode=CONF.get("mode"), updated=datetime.now().isoformat())
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
    path = RUNS_DIR / run["id"] / "state.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(run, indent=2))
    tmp.replace(path)


def load_runs() -> list:
    return [json.loads(p.read_text()) for p in sorted(RUNS_DIR.glob("*/state.json"))]


def git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


def claim_new_inputs() -> list:
    runs = []
    for item in TRIGGER.fetch():
        if item.waiting:
            log(f"skip {item.key}: modified less than {TRIGGER.settle}s ago")
            continue
        run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(item.title)}"
        run_dir = RUNS_DIR / run_id
        workspace = run_dir / "workspace"
        (workspace / "docs").mkdir(parents=True)
        (run_dir / "logs").mkdir()
        (run_dir / "reports").mkdir()
        if not TRIGGER.claim(item, run_dir):
            shutil.rmtree(run_dir)
            continue
        shutil.copy(run_dir / "requirements.md", workspace / "docs/00-requirements.md")
        git(workspace, "init", "-q", "-b", "main")
        git(workspace, "config", "user.name", "Cortana")
        git(workspace, "config", "user.email", "cortana@localhost")
        git(workspace, "add", "-A")
        git(workspace, "commit", "-q", "-m", "Add requirements")
        # The plan and mode are frozen per run, so editing settings never breaks a run in flight.
        pipeline = CONF["pipeline"]
        run = {"id": run_id, "source": item.key, "status": "running", "mode": CONF["mode"],
               "origin": {"trigger": TRIGGER.name, "key": item.key, "ref": item.ref},
               "pipeline": pipeline, "stage": pipeline[0]["id"] if CONF["mode"] == "deterministic" else None,
               "visits": {}, "loops": {}, "attempts": 0, "created": datetime.now().isoformat(),
               "history": [], "decisions": []}
        save(run)
        TRIGGER.ack(item, run)
        log(f"claimed {item.key} as run {run_id} ({CONF['mode']} mode)")
        runs.append(run)
    return runs


# ---------------------------------------------------------------- plan logic

def stage_def(run: dict, stage_id: str) -> dict:
    return next(s for s in run["pipeline"] if s["id"] == stage_id)


def latest(run: dict, stage_id: str) -> dict | None:
    return next((h for h in reversed(run["history"]) if h["stage"] == stage_id and h["outcome"] == "ok"), None)


def passed(stage: dict, entry: dict | None) -> bool:
    return bool(entry) and entry.get("verdict") in stage.get("pass", [])


def failed_gates(run: dict) -> list:
    """Gate stages whose latest finished run did not pass."""
    return [s for s in run["pipeline"] if s.get("pass") and latest(run, s["id"])
            and not passed(s, latest(run, s["id"]))]


def unmet_gates(run: dict) -> list:
    """Required gates that block 'done' in auto mode. A gate placed after a build stage in the plan must
    have passed after the latest build."""
    order = [s["id"] for s in run["pipeline"]]
    builds = [s for s in run["pipeline"] if s["agent"] == "developer"]
    first_build = order.index(builds[0]["id"]) if builds else len(order)
    last_build = max((i for i, h in enumerate(run["history"])
                      if h["outcome"] == "ok" and h.get("agent") == "developer"), default=-1)
    unmet = []
    if builds and last_build < 0:
        unmet.append(builds[0])
    for s in run["pipeline"]:
        if not s.get("required"):
            continue
        idx = max((i for i, h in enumerate(run["history"]) if h["stage"] == s["id"] and h["outcome"] == "ok"),
                  default=-1)
        stale = order.index(s["id"]) > first_build and idx < last_build
        if idx < 0 or stale or not passed(s, run["history"][idx]):
            unmet.append(s)
    return unmet


def advance_deterministic(run: dict, verdict: str) -> None:
    stage = stage_def(run, run["stage"])
    order = [s["id"] for s in run["pipeline"]]
    if verdict == "BLOCKED":
        run["status"] = "blocked"
        run["reason"] = f"{stage['title']} could not continue"
    elif stage.get("pass") and verdict not in stage["pass"]:
        if not count_loop(run, stage, verdict):
            return
        target = stage.get("on_fail")
        if not target:  # default: the nearest earlier build stage, else the stage before
            earlier = order[:order.index(stage["id"])]
            target = next((i for i in reversed(earlier) if stage_def(run, i)["agent"] == "developer"),
                          earlier[-1] if earlier else stage["id"])
        run["stage"] = target
    elif order.index(stage["id"]) + 1 < len(order):
        run["stage"] = order[order.index(stage["id"]) + 1]
    else:
        run["status"] = "done"


def count_loop(run: dict, stage: dict, verdict: str) -> bool:
    """Counts a gate failure. Returns False (and fails the run) past the rework limit."""
    run["loops"][stage["id"]] = run["loops"].get(stage["id"], 0) + 1
    limit = int(CONF.get("max_iterations", 3))
    if run["loops"][stage["id"]] > limit:
        run["status"] = "failed"
        run["reason"] = f"{stage['title']} still says {verdict} after {limit} rework loops"
        return False
    return True


def decide(claude: str, run: dict) -> str:
    """Auto mode: asks the Orchestrator agent for the next stage. Returns a stage id, 'done',
    or an outcome ('limit' / 'error') if the call failed."""
    run_dir = RUNS_DIR / run["id"]
    workspace = run_dir / "workspace"
    catalog = settings.agent_catalog()
    router = settings.effective_agent(CONF, catalog, settings.ROUTER)
    ids = [s["id"] for s in run["pipeline"]]
    unmet = unmet_gates(run)

    lines = [f"Requirement: {run['source']}", "", "Stage catalogue:"]
    for s in run["pipeline"]:
        gate = f" Gate: passes on {', '.join(s['pass'])}." if s.get("pass") else ""
        lines.append(f"- {s['id']}: {s['title']} by {catalog[s['agent']].get('title', s['agent'])}. "
                     f"{s['task']}{gate}{' Required.' if s.get('required') else ''}")
    lines += ["", "History so far:"]
    lines += [f"{i}. {h['stage']} → {h['outcome']}{' ' + h['verdict'] if h.get('verdict') else ''}"
              for i, h in enumerate(run["history"], 1)] or ["(nothing has run yet)"]
    docs = sorted(str(p.relative_to(workspace)) for p in (workspace / "docs").glob("*.md"))
    lines += ["", f"Documents present: {', '.join(docs)}",
              f"Required gates not yet satisfied: {', '.join(s['id'] for s in unmet) or 'none'}",
              f"Stages used: {len(run['history'])} of {CONF.get('max_stations', 20)}",
              "", "Which stage runs next? Answer `done` only if every required gate is satisfied."]
    schema = {"type": "object", "required": ["next", "reason"], "additionalProperties": False,
              "properties": {"next": {"type": "string", "enum": ids + ["done"]}, "reason": {"type": "string"}}}
    visit = len(run["decisions"]) + 1
    cmd = [claude, "-p", "--append-system-prompt", router["prompt"], "--model", router["model"],
           "--effort", router["effort"], "--tools", "", "--permission-prompts", "none",
           "--setting-sources", SETTING_SOURCES, "--output-format", "json",
           "--json-schema", json.dumps(schema), "--name", f"cortana-{run['id']}-orchestrator-{visit}"]

    started = time.time()
    log_path = run_dir / "logs" / f"orchestrator-{visit}-{datetime.now():%H%M%S}.json"
    run["current"] = {"stage": None, "agent": settings.ROUTER, "title": "Choosing the next stage", "visit": visit,
                      "model": router["model"], "effort": router["effort"],
                      "started": datetime.now().isoformat(), "log": f"logs/{log_path.name}"}
    save(run)
    code, stderr = call_claude(cmd, "\n".join(lines), workspace, log_path)
    run.pop("current", None)
    try:
        result = json.loads(log_path.read_text().strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        result = {}
    choice = (result.get("structured_output") or {}).get("next")
    decision = {"at": datetime.now().isoformat(), "started": datetime.fromtimestamp(started).isoformat(),
                "seconds": round(time.time() - started), "model": router["model"], "effort": router["effort"],
                "models": sorted(result.get("modelUsage") or {}),
                "tokens": streamlog.tokens_from_usage(result.get("usage") or {}),
                "cost_usd": result.get("total_cost_usd"), "next": choice,
                "reason": (result.get("structured_output") or {}).get("reason", "")}
    problem = failure(code, result, stderr)
    if problem:
        decision.update(outcome=problem, error=f"{result.get('result', '')} {stderr}".strip()[:500])
        run["decisions"].append(decision)
        return problem
    decision["outcome"] = "ok"
    if choice == "done" and unmet:
        choice = unmet[0]["id"]
        decision["override"] = f"Required gates not met; running {choice} instead."
    elif choice not in ids + ["done"]:
        choice = unmet[0]["id"] if unmet else "done"
        decision["override"] = f"Invalid answer; running {choice} instead."
    decision["chosen"] = choice
    run["decisions"].append(decision)
    log(f"{run['id']}: orchestrator chose {choice}: {decision['reason'][:120]}")
    return choice


# ---------------------------------------------------------------- stations

def run_station(claude: str, run: dict) -> tuple:
    """Runs the run's current stage. Returns (outcome, verdict) with outcome ok|limit|error."""
    stage = stage_def(run, run["stage"])
    catalog = settings.agent_catalog()
    agent = settings.effective_agent(CONF, catalog, stage["agent"])
    run_dir = RUNS_DIR / run["id"]
    workspace = run_dir / "workspace"
    output = workspace / stage["output"]
    visit = run["visits"].get(stage["id"], 0) + 1
    position = [s["id"] for s in run["pipeline"]].index(stage["id"]) + 1

    task = [f"Stage {position} of {len(run['pipeline'])}: {stage['title']} (run {visit}).", stage["task"],
            f"Write your output to {stage['output']}."]
    feedback = [g["output"] for g in failed_gates(run)
                if g["id"] != stage["id"] and (run["mode"] == "auto" or g.get("on_fail") == stage["id"])]
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
    run["current"] = {"stage": stage["id"], "agent": stage["agent"], "title": stage["title"], "visit": visit,
                      "model": agent["model"], "effort": agent["effort"],
                      "started": datetime.now().isoformat(), "log": f"logs/{log_path.name}"}
    save(run)
    log(f"{run['id']}: {stage['title']} ({stage['agent']}, {agent['model']}/{agent['effort']}, run {visit}) started")
    code, stderr = call_claude(cmd, "\n".join(task), workspace, log_path)
    if stderr:
        (run_dir / "logs" / f"{stamp}.stderr").write_text(stderr)
    run.pop("current", None)

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

    problem = failure(code, result, stderr)
    if problem:
        entry.update(outcome=problem, error=f"{result.get('result', '')} {stderr}".strip()[:500])
        run["history"].append(entry)
        return problem, None
    if not output.exists() or output.stat().st_mtime < started:
        entry.update(outcome="error", error=f"{stage['output']} was not written")
        run["history"].append(entry)
        return "error", None

    verdicts = re.findall(r"^\**VERDICT:\s*([A-Z_]+)", output.read_text(), re.M)
    verdict = verdicts[-1] if verdicts else None
    shutil.copy(output, run_dir / "reports" / f"{stage['id']}-run{visit}.md")
    run["visits"][stage["id"]] = visit
    entry.update(outcome="ok", verdict=verdict)
    run["history"].append(entry)
    log(f"{run['id']}: {stage['title']} finished, verdict {verdict}")
    return "ok", verdict


def write_summary(run: dict) -> None:
    lines = [f"# Run {run['id']}", "",
             f"- Source: `{run['source']}`",
             f"- Mode: {run['mode']}",
             f"- Status: **{run['status']}**" + (f" ({run['reason']})" if run.get("reason") else ""),
             f"- Rework loops: {sum(run['loops'].values())}",
             "- Code: `workspace/` (git repo), reports: `reports/`, raw logs: `logs/`", "",
             "| # | Stage | Agent | Run | Outcome | Verdict | Model | Effort | Seconds |",
             "|---|---|---|---|---|---|---|---|---|"]
    for n, h in enumerate(run["history"], 1):
        lines.append(f"| {n} | {h['title']} | {h['agent']} | {h['visit']} | {h['outcome']} | "
                     f"{h.get('verdict') or ''} | {h['model']} | {h['effort']} | {h['seconds']} |")
    if run["decisions"]:
        lines += ["", "## Orchestrator decisions", ""]
        lines += [f"{n}. **{d.get('chosen') or d['outcome']}**: {d.get('reason', '')} {d.get('override', '')}".rstrip()
                  for n, d in enumerate(run["decisions"], 1)]
    (RUNS_DIR / run["id"] / "SUMMARY.md").write_text("\n".join(lines) + "\n")


def report_back(run: dict) -> None:
    """Sends a finished run's outcome to the source it came from (a no-op for the input folder)."""
    try:
        TRIGGER.report(run, (RUNS_DIR / run["id"] / "SUMMARY.md").read_text())
    except Exception as e:  # a source being down must not lose the run's result
        log(f"{run['id']}: could not report back to {TRIGGER.name}: {e}")


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
    while run["status"] == "running":
        if len(run["history"]) >= int(CONF.get("max_stations", 20)):
            run["status"] = "failed"
            run["reason"] = f"stopped after {len(run['history'])} stages (limit {CONF.get('max_stations')})"
            break
        if run["mode"] == "auto" and not run["stage"]:
            choice = decide(claude, run)
            if choice in ("limit", "error"):
                handle_problem(run, choice, "orchestrator", run["decisions"][-1].get("error", ""))
                save(run)
                if run["status"] != "failed":
                    return run["status"] != "paused"
                break
            run["attempts"] = 0
            if choice == "done":
                run["status"] = "done"
                break
            run["stage"] = choice
            save(run)

        outcome, verdict = run_station(claude, run)
        if outcome != "ok":
            handle_problem(run, outcome, stage_def(run, run["stage"])["title"], run["history"][-1].get("error", ""))
            save(run)
            if run["status"] != "failed":
                return run["status"] != "paused"
            break
        run["attempts"] = 0
        if run["mode"] == "auto":
            stage = stage_def(run, run["stage"])
            if verdict == "BLOCKED":
                run["status"], run["reason"] = "blocked", f"{stage['title']} could not continue"
            elif stage.get("pass") and verdict not in stage["pass"]:
                count_loop(run, stage, verdict)
            run["stage"] = None
        else:
            advance_deterministic(run, verdict)
        save(run)
    save(run)
    write_summary(run)
    log(f"{run['id']}: {run['status']}")
    if run["status"] in ("done", "failed", "blocked"):
        report_back(run)
    return True


# ---------------------------------------------------------------- commands

def scan(project: Path, scheduled: bool = True) -> None:
    """One scan of one project: resumes its unfinished runs, then claims and runs new requirements until
    none are left. scheduled=False (Run now, `run`) claims files without waiting for them to settle."""
    kind = settings.trigger(settings.load(Path(project)))["type"]
    settle = (WATCH_SETTLE_SECONDS if kind == "folder-watch" else triggers.SETTLE_SECONDS) if scheduled else 0
    configure(project, settle)
    with open(RUNS_DIR / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another tick is still running, skipping")
            return
        heartbeat(state="scanning", error=None, tick_started=datetime.now().isoformat())
        try:
            if kind == "folder-watch" and scheduled:  # launchd fired as the file appeared; let it finish
                deadline = time.time() + WATCH_WAIT_SECONDS
                while time.time() < deadline and any(i.waiting for i in TRIGGER.fetch()):
                    time.sleep(1)
            claude = find_claude()
            heartbeat(auth=check_subscription(claude), cli=claude)
            todo = [r for r in load_runs() if r["status"] in ("running", "paused")] + claim_new_inputs()
            while todo:
                run = todo.pop(0)
                heartbeat(state="working", run=run["id"])
                if not process(claude, run):
                    heartbeat(limit_hit=datetime.now().isoformat())
                    break
                if not todo:  # requirements that arrived while this run was busy
                    todo = claim_new_inputs()
        except SystemExit as e:
            heartbeat(error=str(e))
            raise
        finally:
            heartbeat(state="idle", run=None, tick_finished=datetime.now().isoformat())


def scan_all(only_due: bool) -> None:
    """Scans every folder-interval and folder-watch project in this process, one after another."""
    failed = []
    for folder, conf in dispatch.service_projects():
        if only_due and not dispatch.due(folder, conf):
            continue
        try:
            scan(folder)
        except SystemExit as e:
            failed.append(f"{folder}: {e}")
    if failed:
        raise SystemExit("\n".join(failed))


def dispatch_due() -> None:
    """What launchd runs: starts a background worker for each project that is due and not already busy."""
    for folder, conf in dispatch.service_projects():
        if dispatch.due(folder, conf) and not dispatch.busy(folder):
            dispatch.spawn_worker(folder, scheduled=True)


def project_arg(value: str | None) -> Path:
    folder = Path(value).expanduser().resolve() if value else settings.home()
    if not folder:
        raise SystemExit("No project selected. Pass --project PATH, or pick one in the dashboard.")
    if not folder.is_dir():
        raise SystemExit(f"Project folder not found: {folder}")
    return folder


def status(project: Path) -> None:
    configure(project)
    runs = load_runs()
    if not runs:
        print(f"No runs yet. Drop a requirements .md file into {INPUT_DIR}.")
    for r in runs:
        print(f"{r['id']:<50} {r['status']:<8} {r['mode']:<13} stage={r['stage'] or '-':<15} "
              f"stages run={len(r['history'])} loops={sum(r['loops'].values())}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="command")
    for name in ("run", "status"):
        p = sub.add_parser(name)
        p.add_argument("--project", help="project folder (default: the active project)")
        if name == "run":
            p.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)  # set by dispatch
    for name in ("tick", "watch", "dispatch"):
        sub.add_parser(name)
    p = sub.add_parser("service-plist", help="print the launchd plist (used by scripts/install-service.sh)")
    p.add_argument("--path", default=os.environ.get("PATH", ""), help="PATH for the agents")
    args = parser.parse_args(sys.argv[1:] or ["tick"])

    if args.command == "run":
        folder = project_arg(args.project)
        if not args.scheduled:
            projects.open_project(folder, activate=False)  # a project run from the CLI shows up in the dashboard
        scan(folder, scheduled=args.scheduled)
    elif args.command == "tick":
        scan_all(only_due=False)
    elif args.command == "dispatch":
        dispatch_due()
    elif args.command == "watch":
        print(f"watching {len(dispatch.service_projects())} project(s); checking every "
              f"{dispatch.DISPATCH_SECONDS}s (Ctrl+C to stop)", flush=True)
        while True:
            try:
                scan_all(only_due=True)
            except SystemExit as e:
                print(f"scan aborted: {e}", flush=True)
            time.sleep(dispatch.DISPATCH_SECONDS)
    elif args.command == "status":
        status(project_arg(args.project))
    elif args.command == "service-plist":
        sys.stdout.buffer.write(dispatch.plist(args.path))


if __name__ == "__main__":
    main()
