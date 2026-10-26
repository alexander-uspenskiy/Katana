#!/usr/bin/env python3
"""Katana orchestrator.

Takes requirements from each registered project's trigger (its input/ folder today; see factory/triggers/)
and drives each one through the plan in that project's katana.config.json, calling the Claude Code CLI
headless on the user's subscription.

- deterministic mode: runs the plan's phases in order. The stages of a phase run at the same time (up to
  max_parallel); the next phase starts when all of them have finished. A failed gate sends the work back
  to its 'on fail' stage.
- auto mode: the Orchestrator agent picks the next stage(s) each time; required gates must pass before done.
- disabled agents: their stages are skipped (deterministic) or never offered to the Orchestrator (auto).
- budgets: a token cap per run fails the run; a daily token cap pauses the factory until midnight.

Gate steps (`"kind": "gate"`) are measured by Katana itself, with no LLM call: see gates.py.

A gate step or a stage with pass verdicts is a gateway. An autonomous gateway sends failed work back by
itself; a manual one stops the run in the `approval` status and waits for a person to approve, send back or
reject (the dashboard records the answer in the run's state, and the next scan carries on).

    python3 factory/orchestrator.py run [--project PATH]   # process the current input now, then exit
    python3 factory/orchestrator.py tick      # scan every folder-interval and folder-watch project now
    python3 factory/orchestrator.py watch     # scan those projects when due, in the foreground
    python3 factory/orchestrator.py dispatch  # what launchd runs: a background worker per due project
    python3 factory/orchestrator.py status [--project PATH]   # list runs
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import auth
import budget
import dispatch
import gates
import gitrepo
import ledger
import notify
import projects
import settings
import streamlog
import triggers

MAX_ATTEMPTS = int(os.environ.get("KATANA_MAX_ATTEMPTS", "3"))
WATCH_SETTLE_SECONDS = 5  # folder-watch: a new file must be unchanged this long before it is claimed
WATCH_WAIT_SECONDS = 30   # ...and a watch scan waits at most this long for it
SETTING_SOURCES = os.environ.get("KATANA_SETTING_SOURCES", "project")

LIMIT_PATTERN = re.compile(
    r"usage limit|rate limit|limit reached|hit your limit|quota|overloaded|\b429\b", re.I)
# Warnings that the subscription window is nearly used up (CLI stderr; rate_limit_event status in stream-json).
NEAR_LIMIT_PATTERN = re.compile(
    r"approaching (your |the )?(usage |rate )?limit|close to (your |the )?(usage )?limit|allowed_warning", re.I)
NEAR_LIMIT_UTILIZATION = 0.9
BUDGET_POLL = float(os.environ.get("KATANA_BUDGET_POLL", "5"))  # seconds between token checks during a call
BUDGET_CODE = -9  # exit code call_claude reports when it stopped a call at a token cap
HUMAN = {"approve": "approved", "rework": "rework", "reject": "rejected"}  # a person's answer at a manual gateway

COMMON_RULES = """
You are one stage in an unattended software pipeline. Nobody is watching and nobody will answer
questions: make reasonable decisions, record them in your output file, and finish the job.
Work only inside the current directory. Do the work yourself; do not delegate to subagents.
Your output file must end with the VERDICT line your instructions define.
"""

# Set by configure() before each project is scanned, from that project's settings.
ROOT = INPUT_DIR = RUNS_DIR = LOG_FILE = TRIGGER = None
CONF: dict = {}
LOCK = threading.RLock()  # stages of one phase run in threads; they share the run dict and its state file


def configure(project: Path, settle: int = triggers.SETTLE_SECONDS) -> None:
    global ROOT, INPUT_DIR, RUNS_DIR, LOG_FILE, CONF, TRIGGER
    ROOT = Path(project).expanduser().resolve()
    CONF = settings.load(ROOT)
    projects.upgrade_layout(ROOT)
    INPUT_DIR, RUNS_DIR = ROOT / "input", projects.runs_dir(ROOT)
    LOG_FILE = projects.logs_dir(ROOT) / "factory.log"
    for d in (INPUT_DIR, RUNS_DIR, LOG_FILE.parent):
        d.mkdir(parents=True, exist_ok=True)
    projects.write_meta(ROOT)
    ledger.backfill(ROOT)
    TRIGGER = triggers.for_project(ROOT, CONF, log=log, settle=settle)


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with LOCK:
        print(line, flush=True)
        if LOG_FILE:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------- claude CLI

def read_beat() -> dict:
    try:
        return json.loads((RUNS_DIR / ".factory.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def heartbeat(**fields) -> None:
    """Records what the factory is doing, for the dashboard."""
    path = RUNS_DIR / ".factory.json"
    beat = read_beat()
    trig = settings.trigger(CONF)
    beat.update(fields, pid=os.getpid(), interval=trig["interval_min"] * 60, trigger=trig["type"],
                mode=CONF.get("mode"), updated=datetime.now().isoformat())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(beat, indent=2))
    tmp.replace(path)


def call_claude(cmd: list, prompt: str, cwd: Path, log_path: Path, watch=None) -> tuple:
    """Runs one CLI call, writing its stdout to log_path. Returns (exit code, stderr).
    `watch(log_path)` runs every few seconds while the call is in flight; if it returns a reason, the call is
    stopped and the exit code is BUDGET_CODE."""
    timeout = int(CONF.get("station_timeout_min", 60)) * 60
    err_path = log_path.with_name(log_path.name + ".err")
    deadline, stop = time.time() + timeout, None
    with open(log_path, "w") as out, open(err_path, "w+") as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                cwd=cwd, text=True, env=auth.agent_env())
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        while True:
            try:
                proc.wait(timeout=BUDGET_POLL)
                break
            except subprocess.TimeoutExpired:
                reason = watch(log_path) if watch else None
                if time.time() > deadline:
                    stop = (-1, f"timeout after {timeout}s")
                elif reason:
                    stop = (BUDGET_CODE, reason)
                if stop:
                    proc.kill()
                    proc.wait()
                    break
        err.seek(0)
        stderr = err.read()
    err_path.unlink(missing_ok=True)
    return stop if stop else (proc.returncode, stderr)


def failure(code: int, result: dict, stderr: str) -> str | None:
    """None if the call succeeded, else 'budget', 'limit' or 'error'."""
    if code == BUDGET_CODE:
        return "budget"
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
        ledger.record(ROOT, run, path.parent)


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


def start_workspace(run_dir: Path, workspace: Path, run_id: str) -> dict | None:
    """Fills a new run's workspace: a clone of the project's git repository on the branch katana/<run-id> when
    there is one (see gitrepo.py), else an empty git repository. Either way it holds the requirements, committed.
    Returns the clone's details for the run's state, or None for a fresh repository."""
    info = None
    repo, why = gitrepo.detect(ROOT, CONF)
    if repo:
        try:
            shutil.rmtree(workspace)
            info = gitrepo.clone(repo, ROOT, workspace, run_id)
            info["publish"] = (CONF.get("git") or {}).get("publish") or gitrepo.DEFAULT_PUBLISH  # frozen per run
            log(f"{run_id}: workspace cloned from {repo} on branch {info['branch']}"
                + (" (the repository has uncommitted changes, which the run does not see)" if info["dirty"] else ""))
        except (RuntimeError, OSError) as e:
            log(f"{run_id}: could not clone {repo} ({e}); starting from an empty repository")
            shutil.rmtree(workspace, ignore_errors=True)
            info = None
    elif (CONF.get("git") or {}).get("mode", "auto") != "off":
        log(f"{run_id}: starting from an empty repository ({why})")
    (workspace / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copy(run_dir / "requirements.md", workspace / "docs/00-requirements.md")
    if not info:
        git(workspace, "init", "-q", "-b", "main")
        git(workspace, "config", "user.name", "Katana")
        git(workspace, "config", "user.email", "katana@localhost")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-q", "--allow-empty", "-m", "Add requirements")
    return info


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
        repo_git = start_workspace(run_dir, workspace, run_id)
        # The plan and mode are frozen per run, so editing settings never breaks a run in flight.
        run = {"id": run_id, "source": item.key, "status": "running", "mode": CONF["mode"],
               "origin": {"trigger": TRIGGER.name, "key": item.key, "ref": item.ref},
               "pipeline": CONF["pipeline"], "artifacts": CONF.get("artifacts") or [],
               "custom_agents": CONF.get("custom_agents") or {}, "git": repo_git,
               "phase": 0 if CONF["mode"] == "deterministic" else None,
               "next": [], "finished": [], "current": [],
               "visits": {}, "loops": {}, "attempts": 0, "created": datetime.now().isoformat(),
               "history": [], "decisions": []}
        save(run)
        TRIGGER.ack(item, run)
        log(f"claimed {item.key} as run {run_id} ({CONF['mode']} mode)")
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


def catalog_for(run: dict) -> dict:
    """Standard agents plus the project's custom ones. A custom agent deleted since the run started still works."""
    custom = dict(run.get("custom_agents") or {})
    custom.update(CONF.get("custom_agents") or {})
    return settings.agent_catalog({"custom_agents": custom})


