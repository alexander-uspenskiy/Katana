# Cortana

An SDLC factory of autonomous AI agents built on Claude: requirements in, a designed, built, tested, reviewed
and security-audited project out. Phase 0 runs on your Mac with your Claude subscription only.

- Architecture, options and scaling plan: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## How it works

Cortana works on **projects**. A project is a folder holding `input/` (requirement files), `runs/`, `logs/`
and `.cortana/settings.json` (that project's settings). Each requirement `.md` file in `input/` becomes a
run that follows the project's **plan**: a list of **phases**, each holding one or more stages
that run at the same time. What starts a run is the project's **trigger**:

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
1 Write TDD (BA) → 2 Refine TDD (Architect) → 3 Validate TDD security (Security) ⟲2 → 4 Build (Developer)
  → 5 { Test (Tester) ∥ Security audit (Security) ∥ Code review (Reviewer) }
  → 6 ◆ Quality gate ⟲4 → done
```

`⟲n` marks a gate: if it doesn't pass, the work goes back to phase n and runs on from there, up to the rework
limit. The next phase starts only when every stage of the current one has finished.

There are two SDLC modes, chosen in Settings:
- **Deterministic:** the classic pipeline. Phases run in plan order, and failed gates loop back.
- **Full auto:** before every step, the Orchestrator agent reads the history and the documents and
  chooses the next stage or stages (several at once, if at most one of them changes code), or `done`.
  Cortana refuses `done` until every required gate, including gate steps, has passed after the latest build.

The plan and mode are frozen when a run starts, so editing settings never breaks a run in flight. Old flat
plans, in older project settings or in runs started before phases existed, still load: each stage becomes
its own phase.

### Parallel stages

- Stages of one phase run in threads, each as its own `claude -p` call, at most **Max stages at once**
  (`max_parallel`, default 2) at a time. Every concurrent stage draws on the same subscription usage window,
  so raise it with care.
- Agents are marked in their frontmatter: `writes_code: yes` for the Developer and the Tester, `no` for the
  others, which only write their own report. A phase may hold at most one code-writing stage, and its
  stages need different output documents. Settings refuses a plan that breaks either rule.
- **Worktrees (off by default):** report-only stages normally read the shared workspace while the phase's
  code-writing stage changes it. That is fine for reviews of a finished build, and it keeps one copy of the
  code. With `worktrees` on, each report-only stage in a parallel phase gets a `git worktree` snapshot of the
  workspace (HEAD plus uncommitted changes) taken when the phase starts, and Cortana copies its report back.
  Turn it on if reviewers get confused by tests appearing mid-review.
- If one stage hits a usage limit or errors, the others finish, and the next scan reruns only the stages of
  the phase that did not finish.

### Quality gates

A **gate step** (`"kind": "gate"`) is a plan step that Cortana measures itself, with no LLM call and no
tokens. Each condition has a metric, an operator, a threshold and a severity (`block` or `warn`):

| Metric | Measured from |
|---|---|
| `test_pass_rate` | Runs the test command and parses the JUnit XML it writes |
| `coverage` | coverage.py JSON (or an istanbul summary or lcov) from the coverage command |
| `security_findings` | Findings at or above a severity: bandit, semgrep or pip-audit when installed, otherwise the Security Engineer's findings table |
| `acceptance_pass` | The share of acceptance criteria the Tester's report marks PASS |
| `review_must_fix` | The Code Reviewer's `MUST_FIX: n` line (or the numbered must-fix items) |
| `run_tokens` | Tokens the run has spent so far |

- The default quality gate blocks unless the test pass rate is 100%, coverage is at least 80%, there are no
  high or critical security findings, and the review has no must-fix items.
- **Test and coverage commands** come from Settings. Empty means detect: Python projects use
  `pytest --junitxml` when pytest is installed, else Cortana's stdlib runner
  [factory/tools/unittest_junit.py](factory/tools/unittest_junit.py), and run under coverage.py when it is
  installed. Node projects use `node --test` with the JUnit and lcov reporters. Commands get `{junit}` and
  `{coverage}` placeholders for the files to write.
- A metric that can't be measured, such as coverage without coverage.py, is **unavailable**. The
  **When a gate tool is missing** setting decides whether that warns (default), blocks or is ignored. A
  report that predates the latest build counts as a failure, so a gate never passes on stale results.
- On failure, the gate writes `docs/gate-report.md`, every finding merged (failing tests, least-covered
  files, security findings, must-fix items), and sends the work to its `on fail` stage (default: the nearest
  earlier build stage). That stage is told to fix every finding in the report.
- Measurements are kept per gate run in `runs/<id>/gates/<gate>-run<n>/` (`gate.log`, `junit.xml`,
  `coverage.json`). Test commands run the generated project's code outside the CLI sandbox, with the same
  stripped environment as the agents.

| Path | What it is |
|---|---|
| [factory/orchestrator.py](factory/orchestrator.py) | State machine for both modes; one headless `claude -p` call per stage, phases in parallel threads |
| [factory/gates.py](factory/gates.py) | Gate steps: measures each condition and writes the gate report |
| [factory/agents/](factory/agents/) | One role prompt per agent: BA, Architect, Security, Developer, Tester, Reviewer, Orchestrator |
| [factory/triggers/](factory/triggers/) | Requirement sources: the connector interface and `FolderTrigger` |
| [factory/projects.py](factory/projects.py) | The project registry |
| [factory/dispatch.py](factory/dispatch.py) | Which projects are due, the background workers that scan them, and the launchd plist |
| [factory/budget.py](factory/budget.py) | Token counting for the per-run and daily caps |
| [factory/notify.py](factory/notify.py) | macOS and Teams notifications |
| [config/defaults.json](config/defaults.json) | Default trigger, mode, limits, parallelism, gate commands, agents, budgets, notifications and plan |
| `config/projects.json` | The registered projects and the active one (not in git) |
| `<project>/.cortana/settings.json` | That project's changes to the defaults, written by the setup wizard and Settings tab |
| `config/auth.json` | How agents sign in: `subscription` or `api_key` (not in git). The keys themselves are in the keychain |
| `config/agents/<name>.md` | Your edited agent prompts, written by Settings → Agents → Prompt (not in git). Shared by all projects |
| `<project>/runs/<id>/` | Per run: `workspace/` (the generated project, its own git repo), `reports/`, `logs/`, `gates/`, `state.json`, `SUMMARY.md` |

**Upgrading:** settings used to live in `config/settings.json`. On the first start, Cortana moves that file
to `<project>/.cortana/settings.json` for the project folder it named (or this repository), registers that
project, and renames the old file to `settings.json.migrated`.

**Authentication:** Settings → Authentication shows whether the agents can sign in, and how. By default they
use your Claude subscription: before every scan the orchestrator checks `claude auth status` for a subscription
login, and it removes `ANTHROPIC_API_KEY` and other API or provider variables from the agents' environment.
Switch to **Anthropic API key** there and the agents get the saved key instead, billed per token. API keys
(Anthropic, OpenAI, Google Gemini) are saved in your macOS login keychain under the service `cortana`, never in a
file; the dashboard shows only their last four characters. Only the Anthropic key reaches agents, and only in
API key mode. The choice is shared by all projects and saved in `config/auth.json`.

**Recovery:**
- If a usage limit is hit, the run pauses and resumes on the next scan.
- A stage that errors is retried on the next scan, up to 3 times.
- After a crash, a run resumes from the last finished stage; in a parallel phase, only the unfinished stages rerun.

**Guardrails:** agents run in `acceptEdits` mode inside their workspace. Shell commands are limited to each
agent's tool allowlist, and anything else is denied automatically. An allowlist you edit in Settings is
checked against a safe pattern: built-in tools such as `Read` or `Edit`, or `Bash(command *)` for one named
command. Bare `Bash`, `Bash(*)`, shells, and network or system commands such as `curl`, `ssh`, `rm` and
`sudo` are refused.

**Agents:** each agent can be switched off in Settings.
- Deterministic mode skips a disabled agent's stages.
- Full auto never offers them to the Orchestrator, and their required gates are waived.
- Settings warns when a gate depends on a disabled agent. You can't disable the Orchestrator in full auto,
  or every agent in the plan.
- Three presets set every agent's model and effort in one click, and you can adjust each agent afterwards:
  - **Fast:** Sonnet and Haiku at low effort.
  - **Balanced:** the shipped defaults.
  - **Max quality:** Opus at high or xhigh effort.

**Budgets** (off by default) count every token type, as the Overview's Spent tokens does:
- **Token cap per run:** a run that reaches it fails, with the reason in its details.
- **Daily token cap:** when all runs together reach it, the factory pauses until midnight. Raising the cap
  lifts the pause on the next scan.
- A stage that crosses either cap is stopped within a few seconds, rather than when it finishes.
- **Pause when usage is near the limit:** when the CLI warns that the subscription window is nearly used up,
  the run pauses after the current stage and resumes after the window resets.

**Notifications:** a macOS notification when a run is done, failed, blocked or paused, with a toggle for
each event. If you add a Teams incoming-webhook URL, which must be an https URL on `webhook.office.com`,
`logic.azure.com` or `powerplatform.com`, Cortana also posts a card there. Nothing is sent to Teams
without one.

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

When a project is opened for the first time, a **setup wizard** walks you through its settings: Mode,
Agents preset, Budgets, Service and Done. Nothing is saved until you press Finish, and Skip setup keeps
the defaults. Run it again from Settings → Run setup again.

The project's tabs refresh every 5 seconds:
- **Overview:**
  - Factory status and key figures, including today's token use against the daily cap.
  - **Spent tokens** over time, filterable by model, effort, project and time range, and stacked by token
    type or by model.
  - Live agents (one card per stage running now, including a gate being measured), the agent roster and the
    runs table, with a pipeline track per run: parallel stages share a dashed group, gates are diamonds.
  - The activity timeline, with a sub-lane per concurrent stage, tokens by model and by agent, models and
    effort, the queue and the log.
  - Run details include a **Gates** tab: every gate run with each condition's measured value and result.
- **Plan:** a swim lane, one column per phase with its stages stacked, and a diamond for each gate step
  listing its conditions with the latest measured values. Pick a run to see its live progress, and in
  full-auto mode the path the Orchestrator took. Below, every stage's agent, model, effort, task, output and
  gate, grouped by phase.
- **Settings:** grouped sections with a side navigation (a scrolling row on phones), and one save bar for
  all of them:
  - **Project:** the project name and folder, **Switch project**, the **trigger** (with the scan interval
    for `folder-interval` and the fallback interval for `folder-watch`), the SDLC mode, and the limits on
    rework loops, stages per run, stage time and stages at once.
  - **Agents & plan:**
    - Presets, and for each agent its model, effort, an on/off switch and its tool allowlist.
    - Each agent's prompt can be viewed and edited. A saved prompt is written to `config/agents/<name>.md`
      and takes effect immediately; **Reset to default** deletes it.
    - Gate settings: test and coverage commands, what a missing tool means, where security findings come
      from, and per-stage worktrees.
    - The plan editor: add phases, add a parallel stage to a phase, move a stage to another phase, reorder
      phases and stages, change each stage's agent, task, output and verdict gate, and edit a quality gate's
      conditions.
  - **Limits & alerts:** budgets and notifications, with a test button.
  - **System:**
    - Whether the service is installed and running, with Install, Start, Stop and Uninstall buttons. These
      act immediately.
    - The last 20 lines of `logs/launchd.log`.

The server listens on localhost only. Every change, including settings, prompts, service actions and test
notifications, needs a JSON POST with a custom header, so other web pages can't make them. The dollar figures are the API-equivalent list price; your subscription is what
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
Settings → Service runs the same scripts, and starts or stops the agent with `launchctl`.

Environment variables: `CORTANA_DISPATCH_INTERVAL` (60 s), `CORTANA_MAX_ATTEMPTS` (3), `CORTANA_BUDGET_POLL`
(how often a running stage is checked against the token caps, 5 s), `CORTANA_CLAUDE_BIN` (path to the CLI),
and `CORTANA_HOME`, which pins the dashboard and the orchestrator to one project folder
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
