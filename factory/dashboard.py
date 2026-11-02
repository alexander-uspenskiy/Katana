#!/usr/bin/env python3
"""Katana dashboard: a local web page that shows the factory's live state and edits its settings.

    python3 factory/dashboard.py [--port 8787] [--home DIR]

Serves on 127.0.0.1 only. The page polls /api/state every 5 seconds and shows the active project from
~/.katana/projects.json; with no project selected it opens on the start screen. The All projects page totals the token
spend of every project, read from the usage ledger in ~/.katana/projects/*/usage/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard" / "index.html"
INSTALL_SCRIPT = HERE.parent / "scripts" / "install-service.sh"
LABEL = dispatch.LABEL
SCRIPTS = HERE.parent / "scripts"
LAUNCHD_LOG = dispatch.LAUNCHD_LOG
SERVICE_ACTIONS = ("install", "start", "stop", "uninstall")
ASSETS = {"graph.js": "text/javascript; charset=utf-8", "graph.css": "text/css; charset=utf-8"}  # served from dashboard/
TASK_LINE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(?:(T\d+)[:.)]?\s*)?(.+)$", re.M)
TOKEN_KEYS = ("input", "cache_write", "cache_read", "output")
_service_cache = {"at": 0.0, "value": None}


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


_state_files: dict = {}  # state.json path -> (mtime, run): the account totals read every run of every project


def read_state(path: Path):
    """A run's state.json, parsed again only when the file changed."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    hit = _state_files.get(path)
    if not hit or hit[0] != mtime:
        hit = _state_files[path] = (mtime, read_json(path))
    return hit[1]


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def service_status(fresh: bool = False) -> dict:
    if fresh or time.time() - _service_cache["at"] > 30:  # launchctl is slow-ish; check twice a minute
        loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                                capture_output=True).returncode == 0
        _service_cache.update(at=time.time(), value={"installed": dispatch.PLIST_PATH.exists(), "loaded": loaded,
                                                     "watching": dispatch.installed_watch_paths() or []})
    return _service_cache["value"]


def sync_service() -> str | None:
    """Rewrites the launchd plist if the folders it should watch changed. Returns a note for the page."""
    installed = dispatch.installed_watch_paths()
    if installed is None or os.environ.get("KATANA_HOME") or installed == dispatch.watch_paths():
        return None
    out = subprocess.run([str(INSTALL_SCRIPT)], capture_output=True, text=True, timeout=60)
    _service_cache["at"] = 0
    return "Service updated to watch the new input folders." if out.returncode == 0 \
        else f"Couldn't update the service: {(out.stderr or out.stdout).strip()[:300]}"


def service_view(fresh: bool = False) -> dict:
    try:
        log_tail = LAUNCHD_LOG.read_text(errors="replace").splitlines()[-20:]
    except OSError:
        log_tail = []
    return dict(service_status(fresh), label=LABEL, plist=str(dispatch.PLIST_PATH), log_path=str(LAUNCHD_LOG),
                log=log_tail, interval=dispatch.DISPATCH_SECONDS)


def service_action(action: str) -> tuple:
    """Runs one service action with the install scripts or launchctl. Returns (ok, message)."""
    target, status = f"gui/{os.getuid()}", service_status(fresh=True)
    if action == "install":
        cmd = [str(SCRIPTS / "install-service.sh")]
    elif action == "uninstall":
        cmd = [str(SCRIPTS / "uninstall-service.sh")]
    elif action == "start":
        if not status["installed"]:
            return False, "Install the service first."
        if status["loaded"]:
            return True, "The service is already running."
        cmd = ["launchctl", "bootstrap", target, str(dispatch.PLIST_PATH)]
    elif action == "stop":
        if not status["loaded"]:
            return True, "The service is already stopped."
        cmd = ["launchctl", "bootout", f"{target}/{LABEL}"]
    else:
        return False, "Unknown action."
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    finally:
        _service_cache["at"] = 0
    if out.returncode != 0:
        return False, (out.stdout + out.stderr).strip()[-600:] or f"{action} failed"
    return True, {"install": "Installed and started.", "uninstall": "Uninstalled.", "start": "Started.",
                  "stop": "Stopped."}[action]