def table_for(run: dict) -> dict:
    """The artifacts of a run, as they were when it started."""
    return settings.artifact_table({"artifacts": run.get("artifacts", CONF.get("artifacts"))})


def gated(stage: dict) -> bool:
    """A gate step, or an agent stage with pass verdicts."""
    return settings.is_gate(stage) or bool(stage.get("pass"))


def passed(stage: dict, entry: dict | None) -> bool:
    if not entry:
        return False
    if entry.get("human") == "approved":  # a person accepted the work although the criteria were not met
        return True
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
    plan = enabled_stages(run)  # gates of disabled agents are waived
    builds = [s for s in plan if is_build(s)]
    first_build = order.index(builds[0]["id"]) if builds else len(order)
    built = last_build(run)
    unmet = []
    if builds and built < 0:
        unmet.append(builds[0])
    for s in plan:
        if not s.get("required"):
            continue
        idx = max((i for i, h in enumerate(run["history"]) if h["stage"] == s["id"] and h["outcome"] == "ok"),
                  default=-1)
        stale = order.index(s["id"]) > first_build and idx < built
        if idx < 0 or stale or not passed(s, run["history"][idx]):
            unmet.append(s)
    return unmet


def enabled_stages(run: dict) -> list:
    """The plan's stages whose agent is enabled in settings (gate steps have no agent and always count)."""
    off = settings.disabled_agents(CONF)
    return [s for s in flat(run) if settings.is_gate(s) or s.get("agent") not in off]


