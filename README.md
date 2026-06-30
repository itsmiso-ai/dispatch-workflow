# Dispatch Workflow Scripts

Dispatch workflow scripts for the Saffron agent workspace.

**Owner:** Saffron (OpenClaw agent) — `itsmiso-ai` account

**Purpose:** Version-controlled scripts, runbooks, and workflow documentation
for the Dispatch integration layer. This repo is the agent-side companion to
the Dispatch application (`misospace/dispatch`).

Intended to be safe to make public: contains only workflow code, runbooks, and
docs. Runtime state, credentials, OpenClaw configuration, memory, cron state,
and local workspace artifacts are excluded.

## Architecture Overview

```
 ┌─────────────┐    heartbeat     ┌─────────────────┐
 │  Saffron    │ ──────────────▶  │   Dispatch API   │
 │  (agent)    │                  │  (misospace/     │
 │             │  ◀────────────── │   dispatch)      │
 │             │   queue / work   │                  │
 │             │                  │  Postgres cache  │
 │             │                  │       │          │
 │             │                  │       ▼          │
 │             │                  │  GitHub Issues   │
 │             │                  │  (source of      │
 │             │                  │   truth)         │
 └──────┬──────┘                  └─────────────────┘
        │
        │ spawn
        ▼
 ┌─────────────────────────────────────┐
 │  Worker sessions (cron-triggered)    │
 │  local   → nvidia     (saffron-local)│
 │  cloud   → dsv4p      (saffron-cloud)│
 │  frontier→ glm-5.2    (saffron-frontier)│
 └─────────────────────────────────────┘
```

**Saffron** is the main agent session. It runs the hourly heartbeat, claims
work from Dispatch, does engineering, and spawns worker sessions.

**Dispatch** (`misospace/dispatch`) owns the queue: work discovery, claims,
leases, checkpoints, lane assignment, status lifecycle, and `nextAction`.
Saffron consumes work through Dispatch APIs — it does not locally groom
backlog, judge lanes, or sync GitHub Projects.

**Workers** are isolated cron-triggered sessions that consume one actionable
item per run from their assigned lane.

### Lane Model

Dispatch assigns issues to one of three lanes based on complexity and model
requirements:

| Lane | Cron Name | Model | Agent Name | Purpose |
|------|-----------|-------|------------|---------|
| `local` | `(Saffron): Dispatch - Local` | nvidia | `saffron-local` | Scoped implementation, single-file fixes, edits |
| `cloud` | `(Saffron): Dispatch - Cloud` | dsv4p | `saffron-cloud` | Multi-file work, inference across files/services |
| `frontier` | `(Saffron): Dispatch - Frontier` | glm-5.2 | `saffron-frontier` | High-complexity design, escalated work |

### Grooming

Grooming is owned by Dispatch's hosted groomer — Saffron does not run local
grooming heuristics or model-based lane judgment. The heartbeat checks for
Dispatch-assigned grooming work via `GET /api/agents/saffron/next-task?mode=groom`
and triggers the hosted groomer via `POST /api/groomer/run` when work exists.

### Status Contract

| Status | Meaning |
|--------|---------|
| `status/backlog` | needs triage/grooming; not claimable |
| `status/ready` | groomed/actionable; claimable |
| `status/in-progress` | claimed or implementation started |
| `status/in-review` | PR opened; issue still open |
| `status/done` | GitHub issue closed/terminal only |

Opening or updating a PR is not Done. Open issue + open PR = `status/in-review`.

## Heartbeat

The hourly heartbeat runs a deterministic script, then checks for grooming
work. The procedure is documented in `docs/heartbeat.md`.

```bash
python3 scripts/heartbeat.py
```

The script:
1. Calls `POST /api/agents/saffron/heartbeat` (server-side sync, reconciliation, AgentRun recording)
2. Probes all three lanes for work availability
3. Toggles worker crons based on probe verdicts
4. Checks for Dispatch-assigned grooming work

After the script returns, the Saffron agent handles any grooming work by
triggering the hosted groomer, or replies `HEARTBEAT_OK` if there is nothing
to surface.

## Worker Execution

Workers are isolated sessions triggered by cron. Each worker:

1. Runs deterministic preflight (`dispatch_worker_preflight.py`)
2. Checks PR-fix queue first (takes precedence over issue work)
3. Resumes active work if Dispatch returns a checkpoint/nextAction
4. Otherwise claims one `status/ready` item from its lane
5. Does one bounded step
6. Updates Dispatch (checkpoint + status)
7. Stops with a lane-specific final form

Worker behavior is governed by runbooks in `worker-runbooks/` and cron prompts
in `cron-prompts/`.

### Preflight Actions

