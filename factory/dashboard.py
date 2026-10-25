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

import settings
import streamlog

HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard" / "index.html"
LABEL = "com.cortana.orchestrator"
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


def service_status() -> dict:
    if time.time() - _service_cache["at"] > 30:  # launchctl is slow-ish; check twice a minute
        plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                                capture_output=True).returncode == 0
        _service_cache.update(at=time.time(), value={"installed": plist.exists(), "loaded": loaded})
    return _service_cache["value"]


def agents_view(conf: dict) -> list:
    catalog = settings.agent_catalog()
    order = list(conf["agents"]) + [n for n in catalog if n not in conf["agents"]]
    out = []
    for name in order:
        if name not in catalog:
            continue
        a = settings.effective_agent(conf, catalog, name)
        out.append({k: a.get(k) for k in ("name", "title", "role", "model", "effort", "tools")})
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
            "settings": {k: conf[k] for k in ("mode", "pipeline", "max_iterations", "max_stations",
                                              "station_timeout_min")},
            "queue": queue_view(root), "runs": runs, "events": events, "stats": aggregate(events), "log": log_tail}


def settings_payload() -> dict:
    conf = settings.load()
    catalog = settings.agent_catalog()
    return {"settings": conf, "defaults": settings.defaults(),
            "options": {"models": settings.MODELS, "efforts": settings.EFFORTS, "modes": settings.MODES,
                        "router": settings.ROUTER},
            "catalog": {n: {k: a.get(k) for k in ("title", "role", "model", "effort", "tools")}
                        for n, a in catalog.items()},
            "home": home_status(settings.home(conf)),
            "env_override": bool(os.environ.get("CORTANA_HOME"))}


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
        if urlparse(self.path).path != "/api/settings":
            return self.send(404, b"not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 200_000)))
        except (ValueError, json.JSONDecodeError):
            return self.json(400, {"ok": False, "errors": ["The request was not valid JSON."]})
        clean, errors = settings.validate(body)
        if errors:
            return self.json(400, {"ok": False, "errors": errors})
        settings.save(clean)
        self.json(200, dict(settings_payload(), ok=True))

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