def skip_disabled(run: dict, batch: list) -> list:
    """Deterministic mode: drops the stages of disabled agents from a phase, recording each as skipped."""
    off = settings.disabled_agents(CONF)
    keep = []
    for s in batch:
        if settings.is_gate(s) or s.get("agent") not in off:
            keep.append(s)
        elif s["id"] not in run.setdefault("skipped", []):
            run["skipped"].append(s["id"])
            log(f"{run['id']}: skipped {s['title']} ({s['agent']} is disabled)")
    return keep


def count_loop(run: dict, stage: dict, verdict: str) -> bool:
    """Counts a gate failure. Returns False (and fails the run) past the rework limit."""
    run["loops"][stage["id"]] = run["loops"].get(stage["id"], 0) + 1
    limit = int(stage.get("max_loops") or CONF.get("max_iterations", 3))
    if run["loops"][stage["id"]] > limit:
        run["status"] = "failed"
        run["reason"] = f"{stage['title']} still says {verdict} after {limit} rework loops"
        return False
    return True


def needs_approval(run: dict, stage: dict) -> bool:
    """A manual gateway whose latest run failed and that nobody has decided on yet."""
    entry = latest(run, stage["id"])
    return (settings.gateway(stage) == "manual" and entry is not None and entry.get("verdict") != "BLOCKED"
            and not passed(stage, entry) and not entry.get("human"))


