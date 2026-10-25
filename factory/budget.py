"""Token budgets: a cap per run and a daily cap across all runs.

Shared by the orchestrator (enforces them) and the dashboard (shows how much is used). Tokens are counted the
same way as the dashboard's "Spent tokens": input, cache write, cache read and output together.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

TOKEN_KEYS = ("input", "cache_write", "cache_read", "output")


def total(tokens: dict | None) -> int:
    return sum((tokens or {}).get(k) or 0 for k in TOKEN_KEYS)


def calls(run: dict) -> list:
    """Every finished CLI call of a run: stages and orchestrator decisions."""
    return run.get("history", []) + run.get("decisions", [])


def run_tokens(run: dict) -> int:
    return sum(total(c.get("tokens")) for c in calls(run))


def day_tokens(runs: list, day: str | None = None) -> int:
    """Tokens of calls that finished on a local calendar day (today by default)."""
    day = day or date.today().isoformat()
    return sum(total(c.get("tokens")) for r in runs for c in calls(r)
               if str(c.get("finished") or c.get("at") or "")[:10] == day)


def caps(conf: dict) -> tuple:
    """(run cap, daily cap); 0 means no cap."""
    b = conf.get("budgets") or {}
    return int(b.get("run_tokens") or 0), int(b.get("daily_tokens") or 0)


def breach(conf: dict, run_used: int, day_used: int) -> str | None:
    """'run' or 'day' when a cap is reached, else None."""
    run_cap, day_cap = caps(conf)
    if run_cap and run_used >= run_cap:
        return "run"
    if day_cap and day_used >= day_cap:
        return "day"
    return None


def next_reset() -> datetime:
    """When the daily cap resets: the next local midnight."""
    return datetime.combine(date.today() + timedelta(days=1), datetime.min.time())
