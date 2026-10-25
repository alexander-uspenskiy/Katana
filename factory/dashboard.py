#!/usr/bin/env python3
"""Darkfactory dashboard: a local web page that shows the factory's live state.

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

import streamlog

HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard" / "index.html"
LABEL = "com.darkfactory.orchestrator"
STAGES = ["analyst", "architect", "developer", "tester", "reviewer"]
TASK_LINE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(?:(T\d+)[:.)]?\s*)?(.+)$", re.M)
AC_ROW = re.compile(r"^\|\s*\**(AC-\d+(?:\.\d+)?)\**\s*\|(.+)$", re.M)
ROOT = Path(os.environ.get("DF_HOME", HERE.parent))
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


def load_agents() -> list:
    agents = []
    for name in STAGES:
        text = (HERE / "agents" / f"{name}.md").read_text()
        _, front, _ = text.split("---", 2)
        meta = dict(line.split(":", 1) for line in front.strip().splitlines() if ":" in line)
        meta = {k.strip(): v.strip() for k, v in meta.items()}
        meta["tools"] = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]
        agents.append(meta)
    return agents


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
        text = (workspace / "docs/04-test-report.md").read_text()
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
    totals = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    cost = seconds = 0
    for h in run.get("history", []):
        for k in totals:
            totals[k] += (h.get("tokens") or {}).get(k, 0)
        cost += h.get("cost_usd") or 0
        seconds += h.get("seconds") or 0
    current = run.get("current")
    if current:
        live = streamlog.summarize(run_dir / current["log"])
        started = datetime.fromisoformat(current["started"])
        current = dict(current, live={
            "model": live["model"], "messages": live["messages"], "tokens": live["tokens"],
            "tools": live["tools"], "events": live["events"],
            "elapsed": round((datetime.now() - started).total_seconds())})
    reports = sorted(p.name for p in (run_dir / "reports").glob("*.md"))
    return dict(run, current=current, tasks=parse_tasks(workspace),
                acceptance=parse_acceptance(workspace), reports=reports,
                totals={"tokens": totals, "cost_usd": round(cost, 4), "seconds": seconds,
                        "stations": len(run.get("history", []))})


def queue_view() -> list:
    items = []
    for f in sorted((ROOT / "input").glob("*.md")):
        if f.name.lower() == "readme.md":
            continue
        st = f.stat()
        age = time.time() - st.st_mtime
        items.append({"name": f.name, "bytes": st.st_size, "age": round(age),
                      "draft": f.name.startswith((".", "_")), "settling": age < 60})
    return items


def aggregate(runs: list) -> dict:
    by_model = defaultdict(lambda: {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
                                    "stations": 0, "seconds": 0, "cost_usd": 0.0, "efforts": {}})
    by_stage = defaultdict(lambda: {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
                                    "stations": 0, "seconds": 0, "ok": 0, "verdicts": {}})
    timeline = []
    for run in runs:
        for h in run.get("history", []):
            tokens = h.get("tokens") or {}
            model = h.get("resolved_model") or "unknown"
            m, s = by_model[model], by_stage[h["stage"]]
            for k in ("input", "cache_write", "cache_read", "output"):
                m[k] += tokens.get(k, 0)
                s[k] += tokens.get(k, 0)
            effort = h.get("effort") or "default"
            m["efforts"][effort] = m["efforts"].get(effort, 0) + 1
            m["stations"] += 1
            m["seconds"] += h.get("seconds") or 0
            m["cost_usd"] += h.get("cost_usd") or 0
            s["stations"] += 1
            s["seconds"] += h.get("seconds") or 0
            s["ok"] += h.get("outcome") == "ok"
            if h.get("verdict"):
                s["verdicts"][h["verdict"]] = s["verdicts"].get(h["verdict"], 0) + 1
            if h.get("started"):
                timeline.append({"run": run["id"], "stage": h["stage"], "iteration": h["iteration"],
                                 "started": h["started"], "finished": h.get("finished"),
                                 "seconds": h.get("seconds"), "outcome": h.get("outcome"),
                                 "verdict": h.get("verdict"), "model": model, "effort": effort,
                                 "tokens": sum(tokens.values())})
        if run.get("current"):
            c = run["current"]
            timeline.append({"run": run["id"], "stage": c["stage"], "iteration": c["iteration"],
                             "started": c["started"], "finished": None, "outcome": "running",
                             "model": c["live"]["model"] or c["model"], "effort": c["effort"],
                             "tokens": sum(c["live"]["tokens"].values())})
    return {"by_model": by_model, "by_stage": by_stage, "timeline": timeline}


def state() -> dict:
    runs_dir = ROOT / "runs"
    runs = []
    for path in sorted(runs_dir.glob("*/state.json"), reverse=True):
        run = read_json(path)
        if run:
            runs.append(run_view(run, path.parent))
    # A station that failed before answering has no model id; borrow the one its alias resolved to elsewhere.
    alias = {h.get("model"): h["models"][0] for r in runs for h in r.get("history", []) if any(h.get("models") or [])}
    for r in runs:
        for h in r.get("history", []):
            known = [m for m in h.get("models") or [] if m]
            h["resolved_model"] = known[0] if known else alias.get(h.get("model"), h.get("model"))
    beat = read_json(runs_dir / ".factory.json", {})
    beat["alive"] = beat.get("state") in ("scanning", "working") and pid_alive(beat.get("pid"))
    log_file = ROOT / "logs" / "factory.log"
    try:
        log_tail = log_file.read_text(errors="replace").splitlines()[-120:]
    except OSError:
        log_tail = []
    return {"generated": datetime.now().isoformat(), "home": str(ROOT), "factory": beat,
            "service": service_status(), "agents": load_agents(), "queue": queue_view(),
            "runs": runs, "stats": aggregate(runs), "log": log_tail,
            "config": {"DF_MAX_ITERATIONS": os.environ.get("DF_MAX_ITERATIONS", "3"),
                       "DF_STATION_TIMEOUT": os.environ.get("DF_STATION_TIMEOUT", "3600")}}


def report(query: dict) -> str | None:
    run_id, name = query.get("run", [""])[0], query.get("name", [""])[0]
    if not re.fullmatch(r"[\w.-]+", run_id) or not re.fullmatch(r"[\w.-]+\.md", name):
        return None
    run_dir = ROOT / "runs" / run_id
    for path in (run_dir / "reports" / name, run_dir / "workspace" / "docs" / name, run_dir / name):
        if path.is_file():
            return path.read_text(errors="replace")
    return None


class Handler(BaseHTTPRequestHandler):
    def send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/":
            self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            self.send(200, json.dumps(state()).encode(), "application/json")
        elif url.path == "/api/report":
            text = report(parse_qs(url.query))
            if text is None:
                self.send(404, b"not found", "text/plain")
            else:
                self.send(200, text.encode(), "text/plain; charset=utf-8")
        else:
            self.send(404, b"not found", "text/plain")

    def log_message(self, *args) -> None:
        pass  # keep the terminal quiet; the page polls every 5 seconds


def main() -> None:
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("DF_DASHBOARD_PORT", 8787)))
    parser.add_argument("--home", help="factory folder to show (default: this repo)")
    args = parser.parse_args()
    if args.home:
        ROOT = Path(args.home).resolve()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Darkfactory dashboard: http://127.0.0.1:{args.port}  (showing {ROOT})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