def request_approval(run: dict, stage: dict) -> None:
    """Stops the run at a manual gateway until a person decides (see apply_decision)."""
    entry = latest(run, stage["id"])
    target = stage_def(run, fail_target(run, stage))
    if settings.is_gate(stage):
        reasons = [f"{r['label']} {gates.fmt_value(r)} (needs {r['op']} {r['threshold']})"
                   for r in entry.get("results") or [] if r["outcome"] == "block"]
    else:
        reasons = [f"verdict {entry.get('verdict') or 'missing'}; passes on {', '.join(stage['pass'])}"]
    run["status"] = "approval"
    run["reason"] = f"{stage['title']} didn't pass and waits for your decision"
    run["approval"] = {"stage": stage["id"], "title": stage["title"], "verdict": entry.get("verdict"),
                       "visit": entry["visit"], "report": stage["output"], "reasons": reasons,
                       "target": target["id"], "target_title": target["title"],
                       "loops_used": run["loops"].get(stage["id"], 0),
                       "loops_max": int(stage.get("max_loops") or CONF.get("max_iterations", 3)),
                       "since": datetime.now().isoformat()}
    log(f"{run['id']}: {stage['title']} didn't pass; waiting for a person to approve, send back or reject")


def apply_decision(run: dict) -> None:
    """A run waiting at a manual gateway carries on once the dashboard has recorded a decision: the gateway's
    latest entry gets `human` = approved | rework | rejected, which settle() then acts on."""
    ap = run.get("approval") or {}
    human = HUMAN.get(ap.get("decision"))
    if not human:
        return
    entry = latest(run, ap["stage"])
    if entry:
        entry.update(human=human, human_at=datetime.now().isoformat(), human_note=ap.get("note") or "")
    run.setdefault("approvals", []).append(dict(ap, human=human, resolved=datetime.now().isoformat()))
    run.pop("approval", None)
    run.pop("notified", None)  # the next approval request notifies again
    log(f"{run['id']}: {ap.get('title')} {human} by a person" + (f": {ap['note']}" if ap.get("note") else ""))


def settle(run: dict, batch: list) -> list | None:
    """After a phase or batch has finished: stops at a manual gateway that failed, blocks the run on BLOCKED
    and counts failed gates. Returns the failed gates, or None if the run stopped."""
    waiting = [s for s in batch if needs_approval(run, s)]
    if waiting:  # `finished` stays, so the run carries on from here instead of repeating the phase
        request_approval(run, waiting[0])
        return None
    run["finished"], run["next"] = [], []
    for s in batch:
        if (latest(run, s["id"]) or {}).get("verdict") == "BLOCKED":
            run["status"], run["reason"] = "blocked", f"{s['title']} could not continue"
            return None
    failed = [s for s in batch if gated(s) and not passed(s, latest(run, s["id"]))]
    for s in failed:
        entry = latest(run, s["id"])
        if entry.get("human") == "rejected":
            run["status"] = "failed"
            run["reason"] = f"{s['title']} was rejected by a person" + (f": {entry['human_note']}" if entry.get("human_note") else "")
            return None
    for s in failed:
        if not count_loop(run, s, latest(run, s["id"]).get("verdict")):
            return None
    return failed


def advance_phase(run: dict) -> None:
    """Deterministic mode, once every stage of the current phase has finished."""
    failed = settle(run, skip_disabled(run, run["pipeline"][run["phase"]]))
    if failed is None:
        return
    if failed:  # go back to the earliest phase any failed gate points at, and run on from there
        run["phase"] = min(phase_of(run, fail_target(run, s)) for s in failed)
    elif run["phase"] + 1 < len(run["pipeline"]):
        run["phase"] += 1
    else:
        run["status"] = "done"


def describe(run: dict, s: dict, catalog: dict) -> str:
    io = "".join(f" {label}: {', '.join(s[key])}." for key, label in (("inputs", "Reads"), ("outputs", "Writes"))
                 if s.get(key))
    manual = " A person decides when it fails." if settings.gateway(s) == "manual" else ""
    if settings.is_gate(s):
        conds = "; ".join(gates.describe(c) for c in s.get("conditions") or [])
        return (f"{s['id']}: {s['title']}, a gate step Katana measures itself (no agent, no tokens): {conds}. "
                f"On fail the Developer gets {s['output']}.{manual}{io}{' Required.' if s.get('required') else ''}")
    gate = f" Gate: passes on {', '.join(s['pass'])}." if s.get("pass") else ""
    code = " Changes code." if settings.writes_code(catalog, s) else " Writes only its report."
    return (f"{s['id']}: {s['title']} by {catalog[s['agent']].get('title', s['agent'])}. "
            f"{s['task']}{gate}{manual}{code}{io}{' Required.' if s.get('required') else ''}")


