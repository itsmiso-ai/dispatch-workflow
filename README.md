# Dispatch Workflow Scripts

Dispatch workflow scripts for the Saffron agent workspace.

**Owner:** Saffron (OpenClaw agent) — `itsmiso-ai` account

**Purpose:** Version-controlled scripts and workflow documentation for the Dispatch integration layer.

This repository is intended to be safe to make public: it should contain only
workflow code, runbooks, and docs. Runtime state, credentials, OpenClaw
configuration, memory, cron state, and local workspace artifacts are excluded.

## Scope

This repo tracks Dispatch workflow files from the Saffron agent workspace:
- Python scripts for heartbeat grooming, lane judging, backlog syncing
- Bash/shell utility scripts
- Workflow documentation and runbooks
- Sanitized cron prompt templates for Dispatch-owned automation
- Public-safe heartbeat contract reference at `docs/heartbeat.md`

## Excluded

The following are intentionally excluded and must never be committed:
- **HEARTBEAT.md** — PVC-backed runtime copy; version the public-safe reference
  copy at `docs/heartbeat.md` instead
- **cron/jobs.json** — Runtime state, managed by `openclaw cron`
- **.state/*** — Runtime queue state and watch lists
- **pr_fix_queue.json** — Legacy local queue state; active PR-fix state lives in Dispatch
- Any file containing tokens, secrets, or credentials
- Any OpenClaw agent config, session, or memory files
- home-ops or dispatch app code

Cron prompts may be versioned only as sanitized templates under
`cron-prompts/`. Do not commit schedules, delivery targets, runtime state,
channel IDs, token values, local operator-only prompts, or the raw
`cron/jobs.json` file.

## Relationship to Dispatch App

The Dispatch application lives separately at `misospace/dispatch`. This repo contains only the agent-side workflow scripts that interact with Dispatch as a consumer. 

## Scripts

| Script | Purpose |
|--------|---------|
| `issue_lane_judge.py` | Classify issues into `normal`/`escalated`/`backlog` lanes |
| `pr_fix_queue.py` | Compatibility CLI for Dispatch-backed PR review-fix queue management |
| `dispatch_worker_preflight.py` | Deterministic Normal/Escalated worker preflight: PR-fix, active work, lane verification, queue selection, optional claim |
| `worker_result_guard.py` | Validate Normal/Escalated worker final text against the terminal worker contract |
| `heartbeat.py` | Run deterministic heartbeat plumbing: Dispatch PR follow-up sync, scheduled sync, reconciliation, cron management, and Dispatch run reporting |
| `backlog_groomer.py` | Deterministic backlog candidate collector for Saffron-owned agent grooming |
| `project_backlog_sync.py` | Compatibility wrapper for Dispatch scheduled sync (`POST /api/sync/scheduled`); no GitHub Projects access |
| `project_groom.py` | Dispatch v0.3 grooming: scheduled sync, status reconciliation, lane classification, cron enablement |
| `wishlist_read_board.py` | Compatibility reader for Dispatch normal queue; does not query GitHub Projects |
| `wishlist_read_gpt_audit_board.py` | Compatibility reader for Dispatch escalated queue; does not query GitHub Projects |
| `dispatch_reporter.py` | Report agent runs to Dispatch using only `DISPATCH_URL`/`DISPATCH_AGENT_TOKEN` |
| `dispatch_work_update.py` | Update Dispatch checkpoints and issue status from worker sessions |
| `research_before_task.py` | Research GitHub issues before implementing |
| `sync_summary.py` | Compact Dispatch sync summary helper |

## Cron Prompt Templates

`cron-prompts/` contains public-safe source templates for Dispatch workflow
cron prompts. These templates intentionally use placeholders such as
`{{WORKFLOW_DIR}}`, `{{DISPATCH_NORMAL_AGENT}}`, and
`{{BLOCKED_MERGE_REPOS}}` instead of live runtime values.

The templates are documentation and review artifacts. Runtime cron jobs are
still managed by `openclaw cron`; `cron/jobs.json` remains excluded because it
contains schedules, delivery targets, model overrides, state, and other
environment-specific data.

## Dispatch v0.3 Worker Semantics

Worker cron prompts no longer reference GitHub Project boards. Instead, they consume work from Dispatch queue APIs and Dispatch-owned lifecycle state:

- **Normal lane:** `GET /api/agents/{agentName}/queue?lane=normal`
- **Escalated lane:** `GET /api/agents/{agentName}/queue?lane=escalated`

Workers claim work via `POST /api/issues/claim` and update lifecycle status via Dispatch status/lease/checkpoint APIs. GitHub Projects are fully deprecated and must not be queried or mutated by active workflow scripts.

Normal/Escalated worker selection starts with deterministic local preflight, not model judgment:

```bash
DISPATCH_AGENT_NAME=saffron-normal python3 scripts/dispatch_worker_preflight.py --lane normal --claim --json
DISPATCH_AGENT_NAME=saffron-escalated python3 scripts/dispatch_worker_preflight.py --lane escalated --claim --json
```

The preflight result action decides the worker path:
- `clear` / `stuck` — reply with the provided `terminal` and stop.
- `pr-fix` — update the existing PR only.
- `resume-active-work` — obey the returned `nextAction` exactly.
- `claim-ready-issue` — implement the returned claimed Ready issue.

Cron result text must match the terminal contract. Use:

```bash
printf '%s\n' "$FINAL_TEXT" | python3 scripts/worker_result_guard.py
```

Board status contract:
- `status/backlog` = needs triage/grooming, not ready for agents.
- `status/ready` = groomed/actionable and available to claim.
- `status/in-progress` = claimed or implementation started.
- `status/in-review` = PR opened/checks/review pending while the issue remains open.
- `status/done` = GitHub issue is closed/terminal only.

Hard rule: opening or updating a PR is not Done. An open issue with an unmerged PR must be In Review, not Done.

Work selection:
- PR-fix queue items from Dispatch have precedence.
- Workers consume exactly one actionable item per run.
- Workers prefer Ready work and do not consume Backlog unless explicitly requested.
- Renovate issues are excluded from agent queues unless explicitly requested.
- If Dispatch returns active work, a checkpoint, or `nextAction`, workers obey that next action exactly, perform one bounded step, update Dispatch with the result/checkpoint, and stop.

## Agent-Owned Backlog Grooming

Backlog grooming is an agent intelligence workflow. Scripts may collect
candidate data and apply explicit Saffron-authored decisions through Dispatch,
but scripts must not call models directly.

Collect backlog candidates for the Saffron heartbeat/sub-agent handoff:

```bash
python3 scripts/backlog_groomer.py --max 10
```

Or call the lower-level collector directly:

```bash
python3 scripts/project_groom.py --no-sync --groom-backlog --groom-backlog-only --groom-backlog-max 10
```

The collector writes a JSON request under
`.state/backlog_grooming_requests/`. Each candidate includes GitHub issue
metadata, labels, body, recent comments, and an `agentBrief` that a Saffron
heartbeat turn or Saffron sub-agent can use for the judgment step.

Saffron/sub-agent recommendations should be translated into Dispatch grooming
actions:

- `ready` -> `POST /api/issues/groom` with `action: "promote_to_ready"`.
- `escalated` -> `POST /api/issues/groom` with `action: "escalate"` and then ensure the issue is ready/claimable when appropriate.
- `needs-info` -> `POST /api/issues/groom` with `action: "mark_needs_info"`.
- `needs-human` / policy ambiguity -> `POST /api/issues/groom` with `action: "mark_not_ready"` and a clear reason.
- blocked dependencies -> `POST /api/issues/groom` with `action: "mark_blocked"`.

Labels remain the source of truth: `status/backlog` is not worker-ready.
Promoting an issue to worker queues requires changing the GitHub/Dispatch
status label to `status/ready`.

The removed flags `--groom-backlog-use-llm` and `--groom-backlog-apply` now
fail intentionally. If a workflow needs judgment, spawn or run Saffron agent
work; do not reintroduce direct model calls into scripts.

Affected cron jobs:
- `(Saffron): MC: Normal` — normal lane, uses Dispatch normal queue
- `(Saffron): MC: Escalated` — escalated lane, uses Dispatch escalated queue

## Security

Secrets and credentials must never be committed. All token handling is done via environment variables injected at runtime.
