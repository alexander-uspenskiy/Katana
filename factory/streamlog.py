"""Parses a `claude -p --output-format stream-json` log into a summary.

Used by the orchestrator when a station finishes and by the dashboard while a station is still running.
"""
from __future__ import annotations

import json
from collections import Counter, deque
from pathlib import Path

TOKEN_KEYS = (("input", "input_tokens"), ("cache_write", "cache_creation_input_tokens"),
              ("cache_read", "cache_read_input_tokens"), ("output", "output_tokens"))


def tokens_from_usage(usage: dict) -> dict:
    return {ours: usage.get(theirs) or 0 for ours, theirs in TOKEN_KEYS}


def _detail(block: dict) -> str:
    args = block.get("input") or {}
    for key in ("command", "file_path", "pattern", "path", "url", "query", "description"):
        if args.get(key):
            return str(args[key])[:160]
    return ""


def summarize(path: Path, max_events: int = 15) -> dict:
    tokens = {k: 0 for k, _ in TOKEN_KEYS}
    tools, seen = Counter(), set()
    events = deque(maxlen=max_events)
    summary = {"model": None, "messages": 0, "tokens": tokens, "tools": tools,
               "events": events, "result": None, "rate_limit": None}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partial line while the station is still writing
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            summary["model"] = event.get("model")
        elif kind == "assistant":
            message = event.get("message") or {}
            if message.get("id") not in seen:
                seen.add(message.get("id"))
                summary["messages"] += 1
                usage = message.get("usage") or {}
                for ours, theirs in TOKEN_KEYS:
                    tokens[ours] += usage.get(theirs) or 0
            for block in message.get("content") or []:
                if block.get("type") == "tool_use":
                    tools[block.get("name", "?")] += 1
                    events.append({"kind": "tool", "name": block.get("name"),
                                   "detail": _detail(block)})
                elif block.get("type") == "text" and block.get("text", "").strip():
                    events.append({"kind": "text", "detail": block["text"].strip()[:240]})
        elif kind == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" \
                        and block.get("is_error"):
                    events.append({"kind": "error", "detail": str(block.get("content"))[:240]})
        elif kind == "rate_limit_event":
            summary["rate_limit"] = event.get("rate_limit_info") or {}
        elif kind == "result":
            summary["result"] = event
    result = summary["result"]
    if result and result.get("usage"):  # the final totals are authoritative
        for ours, theirs in TOKEN_KEYS:
            tokens[ours] = result["usage"].get(theirs) or 0
    summary["tools"] = dict(tools)
    summary["events"] = list(events)
    return summary