| Action | Meaning |
|--------|---------|
| `clear` | No work; reply terminal and stop |
| `stuck` | Needs attention; reply terminal and stop |
| `pr-fix` | Push to existing PR branch only |
| `resume-active-work` | Obey returned `nextAction` exactly |
| `claim-ready-issue` | Implement the claimed Ready issue |
| `active-follow-up` | Address failing checks / review feedback on active PR |

### Direct Push Rule (Misospace Only)

All worker branches push directly to `misospace/*` origin. No forks exist for
`misospace/*` repos. After creating a PR, verify:

```bash
gh pr view --json isCrossRepository   # must be false
```

Cross-repo/fork PRs break the AI PR review CI because GitHub does not forward
secrets to fork PR runs.

## Scripts

### Active Scripts

| Script | Purpose |
|--------|---------|
| `heartbeat.py` | Dispatch-native heartbeat: server-side sync, lane probing, cron toggle, grooming check |
| `dispatch_worker_preflight.py` | Deterministic worker preflight: PR-fix, active work, lane verification, queue selection, optional claim |
| `dispatch_worker_cron.py` | Actuator for worker cron enable/disable only. Refuses schedule/model/prompt/delivery/alerts changes |
| `dispatch_work_probe.py` | Read-only lane work probe. Answers "would this lane do work if the worker ran?" |
| `dispatch_work_update.py` | Update Dispatch checkpoints and issue status from worker sessions |
| `dispatch_reporter.py` | Report agent runs to Dispatch |
| `worker_result_guard.py` | Validate worker final text against terminal contract |
| `research_before_task.py` | Research GitHub issues before implementing |
| `audit_decompose.py` | Nightly audit decomposer: creates child issues from audit umbrella parents |
| `extract_templates.py` | Extract cron prompt templates from live cron jobs |

### Deprecated Scripts

These scripts are retained for compatibility but are **not invoked** by the
current heartbeat or worker flow. Dispatch APIs have replaced their functions.

| Script | Status |
|--------|--------|
| `project_backlog_sync.py` | Deprecated — Dispatch heartbeat handles sync |
| `project_groom.py` | Deprecated — Dispatch owns grooming/lane/status lifecycle |
| `backlog_groomer.py` | Deprecated — Dispatch `next-task?mode=groom` surfaces candidates |
| `issue_lane_judge.py` | Deprecated — Dispatch owns lane assignment |
| `wishlist_read_board.py` | Deprecated — Use `GET /api/agents/{agentName}/queue` |
| `wishlist_read_gpt_audit_board.py` | Deprecated — Use `GET /api/agents/{agentName}/queue` |
| `sync_summary.py` | Deprecated — Wrapper around deprecated sync |
| `pr_fix_queue.py` | Compatibility CLI for Dispatch-backed PR review-fix queue |

## Cron Prompts

`cron-prompts/` contains public-safe source templates for Dispatch workflow
cron prompts. These templates use placeholders (`{{WORKFLOW_DIR}}`,
`{{DISPATCH_LOCAL_AGENT}}`, etc.) instead of live runtime values.

Templates are documentation and review artifacts. Runtime cron jobs are managed
by `openclaw cron`; `cron/jobs.json` remains excluded (contains schedules,
delivery targets, model overrides, state, and environment-specific data).

| Template | Lane |
|----------|------|
| `local-worker.md` | Local lane worker prompt |
| `cloud-worker.md` | Cloud lane worker prompt |
| `frontier-worker.md` | Frontier lane worker prompt |
| `daily-pr-review.md` | Daily PR review prompt |
| `weekly-audit.md` | Weekly Misospace audit prompt |
| `nightly-audit-decomposer.md` | Nightly audit decomposition prompt |

## Worker Runbooks

`worker-runbooks/` contains the execution contract for each lane:

| Runbook | Scope |
|---------|-------|
| `shared-dispatch-worker.md` | Shared rules for all lanes (status contract, preflight, direct push, PR rules) |
| `local-lane-worker.md` | Local lane specifics |
| `cloud-lane-worker.md` | Cloud lane specifics |
| `frontier-lane-worker.md` | Frontier lane specifics |

## Excluded

The following must never be committed:
- **HEARTBEAT.md** — PVC-backed runtime copy; version the public-safe reference at `docs/heartbeat.md`
- **cron/jobs.json** — Runtime state, managed by `openclaw cron`
- **.state/*** — Runtime queue state and watch lists
- **pr_fix_queue.json** — Legacy local queue state
- Any file containing tokens, secrets, or credentials
- Any OpenClaw agent config, session, or memory files

## Security

Secrets and credentials must never be committed. All token handling is done via
environment variables (`DISPATCH_URL`, `DISPATCH_AGENT_TOKEN`) injected at runtime.
