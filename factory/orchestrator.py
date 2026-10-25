#!/usr/bin/env python3
"""Darkfactory orchestrator.

Scans input/ for requirement .md files and drives each one through the station
pipeline (analyst -> architect -> developer -> tester -> reviewer), calling the
Claude Code CLI headless on the user's subscription.

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
import time
from datetime import datetime
from pathlib import Path

import streamlog

ROOT = Path(os.environ.get("DF_HOME", Path(__file__).resolve().parent.parent))
INPUT_DIR = ROOT / "input"
RUNS_DIR = ROOT / "runs"
AGENTS_DIR = Path(__file__).resolve().parent / "agents"

MAX_ITERATIONS = int(os.environ.get("DF_MAX_ITERATIONS", "3"))
MAX_ATTEMPTS = int(os.environ.get("DF_MAX_ATTEMPTS", "3"))
STATION_TIMEOUT = int(os.environ.get("DF_STATION_TIMEOUT", "3600"))
WATCH_INTERVAL = int(os.environ.get("DF_INTERVAL", "600"))
SETTLE_SECONDS = 60  # ignore input files modified more recently (still being written)
SETTING_SOURCES = os.environ.get("DF_SETTING_SOURCES", "project")

# Any of these would make the CLI bill an API account instead of the subscription.
API_ENV_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SIMPLE",
)
LIMIT_PATTERN = re.compile(
    r"usage limit|rate limit|limit reached|hit your limit|quota|overloaded|\b429\b", re.I)

COMMON_RULES = """
You are one station in an unattended software pipeline. Nobody is watching and nobody will answer
questions: make reasonable decisions, record them in your output file, and finish the job.
Work only inside the current directory. Do the work yourself; do not delegate to subagents.
Your output file must end with the VERDICT line your instructions define.
"""


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- claude CLI

def find_claude() -> str:
    candidates = [os.environ.get("DF_CLAUDE_BIN"), shutil.which("claude"),
                  str(Path.home() / ".local/bin/claude")]
    # Fallback: the CLI bundled with the Claude desktop app, newest version first.
    bundled = glob.glob(str(Path.home() / "Library/Application Support/Claude/claude-code/*/*"
                                          "/claude.app/Contents/MacOS/claude"))
    version = lambda p: [int(x) for x in re.findall(r"\d+", Path(p).parents[4].name)]
    candidates += sorted(bundled, key=version, reverse=True)
    for c in candidates:
        if c and os.access(c, os.X_OK):
            return c
    raise SystemExit("Claude Code CLI not found. Install it or set DF_CLAUDE_BIN.")


def subscription_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in API_ENV_VARS}


def check_subscription(claude: str) -> None:
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
    beat.update(fields, pid=os.getpid(), interval=WATCH_INTERVAL, updated=datetime.now().isoformat())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(beat, indent=2))
    tmp.replace(path)


# ---------------------------------------------------------------- agents

def load_agent(name: str) -> dict:
    text = (AGENTS_DIR / f"{name}.md").read_text()
    _, front, body = text.split("---", 2)
    agent = {"prompt": body.strip()}
    for line in front.strip().splitlines():
        key, _, value = line.partition(":")
        agent[key.strip()] = value.strip()
    return agent


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
        git(workspace, "config", "user.name", "Darkfactory")
        git(workspace, "config", "user.email", "darkfactory@localhost")
        git(workspace, "add", "-A")
        git(workspace, "commit", "-q", "-m", "Add requirements")
        run = {"id": run_id, "source": src.name, "status": "running", "stage": "analyst",
               "iteration": 1, "attempts": 0, "created": datetime.now().isoformat(),
               "history": []}
        save(run)
        log(f"claimed {src.name} as run {run_id}")
        runs.append(run)
    return runs


# ---------------------------------------------------------------- stations

def run_station(claude: str, run: dict) -> tuple:
    """Run the run's current station. Returns (outcome, verdict) with outcome ok|limit|error."""
    stage, iteration = run["stage"], run["iteration"]
    agent = load_agent(stage)
    run_dir = RUNS_DIR / run["id"]
    workspace = run_dir / "workspace"
    output = workspace / agent["output"]
    model, effort = agent.get("model", "sonnet"), agent.get("effort", "medium")
    task = (f"Run the {stage} station for iteration {iteration}. "
            f"Write your output to {agent['output']}.")
    cmd = [claude, "-p",
           "--append-system-prompt", COMMON_RULES + "\n" + agent["prompt"],
           "--model", model,
           "--effort", effort,
           "--allowedTools", agent.get("tools", "Read,Glob,Grep"),
           "--permission-mode", "acceptEdits",
           "--permission-prompts", "none",
           "--setting-sources", SETTING_SOURCES,
           "--output-format", "stream-json", "--verbose",
           "--name", f"df-{run['id']}-{stage}-{iteration}"]

    started = time.time()
    stamp = f"{stage}-iter{iteration}-{datetime.now():%H%M%S}"
    log_path = run_dir / "logs" / f"{stamp}.jsonl"
    # The dashboard reads `current` and tails the log while the station runs.
    run["current"] = {"stage": stage, "iteration": iteration, "model": model, "effort": effort,
                      "started": datetime.now().isoformat(), "log": f"logs/{log_path.name}"}
    save(run)
    log(f"{run['id']}: {stage} (iteration {iteration}, {model}/{effort}) started")
    with open(log_path, "w") as out:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.PIPE,
                                cwd=workspace, text=True, env=subscription_env())
        try:
            _, stderr = proc.communicate(task, timeout=STATION_TIMEOUT)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            stderr, code = f"timeout after {STATION_TIMEOUT}s", -1
    if stderr:
        (run_dir / "logs" / f"{stamp}.stderr").write_text(stderr)
    run.pop("current", None)

    summary = streamlog.summarize(log_path)
    result = summary["result"] or {}
    entry = {"stage": stage, "iteration": iteration, "model": model, "effort": effort,
             "models": sorted(result.get("modelUsage") or {}) or [m for m in [summary["model"]] if m],
             "started": datetime.fromtimestamp(started).isoformat(), "finished": datetime.now().isoformat(),
             "seconds": round(time.time() - started),
             "api_seconds": round((result.get("duration_api_ms") or 0) / 1000),
             "turns": result.get("num_turns"), "tokens": summary["tokens"],
             "cost_usd": result.get("total_cost_usd"), "tools": summary["tools"],
             "denials": len(result.get("permission_denials") or []),
             "session_id": result.get("session_id"), "log": f"logs/{log_path.name}"}

    if code != 0 or result.get("is_error") or not result:
        text = f"{result.get('result', '')} {stderr}"
        entry["outcome"] = "limit" if LIMIT_PATTERN.search(text) else "error"
        entry["error"] = text.strip()[:500]
        run["history"].append(entry)
        return entry["outcome"], None

    if not output.exists() or output.stat().st_mtime < started:
        entry.update(outcome="error", error=f"{agent['output']} was not written")
        run["history"].append(entry)
        return "error", None

    verdicts = re.findall(r"^\**VERDICT:\s*([A-Z_]+)", output.read_text(), re.M)
    verdict = verdicts[-1] if verdicts else None
    shutil.copy(output, run_dir / "reports" / f"{stage}-iter{iteration}.md")
    entry.update(outcome="ok", verdict=verdict)
    run["history"].append(entry)
    log(f"{run['id']}: {stage} finished, verdict {verdict}")
    return "ok", verdict


