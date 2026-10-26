"""Usage ledger (ledger.py): what the account-wide totals (the dashboard's Usage page) are built from.

Each run keeps its real state in <project>/.katana/runs/<id>/state.json, which stays where it is. Whenever the
orchestrator saves a run it also writes a small copy to ~/.katana/projects/<key>/usage/<run id>.json: the run's
status and one entry per finished CLI call (agent, model, effort, tokens, cost, timing), without prompts, reports
or logs. The copy is overwritten each time, so writing it twice never counts anything twice, it outlives the
project folder, and it can always be rebuilt from state.json (`backfill`). It is never read by the orchestrator.
"""
from __future__ import annotations

import json
from pathlib import Path

import projects

CALL_KEYS = ("stage", "title", "agent", "visit", "started", "finished", "seconds", "outcome", "verdict", "model",
             "models", "effort", "tokens", "cost_usd")
DECISION_KEYS = ("at", "started", "seconds", "outcome", "chosen", "model", "models", "effort", "tokens", "cost_usd")
LIVE_KEYS = ("stage", "title", "agent", "visit", "started", "model", "effort", "log")
_written: dict = {}  # record path -> the text last written, to skip identical rewrites


def _pick(entry: dict, keys: tuple) -> dict:
    return {k: entry[k] for k in keys if entry.get(k) is not None}


def compact(run: dict, run_dir: Path) -> dict:
    """The ledger copy of a run. Gate steps make no CLI call, so they are left out. `run_dir` lets the dashboard
    read the live stream of a call that is still running."""
    current = run.get("current") or []
    current = [current] if isinstance(current, dict) else current
    return {"id": run["id"], "source": run.get("source") or "", "status": run.get("status"), "run_dir": str(run_dir),
            "history": [_pick(h, CALL_KEYS) for h in run.get("history") or [] if h.get("kind") != "gate"],
            "decisions": [_pick(d, DECISION_KEYS) for d in run.get("decisions") or []],
            "current": [_pick(c, LIVE_KEYS) for c in current if c.get("kind") != "gate"]}


def record(project, run: dict, run_dir: Path, force: bool = False) -> None:
    """Writes the run's ledger copy. The ledger is only a copy, so a failure here never stops a run."""
    try:
        path = projects.usage_dir(project) / f"{run['id']}.json"
        text = json.dumps(compact(run, run_dir), separators=(",", ":"))
        if not force and _written.get(path) == text:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(path)
        _written[path] = text
    except (OSError, KeyError, TypeError, ValueError):
        pass


def backfill(project) -> None:
    """Writes the ledger copy of every run whose state.json is newer than its copy (or has none): runs from before
    the ledger, and runs saved by an older Katana that is still running."""
    for state in projects.runs_dir(project).glob("*/state.json"):
        target = projects.usage_dir(project) / f"{state.parent.name}.json"
        try:
            if target.exists() and target.stat().st_mtime >= state.stat().st_mtime:
                continue
            run = json.loads(state.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(run, dict) and "pipeline" in run and "id" in run:  # skip runs from before plans existed
            record(project, run, state.parent, force=True)