def budget_view(conf: dict, runs: list, beat: dict) -> dict:
    run_cap, day_cap = budget.caps(conf)
    now, today = datetime.now().isoformat(), budget.day_tokens(runs)
    paused_until = beat.get("budget_paused_until")
    capped = day_cap and today >= day_cap  # a raised cap lifts the pause on the next scan
    near = beat.get("near_limit_until")
    return {"run_cap": run_cap, "daily_cap": day_cap, "today": today,
            "resets": budget.next_reset().isoformat(),
            "paused_until": paused_until if capped and paused_until and paused_until > now else None,
            "near_limit_until": near if near and near > now else None,
            "pause_near_limit": bool((conf.get("budgets") or {}).get("pause_near_limit"))}


def agents_view(conf: dict) -> list:
    catalog = settings.agent_catalog(conf)
    order = list(conf["agents"]) + [n for n in catalog if n not in conf["agents"]]
    out = []
    for name in order:
        if name not in catalog:
            continue
        a = settings.effective_agent(conf, catalog, name)
        out.append(dict({k: a.get(k) for k in ("name", "title", "role", "model", "effort", "tools", "enabled",
                                               "prompt_override")},
                        writes_code=settings.writes_code(catalog, {"agent": name}), custom=bool(a.get("custom"))))
    return out


def parse_tasks(workspace: Path) -> dict:
    try:
        text = (workspace / "docs/tasks.md").read_text()
    except OSError:
        return {"total": 0, "done": 0, "items": []}
    items = [{"id": m.group(2) or f"T{i + 1}", "text": m.group(3).strip(), "done": m.group(1) != " "}
             for i, m in enumerate(TASK_LINE.finditer(text))]
    return {"total": len(items), "done": sum(i["done"] for i in items), "items": items}


def parse_acceptance(workspace: Path) -> dict:
    try:
        return gates.parse_acceptance((workspace / "docs/test-report.md").read_text())
    except OSError:
        return {"total": 0, "pass": 0, "items": []}


def run_view(run: dict, run_dir: Path) -> dict:
    workspace = run_dir / "workspace"
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    cost = seconds = 0
    for h in run.get("history", []) + run.get("decisions", []):
        for k in TOKEN_KEYS:
            totals[k] += (h.get("tokens") or {}).get(k, 0)
        cost += h.get("cost_usd") or 0
        seconds += h.get("seconds") or 0
    current = run.get("current") or []
    current = [current] if isinstance(current, dict) else current  # runs from before parallel stages
    live_list = []
    for c in current:  # one entry per stage running now
        elapsed = round((datetime.now() - datetime.fromisoformat(c["started"])).total_seconds())
        if c.get("kind") == "gate":
            live_list.append(dict(c, live={"model": None, "messages": 0, "tokens": dict.fromkeys(TOKEN_KEYS, 0),
                                           "tools": {}, "events": [], "elapsed": elapsed}))
            continue
        live = streamlog.summarize(run_dir / c["log"])
        if not live["messages"] and c["log"].endswith(".json"):  # orchestrator call: one JSON line
            live["model"] = live["model"] or c["model"]
        live_list.append(dict(c, live={
            "model": live["model"], "messages": live["messages"], "tokens": live["tokens"],
            "tools": live["tools"], "events": live["events"], "elapsed": elapsed}))
    pipeline = settings.phases(run.get("pipeline"))
    phase = run.get("phase")
    if "phase" not in run and run.get("stage"):  # runs from before phases
        phase = next((i for i, ph in enumerate(pipeline) if any(s["id"] == run["stage"] for s in ph)), None)
    reports = sorted(p.name for p in (run_dir / "reports").glob("*.md"))
    docs = sorted(str(p.relative_to(workspace)) for p in (workspace / "docs").glob("*.md")) \
        if (workspace / "docs").is_dir() else []
    return dict(run, current=live_list, pipeline=pipeline, phase=phase, next=run.get("next") or
                ([run["stage"]] if run.get("stage") and run.get("mode") == "auto" else []),
                finished=run.get("finished") or [], tasks=parse_tasks(workspace), acceptance=parse_acceptance(workspace),
                reports=reports, docs=docs,
                totals={"tokens": totals, "cost_usd": round(cost, 4), "seconds": seconds,
                        "stations": sum(h.get("kind") != "gate" for h in run.get("history", []))})


