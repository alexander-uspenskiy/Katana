# Cortana

An SDLC factory of autonomous AI agents built on Claude: requirements in, a designed, built, tested, reviewed
and security-audited project out. Phase 0 runs on your Mac with your Claude subscription only.

- Architecture, options and scaling plan: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## How it works

Cortana works on **projects**. A project is a folder holding `input/` (requirement files), `runs/`, `logs/`
and `.cortana/settings.json` (that project's settings). Each requirement `.md` file in `input/` becomes a
run that follows the project's **plan**. What starts a run is the project's **trigger**:

| Trigger | What starts a run |
|---|---|
| `folder-interval` | The service scans `input/` every N minutes (10 by default, set in Settings). |
| `folder-watch` | launchd starts a scan as soon as `input/` changes, through `WatchPaths`. The service still rescans every N minutes as a fallback, which also resumes runs paused by a usage limit. |
| `once` | No service. **Run now** in the dashboard, or `orchestrator.py run`, processes what is in `input/` and stops. A requirement submitted in the dashboard starts straight away. |
| `manual` | Nothing starts by itself, not even a submitted requirement. Press **Run now**, or run the CLI. |

The service runs every registered project whose trigger is `folder-interval` or `folder-watch`, each with
its own settings. A scheduled scan leaves a file alone until it has been unchanged for 60 seconds (5 seconds
for `folder-watch`); **Run now** and `run` take files as they are. Files starting with `.` or `_` are drafts
and are never picked up.

The default plan:

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
| [factory/triggers/](factory/triggers/) | Requirement sources: the connector interface and `FolderTrigger` |
| [factory/projects.py](factory/projects.py) | The project registry |
| [factory/dispatch.py](factory/dispatch.py) | Which projects are due, the background workers that scan them, and the launchd plist |
| [config/defaults.json](config/defaults.json) | Default trigger, mode, limits, agent models and effort, and plan |
| `config/projects.json` | The registered projects and the active one (not in git) |
| `<project>/.cortana/settings.json` | That project's changes to the defaults, written by the Settings tab |
| `<project>/runs/<id>/` | Per run: `workspace/` (the generated project, its own git repo), `reports/`, `logs/`, `state.json`, `SUMMARY.md` |

**Upgrading:** settings used to live in `config/settings.json`. On the first start, Cortana moves that file
to `<project>/.cortana/settings.json` for the project folder it named (or this repository), registers that
project, and renames the old file to `settings.json.migrated`.

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

Then open http://127.0.0.1:8787. With no project selected it opens on the **start screen**: your recent
projects, a path field with **Browse…** (the native macOS folder dialog), **Open project**, and
**Create new project**, which makes the folder with `input/`, `runs/`, `logs/` and `.cortana/` inside.
`--home DIR` opens a folder directly.

Once a project is open, the project name in the top bar switches between recent projects, and every tab
follows the project you pick. The top bar also has:
- **+ Requirement:** paste a title and markdown, and it is saved as `input/<title>.md`.
- **Run now:** starts a scan of this project in the background. The project's lock prevents a second scan
  while one is running.

The project's tabs refresh every 5 seconds:
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
  - The project name and folder, and **Switch project**.
  - The **trigger**, with the scan interval for `folder-interval` and the fallback interval for
    `folder-watch`.
  - The SDLC mode.
  - Limits on rework loops, stages per run and stage time.
  - The model and effort for each agent.
  - A plan editor: add, remove and reorder stages, and change each stage's agent, task, output and gate.

The server listens on localhost only. Settings changes need a JSON request with a custom header, so other
web pages can't make them. The dollar figures are the API-equivalent list price; your subscription is what
is billed.

## Service

```bash
python3 factory/orchestrator.py run --project ~/Projects/my-app
```

processes that project's current input now and exits. Without `--project` it uses the active project.
This works for every trigger and is how `once` and `manual` projects run from a terminal or a script.

```bash
python3 factory/orchestrator.py tick
```

scans every `folder-interval` and `folder-watch` project now, one after another.

```bash
python3 factory/orchestrator.py watch
```

does the same in the foreground, scanning each project when it is due.

```bash
python3 factory/orchestrator.py status --project ~/Projects/my-app
```

lists a project's runs and where each one is.

```bash
scripts/install-service.sh
```

installs the background service, a launchd agent that runs while you are logged in. It generates the plist
from `config/projects.json`:
- Every minute, launchd runs `orchestrator.py dispatch`, which starts a background worker for each project
  that is due. A long run in one project never holds up another.
- `WatchPaths` lists the `input/` folder of every `folder-watch` project, so a new file starts a scan
  straight away.

Run the script again after adding a `folder-watch` project. When the service is installed, the dashboard
does this for you whenever the watched folders change. `scripts/uninstall-service.sh` removes the service.

Environment variables: `CORTANA_DISPATCH_INTERVAL` (60 s), `CORTANA_MAX_ATTEMPTS` (3), `CORTANA_CLAUDE_BIN`
(path to the CLI), and `CORTANA_HOME`, which pins the dashboard and the orchestrator to one project folder
for testing.

The orchestrator finds the CLI on `PATH`, then in `~/.local/bin`, then uses the copy bundled with the
Claude desktop app. A standalone install is more stable for the service:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

The laptop must be awake for scans to run. Scans missed while it sleeps run once when it wakes.

## Connectors

A trigger's connector is where requirements come from and where results go back to. The orchestrator
talks to a source only through four calls on a `Trigger` subclass, defined in
[factory/triggers/base.py](factory/triggers/base.py):

| Call | What it does |
|---|---|
| `fetch()` | Lists the items waiting, without taking them. |
| `claim(item, run_dir)` | Takes one item so no other scan gets it, and writes `requirements.md`. |
| `ack(item, run)` | Confirms to the source that the run has started. |
| `report(run, summary)` | Sends the outcome back once the run is done, failed or blocked. |

Each run records where it came from in `state.json` as `origin`, so `report()` still works after a
restart. `FolderTrigger` is the only connector today: it claims by moving the file out of `input/`, and
its report is the run's `SUMMARY.md`.

Planned connectors would plug in like this; they are not built yet:
- **Jira:** `fetch()` polls a JQL query such as `status = "Ready for Cortana"`. `claim()` transitions the
  issue to *In Progress*, and only a successful transition counts as a claim. `ack()` comments the run
  id. `report()` comments the summary and transitions the issue to *Done*, or to a status such as
  *Needs input* for a failed or blocked run.
- **Confluence:** `fetch()` runs a CQL search for pages labelled `cortana-ready`. `claim()` swaps the label
  for `cortana-claimed` and exports the page as markdown. `report()` adds a page comment and labels the
  page `cortana-done` or `cortana-failed`.

A new connector registers its trigger type in `CONNECTORS` in
[factory/triggers/__init__.py](factory/triggers/__init__.py) and adds its fields to `settings.validate()`.
Credentials belong in the macOS keychain, not in a settings file.
