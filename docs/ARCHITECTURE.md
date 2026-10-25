# Katana — Architecture & Options

A "lights-out" SDLC factory: a work item goes in, a reviewed, tested pull request comes out.
Humans stay at the gates (spec approval, merge, release) until the metrics earn more autonomy.

---

## 1. Runtime options (the engine that runs the agents)

| | Option | Auth / cost | Strengths | Limits | Use for |
|---|---|---|---|---|---|
| **A** | **Claude Code CLI, headless** (`claude -p`, subagents, skills, hooks, git worktrees) | Your subscription login, flat cost | Zero infra, everything Claude Code already does (tools, MCP, permissions, sandbox), fast to start | 5-hour and weekly usage caps; one user's seat, not a shared service; scheduling is your own scripts | **Phase 0–1** |
| **B** | **Claude Agent SDK** (Python / TypeScript) behind your own orchestrator | API key (Console, Bedrock or Vertex), pay per token | Programmatic control, typed results, budgets per run, deploy as a service, scales horizontally | Token cost, API rate-limit tiers, you run the infra | **Phase 2+** |
| **C** | **Hosted runners** — Claude Code GitHub Action, Anthropic Managed Agents, Claude Code cloud sessions / routines | API key (Action, Managed Agents); subscription (cloud sessions) | Event-driven (issue → PR), no machines to run, sandboxed | Less control over the loop, per-platform limits | Triggers and overflow capacity |
| **D** | **Third-party frameworks** (LangGraph, CrewAI, OpenHands, AutoGen) | API key | Model-agnostic, graph tooling | You rebuild what Claude Code already has (tools, permissions, editing, context) | Only if multi-vendor is a hard requirement |

**Recommendation: A → B, with one seam.** Start on the CLI with your subscription, but put every agent
call behind a single `AgentRunner` interface. The first implementation shells out to
`claude -p --output-format stream-json`; the second uses the Agent SDK. Agent definitions
(`.claude/agents/*.md`, skills, hooks, `CLAUDE.md`) are plain files that both the CLI and the SDK load,
so nothing is rewritten when you switch.