def queue_view(root: Path) -> list:
    items = []
    for f in sorted((root / "input").glob("*.md")):
        if f.name.lower() == "readme.md":
            continue
        st = f.stat()
        age = time.time() - st.st_mtime
        items.append({"name": f.name, "bytes": st.st_size, "age": round(age),
                      "draft": f.name.startswith((".", "_")), "settling": age < 60})
    return items


def resolve_models(runs: list) -> None:
    """A station that failed before answering has no model id; borrow the one its alias resolved to elsewhere."""
    entries = [h for r in runs for h in r.get("history", []) + r.get("decisions", [])]
    alias = {h.get("model"): h["models"][0] for h in entries if any(h.get("models") or [])}
    for h in entries:
        known = [m for m in h.get("models") or [] if m]
        h["resolved_model"] = known[0] if known else alias.get(h.get("model"), h.get("model"))


def spend_events(runs: list) -> list:
    """One record per CLI call (stage, orchestrator decision, or live stage so far), for charts and filters."""
    events = []
    for run in runs:
        base = {"run": run["id"], "project": run["source"].replace(".md", "")}
        for h in run.get("history", []):
            if h.get("started") and h.get("kind") != "gate":  # gate steps make no CLI call
                events.append(dict(base, kind="stage", stage=h["stage"], title=h.get("title"), agent=h.get("agent"),
                                   visit=h.get("visit"), started=h["started"], finished=h.get("finished"),
                                   seconds=h.get("seconds"), outcome=h.get("outcome"), verdict=h.get("verdict"),
                                   model=h["resolved_model"], effort=h.get("effort"), tokens=h.get("tokens") or {},
                                   cost_usd=h.get("cost_usd")))
        for d in run.get("decisions", []):
            events.append(dict(base, kind="decision", stage="orchestrator", title="Choose next stage",
                               agent=settings.ROUTER, started=d.get("started") or d["at"], finished=d["at"],
                               seconds=d.get("seconds"), outcome=d.get("outcome"), verdict=d.get("chosen"),
                               model=d["resolved_model"], effort=d.get("effort"), tokens=d.get("tokens") or {},
                               cost_usd=d.get("cost_usd")))
        for c in run.get("current") or []:
            if c.get("kind") == "gate":
                continue
            events.append(dict(base, kind="live", stage=c.get("stage") or "orchestrator", title=c.get("title"),
                               agent=c.get("agent"), visit=c.get("visit"), started=c["started"], finished=None,
                               outcome="running", model=c["live"]["model"] or c["model"], effort=c["effort"],
                               tokens=c["live"]["tokens"]))
    return sorted(events, key=lambda e: e["started"])


def aggregate(events: list) -> dict:
    def bucket():
        return dict(dict.fromkeys(TOKEN_KEYS, 0), stations=0, seconds=0, ok=0, cost_usd=0.0, verdicts={})
    by_model, by_agent = defaultdict(bucket), defaultdict(bucket)
    for e in events:
        if e["kind"] == "live":
            continue
        for b in (by_model[e["model"] or "unknown"], by_agent[e["agent"] or "unknown"]):
            for k in TOKEN_KEYS:
                b[k] += e["tokens"].get(k, 0)
            b["stations"] += 1
            b["seconds"] += e.get("seconds") or 0
            b["ok"] += e.get("outcome") == "ok"
            b["cost_usd"] += e.get("cost_usd") or 0
            if e.get("verdict") and e["kind"] == "stage":
                b["verdicts"][e["verdict"]] = b["verdicts"].get(e["verdict"], 0) + 1
    return {"by_model": by_model, "by_agent": by_agent}


def usage_runs(state_dir: Path) -> list:
    """One project's runs for the account totals, from its usage ledger (see ledger.py), with the tokens of any
    call still running read from that run's stream log while its folder exists."""
    runs = []
    for path in sorted((state_dir / "usage").glob("*.json"), reverse=True):
        rec = read_state(path)
        if not isinstance(rec, dict) or "id" not in rec:
            continue
        live = []
        for c in rec.get("current") or []:
            log = Path(rec.get("run_dir") or "") / (c.get("log") or "")
            s = streamlog.summarize(log) if c.get("log") and log.is_file() else {"model": None, "tokens": dict.fromkeys(TOKEN_KEYS, 0)}
            live.append(dict(c, live={"model": s["model"] or c.get("model"), "tokens": s["tokens"]}))
        runs.append(dict(rec, current=live))
    resolve_models(runs)
    return runs


