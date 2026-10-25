# Cortana

An SDLC factory of autonomous AI agents built on Claude: requirements in, a designed, built, tested, reviewed
and security-audited project out. Phase 0 runs on your Mac with your Claude subscription only.

- Architecture, options and scaling plan: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## How it works

Every 10 minutes, launchd runs the orchestrator, which looks for requirement `.md` files in the project
folder's `input/`. Each file becomes a run that follows the **plan**: a list of **phases**, each holding one
or more stages that run at the same time. The default plan:

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
plans, in `config/settings.json` or in runs started before phases existed, still load: each stage becomes
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
| [config/defaults.json](config/defaults.json) | Default project folder, mode, limits, parallelism, gate commands, agent models and effort, and plan |
| `config/settings.json` | Your changes, written by the dashboard's Settings tab (not in git) |
| `<project folder>/runs/<id>/` | Per run: `workspace/` (the generated project, its own git repo), `reports/`, `logs/`, `gates/`, `state.json`, `SUMMARY.md` |

**Subscription only:** before every scan the orchestrator checks `claude auth status` for a subscription
login. It also removes `ANTHROPIC_API_KEY` and other API or provider variables from the agents' environment.

**Recovery:**
- If a usage limit is hit, the run pauses and resumes on the next scan.
- A stage that errors is retried on the next scan, up to 3 times.
- After a crash, a run resumes from the last finished stage; in a parallel phase, only the unfinished stages rerun.

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
  - Live agents (one card per stage running now, including a gate being measured), the agent roster and the
    runs table, with a pipeline track per run: parallel stages share a dashed group, gates are diamonds.
  - The activity timeline, with a sub-lane per concurrent stage, tokens by model and by agent, models and
    effort, the queue and the log.
  - Run details include a **Gates** tab: every gate run with each condition's measured value and result.
- **Plan:** a swim lane, one column per phase with its stages stacked, and a diamond for each gate step
  listing its conditions with the latest measured values. Pick a run to see its live progress, and in
  full-auto mode the path the Orchestrator took. Below, every stage's agent, model, effort, task, output and
  gate, grouped by phase.
- **Settings:**
  - The project folder and the SDLC mode.
  - Limits on rework loops, stages per run, stage time and stages at once.
  - Gate settings: test and coverage commands, what a missing tool means, where security findings come from,
    and per-stage worktrees.
  - The model and effort for each agent.
  - A plan editor: add phases, add a parallel stage to a phase, move a stage to another phase, reorder
    phases and stages, change each stage's agent, task, output and verdict gate, and edit a quality gate's
    conditions.

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