def next_stage(run: dict, verdict: str) -> None:
    """Advance the run's state machine after a successful station."""
    stage = run["stage"]
    rework = (stage == "tester" and verdict != "PASS") or \
             (stage == "reviewer" and verdict != "APPROVE")
    if stage == "analyst" and verdict == "BLOCKED":
        run["status"] = "blocked"
    elif rework:
        if run["iteration"] >= MAX_ITERATIONS:
            run["status"] = "failed"
            run["reason"] = f"{stage} still says {verdict} after {MAX_ITERATIONS} iterations"
        else:
            run["iteration"] += 1
            run["stage"] = "developer"
    elif stage == "reviewer":
        run["status"] = "done"
    else:
        order = ["analyst", "architect", "developer", "tester", "reviewer"]
        run["stage"] = order[order.index(stage) + 1]


def write_summary(run: dict) -> None:
    lines = [f"# Run {run['id']}", "",
             f"- Source: `{run['source']}`",
             f"- Status: **{run['status']}**" + (f" ({run['reason']})" if run.get("reason") else ""),
             f"- Iterations: {run['iteration']}",
             "- Code: `workspace/` (git repo), reports: `reports/`, raw logs: `logs/`", "",
             "| Stage | Iter | Outcome | Verdict | Seconds | Turns |",
             "|---|---|---|---|---|---|"]
    for h in run["history"]:
        lines.append(f"| {h['stage']} | {h['iteration']} | {h['outcome']} | "
                     f"{h.get('verdict') or ''} | {h['seconds']} | {h.get('turns') or ''} |")
    (RUNS_DIR / run["id"] / "SUMMARY.md").write_text("\n".join(lines) + "\n")


def process(claude: str, run: dict) -> bool:
    """Drive a run until it finishes or pauses. Returns False if a usage limit was hit."""
    run["status"] = "running"
    while run["status"] == "running":
        outcome, verdict = run_station(claude, run)
        if outcome == "limit":
            run["status"] = "paused"
            log(f"{run['id']}: usage limit hit, pausing until the next tick")
            save(run)
            return False
        if outcome == "error":
            run["attempts"] += 1
            log(f"{run['id']}: {run['stage']} failed "
                f"(attempt {run['attempts']}/{MAX_ATTEMPTS}): {run['history'][-1]['error']}")
            if run["attempts"] >= MAX_ATTEMPTS:
                run["status"] = "failed"
                run["reason"] = f"{run['stage']} failed {MAX_ATTEMPTS} times"
            save(run)
            if run["status"] == "running":
                return True  # retry on the next tick
            break
        run["attempts"] = 0
        next_stage(run, verdict)
        save(run)
    write_summary(run)
    log(f"{run['id']}: {run['status']}")
    return True


# ---------------------------------------------------------------- commands

def tick() -> None:
    INPUT_DIR.mkdir(exist_ok=True)
    RUNS_DIR.mkdir(exist_ok=True)
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
    runs = load_runs()
    if not runs:
        print("No runs yet. Drop a requirements .md file into input/.")
    for r in runs:
        print(f"{r['id']:<50} {r['status']:<8} stage={r['stage']:<10} iteration={r['iteration']}")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if command == "tick":
        tick()
    elif command == "watch":
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