_ledger = {"at": 0.0}


def refresh_ledger() -> None:
    """Brings the usage ledger up to date for every project this dashboard knows (a few stat calls each), at most
    every 20 seconds."""
    if time.time() - _ledger["at"] < 20:
        return
    _ledger["at"] = time.time()
    home = settings.home()
    for folder in {*projects.registered(), *([home] if home else [])}:
        if folder.is_dir():
            ledger.backfill(folder)


def sum_tokens(events: list) -> dict:
    tokens = dict.fromkeys(TOKEN_KEYS, 0)
    for e in events:
        for k in TOKEN_KEYS:
            tokens[k] += e["tokens"].get(k, 0)
    return tokens


def usage_view(selected: list | None = None) -> dict:
    """Token spend of the whole account: every project that has a usage ledger under ~/.katana/projects/, including ones
    since removed from the list. `projects` and `totals` always cover all of them; `events`, `stats` and `runs`
    only the `selected` project keys (all when None), for the deeper look."""
    refresh_ledger()
    registered = {projects.project_key(p): p for p in projects.registered()}
    active = settings.home()
    active_key = projects.project_key(active) if active else None
    today = datetime.now().strftime("%Y-%m-%d")
    rows, chosen, run_rows = [], [], []
    for entry in projects.known_state():
        runs = usage_runs(entry["dir"])
        folder = registered.get(entry["key"])
        name = (read_json(projects.config_path(folder), {}) if folder else {}).get("project") or entry["name"]
        events = [dict(e, project_key=entry["key"], project_name=name) for e in spend_events(runs)]
        calls = [e for e in events if e["kind"] != "live"]
        tokens = sum_tokens(events)
        rows.append({"key": entry["key"], "name": name, "path": entry["path"], "registered": bool(folder),
                     "active": entry["key"] == active_key, "exists": bool(entry["path"]) and Path(entry["path"]).is_dir(),
                     "runs": len(runs), "calls": len(calls), "tokens": tokens, "total": sum(tokens.values()),
                     "today": budget.day_tokens(runs), "cost_usd": round(sum(e.get("cost_usd") or 0 for e in calls), 4),
                     "seconds": sum(e.get("seconds") or 0 for e in calls),
                     "last": max((e.get("finished") or e["started"] for e in events), default=None),
                     "running": sum(1 for e in events if e["kind"] == "live")})
        if selected is None or entry["key"] in selected:
            chosen += events
            for run in runs:
                mine = [e for e in events if e["run"] == run["id"]]
                run_rows.append({"project_key": entry["key"], "project_name": name, "run": run["id"],
                                 "source": run["source"], "status": run.get("status"),
                                 "started": min((e["started"] for e in mine), default=None),
                                 "tokens": sum_tokens(mine), "calls": sum(e["kind"] != "live" for e in mine),
                                 "cost_usd": round(sum(e.get("cost_usd") or 0 for e in mine), 4),
                                 "seconds": sum(e.get("seconds") or 0 for e in mine)})
    rows.sort(key=lambda r: r["total"], reverse=True)
    run_rows.sort(key=lambda r: r["started"] or "", reverse=True)
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    for r in rows:
        for k in TOKEN_KEYS:
            totals[k] += r["tokens"][k]
    return {"generated": datetime.now().isoformat(), "today": today, "projects": rows,
            "totals": {"tokens": totals, "total": sum(totals.values()), "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
                       "calls": sum(r["calls"] for r in rows), "runs": sum(r["runs"] for r in rows),
                       "today": sum(r["today"] for r in rows)},
            "selected": None if selected is None else [r["key"] for r in rows if r["key"] in selected],
            "events": sorted(chosen, key=lambda e: e["started"]), "stats": aggregate(chosen), "runs": run_rows}


def home_status(root: Path) -> dict:
    return {"path": str(root), "exists": root.is_dir(),
            "runs": len(list(projects.runs_dir(root).glob("*/state.json"))) if root.is_dir() else 0,
            "inputs": len([p for p in (root / "input").glob("*.md") if p.name.lower() != "readme.md"])
            if root.is_dir() else 0}