def decide(claude: str, run: dict):
    """Auto mode: asks the Orchestrator agent what runs next. Returns a list of stage ids, ['done'],
    or an outcome ('limit' / 'error') if the call failed."""
    run_dir = RUNS_DIR / run["id"]
    workspace = run_dir / "workspace"
    catalog = catalog_for(run)
    router = settings.effective_agent(CONF, catalog, settings.ROUTER)
    plan = enabled_stages(run)  # the Orchestrator never sees stages of disabled agents
    allowed = {s["id"] for s in plan}
    ids = [s["id"] for s in plan]
    unmet = unmet_gates(run)

    lines = [f"Requirement: {run['source']}", "", "Stage catalogue, by plan phase (stages in one phase may run together):"]
    for p, phase in enumerate(run["pipeline"], 1):
        lines += [f"- [phase {p}] {describe(run, s, catalog)}" for s in phase if s["id"] in allowed]
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
           "--json-schema", json.dumps(schema), "--name", f"katana-{run['id']}-orchestrator-{visit}"]

    started = time.time()
    log_path = run_dir / "logs" / f"orchestrator-{visit}-{datetime.now():%H%M%S}.json"
    cur = {"stage": None, "agent": settings.ROUTER, "title": "Choosing the next stage", "visit": visit,
           "model": router["model"], "effort": router["effort"],
           "started": datetime.now().isoformat(), "log": f"logs/{log_path.name}"}
    with LOCK:
        run["current"].append(cur)
        save(run)
    code, stderr = call_claude(cmd, "\n".join(lines), workspace, log_path, budget_watch(run))
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
    catalog = catalog_for(run)
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
    table = table_for(run)
    reads = [table[a] for a in stage.get("inputs") or [] if a in table]
    extra = [table[a] for a in stage.get("outputs") or [] if a in table and table[a].get("path")
             and table[a]["path"] != stage["output"]]
    absent = [a["path"] for a in reads if a.get("path") and not (cwd / a["path"]).exists()]
    if reads:
        task.append("Read first: " + "; ".join(f"{a['path']} ({a['title']})" if a.get("path") else
                                               f"the project code ({a['title']})" for a in reads) + "."
                    + (f" Not written yet: {', '.join(absent)}; say so in your output if you needed it." if absent else ""))
    if extra:
        task.append("Also write: " + "; ".join(f"{a['path']} ({a['title']})" for a in extra) + ".")
    feedback = [g["output"] for g in failed_gates(run) if g["id"] != stage["id"]
                and (fail_target(run, g) == stage["id"] or (run["mode"] == "auto" and is_build(stage)))]
    if feedback:
        task.append(f"Fix every finding in: {', '.join(feedback)}.")
    if run.get("git"):
        task.append(f"The workspace is a clone of an existing repository, on the branch {run['git']['branch']}. Treat its code as "
                    "the codebase to extend: follow its structure and conventions, leave unrelated files alone, and keep its git history.")
    cmd = [claude, "-p",
           "--append-system-prompt", COMMON_RULES + "\n" + agent["prompt"],
           "--model", agent["model"], "--effort", agent["effort"],
           "--allowedTools", ", ".join(agent["tools"]) or "Read",
           "--permission-mode", "acceptEdits", "--permission-prompts", "none",
           "--setting-sources", SETTING_SOURCES,
           "--output-format", "stream-json", "--verbose",
           "--name", f"katana-{run['id']}-{stage['id']}-{visit}"]

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
    code, stderr = call_claude(cmd, "\n".join(task), cwd, log_path, budget_watch(run))
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
             "session_id": result.get("session_id"), "log": f"logs/{log_path.name}",
             "near_limit": near_limit(summary, stderr)}
    if alongside:
        entry["alongside"] = alongside
    if absent:
        entry["missing_inputs"] = absent

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
        unwritten = []
        for a in extra:  # the other artifacts the stage promised: noted, not fatal
            made = cwd / a["path"]
            if made.is_file() and made.stat().st_mtime >= started:
                if snapshot:
                    (workspace / a["path"]).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(made, workspace / a["path"])
            else:
                unwritten.append(a["path"])
        if unwritten:
            entry["missing_outputs"] = unwritten
            log(f"{run['id']}: {stage['title']} did not write {', '.join(unwritten)}")
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
            "sources": sources, "run_tokens": tokens, "env": auth.subscription_env()}


