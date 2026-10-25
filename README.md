# Darkfactory

An SDLC factory of autonomous AI agents built on Claude: requirements in, tested and reviewed code out.
Phase 0 runs on your Mac with your Claude subscription only.

- Architecture, options and scaling plan: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## How it works

```
input/*.md ──(every 10 min, launchd)──▶ orchestrator ──▶ Analyst ─▶ Architect ─▶ Developer ─▶ Tester ─▶ Reviewer ─▶ done
                                                                                    ▲             │          │
                                                                                    └── FAIL ─────┘          │
                                                                                    └── CHANGES_REQUESTED ───┘  (max 3 iterations)
```

- [factory/orchestrator.py](factory/orchestrator.py) is a state machine. Each station is one headless
  `claude -p` call with a fresh context.
- [factory/agents/](factory/agents/) holds one prompt file per station, with model and allowed tools in its
  frontmatter. Edit these to tune the factory.
- Each requirement gets `runs/<id>/`, with `workspace/` (the generated project, its own git repo),
  `reports/`, `logs/`, `state.json` and `SUMMARY.md`.

**Subscription only:** before every scan the orchestrator checks `claude auth status` for a subscription
login. It also removes `ANTHROPIC_API_KEY` and other API or provider variables from the agents' environment,
so nothing is billed to an API account.

**Recovery:** if a usage limit is hit, the run pauses and resumes on the next scan. A station that errors is
retried on the next scan, up to 3 times. A crash in the middle of a run resumes from the last finished station.

**Guardrails:** agents run in `acceptEdits` mode inside their workspace. Shell commands are limited to each
agent's `tools` allowlist, and anything else is denied automatically.

## Use

```bash
python3 factory/orchestrator.py tick
```

runs one scan now.

```bash
python3 factory/orchestrator.py watch
```

scans every 10 minutes in the foreground.

```bash
python3 factory/orchestrator.py status
```

lists runs and where each one is.

```bash
scripts/install-service.sh
```

installs the background service: a launchd agent that scans every 10 minutes while you are logged in.
Its log is `logs/factory.log`, and `scripts/uninstall-service.sh` removes it.

## Dashboard

```bash
python3 factory/dashboard.py
```

Then open http://127.0.0.1:8787. It refreshes every 5 seconds and shows:
- **Factory status:** service, current scan, subscription and next-scan countdown.
- **Tokens:** totals by type (input, cache write, cache read, output) and per station run.
- **Live agents:** model, effort, elapsed time, turns, tokens and tool calls as they happen.
- **Agent roster.**
- **Runs:** each run's pipeline track, tasks done against tasks planned (from `docs/tasks.md`) and acceptance
  criteria passing (from the test report). Click a row for every station run's details and the reports.
- **Charts:** an activity timeline, plus tokens by model and by station.
- **Queue and log:** the models and effort used, the input queue, and the factory log.

The server listens on localhost only. The dollar figures are the API-equivalent list price; your subscription
is what is billed.

Settings (environment variables): `DF_INTERVAL` (600), `DF_MAX_ITERATIONS` (3), `DF_MAX_ATTEMPTS` (3),
`DF_STATION_TIMEOUT` (3600 s), `DF_CLAUDE_BIN` (path to the CLI).

The orchestrator finds the CLI on `PATH`, then in `~/.local/bin`, then uses the copy bundled with the
Claude desktop app. A standalone install is more stable for the service:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

The laptop must be awake for scans to run. Scans missed while it sleeps run once when it wakes.