def projects_view() -> dict:
    reg = projects.load()
    root = settings.home()
    items = []
    for entry in reg["projects"]:
        path = Path(entry["path"])
        conf = settings.load(path) if path.is_dir() else {}
        items.append(dict(home_status(path), name=conf.get("project") or path.name, opened=entry.get("opened"),
                          trigger=settings.trigger(conf) if conf else None,
                          busy=path.is_dir() and dispatch.busy(path)))
    return {"active": str(root) if root else None, "override": bool(os.environ.get("KATANA_HOME")),
            "projects": items}


def state() -> dict:
    root = settings.home()
    base = {"generated": datetime.now().isoformat(), "projects": projects_view(), "auth": auth.status()}
    if not root or not root.is_dir():
        return dict(base, project=None, home=str(root) if root else None)
    conf = settings.load(root)
    runs_dir = projects.runs_dir(root)
    runs = []
    for path in sorted(runs_dir.glob("*/state.json"), reverse=True):
        run = read_json(path)
        if run and "pipeline" in run:  # skip runs from before plans existed
            runs.append(run_view(run, path.parent))
    resolve_models(runs)
    events = spend_events(runs)
    beat = read_json(runs_dir / ".factory.json", {})
    beat["alive"] = beat.get("state") in ("scanning", "working") and pid_alive(beat.get("pid"))
    try:
        log_tail = (projects.logs_dir(root) / "factory.log").read_text(errors="replace").splitlines()[-150:]
    except OSError:
        log_tail = []
    trig = settings.trigger(conf)
    svc = dict(service_status())
    svc["watched"] = str(root / "input") in svc.pop("watching")
    return {**base, "project": conf.get("project", "Katana"), "home": str(root),
            "factory": beat, "service": svc, "agents": agents_view(conf), "trigger": trig,
            "busy": dispatch.busy(root), "budget": budget_view(conf, runs, beat),
            "settings": {k: conf.get(k) for k in ("mode", "pipeline", "max_iterations", "max_stations",
                                                  "station_timeout_min", "max_parallel", "worktrees", "gates",
                                                  "artifacts")},
            "artifacts": list(settings.artifact_table(conf).values()),
            "metrics": gates.METRICS,
            "queue": queue_view(root), "runs": runs, "events": events, "stats": aggregate(events), "log": log_tail}


def settings_payload() -> dict:
    root = settings.home()
    conf = settings.load(root)
    catalog = settings.agent_catalog(conf)
    conf["trigger"] = settings.trigger(conf)
    runs_dir = projects.runs_dir(root) if root else None
    repo, why = gitrepo.detect(root, conf) if root else (None, "no project")
    # The draft holds what the project itself chose ("" = follows the user-level setting), not the merged value.
    conf["git"] = dict(conf.get("git") or {}, publish=settings.project_publish(root) if root else "")
    conf["user_publish"] = settings.load_user().get("git", {}).get("publish") or gitrepo.DEFAULT_PUBLISH
    return {"settings": conf, "defaults": settings.defaults(),
            "git": {"repo": str(repo) if repo else None, "why": why, "remote": gitrepo.remote(repo) if repo else None,
                    "gh": bool(shutil.which("gh")), "publish": gitrepo.PUBLISH, "default_publish": gitrepo.DEFAULT_PUBLISH},
            "options": {"models": settings.MODELS, "efforts": settings.EFFORTS, "modes": settings.MODES,
                        "router": settings.ROUTER, "triggers": settings.TRIGGERS,
                        "metrics": gates.METRICS, "ops": list(gates.OPS),
                        "severities": gates.SEVERITIES, "levels": gates.LEVELS,
                        "unavailable": gates.UNAVAILABLE, "security_sources": gates.SECURITY_SOURCES,
                        "gateways": settings.GATEWAYS, "default_tools": settings.DEFAULT_TOOLS,
                        "presets": settings.presets(), "notify_events": settings.NOTIFY_EVENTS,
                        "tool_pattern": settings.SAFE_TOOL.pattern,
                        "unsafe_commands": sorted(settings.UNSAFE_COMMANDS)},
            "catalog": {n: dict({k: a.get(k) for k in ("title", "role", "model", "effort", "tools", "prompt_override")},
                                writes_code=settings.writes_code(catalog, {"agent": n}), custom=bool(a.get("custom")),
                                **({"prompt": a["prompt"]} if a.get("custom") else {}))
                        for n, a in catalog.items()},
            "artifact_table": list(settings.artifact_table(conf).values()),
            "warnings": settings.warnings(settings.validate(conf)[0]),
            "budget": budget_view(conf, [r for r in map(read_json, runs_dir.glob("*/state.json")) if r]
                                  if runs_dir else [], read_json(runs_dir / ".factory.json", {}) if runs_dir else {}),
            "home": home_status(root) if root else None, "service": service_status(),
            "env_override": bool(os.environ.get("KATANA_HOME"))}