def run_gate(run: dict, gate: dict) -> str:
    """Measures a gate step: Katana runs the checks itself, with no CLI call. Returns ok or error."""
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
        catalog = catalog_for(run)
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
        if any(o == "budget" for o, _ in outcomes):
            return "budget"
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

def hand_back(run: dict) -> None:
    """Fetches the run's branch into the project's repository, once the run has ended, then publishes it as
    the run's `git.publish` setting says (pull request, push, merge or nothing)."""
    info = run["git"]
    if not (info.get("handed_back") or info.get("error")):
        gitrepo.hand_back(info, RUNS_DIR / run["id"] / "workspace")
        save(run)
        log(f"{run['id']}: " + (f"branch {info['branch']} handed back to {info['repo']} ({info['commits']} commits)" if info.get("handed_back")
                                else f"could not hand the branch back: {info['error']}" if info.get("error") else "no new commits to hand back"))
    if info.get("handed_back") and "published" not in info:
        slug = run["id"].split("-", 2)[-1].replace("-", " ")
        stages = "\n".join(f"- {h['title']}: {h.get('outcome') or '-'}" + (f" ({h['verdict']})" if h.get("verdict") else "") for h in run["history"])
        body = (f"Opened by Katana for run `{run['id']}` (status: {run['status']}), from `{run['source']}`.\n\n"
                f"{info['commits']} commit{'s' if info['commits'] != 1 else ''} on `{info['branch']}`.\n\n{stages}\n")
        gitrepo.publish(info, run["status"], f"Katana: {slug}", body)
        save(run)
        pub = info["published"]
        log(f"{run['id']}: " + (f"could not publish branch {info['branch']}: {pub['error']}" if pub.get("error")
                                else {"local": "branch kept local", "pushed": f"branch pushed to origin", "pr": f"pull request {pub.get('url')}",
                                      "merged": f"merged into {pub.get('base')} on origin"}[pub["result"]]
                                + (f" ({pub['note']})" if pub.get("note") else "")))


def publish_line(g: dict) -> str:
    p = g.get("published")
    if not p:
        return ""
    what = (f" Publishing failed: {p['error']}." if p.get("error")
            else {"local": " Kept local; merge it yourself.", "pushed": " Pushed to origin; open a pull request.",
                  "pr": f" Pull request: {p.get('url')}", "merged": f" Merged into `{p.get('base')}` on origin."}[p["result"]])
    return what + (f" Note: {p['note']}." if p.get("note") else "")