**Auth notes (checked 2026-10-24 against Anthropic docs):**
- Headless on a subscription: run `claude setup-token` and set `CLAUDE_CODE_OAUTH_TOKEN` for scripts
  ([authentication](https://code.claude.com/docs/en/authentication)).
- If `ANTHROPIC_API_KEY` is set, `claude -p` uses it and bills the API account, so keep it out of the Phase 0 env.
- Pro and Max limits (a 5-hour window and a weekly cap) are shared between Claude and Claude Code. Published limits
  "assume ordinary, individual usage", so a 24/7 fleet belongs on an API key
  ([legal & compliance](https://code.claude.com/docs/en/legal-and-compliance)).
- Agent SDK: the docs say to use an API key. A support article from Oct 2026 says the SDK can also run on your own
  subscription limits for personal use
  ([article](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)).
  Anything you offer to other people must use API keys.
- Managed Agents need a Console API key. The GitHub Action accepts an API key or a subscription OAuth token,
  and an API key is recommended for org-wide secrets.

> Using company code? Run it on your company-approved Claude seat/API org, not a personal plan,
> and check the internal AI-usage policy before pointing the factory at production repos.

---

## 2. Reference architecture

```
             ┌──────────────── INTAKE ────────────────┐
             │ GitHub Issues · Jira · ServiceNow SPM   │
             └───────────────────┬────────────────────┘
                                 ▼
┌──────────────────── CONTROL PLANE (deterministic code, not an LLM) ────────────────────┐
│ Work-item state machine · queue · scheduler (concurrency, rate-limit & budget aware)    │
│ Policy: autonomy level per repo · gate rules · retries · escalation to a human          │
└───────┬─────────────────────────────────────────────────────────────────────┬───────────┘
        ▼                                                                     ▼
┌──────── STATIONS (agent roles, fresh context each) ────────┐   ┌──── QUALITY GATES ────┐
│ Triage → Spec → Plan → Implement → Test → Review →        │──▶│ build · test · lint ·  │
│ Security → PR → Release → Observe → Retro                 │◀──│ typecheck · coverage · │
└───────┬────────────────────────────────────────────────────┘   │ SAST · LLM judge ·     │
        ▼                                                        │ human gate             │
┌──────── EXECUTION PLANE ───────────────────────────────────┐   └────────────────────────┘
│ One job = one git worktree (later: one container)          │
│ Permission allowlist · OS sandbox · no prod secrets ·      │
│ egress allowlist · time & token budget per job             │
└───────┬────────────────────────────────────────────────────┘
        ▼
┌──────── KNOWLEDGE & OBSERVABILITY ─────────────────────────┐
│ CLAUDE.md · skills · ADRs · lessons learned                │
│ run logs (stream-json) · OpenTelemetry · cost/success KPIs │
└────────────────────────────────────────────────────────────┘
```

### Stations

| Station | Model tier | Output | Gate |
|---|---|---|---|
| Triage | Haiku | labels, size, duplicate check, "ready / needs info" | auto |
| Spec (PM) | Opus | acceptance criteria, non-goals | **human approve** (L1–L2) |
| Plan (architect) | Opus | task breakdown, files touched, test plan | auto; human on L1 |
| Implement | Sonnet (Opus for hard tasks) | commits on a branch, in a worktree | build + unit tests green |
| Test | Sonnet | new / updated tests, coverage delta | coverage threshold |
| Review | Opus, **fresh context, different prompt** | findings, must-fix list → loops back to Implement | no blockers |
| Security | Sonnet + SAST tools | findings | no high/critical |
| PR | Haiku | PR description, changelog | **human merge** (L1–L3) |
| Release / Observe | Sonnet | deploy, smoke checks, rollback | health checks |
| Retro (foreman) | Opus | edits to `CLAUDE.md` / skills from failed runs | human review of the edit |

Design rules:
- **The orchestrator is code, the stations are agents.** State, retries and gates never live in a prompt.
- **One station, one fresh context.** Pass artifacts (spec, plan, diff) between stations, not transcripts.
- **Reviewer ≠ author.** Separate session, separate instructions, ideally a stronger model.
- **Deterministic gates first, LLM judges second, humans last.**
- **Small work items.** Limit a job to ~1 PR / a few hundred lines; split anything bigger at Plan.

### Autonomy levels (per repo)

| Level | Humans approve | Promote when |
|---|---|---|
| L0 Assist | everything | — |
| L1 Supervised | spec, plan, merge | factory runs reliably |
| L2 Gated | spec, merge | ≥ 80 % of PRs merged without rework for 4 weeks |
| L3 Dark (low-risk) | merge only for risky paths; auto-merge docs/tests/deps | escaped-defect rate ≈ human baseline |
| L4 Dark | exceptions only | sustained L3 metrics + rollback proven |

---

## 3. Framework / stack

| Layer | Phase 0–1 (subscription) | Phase 2+ (scale) |
|---|---|---|
| Agent runtime | Claude Code CLI headless | Claude Agent SDK (Python) |
| Agent definitions | `.claude/agents`, skills, hooks, `CLAUDE.md` | same files |
| Orchestrator | Python script + SQLite | Python service + Postgres + a queue (Redis / SQS) |
| Intake & state | GitHub Issues + labels as the state machine | webhooks from GitHub / Jira / ServiceNow |
| Isolation | git worktrees + Claude Code sandbox | containers (devcontainer image), one per job |
| Tools | MCP servers (GitHub, issue tracker, docs) | same |
| Observability | `runs/<id>.jsonl`, a daily summary | OpenTelemetry → Grafana / Datadog |

---

## 4. Scaling strategy

**Phase 0 — Pilot (week 1–2).** One machine, one repo, 1–3 parallel worktrees, subscription.
Manual trigger (`factory run <issue>`). Human merges everything. Goal: an end-to-end PR on real tickets.

**Phase 1 — Pipeline (week 3–6).** Label-driven queue, scheduler that respects usage windows
(pauses on limit, resumes when the window resets), 3–5 concurrent jobs, model tiering, run metrics.
Goal: measure merge rate, rework loops, cost per PR, cycle time.

**Phase 2 — Service (month 2–3).** Switch `AgentRunner` to the Agent SDK + API key, containers,
a worker pool you can grow, budget per work item, webhook intake, several repos.

**Phase 3 — Fleet.** Kubernetes/ECS or Managed Agents for burst capacity, per-repo autonomy levels,
the Retro station improving prompts and skills from failures, scheduled maintenance work
(dependency bumps, flaky tests, docs) running overnight.

Scaling levers, in order of payoff:
1. **Smaller work items** — biggest effect on success rate.
2. **Model tiering** — Haiku for triage / summaries, Sonnet to build, Opus to plan and review.
3. **Context hygiene** — fresh context per station, tight `CLAUDE.md`, skills loaded on demand.
4. **Prompt caching** and Batch API for non-urgent bulk work (Phase 2).
5. **Concurrency** — limited by merge conflicts and CI time more than by agents; lock by module/path.

Expected bottlenecks: subscription caps (Phase 0–1), CI runtime, merge conflicts, human review bandwidth.

---

## 5. KPIs

Merge rate without human edits · review loops per PR · cycle time (issue → PR) · cost / tokens per PR ·
escaped defects (reverts, bugs traced to factory PRs) · human minutes per PR.

---

## 6. Open decisions

1. Runtime to start with (recommended: A, CLI on subscription).
2. Pilot repository and stack.
3. Intake system: GitHub Issues, Jira or ServiceNow SPM.
4. Orchestrator language (recommended: Python, to match the Agent SDK later).