def choose_folder() -> dict:
    """Shows the native macOS folder dialog on this Mac. Waits until the user picks a folder or cancels."""
    script = 'POSIX path of (choose folder with prompt "Choose a Katana project folder")'
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "errors": [f"Couldn't open the folder dialog: {e}"]}
    if out.returncode != 0:
        if "-128" in out.stderr:  # User canceled.
            return {"ok": False, "cancelled": True}
        return {"ok": False, "errors": [out.stderr.strip() or "Couldn't open the folder dialog."]}
    path = out.stdout.strip()
    return {"ok": True, "path": path.rstrip("/") or "/"}


def project_action(body: dict) -> tuple:
    """open | create | forget a project. Returns (HTTP code, response)."""
    action, path = body.get("action"), str(body.get("path") or "").strip()
    if action == "forget":
        projects.forget(path)
        return 200, {"ok": True, "projects": projects_view(), "note": sync_service()}
    if action not in ("open", "create"):
        return 400, {"ok": False, "errors": ["Unknown action."]}
    error = projects.check_folder(path, must_exist=action == "open")
    if error:
        return 400, {"ok": False, "errors": [error]}
    if os.environ.get("KATANA_HOME"):
        return 409, {"ok": False, "errors": ["KATANA_HOME is set, so the dashboard can't switch projects."]}
    folder = projects.open_project(path)
    return 200, {"ok": True, "path": str(folder), "projects": projects_view(), "note": sync_service()}


def run_now() -> tuple:
    root = settings.home()
    if not root or not root.is_dir():
        return 409, {"ok": False, "errors": ["Pick a project first."]}
    if dispatch.busy(root):
        return 200, {"ok": True, "started": False, "note": "A scan of this project is already running."}
    dispatch.spawn_worker(root, scheduled=False)
    return 200, {"ok": True, "started": True}


def submit(body: dict) -> tuple:
    """Writes a pasted requirement into input/ as <title>.md. A once project starts on it right away."""
    root = settings.home()
    if not root or not root.is_dir():
        return 409, {"ok": False, "errors": ["Pick a project first."]}
    title, text = str(body.get("title") or "").strip(), str(body.get("markdown") or "").strip()
    errors = [m for bad, m in ((not title, "Give the requirement a title."), (len(title) > 120, "Keep the title under 120 characters."),
                               (not text, "Paste the requirement text.")) if bad]
    if errors:
        return 400, {"ok": False, "errors": errors}
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "requirement"
    folder = root / "input"
    folder.mkdir(parents=True, exist_ok=True)
    name, n = f"{slug}.md", 2
    while (folder / name).exists():
        name, n = f"{slug}-{n}.md", n + 1
    body_text = text if text.lstrip().startswith("# ") else f"# {title}\n\n{text}"
    tmp = folder / f".{name}.tmp"  # a leading dot keeps scans away until the rename
    tmp.write_text(body_text + "\n")
    past = time.time() - triggers.SETTLE_SECONDS - 1  # written in one go, so there's nothing to settle
    os.utime(tmp, (past, past))
    tmp.replace(folder / name)
    kind = settings.trigger(settings.load(root))["type"]
    started = kind == "once" and not dispatch.busy(root)
    if started:
        dispatch.spawn_worker(root, scheduled=False)
    return 200, {"ok": True, "name": name, "trigger": kind, "started": started}


def prompt_view(name: str) -> dict | None:
    catalog = settings.agent_catalog()
    if name not in catalog:
        return None
    a = catalog[name]
    custom = bool(a.get("custom"))  # a custom agent's prompt is part of the project's settings
    return {"agent": name, "title": a.get("title", name), "prompt": a["prompt"], "default": a["default_prompt"],
            "overridden": a["prompt_override"], "custom": custom,
            "path": str(settings.settings_path(settings.home())) if custom else str(settings.PROMPTS_DIR / f"{name}.md")}