def write_summary(run: dict) -> None:
    lines = [f"# Run {run['id']}", "",
             f"- Source: `{run['source']}`",
             f"- Mode: {run['mode']}",
             f"- Status: **{run['status']}**" + (f" ({run['reason']})" if run.get("reason") else ""),
             f"- Rework loops: {sum(run['loops'].values())}",
             "- Code: `workspace/` (git repo), reports: `reports/`, raw logs: `logs/`, gate measurements: `gates/`"]
    g = run.get("git")
    if g:
        lines.append(f"- Branch: `{g['branch']}` from `{g['base'][:8]}` in `{g['repo']}`: " + (
            f"{g['commits']} commit{'s' if g['commits'] != 1 else ''} handed back; review with `git log {g['branch']}`." + publish_line(g) if g.get("handed_back")
            else f"could not be handed back ({g['error']}); the work is in `workspace/`" if g.get("error")
            else "no new commits" if "commits" in g else "work in progress"))
    lines += ["",
             "| # | Phase | Stage | Agent | Run | Outcome | Verdict | Model | Effort | Seconds |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for n, h in enumerate(run["history"], 1):
        lines.append(f"| {n} | {phase_of(run, h['stage']) + 1} | {h['title']} | {h.get('agent') or 'Katana'} | "
                     f"{h['visit']} | {h['outcome']} | {h.get('verdict') or ''} | {h.get('model') or '-'} | "
                     f"{h.get('effort') or '-'} | {h['seconds']} |")
    gate_runs = [h for h in run["history"] if h.get("kind") == "gate" and h.get("results")]
    if gate_runs:
        lines += ["", "## Quality gates", ""]
        for h in gate_runs:
            lines.append(f"- **{h['title']}** run {h['visit']}: {h['verdict']} · " + "; ".join(
                f"{gates.condition_text(r)} → {gates.fmt_value(r)} {r['status']}" for r in h["results"]))
    if run.get("approvals"):
        lines += ["", "## Manual gateways", ""]
        lines += [f"- **{a['title']}** run {a.get('visit')}: {a['human']}" + (f" ({a['note']})" if a.get("note") else "")
                  + (f"; {'; '.join(a['reasons'])}" if a.get("reasons") else "") for a in run["approvals"]]
    if run["decisions"]:
        lines += ["", "## Orchestrator decisions", ""]
        lines += [f"{n}. **{', '.join(d.get('chosen') or []) if isinstance(d.get('chosen'), list) else d.get('chosen') or d['outcome']}**: "
                  f"{d.get('reason', '')} {d.get('override', '')}".rstrip()
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


# ---------------------------------------------------------------- budgets + notifications

def usage(run: dict) -> tuple:
    """(tokens this run has used, tokens all runs used today), counting the in-memory copy of this run."""
    others = [r for r in load_runs() if r["id"] != run["id"]]
    return budget.run_tokens(run), budget.day_tokens(others + [run])


def budget_watch(run: dict):
    """A check for call_claude: stops the call once the run cap or the daily cap is reached. None if no caps."""
    if not any(budget.caps(CONF)):
        return None
    run_used, day_used = usage(run)

    def check(log_path: Path) -> str | None:
        live = budget.total(streamlog.summarize(log_path)["tokens"])
        kind = budget.breach(CONF, run_used + live, day_used + live)
        return {"run": "token cap per run reached", "day": "daily token cap reached"}.get(kind)
    return check


def stop_for_budget(run: dict, kind: str) -> bool:
    """Fails the run at its token cap, or pauses it (and the factory) at the daily cap.
    Returns False when the factory should stop for the day."""
    (run_used, day_used), (run_cap, day_cap) = usage(run), budget.caps(CONF)
    if kind == "run":
        run["status"] = "failed"
        run["reason"] = f"token cap per run reached: {run_used:,} of {run_cap:,} tokens"
        log(f"{run['id']}: {run['reason']}")
        return True
    run["status"] = "paused"
    run["reason"] = f"daily token cap reached: {day_used:,} of {day_cap:,} tokens today; resumes tomorrow"
    heartbeat(budget_paused_until=budget.next_reset().isoformat(), budget_notified=date.today().isoformat())
    log(f"{run['id']}: {run['reason']}")
    return False


def near_limit(summary: dict, stderr: str) -> dict | None:
    """Whether the CLI warned that the subscription's usage window is nearly used up."""
    info = summary.get("rate_limit") or {}
    util = info.get("utilization")
    util = util / 100 if isinstance(util, (int, float)) and util > 1 else util
    warned = info.get("status") == "allowed_warning" or NEAR_LIMIT_PATTERN.search(stderr or "")
    if not warned and not (isinstance(util, (int, float)) and util >= NEAR_LIMIT_UTILIZATION):
        return None
    resets = info.get("resetsAt")
    return {"status": info.get("status") or "warning", "utilization": util,
            "resets_at": datetime.fromtimestamp(resets).isoformat() if isinstance(resets, (int, float)) else None}


def pause_near_limit(run: dict) -> bool:
    """With 'pause when usage is near the limit' on, pauses the run after a stage that got a warning."""
    warning = run["history"][-1].get("near_limit") if run["history"] else None
    if not warning or not (CONF.get("budgets") or {}).get("pause_near_limit") or run["status"] != "running":
        return False
    run["status"] = "paused"
    run["reason"] = "usage is near the subscription limit" + (
        f"; resumes after {warning['resets_at'][11:16]}" if warning.get("resets_at") else "; resumes on a later scan")
    heartbeat(near_limit_until=warning.get("resets_at"), limit_hit=datetime.now().isoformat())
    log(f"{run['id']}: {run['reason']}")
    return True


def announce(run: dict) -> None:
    """Sends a notification when a run reaches done, failed, blocked or paused (once per status)."""
    status = run["status"]
    if status not in settings.NOTIFY_EVENTS or run.get("notified") == status:
        return
    run["notified"] = status
    text = run.get("reason") or {"done": "Every stage finished.",
                                 "paused": "Usage limit reached; it resumes on a later scan."}.get(status, status)
    for problem in notify.send(CONF, status, run["source"], text):
        log(f"{run['id']}: {problem}")


def factory_paused() -> bool:
    """True while the daily token cap, or a near-limit warning, holds the factory until a later time."""
    beat, (_, day_cap) = read_beat(), budget.caps(CONF)
    used = budget.day_tokens(load_runs())
    if day_cap and used >= day_cap:
        until = budget.next_reset()
        heartbeat(budget_paused_until=until.isoformat())
        log(f"daily token cap reached ({used:,} of {day_cap:,} tokens); paused until {until:%Y-%m-%d %H:%M}")
        if beat.get("budget_notified") != date.today().isoformat():
            heartbeat(budget_notified=date.today().isoformat())
            for problem in notify.send(CONF, "paused", "Daily token cap reached",
                                       f"{used:,} of {day_cap:,} tokens used today. The factory resumes tomorrow."):
                log(problem)
        return True
    if beat.get("budget_paused_until"):
        heartbeat(budget_paused_until=None)
    until = beat.get("near_limit_until")
    if until and (CONF.get("budgets") or {}).get("pause_near_limit") and datetime.fromisoformat(until) > datetime.now():
        log(f"usage is near the subscription limit; paused until {until[11:16]}")
        return True
    return False


def process(claude: str, run: dict) -> bool:
    """Drives a run, then sends any notification. Returns False if the tick should stop (usage limit or
    daily token cap)."""
    keep_going = drive(claude, run)
    announce(run)
    save(run)
    return keep_going


def drive(claude: str, run: dict) -> bool:
    """Drives a run until it finishes or pauses. Returns False if a usage limit was hit."""
    if run["status"] == "approval":
        apply_decision(run)
    run["status"] = "running"
    run["current"] = []  # anything listed here was cut off when the factory last stopped
    run.pop("reason", None)
    while run["status"] == "running":
        used = sum(h.get("kind") != "gate" for h in run["history"])
        if used >= int(CONF.get("max_stations", 20)):
            run["status"] = "failed"
            run["reason"] = f"stopped after {used} stages (limit {CONF.get('max_stations')})"
            break
        kind = budget.breach(CONF, *usage(run)) if any(budget.caps(CONF)) else None
        if kind:
            if not stop_for_budget(run, kind):
                save(run)
                return False
            break
        if run["mode"] == "auto" and not run["next"]:
            choice = decide(claude, run)
            if choice == "budget":
                continue
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
        if run["mode"] == "auto":
            batch = [stage_def(run, i) for i in run["next"]]
        else:
            batch = skip_disabled(run, run["pipeline"][run["phase"]])
        problem = run_batch(claude, run, [s for s in batch if s["id"] not in run["finished"]])
        if problem == "budget":
            if not stop_for_budget(run, budget.breach(CONF, *usage(run)) or "run"):
                save(run)
                return False
            break
        if problem:
            save(run)
            if run["status"] != "failed":
                return run["status"] != "paused"
            break
        if run.get("notified") == "paused":
            run.pop("notified")  # it made progress since the last pause notification
        if run["mode"] == "auto":
            settle(run, batch)
        else:
            advance_phase(run)
        if pause_near_limit(run):
            save(run)
            return False
        save(run)
    save(run)
    if run.get("git") and run["status"] in ("done", "failed", "blocked"):
        hand_back(run)
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
            claude = auth.find_claude()
            heartbeat(auth=auth.check(claude), cli=claude)
            if factory_paused():
                return
            todo = [r for r in load_runs() if r["status"] in ("running", "paused")
                    or (r["status"] == "approval" and (r.get("approval") or {}).get("decision"))] + claim_new_inputs()
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
        at = f"phase {r['phase'] + 1}/{len(r['pipeline'])}" if r["phase"] is not None and r["status"] != "done" \
            else ",".join(r["next"]) or "-"
        print(f"{r['id']:<50} {r['status']:<8} {r['mode']:<13} at={at:<15} "
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
