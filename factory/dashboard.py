#!/usr/bin/env python3
"""Cortana dashboard: a local web page that shows the factory's live state and edits its settings.

    python3 factory/dashboard.py [--port 8787] [--home DIR]

Serves on 127.0.0.1 only. The page polls /api/state every 5 seconds.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import budget
import notify
import settings
import streamlog

HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard" / "index.html"
LABEL = "com.cortana.orchestrator"
SCRIPTS = HERE.parent / "scripts"
LAUNCHD_LOG = HERE.parent / "logs" / "launchd.log"
SERVICE_ACTIONS = ("install", "start", "stop", "uninstall")
TASK_LINE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(?:(T\d+)[:.)]?\s*)?(.+)$", re.M)
AC_ROW = re.compile(r"^\|\s*\**(AC-\d+(?:\.\d+)?)\**\s*\|(.+)$", re.M)
TOKEN_KEYS = ("input", "cache_write", "cache_read", "output")
_service_cache = {"at": 0.0, "value": None}


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def service_status(fresh: bool = False) -> dict:
    if fresh or time.time() - _service_cache["at"] > 30:  # launchctl is slow-ish; check twice a minute
        loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                                capture_output=True).returncode == 0
        _service_cache.update(at=time.time(), value={"installed": plist_path().exists(), "loaded": loaded})
    return _service_cache["value"]


def service_view(fresh: bool = False) -> dict:
    try:
        log_tail = LAUNCHD_LOG.read_text(errors="replace").splitlines()[-20:]
    except OSError:
        log_tail = []
    return dict(service_status(fresh), label=LABEL, plist=str(plist_path()), log_path=str(LAUNCHD_LOG),
                log=log_tail, interval=int(os.environ.get("CORTANA_INTERVAL", "600")))


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
        cmd = ["launchctl", "bootstrap", target, str(plist_path())]
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
    catalog = settings.agent_catalog()
    order = list(conf["agents"]) + [n for n in catalog if n not in conf["agents"]]
    out = []
    for name in order:
        if name not in catalog:
            continue
        a = settings.effective_agent(conf, catalog, name)
        out.append({k: a.get(k) for k in ("name", "title", "role", "model", "effort", "tools", "enabled",
                                          "prompt_override")})
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
        text = (workspace / "docs/test-report.md").read_text()
    except OSError:
        return {"total": 0, "pass": 0, "items": []}
    items = {}
    for m in AC_ROW.finditer(text):
        cells = [c.strip(" *`") for c in m.group(2).split("|")]
        result = next((c.upper() for c in cells if c.upper() in ("PASS", "FAIL")), None)
        test = next((c for c in cells if c and c.upper() not in ("PASS", "FAIL")), "")
        if result:
            items[m.group(1)] = {"id": m.group(1), "test": test, "result": result}
    items = list(items.values())
    return {"total": len(items), "pass": sum(i["result"] == "PASS" for i in items), "items": items}


def run_view(run: dict, run_dir: Path) -> dict:
    workspace = run_dir / "workspace"
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    cost = seconds = 0
    for h in run.get("history", []) + run.get("decisions", []):
        for k in TOKEN_KEYS:
            totals[k] += (h.get("tokens") or {}).get(k, 0)
        cost += h.get("cost_usd") or 0
        seconds += h.get("seconds") or 0
    current = run.get("current")
    if current:
        live = streamlog.summarize(run_dir / current["log"])
        if not live["messages"] and current["log"].endswith(".json"):  # orchestrator call: one JSON line
            live["model"] = live["model"] or current["model"]
        started = datetime.fromisoformat(current["started"])
        current = dict(current, live={
            "model": live["model"], "messages": live["messages"], "tokens": live["tokens"],
            "tools": live["tools"], "events": live["events"],
            "elapsed": round((datetime.now() - started).total_seconds())})
    reports = sorted(p.name for p in (run_dir / "reports").glob("*.md"))
    docs = sorted(str(p.relative_to(workspace)) for p in (workspace / "docs").glob("*.md")) \
        if (workspace / "docs").is_dir() else []
    return dict(run, current=current, tasks=parse_tasks(workspace), acceptance=parse_acceptance(workspace),
                reports=reports, docs=docs,
                totals={"tokens": totals, "cost_usd": round(cost, 4), "seconds": seconds,
                        "stations": len(run.get("history", []))})


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
            if h.get("started"):
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
        c = run.get("current")
        if c:
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


def home_status(root: Path) -> dict:
    return {"path": str(root), "exists": root.is_dir(),
            "runs": len(list((root / "runs").glob("*/state.json"))) if root.is_dir() else 0,
            "inputs": len([p for p in (root / "input").glob("*.md") if p.name.lower() != "readme.md"])
            if root.is_dir() else 0}


def state() -> dict:
    conf = settings.load()
    root = settings.home(conf)
    runs_dir = root / "runs"
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
        log_tail = (root / "logs" / "factory.log").read_text(errors="replace").splitlines()[-150:]
    except OSError:
        log_tail = []
    return {"generated": datetime.now().isoformat(), "project": conf.get("project", "Cortana"), "home": str(root),
            "factory": beat, "service": service_status(), "agents": agents_view(conf),
            "budget": budget_view(conf, runs, beat),
            "settings": {k: conf[k] for k in ("mode", "pipeline", "max_iterations", "max_stations",
                                              "station_timeout_min")},
            "queue": queue_view(root), "runs": runs, "events": events, "stats": aggregate(events), "log": log_tail}


def settings_payload() -> dict:
    conf = settings.load()
    catalog = settings.agent_catalog()
    runs_dir = settings.home(conf) / "runs"
    return {"settings": conf, "defaults": settings.defaults(),
            "options": {"models": settings.MODELS, "efforts": settings.EFFORTS, "modes": settings.MODES,
                        "router": settings.ROUTER, "presets": settings.presets(),
                        "notify_events": settings.NOTIFY_EVENTS, "tool_pattern": settings.SAFE_TOOL.pattern,
                        "unsafe_commands": sorted(settings.UNSAFE_COMMANDS)},
            "catalog": {n: {k: a.get(k) for k in ("title", "role", "model", "effort", "tools", "prompt_override")}
                        for n, a in catalog.items()},
            "warnings": settings.warnings(settings.validate(conf)[0]),
            "budget": budget_view(conf, [r for r in map(read_json, runs_dir.glob("*/state.json")) if r],
                                  read_json(runs_dir / ".factory.json", {})),
            "home": home_status(settings.home(conf)),
            "env_override": bool(os.environ.get("CORTANA_HOME"))}


def prompt_view(name: str) -> dict | None:
    catalog = settings.agent_catalog()
    if name not in catalog:
        return None
    a = catalog[name]
    return {"agent": name, "title": a.get("title", name), "prompt": a["prompt"], "default": a["default_prompt"],
            "overridden": a["prompt_override"], "path": str(settings.PROMPTS_DIR / f"{name}.md")}


def post_settings(body: dict) -> tuple:
    clean, errors = settings.validate(body)
    if errors:
        return 400, {"ok": False, "errors": errors}
    settings.save(clean)
    return 200, dict(settings_payload(), ok=True)


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
    settings.save_prompt(name, text)
    return 200, dict(prompt_view(name), ok=True)


def post_notify_test(body: dict) -> tuple:
    errors = []
    conf = dict(settings.load(), notify=settings._validate_notify(body.get("notify") or {}, errors))
    if errors:
        return 400, {"ok": False, "errors": errors}
    if not conf["notify"]["macos"] and not conf["notify"]["teams_webhook"]:
        return 400, {"ok": False, "errors": ["Turn on macOS notifications or add a Teams webhook first."]}
    problems = notify.send(conf, "test", "Test notification", "Notifications from Cortana work.", force=True)
    return (500 if problems else 200), {"ok": not problems, "errors": problems}


POST_ROUTES = {"/api/settings": post_settings, "/api/service": post_service, "/api/prompt": post_prompt,
               "/api/notify-test": post_notify_test}


def report(query: dict) -> str | None:
    root = settings.home(settings.load())
    run_id, name = query.get("run", [""])[0], query.get("name", [""])[0]
    if not re.fullmatch(r"[\w.-]+", run_id) or not re.fullmatch(r"[\w-]+(/[\w-]+)*\.md", name):
        return None
    run_dir = root / "runs" / run_id
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
        elif url.path == "/api/state":
            self.json(200, state())
        elif url.path == "/api/settings":
            self.json(200, settings_payload())
        elif url.path == "/api/service":
            self.json(200, service_view(fresh=True))
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
        if not self.local_host() or self.headers.get("X-Cortana") != "1" \
                or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(403, b"forbidden", "text/plain")
        handler = POST_ROUTES.get(urlparse(self.path).path)
        if not handler:
            return self.send(404, b"not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 200_000)))
        except (ValueError, json.JSONDecodeError):
            return self.json(400, {"ok": False, "errors": ["The request was not valid JSON."]})
        if not isinstance(body, dict):
            return self.json(400, {"ok": False, "errors": ["The request must be a JSON object."]})
        self.json(*handler(body))

    def log_message(self, *args) -> None:
        pass  # keep the terminal quiet; the page polls every 5 seconds


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("CORTANA_DASHBOARD_PORT", 8787)))
    parser.add_argument("--home", help="project folder to show, overriding settings (for testing)")
    args = parser.parse_args()
    if args.home:
        os.environ["CORTANA_HOME"] = str(Path(args.home).resolve())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Cortana dashboard: http://127.0.0.1:{args.port}  (project folder {settings.home(settings.load())})",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