def post_settings(body: dict) -> tuple:
    root = settings.home()
    if not root:
        return 409, {"ok": False, "errors": ["Pick a project first."]}
    clean, errors = settings.validate(body)
    if errors:
        return 400, {"ok": False, "errors": errors}
    user_publish = body.get("user_publish")
    if user_publish not in (None, *gitrepo.PUBLISH):
        return 400, {"ok": False, "errors": ["Git: choose how to publish the branch: pull request, push, merge or keep it local."]}
    settings.save(clean, root)
    if user_publish:
        settings.save_user("" if user_publish == gitrepo.DEFAULT_PUBLISH else user_publish)
    return 200, dict(settings_payload(), ok=True, note=sync_service())


def post_service(body: dict) -> tuple:
    action = body.get("action")
    if action not in SERVICE_ACTIONS:
        return 400, {"ok": False, "errors": ["Unknown service action."]}
    ok, message = service_action(action)
    return (200 if ok else 500), {"ok": ok, "message": message, "service": service_view(fresh=True)}


def post_prompt(body: dict) -> tuple:
    name, text = body.get("agent"), body.get("prompt")
    if name not in settings.agent_catalog():
        return 400, {"ok": False, "errors": ["Unknown agent."]}
    if body.get("reset"):
        text = ""
    if not isinstance(text, str) or len(text) > 50_000:
        return 400, {"ok": False, "errors": ["The prompt must be text of at most 50,000 characters."]}
    if settings.agent_catalog()[name].get("custom"):
        if not text.strip():
            return 400, {"ok": False, "errors": ["A custom agent needs a prompt; it has no default to go back to."]}
        settings.set_custom_prompt(name, text)
    else:
        settings.save_prompt(name, text)
    return 200, dict(prompt_view(name), ok=True)


def post_validate(body: dict) -> tuple:
    """Checks a settings draft without saving it, for the graph editor: errors and warnings per stage."""
    return 200, dict(settings.diagnose(body), ok=True)


def post_approval(body: dict) -> tuple:
    """A person's answer at a manual gateway: approve (carry on), rework (send back) or reject (fail the run).
    The answer is written into the run's state, and a scan starts at once if none is running."""
    root = settings.home()
    if not root or not root.is_dir():
        return 409, {"ok": False, "errors": ["Pick a project first."]}
    run_id, decision = str(body.get("run") or ""), body.get("decision")
    if not re.fullmatch(r"[\w.-]+", run_id) or decision not in ("approve", "rework", "reject"):
        return 400, {"ok": False, "errors": ["Choose approve, rework or reject for a run."]}
    path = projects.runs_dir(root) / run_id / "state.json"
    run = read_json(path)
    if not run:
        return 404, {"ok": False, "errors": ["That run doesn't exist."]}
    ap = run.get("approval")
    if run.get("status") != "approval" or not ap or ap.get("decision") or (body.get("stage") and body["stage"] != ap["stage"]):
        return 409, {"ok": False, "errors": ["This run isn't waiting for that decision (anymore)."]}
    ap.update(decision=decision, note=str(body.get("note") or "").strip()[:500], decided=datetime.now().isoformat())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(run, indent=2))
    tmp.replace(path)
    started = not dispatch.busy(root)
    if started:
        dispatch.spawn_worker(root, scheduled=False)
    return 200, {"ok": True, "started": started}


def post_auth(body: dict) -> tuple:
    """save-key | delete-key | method | refresh. Keys go straight to the keychain; replies never carry them."""
    action, provider = body.get("action"), body.get("provider")
    if action in ("save-key", "delete-key") and provider not in auth.PROVIDERS:
        return 400, {"ok": False, "errors": ["Unknown provider."]}
    if action == "save-key":
        error = auth.set_key(provider, str(body.get("key") or ""))
        if error:
            return 400, {"ok": False, "errors": [error], "auth": auth.status(fresh=True)}
    elif action == "delete-key":
        auth.delete_key(provider)
    elif action == "method":
        if body.get("method") not in auth.METHODS:
            return 400, {"ok": False, "errors": ["Choose the subscription or an API key."]}
        auth.set_method(body["method"])
    elif action != "refresh":
        return 400, {"ok": False, "errors": ["Unknown action."]}
    return 200, {"ok": True, "auth": auth.status(fresh=True)}


