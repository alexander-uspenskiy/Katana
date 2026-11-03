# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## What this is

Katana is an SDLC "factory": a requirements `.md` file goes in, and a run of headless `claude -p` agents (BA → Architect → Security → Developer → Tester/Security/Reviewer → quality gate) produces a built, tested, reviewed project on a git branch. [README.md](README.md) is the full behaviour reference (triggers, plan/phases, gateways, artifacts, gates, git publishing, budgets); [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the design and scaling rationale (CLI runtime now, Agent SDK later behind one runner seam).

## Commands

Pure Python 3 standard library: no dependencies, no manifest, no build step, no linter config, and **no test suite for Katana itself** (the `tester` agent and `factory/tools/unittest_junit.py` exist to test the *generated* projects).

```bash
python3 factory/dashboard.py [--port 8787] [--home <project dir>]   # dashboard at http://127.0.0.1:8787 (also .claude/launch.json "dashboard")
python3 factory/orchestrator.py run --project <dir>                 # process a project's input/ now, then exit
python3 factory/orchestrator.py status --project <dir>              # list runs
python3 factory/orchestrator.py tick                                # one scan of all folder-* projects
python3 factory/orchestrator.py watch                               # foreground scan loop
scripts/install-service.sh                                          # launchd agent (macOS); uninstall-service.sh removes it
python3 factory/service.py install|uninstall|start|stop|status      # the same service, on macOS or Windows
```

Windows (PowerShell), same commands with `python` and the `.ps1` scripts (also .claude/launch.json "dashboard-windows"):

```powershell
python factory\dashboard.py [--port 8787] [--home <project dir>]
python factory\orchestrator.py run --project <dir>
scripts\install-service.ps1                                         # Task Scheduler task "Katana"; uninstall-service.ps1 removes it
```

Useful env vars: `KATANA_HOME` (pin one project folder for testing), `KATANA_USER_DIR` (move `~/.katana`), `KATANA_CLAUDE_BIN`, `KATANA_MAX_ATTEMPTS`, `KATANA_DISPATCH_INTERVAL`, `KATANA_BUDGET_POLL`.

**Platform:** the code runs natively on macOS and Windows; Linux is best effort (no background service there). Platform differences live only in [factory/plat.py](factory/plat.py) and [factory/service.py](factory/service.py). New platform-specific code goes there. Never call `fcntl`, `msvcrt`, `os.kill(pid, 0)` (on Windows it kills the process), `osascript` or `security` directly from any other module.

## Architecture

All code is in `factory/`; modules import each other flat (`import settings`, `import gates`), so scripts are run by path from the repo root, not as a package.

- **[orchestrator.py](factory/orchestrator.py)** is the core state machine for both modes (deterministic and full auto). It runs one `claude -p` call per stage, runs the stages of a phase in threads (`max_parallel`), enforces per-project locking, retries, rework loops, usage-limit pause/resume, and token caps. Run state is persisted in `<project>/.katana/runs/<id>/state.json` and resumed from the last finished stage after a crash.
- **Plan model** (defined in [config/defaults.json](config/defaults.json), validated in [settings.py](factory/settings.py)): `pipeline` is a list of phases, each a list of stages. A stage reads/writes named **artifacts**; a stage with `pass` verdicts or `kind: "gate"` is a gateway with `on_fail` pointing back to an earlier stage. The plan and mode are frozen into a run at start. Old flat plans must keep loading (each stage becomes its own phase), and `settings.validate()` rejects a phase with more than one code-writing stage or two stages writing the same artifact.
- **[gates.py](factory/gates.py)**: gate steps are measured by Katana itself (no LLM): test pass rate (JUnit XML), coverage, security findings, acceptance pass rate, must-fix count, run tokens. A failing gate writes `docs/gate-report.md` and sends work back to its `on_fail` stage.
- **[agents/*.md](factory/agents)**: one prompt per role, with frontmatter (`model`, `effort`, `writes_code`, `tools`) that drives parallelism rules and the tool allowlist. User edits live in `~/.katana/agents/<name>.md` and override these. Allowlists are safety-checked (no bare `Bash`, shells, `curl`, `rm`, `sudo`, …).
- **[triggers/](factory/triggers)**: requirement sources behind `fetch / claim / ack / report` ([base.py](factory/triggers/base.py)). `FolderTrigger` is the only one; new connectors register in `CONNECTORS` in `triggers/__init__.py` and add fields to `settings.validate()`.
- **[gitrepo.py](factory/gitrepo.py)**: the workspace is a local clone with no `origin` on branch `katana/<run-id>` so agents cannot touch the user's repo; Katana alone fetches and publishes (pr/push/merge/local).
- **[dispatch.py](factory/dispatch.py)** (due projects, background workers, launchd plist), **[service.py](factory/service.py)** (the background service: launchd agent on macOS, Task Scheduler task `Katana` on Windows; the dashboard's Service controls go through it), **[plat.py](factory/plat.py)** (every platform difference: file locks, API key store, desktop notifications, folder dialog, process checks, detached workers, UTF-8 restart on Windows), **[projects.py](factory/projects.py)** (registry in `~/.katana/projects.json`), **[auth.py](factory/auth.py)** (subscription vs API key; keys in the system secret store via `plat.py`, API env vars stripped from agents in subscription mode; on Windows it prefers `claude.exe`), **[budget.py](factory/budget.py)**, **[ledger.py](factory/ledger.py)** (per-run usage record under `~/.katana/projects/<key>/usage/`), **[notify.py](factory/notify.py)**.
- **[dashboard.py](factory/dashboard.py)** is a localhost-only `http.server` JSON API (state-changing calls need a JSON POST with a custom header) serving the single-page UI in [factory/dashboard/](factory/dashboard) (`index.html`, plus the plan graph editor `graph.js`/`graph.css`). There is no frontend build; edit the files directly.

State locations: project-level in `<project>/katana.config.json` and `<project>/.katana/` (gitignored); user-level in `~/.katana/`. The old `config/*.json` and `.katana` layouts are auto-migrated on first start, so keep those migrations working when touching `projects.py`/`settings.py`.

## Conventions

- Commit style is a plain imperative one-liner describing the feature, no prefixes (see `git log`).
- Update the relevant README.md section when behaviour or settings change; it doubles as the spec.
