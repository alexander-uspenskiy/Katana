# Cortana

An SDLC factory of autonomous AI agents built on Claude: requirements in, a designed, built, tested, reviewed
and security-audited project out. Phase 0 runs on your Mac with your Claude subscription only.

- Architecture, options and scaling plan: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## How it works

Every 10 minutes, launchd runs the orchestrator, which looks for requirement `.md` files in the project
folder's `input/`. Each file becomes a run that follows the **plan**. The default plan:

```
1 Write TDD (BA) → 2 Refine TDD (Architect) → 3 Validate TDD security (Security) ⟲2
  → 4 Build (Developer) → 5 Test (Tester) ⟲4 → 6 Code review (Reviewer) ⟲4 → 7 Security audit (Security) ⟲4 → done
```

`⟲n` marks a gate: if the verdict doesn't pass, the work goes back to stage n, up to the rework limit.

There are two SDLC modes, chosen in Settings:
- **Deterministic:** the classic pipeline. Stages run in plan order, and failed gates loop back.
- **Full auto:** before every stage, the Orchestrator agent reads the history and the documents and
  chooses the next stage, or `done`. Cortana refuses `done` until every required gate has passed after the
  latest build.

The plan and mode are frozen when a run starts, so editing settings never breaks a run in flight.

| Path | What it is |
|---|---|
| [factory/orchestrator.py](factory/orchestrator.py) | State machine for both modes; one headless `claude -p` call per stage |
| [factory/agents/](factory/agents/) | One role prompt per agent: BA, Architect, Security, Developer, Tester, Reviewer, Orchestrator |
| [config/defaults.json](config/defaults.json) | Default project folder, mode, limits, agent models and effort, and plan |
| `config/settings.json` | Your changes, written by the dashboard's Settings tab (not in git) |
| `<project folder>/runs/<id>/` | Per run: `workspace/` (the generated project, its own git repo), `reports/`, `logs/`, `state.json`, `SUMMARY.md` |

**Subscription only:** before every scan the orchestrator checks `claude auth status` for a subscription
login. It also removes `ANTHROPIC_API_KEY` and other API or provider variables from the agents' environment.

**Recovery:**
- If a usage limit is hit, the run pauses and resumes on the next scan.
- A stage that errors is retried on the next scan, up to 3 times.
- After a crash, a run resumes from the last finished stage.

**Guardrails:** agents run in `acceptEdits` mode inside their workspace. Shell commands are limited to each
agent's tool allowlist, and anything else is denied automatically.

## Dashboard

```bash
python3 factory/dashboard.py
```

Then open http://127.0.0.1:8787. It refreshes every 5 seconds and has three tabs:
- **Overview:**
  - Factory status and key figures.
  - **Spent tokens** over time, filterable by model, effort, project and time range, and stacked by token
    type or by model.
  - Live agents, the agent roster and the runs table, with a pipeline track per run.
  - The activity timeline, tokens by model and by agent, models and effort, the queue and the log.
- **Plan:** the stage sequence as a diagram. Each stage shows its agent, model, effort, task, output and
  gate, including where a failure loops back to. Pick a run to see its live progress, and in full-auto
  mode the path the Orchestrator took.
- **Settings:**
  - The project folder and the SDLC mode.
  - Limits on rework loops, stages per run and stage time.
  - The model and effort for each agent.
  - A plan editor: add, remove and reorder stages, and change each stage's agent, task, output and gate.

The server listens on localhost only. Settings changes need a JSON request with a custom header, so other
web pages can't make them. The dollar figures are the API-equivalent list price; your subscription is what
is billed.

## Service

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
`scripts/uninstall-service.sh` removes it.

Environment variables: `CORTANA_INTERVAL` (600 s), `CORTANA_MAX_ATTEMPTS` (3), `CORTANA_CLAUDE_BIN`
(path to the CLI), and `CORTANA_HOME`, which overrides the project folder for testing.

The orchestrator finds the CLI on `PATH`, then in `~/.local/bin`, then uses the copy bundled with the
Claude desktop app. A standalone install is more stable for the service:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

The laptop must be awake for scans to run. Scans missed while it sleeps run once when it wakes.