def post_notify_test(body: dict) -> tuple:
    errors = []
    conf = dict(settings.load(), notify=settings._validate_notify(body.get("notify") or {}, errors))
    if errors:
        return 400, {"ok": False, "errors": errors}
    if not conf["notify"]["macos"] and not conf["notify"]["teams_webhook"]:
        return 400, {"ok": False, "errors": ["Turn on macOS notifications or add a Teams webhook first."]}
    problems = notify.send(conf, "test", "Test notification", "Notifications from Katana work.", force=True)
    return (500 if problems else 200), {"ok": not problems, "errors": problems}


POST_ROUTES = {"/api/settings": post_settings, "/api/validate": post_validate, "/api/approval": post_approval, "/api/service": post_service, "/api/prompt": post_prompt,
               "/api/notify-test": post_notify_test, "/api/projects": project_action,
               "/api/browse": lambda body: (200, choose_folder()), "/api/run": lambda body: run_now(),
               "/api/submit": submit, "/api/auth": post_auth}


def report(query: dict) -> str | None:
    root = settings.home()
    if not root:
        return None
    run_id, name = query.get("run", [""])[0], query.get("name", [""])[0]
    if not re.fullmatch(r"[\w.-]+", run_id) or not re.fullmatch(r"[\w-]+(/[\w-]+)*\.md", name):
        return None
    run_dir = projects.runs_dir(root) / run_id
    for path in (run_dir / "reports" / name, run_dir / "workspace" / name, run_dir / "workspace" / "docs" / name,
                 run_dir / name):
        if path.is_file() and run_dir.resolve() in path.resolve().parents:
            return path.read_text(errors="replace")
    return None


class Handler(BaseHTTPRequestHandler):
    def send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def json(self, code: int, data) -> None:
        self.send(code, json.dumps(data).encode(), "application/json")

    def local_host(self) -> bool:
        # Blocks DNS-rebinding: only answer requests addressed to this machine by name.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def do_GET(self) -> None:
        if not self.local_host():
            return self.send(403, b"forbidden", "text/plain")
        url = urlparse(self.path)
        if url.path == "/":
            self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif url.path.startswith("/assets/") and url.path[8:] in ASSETS:
            self.send(200, (HERE / "dashboard" / url.path[8:]).read_bytes(), ASSETS[url.path[8:]])
        elif url.path == "/api/state":
            self.json(200, state())
        elif url.path == "/api/usage":
            chosen = parse_qs(url.query, keep_blank_values=True).get("projects")
            self.json(200, usage_view(None if chosen is None else [k for k in chosen[0].split(",") if k]))
        elif url.path == "/api/settings":
            self.json(200, settings_payload())
        elif url.path == "/api/projects":
            self.json(200, projects_view())
        elif url.path == "/api/service":
            self.json(200, service_view(fresh=True))
        elif url.path == "/api/auth":
            self.json(200, auth.status())
        elif url.path == "/api/prompt":
            view = prompt_view(parse_qs(url.query).get("agent", [""])[0])
            if view is None:
                self.send(404, b"not found", "text/plain")
            else:
                self.json(200, view)
        elif url.path == "/api/report":
            text = report(parse_qs(url.query))
            if text is None:
                self.send(404, b"not found", "text/plain")
            else:
                self.send(200, text.encode(), "text/plain; charset=utf-8")
        else:
            self.send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        # The custom header and JSON type force a CORS preflight, which this server never approves,
        # so other web pages cannot change settings.
        if not self.local_host() or self.headers.get("X-Katana") != "1" \
                or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(403, b"forbidden", "text/plain")
        handler = POST_ROUTES.get(urlparse(self.path).path)
        if not handler:
            return self.send(404, b"not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 500_000)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self.json(400, {"ok": False, "errors": ["The request was not valid JSON."]})
        if not isinstance(body, dict):
            return self.json(400, {"ok": False, "errors": ["The request must be a JSON object."]})
        self.json(*handler(body))

    def log_message(self, *args) -> None:
        pass  # keep the terminal quiet; the page polls every 5 seconds


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("KATANA_DASHBOARD_PORT", 8787)))
    parser.add_argument("--home", help="open this project folder (registers it and makes it active)")
    args = parser.parse_args()
    projects.upgrade_all()
    if args.home:
        projects.open_project(Path(args.home).resolve())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Katana dashboard: http://127.0.0.1:{args.port}  (project {settings.home() or 'not chosen yet'})",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
